"""
ingest.py — watchlist ingestion pipeline

Flow:
  raw text / file
    → parse_entries()      [Claude Haiku — extracts structured entries from messy text]
    → match_to_tmdb()      [TMDB API — confidence-scored match per entry]
    → triage()             [auto / batch_confirm / unresolved]
    → batch_confirmation() [interactive CLI — one pass for all ambiguous items]
    → write_to_db()        [db.py — final write]

Tiers:
  Auto-import    — clear title, exact/near-exact TMDB match → write directly
  Batch confirm  — ambiguous title or multiple TMDB candidates → user picks in one pass
  Unresolved     — no TMDB match or unparseable → saved to watchlist_unresolved table
"""

import os
import json
import hashlib
from pathlib import Path

import click
from dotenv import load_dotenv

from filmlog import db, tmdb as tmdb_client

load_dotenv(override=True)

# Confidence thresholds for auto-import vs batch vs unresolved
AUTO_THRESHOLD = 0.85
BATCH_THRESHOLD = 0.40


# ---------------------------------------------------------------------------
# Phase 1 — Parse raw text into structured entries using Claude Haiku
# ---------------------------------------------------------------------------

PARSE_PROMPT = """You are parsing a personal film/series watchlist. The text may be messy —
inline director names, recommender attributions, personal notes, section headers,
grouped multi-film lines, star ratings, numbered rankings, and language/country hints.

Extract every distinct film or series entry. For each one return:
- title: clean title only (no year, director, or notes)
- year: integer or null
- director: string or null (if clearly stated)
- country_hint: string or null (e.g. "Japanese", "Tamil", "Korean", "Malayalam", "French")
- is_series: true if clearly a TV series/show, false otherwise
- status: "watched" if the source context clearly marks these as seen/loved (e.g. "hall of fame",
  "favourite films", "watched", "-watched" suffix). Otherwise "watchlist".
- rewatch: true if explicitly flagged as a rewatch
- personal_notes: any context worth keeping — recommender name, why it was added,
  watch reason (e.g. "watch for cinematography"), thematic tag from the section it's in,
  personal reaction. Keep it natural, max 2 sentences.
- quality_signal: "loved" if in a loved/hall-of-fame/favourite section,
  "anti" if in a dogshit/hated section, "recommended" if flagged as must-watch/highly recommended,
  null otherwise

Rules:
- Split multi-film lines into separate entries (e.g. "The Mist(2007) Event Horizon(1997)" → 2 entries)
- SKIP: pure section headers, director wishlists ("watch every film by X"),
  commentary-only lines with no film title
- For film series (e.g. "Police Story 1 to 4"), create ONE entry with the first film
  and note the series in personal_notes
- For director groups (e.g. "Tarkovsky films: The Mirror, Stalker"),
  create individual entries for each named film
- Preserve recommender names in personal_notes (e.g. "Nawaz recommends", "Varun Grover recommends")
- The section a film appears in (e.g. "wall-staring films", "cosmic horror", "erotic")
  should be captured in personal_notes as thematic context

Return ONLY valid JSON in this exact format:
{
  "entries": [
    {
      "title": "string",
      "year": null,
      "director": null,
      "country_hint": null,
      "is_series": false,
      "status": "watchlist",
      "rewatch": false,
      "personal_notes": null,
      "quality_signal": null
    }
  ],
  "skipped": ["list of lines that were skipped and why"]
}"""

# Lines-per-chunk limit for parse batching (keeps response within Haiku's 8192 token limit)
PARSE_CHUNK_LINES = 40


def _parse_chunk(client, chunk_text: str) -> tuple[list, list]:
    """Parse one chunk of text. Returns (entries, skipped)."""
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8192,
        messages=[
            {
                "role": "user",
                "content": f"{PARSE_PROMPT}\n\n---\n\nText to parse:\n\n{chunk_text}"
            }
        ]
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    parsed = json.loads(raw)
    return parsed.get("entries", []), parsed.get("skipped", [])


def parse_entries(raw_text: str) -> list[dict]:
    """
    Use Claude Haiku to parse messy text into structured film entries.
    Splits large files into chunks to stay within output token limits.
    Returns a list of entry dicts.
    """
    from filmlog.ai import call_ai
    # Split into chunks of PARSE_CHUNK_LINES lines to avoid output token overflow
    lines = raw_text.splitlines()
    chunks = []
    for i in range(0, len(lines), PARSE_CHUNK_LINES):
        chunk = "\n".join(lines[i:i + PARSE_CHUNK_LINES]).strip()
        if chunk:
            chunks.append(chunk)

    all_entries = []
    total_skipped = 0

    if len(chunks) == 1:
        # Single chunk — parse directly
        entries, skipped = _parse_chunk(client, chunks[0])
        all_entries = entries
        total_skipped = len(skipped)
    else:
        # Multiple chunks — parse each and combine
        for idx, chunk in enumerate(chunks, 1):
            try:
                entries, skipped = _parse_chunk(client, chunk)
                all_entries.extend(entries)
                total_skipped += len(skipped)
            except Exception as e:
                click.echo(f"  ⚠ Chunk {idx}/{len(chunks)} parse error: {e} — skipping", err=True)

    if total_skipped:
        click.echo(f"  ↳ Skipped {total_skipped} non-film lines (section headers, director lists, etc.)")

    return all_entries


# ---------------------------------------------------------------------------
# Phase 2 — TMDB match each entry
# ---------------------------------------------------------------------------

def match_entry(entry: dict) -> dict:
    """
    Attempt to match an entry to a TMDB record.
    Adds to the entry dict:
      - tmdb_candidates: list of TMDB search results
      - tmdb_match: best single match (or None)
      - confidence: float 0–1
      - tier: "auto" | "batch" | "unresolved"
    """
    title = entry.get("title", "")
    year = entry.get("year")

    is_series = entry.get("is_series", False)
    try:
        if is_series:
            candidates = tmdb_client.search_multi(title, year, prefer_tv=True)
        else:
            candidates = tmdb_client.search_movies(title, year)
    except RuntimeError as e:
        # TMDB unavailable — mark unresolved
        entry["tmdb_candidates"] = []
        entry["tmdb_match"] = None
        entry["confidence"] = 0.0
        entry["tier"] = "unresolved"
        entry["match_error"] = str(e)
        return entry

    if not candidates:
        entry["tmdb_candidates"] = []
        entry["tmdb_match"] = None
        entry["confidence"] = 0.0
        entry["tier"] = "unresolved"
        return entry

    # When is_series, prefer TV results: pick the first TV candidate if available
    if is_series:
        tv_candidates = [c for c in candidates if c.get("media_type") == "tv"]
        if tv_candidates:
            candidates = tv_candidates + [c for c in candidates if c.get("media_type") != "tv"]

    # Score candidates
    best = candidates[0]
    score = _score_match(entry, best)

    # Media type match bonus
    if is_series and best.get("media_type") == "tv":
        score = min(1.0, score + 0.10)
    elif not is_series and best.get("media_type") == "movie":
        score = min(1.0, score + 0.05)

    # If year provided and top result year matches — boost confidence
    if year and best.get("year") == year:
        score = min(1.0, score + 0.15)

    # If multiple candidates have similar scores — lower confidence
    # But skip this penalty when media types differ and entry type matches best
    if len(candidates) > 1:
        second_score = _score_match(entry, candidates[1])
        same_type = best.get("media_type") == candidates[1].get("media_type")
        if second_score > score * 0.85 and same_type:
            score *= 0.75  # ambiguous — knock down

    entry["tmdb_candidates"] = candidates[:5]  # keep top 5 for batch UI
    entry["tmdb_match"] = best if score >= BATCH_THRESHOLD else None
    entry["confidence"] = score
    entry["tier"] = _tier(score)

    return entry


def _score_match(entry: dict, candidate: dict) -> float:
    """Simple title similarity score (0–1)."""
    query = entry.get("title", "").lower().strip()
    result = candidate.get("title", "").lower().strip()

    if query == result:
        return 0.95
    if query in result or result in query:
        return 0.80
    # Token overlap
    q_tokens = set(query.split())
    r_tokens = set(result.split())
    if not q_tokens:
        return 0.0
    overlap = len(q_tokens & r_tokens) / len(q_tokens)
    return overlap * 0.70


def _tier(score: float) -> str:
    if score >= AUTO_THRESHOLD:
        return "auto"
    if score >= BATCH_THRESHOLD:
        return "batch"
    return "unresolved"


# ---------------------------------------------------------------------------
# Phase 3 — Triage
# ---------------------------------------------------------------------------

def triage(matched_entries: list[dict]) -> tuple[list, list, list]:
    """Split matched entries into (auto, batch, unresolved) lists."""
    auto, batch, unresolved = [], [], []
    for e in matched_entries:
        tier = e.get("tier", "unresolved")
        if tier == "auto":
            auto.append(e)
        elif tier == "batch":
            batch.append(e)
        else:
            unresolved.append(e)
    return auto, batch, unresolved


# ---------------------------------------------------------------------------
# Phase 4 — Interactive batch confirmation
# ---------------------------------------------------------------------------

def run_batch_confirmation(batch: list[dict]) -> tuple[list, list]:
    """
    Present ambiguous matches to the user in one scrollable pass.
    Returns (confirmed, still_unresolved).
    """
    if not batch:
        return [], []

    confirmed = []
    still_unresolved = []

    click.echo(f"\n── {len(batch)} entries need a quick check ─────────────────")
    click.echo("For each one: press Enter to accept, type a number to pick a different result,")
    click.echo("or type 's' to skip (saves as unresolved).\n")

    for i, entry in enumerate(batch, 1):
        title = entry.get("title", "?")
        year = entry.get("year", "")
        best = entry.get("tmdb_match")
        candidates = entry.get("tmdb_candidates", [])

        click.echo(f"[{i}/{len(batch)}] You wrote: \"{title}\" {f'({year})' if year else ''}")

        if best:
            click.echo(f"  Best match: {best['title']} ({best.get('year', '?')}) [{best.get('language', '?')}]")
        else:
            click.echo("  No confident match found.")

        if len(candidates) > 1:
            click.echo("  Other options:")
            for j, c in enumerate(candidates[1:5], 2):
                click.echo(f"    {j}. {c['title']} ({c.get('year', '?')}) [{c.get('language', '?')}]")

        choice = click.prompt("  Accept / pick number / skip", default="1" if best else "s")

        if choice.lower() == "s":
            still_unresolved.append(entry)
        else:
            try:
                idx = int(choice) - 1
                selected = candidates[idx] if idx < len(candidates) else (best or candidates[0])
                entry["tmdb_match"] = selected
                entry["confidence"] = 0.90  # manually confirmed
                confirmed.append(entry)
            except (ValueError, IndexError):
                if best:
                    confirmed.append(entry)
                else:
                    still_unresolved.append(entry)

        click.echo()

    return confirmed, still_unresolved


# ---------------------------------------------------------------------------
# Offline mode — Claude-inferred metadata (no TMDB needed)
# ---------------------------------------------------------------------------

METADATA_PROMPT = """For each film/series below, provide basic metadata from your knowledge.
Return a JSON array — one object per film, in the same order.

Fields:
  "title": canonical English title (or original if no English title)
  "year": release year as integer, or null
  "language": two-letter ISO code (en, ko, ja, hi, ml, ta, fr, zh, etc.)
  "country": two-letter ISO country code (US, KR, JP, IN, FR, etc.)
  "runtime": approximate runtime in minutes as integer, or null
  "genres": array of 1-3 genre strings

Films:
{film_list}

Return ONLY the JSON array. No explanation."""


def _synthetic_id(title: str, year: int | None) -> int:
    """Stable negative integer ID for offline-ingested films (avoids real TMDB ID collisions)."""
    key = f"{title.lower().strip()}{year or ''}".encode()
    return -int(hashlib.md5(key).hexdigest()[:8], 16)


def _infer_metadata_batch(entries: list[dict]) -> list[dict]:
    """
    Use Claude Haiku to fill in film metadata for a batch of entries.
    Called in offline mode instead of TMDB matching.
    Returns entries with tmdb_match populated from Claude's knowledge.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        # No API — assign minimal synthetic records
        for entry in entries:
            entry["tmdb_match"] = {
                "tmdb_id": _synthetic_id(entry.get("title", ""), entry.get("year")),
                "title": entry.get("title", "Unknown"),
                "year": entry.get("year"),
                "language": None, "country": None,
                "runtime": None, "tmdb_genres": [],
                "poster_url": None,
            }
            entry["confidence"] = 0.5
            entry["tier"] = "auto"
        return entries

    try:
        from filmlog.ai import call_ai
        raw = call_ai(METADATA_PROMPT.format(film_list=film_list), max_tokens=2048)
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        metadata_list = json.loads(raw)
    except Exception as e:
        click.echo(f"  ⚠ Metadata inference failed: {e} — using titles only", err=True)
        metadata_list = [{}] * len(entries)

    for entry, meta in zip(entries, metadata_list):
        title = meta.get("title") or entry.get("title", "Unknown")
        year = meta.get("year") or entry.get("year")
        entry["tmdb_match"] = {
            "tmdb_id": _synthetic_id(title, year),
            "title": title,
            "year": year,
            "language": meta.get("language"),
            "country": meta.get("country"),
            "runtime": meta.get("runtime"),
            "tmdb_genres": meta.get("genres", []),
            "poster_url": None,  # no poster without TMDB
        }
        entry["confidence"] = 0.88  # Claude knows these films well
        entry["tier"] = "auto"

    return entries


def write_to_db_offline(entries: list[dict], source: str) -> dict:
    """
    Write entries to DB in offline mode.
    Uses Claude Haiku to also infer default taste attributes per film.
    """
    from filmlog.log import infer_default_attributes

    stats = {"written": 0, "skipped_duplicate": 0, "errors": 0}

    for entry in entries:
        match = entry.get("tmdb_match")
        if not match:
            stats["errors"] += 1
            continue

        tmdb_id = match["tmdb_id"]

        existing = db.get_user_film_by_tmdb(tmdb_id)
        if existing:
            stats["skipped_duplicate"] += 1
            continue

        try:
            # Infer taste attributes from Claude's knowledge
            attrs = infer_default_attributes(match["title"], match.get("year"))

            db.upsert_film(
                tmdb_id=tmdb_id,
                title=match["title"],
                year=match.get("year"),
                runtime=match.get("runtime"),
                language=match.get("language"),
                country=match.get("country"),
                poster_url=None,
                tmdb_genres=match.get("tmdb_genres", []),
                default_taste_attributes=attrs,
            )

            status = entry.get("status", "watchlist")
            if entry.get("rewatch"):
                status = "rewatch_candidate"

            db.add_user_film(
                tmdb_id=tmdb_id,
                status=status,
                personal_notes=entry.get("personal_notes"),
                watch_context=None,
                source=source,
            )
            stats["written"] += 1

        except Exception as e:
            stats["errors"] += 1
            click.echo(f"  ✗ Error writing {entry.get('title')}: {e}", err=True)

    return stats


# ---------------------------------------------------------------------------
# Phase 5 — Write to DB
# ---------------------------------------------------------------------------

def write_to_db(entries: list[dict], source: str) -> dict:
    """
    Write confirmed entries to the DB.
    Each entry needs tmdb_match set. Returns stats dict.
    """
    import time
    stats = {"written": 0, "skipped_duplicate": 0, "errors": 0}
    total = len(entries)
    start_time = time.time()

    for idx, entry in enumerate(entries, 1):
        match = entry.get("tmdb_match")
        if not match:
            stats["errors"] += 1
            continue

        tmdb_id = match["tmdb_id"]

        # Check if already in DB
        existing = db.get_user_film_by_tmdb(tmdb_id)
        if existing:
            stats["skipped_duplicate"] += 1
            continue

        try:
            media_type = match.get("media_type", "movie")
            if media_type == "tv":
                full_data = tmdb_client.fetch_tv_data(tmdb_id)
            else:
                full_data = tmdb_client.fetch_film_data(tmdb_id)
            db.upsert_film(**full_data)

            status = entry.get("status", "watchlist")
            if entry.get("rewatch"):
                status = "rewatch_candidate"

            db.add_user_film(
                tmdb_id=tmdb_id,
                status=status,
                personal_notes=entry.get("personal_notes"),
                watch_context=None,  # not inferrable at import time
                source=source,
            )
            stats["written"] += 1

        except Exception as e:
            stats["errors"] += 1
            click.echo(f"\n  ✗ Error writing {entry.get('title')}: {e}", err=True)

        elapsed = time.time() - start_time
        avg_per = elapsed / idx
        remaining = avg_per * (total - idx)
        mins, secs = divmod(int(remaining), 60)
        time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        click.echo(f"\r  Writing [{idx}/{total}] ~{time_str} remaining", nl=False)

    click.echo()
    return stats


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def ingest_text(
    raw_text: str,
    source_label: str = "text_import",
    non_interactive: bool = False,
    offline: bool = False,
) -> None:
    """
    Full ingestion pipeline for a block of raw text.
    non_interactive=True skips batch confirmation (auto-only, rest go to unresolved).
    offline=True uses Claude Haiku for metadata instead of TMDB (for when TMDB is unavailable).
    """
    click.echo(f"\n── Parsing entries …")
    entries = parse_entries(raw_text)
    click.echo(f"  Found {len(entries)} film/series entries")

    if offline:
        # ── Offline path: Claude infers metadata in batches of 20 ──────────
        click.echo(f"\n── Inferring metadata via Claude (offline mode) …")
        all_entries = []
        batch_size = 20
        for i in range(0, len(entries), batch_size):
            chunk = entries[i:i + batch_size]
            click.echo(f"  Batch {i // batch_size + 1}/{(len(entries) + batch_size - 1) // batch_size} ({len(chunk)} films) …")
            all_entries.extend(_infer_metadata_batch(chunk))

        click.echo(f"\n── Writing {len(all_entries)} entries to database …")
        stats = write_to_db_offline(all_entries, source=source_label)
        click.echo(f"  ✓ Written      : {stats['written']}")
        if stats["skipped_duplicate"]:
            click.echo(f"  ~ Duplicates   : {stats['skipped_duplicate']} (already in DB)")
        if stats["errors"]:
            click.echo(f"  ✗ Errors       : {stats['errors']}")

    else:
        # ── Online path: TMDB matching ──────────────────────────────────────
        import time
        total = len(entries)
        click.echo(f"\n── Matching {total} entries to TMDB …")
        matched = []
        start_time = time.time()
        for idx, entry in enumerate(entries, 1):
            matched.append(match_entry(entry))
            elapsed = time.time() - start_time
            avg_per = elapsed / idx
            remaining = avg_per * (total - idx)
            mins, secs = divmod(int(remaining), 60)
            time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            click.echo(f"\r  [{idx}/{total}] ~{time_str} remaining", nl=False)
        click.echo()

        auto, batch, unresolved = triage(matched)
        click.echo(f"\n  Auto-import   : {len(auto)}")
        click.echo(f"  Needs review  : {len(batch)}")
        click.echo(f"  Unresolved    : {len(unresolved)}")

        # Batch confirmation
        if batch and not non_interactive:
            confirmed, still_unresolved = run_batch_confirmation(batch)
            unresolved.extend(still_unresolved)
            auto.extend(confirmed)
        elif batch and non_interactive:
            click.echo("  (Skipping batch confirmation — saving ambiguous as unresolved)")
            unresolved.extend(batch)

        # Write confirmed to DB
        click.echo(f"\n── Writing {len(auto)} entries to database …")
        stats = write_to_db(auto, source=source_label)
        click.echo(f"  ✓ Written      : {stats['written']}")
        if stats["skipped_duplicate"]:
            click.echo(f"  ~ Duplicates   : {stats['skipped_duplicate']} (already in DB)")
        if stats["errors"]:
            click.echo(f"  ✗ Errors       : {stats['errors']}")

        # Save unresolved
        if unresolved:
            click.echo(f"\n── Saving {len(unresolved)} unresolved entries …")
            for entry in unresolved:
                db.add_unresolved(
                    raw_text=entry.get("title", "unknown"),
                    source="text",
                    confidence_score=entry.get("confidence", 0.0),
                )
            click.echo(f"  Saved to watchlist_unresolved table.")
            click.echo(f"  Run: python main.py export-unresolved   to review them later.")

    click.echo(f"\n── Done.\n")


def ingest_file(filepath: str, non_interactive: bool = False, offline: bool = False) -> None:
    """Ingest from a text file."""
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    raw_text = path.read_text(encoding="utf-8")
    source_label = path.stem  # use filename as source label
    mode = " [offline]" if offline else ""
    click.echo(f"Ingesting: {path.name} ({len(raw_text)} chars){mode}")
    ingest_text(raw_text, source_label=source_label, non_interactive=non_interactive, offline=offline)


def ingest_all_raw(non_interactive: bool = False, offline: bool = False) -> None:
    """
    Ingest all files in data/raw/ in sequence.
    Used for the initial bulk import of all saved lists.
    """
    raw_dir = Path(__file__).parent.parent / "data" / "raw"
    files = sorted(raw_dir.glob("*.txt"))

    if not files:
        click.echo("No .txt files found in data/raw/")
        return

    mode = " (offline mode — Claude metadata)" if offline else ""
    click.echo(f"\nFound {len(files)} files to ingest{mode}:\n")
    for f in files:
        click.echo(f"  • {f.name}")

    click.echo()
    if not non_interactive:
        confirm = click.confirm("Proceed with full import?", default=True)
        if not confirm:
            return

    for f in files:
        click.echo(f"\n{'='*50}")
        click.echo(f"FILE: {f.name}")
        click.echo('='*50)
        ingest_file(str(f), non_interactive=non_interactive, offline=offline)

    click.echo("\n── All files processed.")
