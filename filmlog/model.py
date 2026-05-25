"""
model.py — taste model generation (the rescan command).

Flow:
  1. Pull all watched + watchlist films from DB
  2. Assemble a quality-weighted context (loved films count 3x, watchlist 0.5x)
  3. Call Claude Opus — produces taste_model.md prose + structured attributes JSON
  4. Write taste_model.md to disk
  5. Clear + repopulate taste_attributes table from structured output
  6. Save version to taste_model_versions table

Quality weights (from CLAUDE.md design principle):
  loved        → 3.0  (hall of fame, explicit ranking, 5-star)
  recommended  → 1.5  (must watch, highly recommended)
  watchlist    → 0.5  (added, no strong signal)
  anti         → -2.0 (explicitly hated, dogshit)
"""

import os
import json
import re
from pathlib import Path
from datetime import datetime, timezone

import click
from dotenv import load_dotenv

from filmlog import db, log as log_module

load_dotenv(override=True)

TASTE_MODEL_PATH = Path(__file__).parent.parent / "taste_model.md"


# ---------------------------------------------------------------------------
# Quality signal inference
# ---------------------------------------------------------------------------

def _quality_signal(source: str | None, notes: str | None) -> str | None:
    """Infer quality signal from source file name and personal notes."""
    source = (source or "").lower()
    notes = (notes or "").lower()

    # Anti-signal checked FIRST — notes override source name
    if any(x in notes for x in [
        "dogshit", "hated", "really boring", "got really boring",
        "not good", "disliked", "started good, but got really boring"
    ]):
        return "anti"

    # Source file name: loved lists
    if any(x in source for x in ["loved", "hall_of_fame", "favourite", "favorite"]):
        return "loved"

    # Notes-based positive signals
    if any(x in notes for x in [
        "must watch", "highly recommended", "highly acclaimed",
        "#1", "#2", "#3", "goat", "5 star", "*****"
    ]):
        return "recommended"

    return None


def _quality_weight(signal: str | None) -> float:
    weights = {"loved": 3.0, "recommended": 1.5, "anti": -2.0}
    return weights.get(signal, 0.5)


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def _format_film_for_context(row: dict, signal: str | None) -> str:
    """Format one film as a compact context line for Claude."""
    title = row.get("title", "Unknown")
    year = row.get("year")
    status = row.get("status", "watchlist")
    context = row.get("watch_context")
    notes = row.get("personal_notes", "") or ""
    is_series = False  # will refine when series tagging is added

    # Label
    if signal == "loved":
        label = "LOVED"
    elif signal == "anti":
        label = "ANTI"
    elif status == "watched":
        label = "WATCHED"
    else:
        label = "WATCHLIST"

    # Build line
    parts = [f"[{label}] {title}"]
    if year:
        parts[0] += f" ({year})"
    if context:
        parts.append(context)
    if notes:
        # Truncate long notes
        short = notes[:120] + "…" if len(notes) > 120 else notes
        parts.append(f'"{short}"')

    return " — ".join(parts)


def _build_context(watched: list[dict], watchlist: list[dict]) -> str:
    """
    Assemble the full context string to send to Claude.
    Watched films first (quality-sorted), then a representative watchlist sample.
    """
    lines = []

    # Sort watched by quality weight descending
    def sort_key(row):
        sig = _quality_signal(row.get("source"), row.get("personal_notes"))
        return -_quality_weight(sig)

    watched_sorted = sorted(watched, key=sort_key)

    lines.append("=== WATCHED / LOGGED FILMS ===")
    for row in watched_sorted:
        sig = _quality_signal(row.get("source"), row.get("personal_notes"))
        lines.append(_format_film_for_context(row, sig))

    # Watchlist: send all (or first 200 if very large)
    if watchlist:
        lines.append("")
        lines.append("=== WATCHLIST (wants to watch) ===")
        for row in watchlist[:200]:
            sig = _quality_signal(row.get("source"), row.get("personal_notes"))
            lines.append(_format_film_for_context(row, sig))

        if len(watchlist) > 200:
            lines.append(f"… and {len(watchlist) - 200} more watchlist items")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude Opus prompt
# ---------------------------------------------------------------------------

MODEL_PROMPT = """You are building a personal taste model for Prashil's film recommendation engine.

You have been given his complete film log: everything he has watched (with notes and context),
everything he loves (hall of fame picks), things he hates (anti-signals), and his watchlist.

Your job is to produce two things:

---

PART 1: A human-readable taste model (taste_model.md content).
This is what gets read at recommendation time. It must be:
- Specific, not generic. Not "likes dark films" but "drawn to stories where moral resolution
  is deliberately withheld — the ambiguity is the point, not a flaw."
- Honest about patterns across language, genre, era. The model should cross-reference
  a Malayalam film against a Korean series against a French arthouse film if they share
  the same underlying quality.
- Personal. Reference specific films from his log to illustrate each signal.
- Compact. This gets read by Claude before generating a recommendation — keep it tight.

Format PART 1 exactly like this:

# Taste Model — v{version}
*{date} · {watched_count} watched · {watchlist_count} in watchlist*

## What drives the enjoyment

[2–3 paragraphs. The actual mechanics of his taste — not what genres he likes,
but what specific qualities consistently produce enjoyment. Be precise.]

## Strong positive signals

[List each signal as: **attribute name** — which films exemplify it, what the exact quality is.
Aim for 6–10 signals. These are the things to look for when recommending.]

## Anti-signals (consistent avoidance or disappointment)

[List what consistently fails for him — with specific examples from his log.
Even if a film is acclaimed, if he bounced off it, that's real signal.]

## Taste clusters

### Solo / late night
[What kinds of films work when he's watching alone — specific qualities, not genres]

### With others
[What shifts when he's watching with family or partner — what gets filtered in/out]

## Contradictions worth surfacing

[1–3 tensions in his log that — if he answered one question — would sharpen the model
significantly. Format as: "You loved X but [bounced off / avoided] Y — both are [shared quality].
What was different?"]

## Top watchlist picks right now

[7–10 films from his watchlist that best match his strongest positive signals.
Brief reason for each — one sentence, not a plot summary. Reference a specific signal.]

---

PART 2: Structured attributes JSON (for database storage).
This must be valid JSON. Output it after a line that reads exactly: [ATTRIBUTES JSON]

The JSON should be an array of objects, one per distinct taste attribute:
[
  {{
    "attribute_name": "slow-building existential dread",
    "confidence_score": 0.92,
    "cluster": "solo_late_night",
    "polarity": "positive",
    "evidence_titles": ["The Wailing", "Hereditary", "The Ring"]
  }},
  ...
]

Rules for the JSON:
- confidence_score: 0.0–1.0. Weight by quality signal (loved films = strong evidence).
  Normalize — don't just count films, weight by how strongly each film represents the attribute.
- cluster: "solo_late_night" | "family" | "partner" | "general" | null
- polarity: "positive" | "negative"
- Include negative polarity entries for anti-signals
- Aim for 15–25 attributes total. Quality over quantity.

---

Here is Prashil's complete film log:

{context}
"""


# ---------------------------------------------------------------------------
# Generate model
# ---------------------------------------------------------------------------

def _call_claude_opus(prompt: str) -> str:
    """Call Claude Opus for taste model generation."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set.\n"
            "Add it to your .env file: ANTHROPIC_API_KEY=your_key_here"
        )

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    click.echo("  Calling Claude Opus (this takes ~30 seconds) …")

    message = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


ATTR_EXTRACT_PROMPT = """Read this taste model and extract structured taste attributes for database storage.

Return a JSON array only — no preamble. Each object:
{{
  "attribute_name": "short phrase (3–6 words)",
  "confidence_score": 0.0–1.0,
  "cluster": "solo_late_night" | "family" | "partner" | "general" | null,
  "polarity": "positive" | "negative",
  "evidence_titles": ["Film 1", "Film 2", "Film 3"]
}}

Rules:
- Extract 15–25 attributes from the positive signals, anti-signals, and clusters
- confidence_score: how strongly supported by the model text (0.7–0.95 for clear signals)
- Include negative polarity for anti-signals
- evidence_titles: 2–4 specific film titles mentioned in the model

Taste model:
{model_text}

Return ONLY valid JSON array."""


def _extract_attributes_via_haiku(model_text: str) -> list[dict]:
    """
    Fallback: extract structured attributes from prose taste model using Claude Haiku.
    Used when the main Opus call didn't produce the JSON block.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return []

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{"role": "user", "content": ATTR_EXTRACT_PROMPT.format(model_text=model_text)}]
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        raw = raw.strip()
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception as e:
        click.echo(f"  ⚠ Haiku attribute extraction failed: {e}", err=True)
        return []


def _split_model_output(raw: str) -> tuple[str, list[dict]]:
    """
    Split Claude's output into (taste_model_md, attributes_list).
    Returns (full_text, []) if JSON section is missing or unparseable.
    """
    marker = "[ATTRIBUTES JSON]"
    if marker not in raw:
        return raw.strip(), []

    parts = raw.split(marker, 1)
    model_text = parts[0].strip()
    json_raw = parts[1].strip()

    # Strip markdown code fences
    if json_raw.startswith("```"):
        json_raw = re.sub(r"^```[a-z]*\n?", "", json_raw)
        json_raw = re.sub(r"\n?```$", "", json_raw)
    json_raw = json_raw.strip()

    try:
        attributes = json.loads(json_raw)
        if not isinstance(attributes, list):
            attributes = []
    except json.JSONDecodeError:
        click.echo("  ⚠ Could not parse attributes JSON — model text saved, attributes skipped.", err=True)
        attributes = []

    return model_text, attributes


# ---------------------------------------------------------------------------
# Write taste_model.md
# ---------------------------------------------------------------------------

def _write_taste_model_file(model_text: str, version: int) -> None:
    """Write taste_model.md, replacing the version placeholder if needed."""
    # Fill in version placeholder if Claude left it as {version}
    text = re.sub(r"\{version\}", str(version), model_text)
    TASTE_MODEL_PATH.write_text(text, encoding="utf-8")


DIFF_PROMPT = """You are updating an existing personal taste model for Prashil's film recommendation engine.

The current model (v{prev_version}) is below. Since it was last generated, the following changes have been made:

CHANGES SINCE LAST MODEL:
{diff_context}

Your job: revise the existing model to incorporate these changes. This is a surgical update, not a rewrite.

Rules:
- If a film was REMOVED, reduce confidence in attributes it was evidence for — but don't remove
  an attribute unless you're confident it was only supported by that film.
- If new films were ADDED, check if they reinforce existing attributes or reveal new ones.
- If NOTES were edited, treat the new note as fresh signal — it may sharpen, contradict, or
  add nuance to an existing attribute.
- Keep the same structure and format as the existing model.
- Update the version number to v{next_version} and the date to {date}.
- Update watched/watchlist counts to: {watched_count} watched, {watchlist_count} in watchlist.

After the revised model, output a line that reads exactly: [ATTRIBUTES JSON]
Then output the full updated attributes array (same format as the original model generation).

EXISTING MODEL:
{existing_model}
"""


DIFF_THRESHOLD = 30

# ---------------------------------------------------------------------------
# Main rescan function
# ---------------------------------------------------------------------------

def rescan(trigger: str = "manual", force_full: bool = False) -> None:
    """
    Taste model regeneration — diff-based when possible, full when needed.

    Diff rescan: if the last model exists and fewer than DIFF_THRESHOLD films
    changed since then, send only the diff + existing model to Claude. Much
    cheaper and faster than a full rescan.

    Full rescan: no existing model, or too many changes, or force_full=True.
    """
    click.echo("\n── Rescan starting ─────────────────────────────")

    # ── 1. Pull data from DB ─────────────────────────────────────────────────
    watched = db.get_watched_films()
    watchlist = db.get_watchlist()

    click.echo(f"  Watched films    : {len(watched)}")
    click.echo(f"  Watchlist items  : {len(watchlist)}")

    if not watched and not watchlist:
        click.echo("\n  Nothing to model yet — add some films first.")
        click.echo("  Run: python main.py add \"Film Title\"")
        click.echo("  Or:  python main.py ingest-all   (requires API keys)\n")
        return

    if len(watched) < 3:
        click.echo("\n  Tip: the model improves significantly with more watched films.")
        click.echo(f"  Currently have {len(watched)} watched — 10+ gives a much sharper model.")
        if not click.confirm("  Generate anyway?", default=True):
            return

    # ── 2. Decide: diff or full ──────────────────────────────────────────────
    existing = db.get_latest_taste_model()
    next_version = (existing["version_number"] + 1) if existing else 1
    use_diff = False
    changed_films = []

    if existing and not force_full:
        last_generated = existing.get("generated_at", "")
        if last_generated:
            changed_films = db.get_films_changed_since(last_generated)
            if 0 < len(changed_films) <= DIFF_THRESHOLD:
                use_diff = True
                click.echo(f"\n  {len(changed_films)} film(s) changed since v{existing['version_number']} — using diff rescan")
            elif len(changed_films) == 0:
                click.echo("\n  No changes since last model. Nothing to update.")
                return
            else:
                click.echo(f"\n  {len(changed_films)} changes — too many for diff, running full rescan")

    # ── 3. Build prompt ──────────────────────────────────────────────────────
    if use_diff:
        diff_lines = []
        for film in changed_films:
            sig = _quality_signal(film.get("source"), film.get("personal_notes"))
            diff_lines.append(_format_film_for_context(film, sig))

        existing_model = existing.get("model_summary", "")
        if not existing_model and TASTE_MODEL_PATH.exists():
            existing_model = TASTE_MODEL_PATH.read_text(encoding="utf-8")

        prompt = DIFF_PROMPT.format(
            prev_version=existing["version_number"],
            next_version=next_version,
            date=datetime.now(timezone.utc).date().isoformat(),
            watched_count=len(watched),
            watchlist_count=len(watchlist),
            diff_context="\n".join(diff_lines),
            existing_model=existing_model[:6000],
        )
        click.echo("  Sending diff to Claude Opus …")
    else:
        click.echo("\n  Building full context …")
        context = _build_context(watched, watchlist)
        prompt = MODEL_PROMPT.format(
            version=next_version,
            date=datetime.now(timezone.utc).date().isoformat(),
            watched_count=len(watched),
            watchlist_count=len(watchlist),
            context=context,
        )

    # ── 4. Call Claude Opus ──────────────────────────────────────────────────
    try:
        raw_output = _call_claude_opus(prompt)
    except RuntimeError as e:
        click.echo(f"\n  Error: {e}", err=True)
        return

    # ── 5. Split model text from attributes JSON ──────────────────────────────
    model_text, attributes = _split_model_output(raw_output)

    # Fallback: if Claude didn't produce the JSON block, extract via Haiku
    if not attributes:
        click.echo("  Extracting attributes via Haiku fallback …")
        attributes = _extract_attributes_via_haiku(model_text)
        if attributes:
            click.echo(f"  ✓ Extracted {len(attributes)} attributes via Haiku")

    # ── 6. Write taste_model.md ───────────────────────────────────────────────
    _write_taste_model_file(model_text, next_version)
    click.echo(f"  ✓ taste_model.md written (v{next_version})")

    # ── 7. Save version to DB ─────────────────────────────────────────────────
    version_id = db.save_taste_model_version(
        model_summary=model_text,
        trigger=trigger,
    )

    # ── 8. Repopulate taste_attributes table ──────────────────────────────────
    if attributes:
        db.clear_taste_attributes()
        written = 0
        for attr in attributes:
            name = attr.get("attribute_name", "").strip()
            if not name:
                continue
            # Get tmdb_ids for evidence titles
            evidence_ids = []
            for title in attr.get("evidence_titles", []):
                results = db.search_films_local(title)
                if results:
                    evidence_ids.append(results[0]["tmdb_id"])

            db.upsert_taste_attribute(
                attribute_name=name,
                confidence_score=float(attr.get("confidence_score", 0.5)),
                evidence_film_ids=evidence_ids,
                cluster=attr.get("cluster"),
                polarity=attr.get("polarity", "positive"),
            )
            written += 1

        click.echo(f"  ✓ {written} taste attributes written to DB")
    else:
        click.echo("  ⚠ No structured attributes extracted — taste_model.md saved but DB not updated")

    # ── 9. Also rebuild taste_log.md to reflect current DB state ─────────────
    click.echo("  Rebuilding taste_log.md …")
    log_module.write_taste_log()
    click.echo("  ✓ taste_log.md rebuilt")

    click.echo(f"\n── Rescan complete — taste model v{next_version} ready.")
    click.echo(f"   Open taste_model.md to read your taste profile.\n")


# ---------------------------------------------------------------------------
# Tier 2 recommendation (no live AI needed)
# ---------------------------------------------------------------------------

def recommend_from_model(cluster: str | None = None, limit: int = 3) -> list[dict]:
    """
    Generate a recommendation purely from stored taste_attributes + watchlist.
    No live Claude call — works offline (Tier 2 degradation).

    Scores each watchlist film by how many of its default_taste_attributes
    match Prashil's positive taste signals, weighted by confidence.
    Returns top `limit` films.
    """
    # Get positive taste attributes for this cluster
    positive_attrs = db.get_taste_attributes(cluster=cluster, polarity="positive")
    negative_attrs = db.get_taste_attributes(cluster=cluster, polarity="negative")
    watchlist = db.get_watchlist()

    if not positive_attrs or not watchlist:
        return []

    # Build attribute sets for fast lookup
    pos_map = {a["attribute_name"].lower(): a["confidence_score"] for a in positive_attrs}
    neg_set = {a["attribute_name"].lower() for a in negative_attrs}

    scored = []
    for film in watchlist:
        raw_attrs = film.get("default_taste_attributes") or "[]"
        if isinstance(raw_attrs, str):
            try:
                film_attrs = json.loads(raw_attrs)
            except Exception:
                film_attrs = []
        else:
            film_attrs = raw_attrs

        score = 0.0
        for fa in film_attrs:
            fa_lower = fa.lower()
            # Positive match
            for taste_attr, confidence in pos_map.items():
                if taste_attr in fa_lower or fa_lower in taste_attr:
                    score += confidence
            # Negative match (penalise)
            for neg_attr in neg_set:
                if neg_attr in fa_lower or fa_lower in neg_attr:
                    score -= 0.5

        if score > 0:
            scored.append((score, film))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [film for _, film in scored[:limit]]
