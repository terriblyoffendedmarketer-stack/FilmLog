"""
log.py — taste log: manual entry flow, taste_log.md read/write, entry parsing.

taste_log.md format (designed to be both human-readable and machine-parseable):

    ## Film Title (Year)
    *watched · solo · 2024-03-15*

    The dread builds so slowly. The ambiguous ending is part of the point —
    there's no clean resolution because there isn't one.

    **Attributes:** slow-building dread · folk horror · ambiguous ending · rural isolation
    **Confidence:** high

    ---

The ## header + *metadata line* + freeform notes + **Attributes:** line
is what the rescan parser reads. Everything in between is editable prose.
"""

import os
import re
import json
from pathlib import Path
from datetime import datetime, timezone

import click
from dotenv import load_dotenv

from filmlog import db, tmdb as tmdb_client

load_dotenv(override=True)

DATA_DIR = Path(os.getenv("FILMLOG_DATA_DIR", Path(__file__).parent.parent))
TASTE_LOG_PATH = DATA_DIR / "taste_log.md"

# How many new films trigger an auto-rescan suggestion
RESCAN_SUGGESTION_THRESHOLD = 5

# Quality signal → confidence label
QUALITY_TO_CONFIDENCE = {
    "loved": "high",
    "recommended": "medium-high",
    None: "medium",
}


# ---------------------------------------------------------------------------
# Default taste attribute inference (Claude Haiku)
# ---------------------------------------------------------------------------

ATTRIBUTE_PROMPT = """You are a film critic with deep knowledge of world cinema.
For the film "{title}" ({year}), list the 4–6 taste attributes that best explain
why people who love this film love it.

Go beyond genre. Think about: emotional register, pacing, moral complexity,
visual style, thematic weight, the specific quality of feeling it creates.

Examples of good attributes: "slow-building existential dread", "survival stakes",
"moral ambiguity without resolution", "warm humanistic comedy", "grief as texture",
"kinetic action as spectacle".

Return ONLY a JSON array of strings, no explanation:
["attribute one", "attribute two", ...]"""


def infer_default_attributes(title: str, year: int | None) -> list[str]:
    """Call Claude Haiku to infer default taste attributes for a film."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return []  # Graceful degradation — no attributes if key missing

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": ATTRIBUTE_PROMPT.format(
                    title=title,
                    year=year or "unknown year"
                )
            }]
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        attrs = json.loads(raw)
        return attrs if isinstance(attrs, list) else []
    except Exception:
        return []  # Never crash on attribute inference failure


# ---------------------------------------------------------------------------
# taste_log.md formatting
# ---------------------------------------------------------------------------

def _format_entry(film: dict, user_film: dict) -> str:
    """Format one film entry as a taste_log.md block."""
    title = film.get("title", "Unknown")
    year = film.get("year", "")
    status = user_film.get("status", "watchlist")
    context = user_film.get("watch_context")
    notes = user_film.get("personal_notes", "")

    # Metadata line: *watched · solo · 2024-03-15*
    meta_parts = [status]
    if context:
        meta_parts.append(context)
    if status == "watched" and user_film.get("date_watched"):
        meta_parts.append(user_film["date_watched"])
    elif user_film.get("date_added"):
        meta_parts.append(f"added {user_film['date_added']}")

    meta_line = " · ".join(meta_parts)

    # Attributes
    attrs_raw = film.get("default_taste_attributes") or []
    if isinstance(attrs_raw, str):
        try:
            attrs_raw = json.loads(attrs_raw)
        except Exception:
            attrs_raw = []

    quality_signal = user_film.get("quality_signal")  # stored in personal_notes parsing
    confidence = QUALITY_TO_CONFIDENCE.get(quality_signal, "medium")

    # Build the block
    lines = [f"## {title} ({year})" if year else f"## {title}"]
    lines.append(f"*{meta_line}*")
    lines.append("")

    if notes:
        lines.append(notes)
        lines.append("")

    if attrs_raw:
        lines.append(f"**Attributes:** {' · '.join(attrs_raw)}")

    lines.append(f"**Confidence:** {confidence}")
    lines.append("")
    lines.append("---")
    lines.append("")

    return "\n".join(lines)


def write_taste_log() -> None:
    """
    Regenerate taste_log.md from scratch using DB contents.
    Watched films first (sorted by date_watched DESC), then watchlist.
    Called after rescan or when a full rebuild is needed.
    """
    watched = db.get_watched_films()
    watchlist = db.get_watchlist()

    lines = [
        "# Taste Log",
        "*Prashil's film journal. Edit freely — changes are picked up on rescan.*",
        "",
        "---",
        "",
    ]

    if watched:
        lines.append("<!-- WATCHED -->\n")
        for row in watched:
            film = {k: row[k] for k in ["tmdb_id", "title", "year", "default_taste_attributes"]}
            user_film = {
                k: row[k]
                for k in ["status", "watch_context", "date_watched", "date_added", "personal_notes"]
                if k in row
            }
            lines.append(_format_entry(film, user_film))

    if watchlist:
        lines.append("<!-- WATCHLIST -->\n")
        for row in watchlist:
            film = {k: row[k] for k in ["tmdb_id", "title", "year", "default_taste_attributes"]}
            user_film = {
                k: row[k]
                for k in ["status", "watch_context", "date_watched", "date_added", "personal_notes"]
                if k in row
            }
            lines.append(_format_entry(film, user_film))

    TASTE_LOG_PATH.write_text("\n".join(lines), encoding="utf-8")


def append_to_taste_log(film: dict, user_film: dict) -> None:
    """
    Append a single new entry to taste_log.md.
    Much faster than a full rewrite — used during manual add.
    """
    entry = _format_entry(film, user_film)

    if not TASTE_LOG_PATH.exists():
        # First entry — write the header too
        header = (
            "# Taste Log\n"
            "*Prashil's film journal. Edit freely — changes are picked up on rescan.*\n\n"
            "---\n\n"
        )
        TASTE_LOG_PATH.write_text(header + entry, encoding="utf-8")
    else:
        # Append to existing file
        existing = TASTE_LOG_PATH.read_text(encoding="utf-8")
        TASTE_LOG_PATH.write_text(existing + entry, encoding="utf-8")


# ---------------------------------------------------------------------------
# taste_log.md parsing (used by model.py at rescan time)
# ---------------------------------------------------------------------------

ENTRY_PATTERN = re.compile(
    r"## (.+?) \((\d{4})\)\n\*(.+?)\*\n\n?(.*?)\n?\*\*Attributes:\*\* (.+?)\n\*\*Confidence:\*\* (\w[\w -]*)",
    re.DOTALL,
)

ENTRY_PATTERN_NO_YEAR = re.compile(
    r"## (.+?)\n\*(.+?)\*\n\n?(.*?)\n?\*\*Attributes:\*\* (.+?)\n\*\*Confidence:\*\* (\w[\w -]*)",
    re.DOTALL,
)


def parse_taste_log() -> list[dict]:
    """
    Parse taste_log.md and return a list of structured entry dicts.
    Used by model.py during rescan.

    Each dict has:
        title, year, status, watch_context, date_watched, date_added,
        personal_notes, attributes (list), confidence
    """
    if not TASTE_LOG_PATH.exists():
        return []

    text = TASTE_LOG_PATH.read_text(encoding="utf-8")
    entries = []

    for match in ENTRY_PATTERN.finditer(text):
        title, year_str, meta, notes, attrs_str, confidence = match.groups()

        meta_parts = [p.strip() for p in meta.split("·")]
        status = meta_parts[0] if meta_parts else "watchlist"
        watch_context = None
        date_watched = None
        date_added = None

        for part in meta_parts[1:]:
            part = part.strip()
            if part in ("solo", "family", "partner", "friends"):
                watch_context = part
            elif part.startswith("added "):
                date_added = part[6:]
            elif re.match(r"\d{4}-\d{2}-\d{2}", part):
                if status == "watched":
                    date_watched = part
                else:
                    date_added = part

        attrs = [a.strip() for a in attrs_str.split("·") if a.strip()]

        entries.append({
            "title": title.strip(),
            "year": int(year_str),
            "status": status.strip(),
            "watch_context": watch_context,
            "date_watched": date_watched,
            "date_added": date_added,
            "personal_notes": notes.strip() if notes.strip() else None,
            "attributes": attrs,
            "confidence": confidence.strip(),
        })

    return entries


# ---------------------------------------------------------------------------
# Manual entry flow (interactive CLI)
# ---------------------------------------------------------------------------

def add_film_interactive(title: str) -> None:
    """
    Full interactive flow for adding a film via `python main.py add`.
    Searches TMDB, confirms match, collects status/context/notes, writes to DB + log.
    """
    # ── 1. TMDB search ──────────────────────────────────────────────────────
    click.echo(f"\nSearching for \"{title}\" …\n")

    try:
        candidates = tmdb_client.search_movies(title)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        return

    if not candidates:
        click.echo("No results found on TMDB.")
        click.echo("You can still add it manually — file a note in taste_log.md.")
        return

    # ── 2. Pick the right film ───────────────────────────────────────────────
    if len(candidates) == 1:
        chosen = candidates[0]
        year_str = f" ({chosen['year']})" if chosen.get("year") else ""
        lang_str = f" [{chosen.get('language', '?')}]" if chosen.get("language") else ""
        click.echo(f"Found: {chosen['title']}{year_str}{lang_str}")
        if not click.confirm("Is this the right film?", default=True):
            click.echo("Try a more specific title, or add --year to narrow it down.")
            return
    else:
        click.echo(f"Found {min(len(candidates), 6)} results:\n")
        for i, c in enumerate(candidates[:6], 1):
            year_str = f" ({c['year']})" if c.get("year") else ""
            lang_str = f" [{c.get('language', '?')}]" if c.get("language") else ""
            click.echo(f"  {i}. {c['title']}{year_str}{lang_str}")

        raw = click.prompt("\nWhich one? (number, or 0 to cancel)", default="1")
        try:
            idx = int(raw) - 1
            if idx < 0 or idx >= len(candidates[:6]):
                click.echo("Cancelled.")
                return
            chosen = candidates[idx]
        except ValueError:
            click.echo("Cancelled.")
            return

    tmdb_id = chosen["tmdb_id"]

    # ── 3. Check for duplicate ───────────────────────────────────────────────
    existing = db.get_user_film_by_tmdb(tmdb_id)
    if existing:
        click.echo(
            f"\n{chosen['title']} is already in your log "
            f"(status: {existing['status']})."
        )
        if not click.confirm("Update it anyway?", default=False):
            return

    # ── 4. Fetch full metadata + infer attributes ────────────────────────────
    click.echo("\nFetching details …")
    try:
        full = tmdb_client.fetch_film_data(tmdb_id)
    except RuntimeError as e:
        click.echo(f"Error fetching TMDB details: {e}", err=True)
        return

    click.echo("Inferring taste attributes …")
    attrs = infer_default_attributes(full["title"], full.get("year"))
    full["default_taste_attributes"] = attrs

    db.upsert_film(**full)

    # ── 5. Status ────────────────────────────────────────────────────────────
    click.echo(f"\n── {full['title']} ({full.get('year', '?')}) ──")
    if full.get("runtime"):
        click.echo(f"   {full['runtime']} min  ·  {full.get('language', '?')}  ·  {', '.join(full.get('tmdb_genres') or [])}")

    click.echo()
    status_choice = click.prompt(
        "Watched it or adding to watchlist?",
        type=click.Choice(["watched", "watchlist"], case_sensitive=False),
        default="watchlist",
    )

    # ── 6. Watch context (if watched) ────────────────────────────────────────
    watch_context = None
    date_watched = None

    if status_choice == "watched":
        ctx_map = {"1": "solo", "2": "partner", "3": "family", "4": "friends"}
        click.echo("\nWho were you watching with?")
        click.echo("  1. Solo   2. Partner   3. Family   4. Friends")
        ctx_raw = click.prompt("Choice (or Enter to skip)", default="")
        watch_context = ctx_map.get(ctx_raw.strip())

        date_raw = click.prompt(
            "\nWhen did you watch it? (YYYY-MM-DD or Enter for today)",
            default="",
        )
        if date_raw.strip():
            date_watched = date_raw.strip()
        else:
            date_watched = datetime.now(timezone.utc).date().isoformat()

    # ── 7. Personal notes ────────────────────────────────────────────────────
    click.echo()
    if status_choice == "watched":
        prompt_text = "Any notes? What worked, what stuck, why it landed (Enter to skip)"
    else:
        prompt_text = "Why are you adding this? Recommender, what drew you to it (Enter to skip)"

    notes_raw = click.prompt(prompt_text, default="")
    personal_notes = notes_raw.strip() if notes_raw.strip() else None

    # ── 8. Write to DB ───────────────────────────────────────────────────────
    if existing:
        db.update_user_film(
            existing["id"],
            status=status_choice,
            date_watched=date_watched,
            personal_notes=personal_notes,
            watch_context=watch_context,
        )
        row_id = existing["id"]
    else:
        row_id = db.add_user_film(
            tmdb_id=tmdb_id,
            status=status_choice,
            personal_notes=personal_notes,
            watch_context=watch_context,
            source="manual",
            date_watched=date_watched,
        )

    # ── 9. Append to taste_log.md ────────────────────────────────────────────
    user_film = {
        "status": status_choice,
        "watch_context": watch_context,
        "date_watched": date_watched,
        "date_added": datetime.now(timezone.utc).date().isoformat(),
        "personal_notes": personal_notes,
    }
    append_to_taste_log(full, user_film)

    # ── 10. Confirm ──────────────────────────────────────────────────────────
    status_label = "watched" if status_choice == "watched" else "added to watchlist"
    ctx_label = f" · {watch_context}" if watch_context else ""
    click.echo(f"\n✓ {full['title']} ({full.get('year', '?')}) — {status_label}{ctx_label}")
    if attrs:
        click.echo(f"  Attributes: {' · '.join(attrs)}")
    click.echo(f"  taste_log.md updated\n")

    # ── 11. Rescan suggestion ────────────────────────────────────────────────
    counts = db.count_user_films()
    total = sum(counts.values())
    model = db.get_latest_taste_model()

    if model is None and total >= 3:
        click.echo("Tip: you have enough entries to generate your first taste model.")
        click.echo("     Run: python main.py rescan\n")
    elif model and total % RESCAN_SUGGESTION_THRESHOLD == 0:
        click.echo(f"Tip: {RESCAN_SUGGESTION_THRESHOLD} new films since last rescan.")
        click.echo("     Run: python main.py rescan   to sharpen your taste model.\n")
