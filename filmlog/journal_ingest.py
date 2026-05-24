"""
journal_ingest.py — parse raw markdown journal dumps into structured journal entries.

Handles Prashil's unstructured film notes from Notion, Google Keep, and Substack.
Splits on H1 headers, detects entry type, auto-links mentioned films, extracts tags.
"""

import re
from pathlib import Path

from filmlog import db


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _split_into_entries(text: str) -> list[dict]:
    """
    Split a markdown file into individual entries based on H1 headers
    or bold-linked titles after dot-separators (e.g. Substack embeds).
    """
    lines = text.split("\n")
    entries: list[dict] = []
    current_title = None
    current_lines: list[str] = []
    # Track if previous non-blank line was a dot separator
    prev_was_separator = False

    for line in lines:
        stripped = line.strip()

        # H1 header = new entry boundary
        if line.startswith("# "):
            if current_title is not None:
                entries.append({
                    "raw_title": current_title,
                    "raw_body": "\n".join(current_lines).strip(),
                })
            current_title = line[2:].strip()
            current_lines = []
            prev_was_separator = False
            continue

        # Bold-linked title after a separator = new entry boundary
        # Pattern: [**Title**](url) on its own line after ……
        if prev_was_separator and re.match(r"\[?\*\*.*\*\*\]?\(https?://", stripped):
            if current_title is not None:
                entries.append({
                    "raw_title": current_title,
                    "raw_body": "\n".join(current_lines).strip(),
                })
            current_title = stripped
            current_lines = []
            prev_was_separator = False
            continue

        # Track separators
        if re.match(r"^[…\.·]{3,}\s*$", stripped):
            prev_was_separator = True
        elif stripped:
            prev_was_separator = False

        current_lines.append(line)

    # Save last entry
    if current_title is not None:
        entries.append({
            "raw_title": current_title,
            "raw_body": "\n".join(current_lines).strip(),
        })

    return entries


def _clean_title(raw_title: str) -> str:
    """Extract a clean title from the raw H1 text, stripping markdown links and escapes."""
    # Extract link text: [Title](url) or [**Title**](url) → Title
    link_match = re.match(r"\[?\*{0,2}\[?(.+?)\]?\*{0,2}\]?\(https?://.+?\)", raw_title)
    if link_match:
        title = link_match.group(1)
    else:
        # Try standard markdown link
        link_match = re.match(r"\[(.+?)\]\(.+?\)", raw_title)
        if link_match:
            title = link_match.group(1)
        else:
            title = raw_title

    # Remove bold markers
    title = title.replace("**", "")
    # Remove markdown escape backslashes
    title = re.sub(r"\\(.)", r"\1", title)
    return title.strip()


def _extract_url(raw_title: str) -> str | None:
    """Extract a URL from a markdown link in the title, if present."""
    match = re.search(r"\((https?://[^\)]+)\)", raw_title)
    return match.group(1) if match else None


def _detect_type(title: str, body: str, url: str | None) -> str:
    """Classify entry as newsletter, musing, or note."""
    if url and "substack" in url:
        return "newsletter"
    # Short entries (under ~200 chars of actual content) are notes
    stripped = re.sub(r"\s+", " ", body).strip()
    if len(stripped) < 200:
        return "note"
    return "musing"


def _clean_body(body: str) -> str:
    """Strip trailing dot-separators, image data, and references from body text."""
    # Remove base64 image reference definitions (can be megabytes)
    body = re.sub(r"\[image\d+\]:.*", "", body)
    # Remove trailing separator lines
    body = re.sub(r"\n[…\.·]{3,}\s*$", "", body)
    # Remove image references like ![alt text][image1] or ![alt](url)
    body = re.sub(r"!\[.*?\]\[.*?\]", "", body)
    body = re.sub(r"!\[.*?\]\(.*?\)", "", body)
    # Remove leftover markdown escapes
    body = re.sub(r"\\([#\-_!.*\[\]])", r"\1", body)
    # Clean up excessive blank lines
    body = re.sub(r"\n{4,}", "\n\n\n", body)
    return body.strip()


def _extract_tags(title: str, body: str, entry_type: str) -> list[str]:
    """Extract relevant tags from entry content."""
    tags = []

    if entry_type == "newsletter":
        tags.append("newsletter")
        # Extract ThreeK issue number
        issue_match = re.search(r"[Tt]hree[Kk]\s*#?\s*(\d+)", title)
        if issue_match:
            tags.append(f"threek-{issue_match.group(1)}")

    # Detect writing/craft entries
    craft_signals = ["writer", "writing", "screenplay", "narrative", "character",
                     "scene", "audience", "protagonist", "villain"]
    lower_body = body.lower()
    if any(s in lower_body for s in craft_signals) and entry_type != "note":
        tags.append("craft")

    # Detect media-specific tags from title
    title_lower = title.lower()
    if "kdrama" in title_lower or "k-drama" in title_lower:
        tags.append("kdrama")
    if "anime" in title_lower:
        tags.append("anime")
    if any(w in title_lower for w in ["day ", "day-"]):
        tags.append("diary")

    return tags


# ---------------------------------------------------------------------------
# Film matching
# ---------------------------------------------------------------------------

def _build_film_index() -> dict[str, int]:
    """
    Build a lowercase title → tmdb_id lookup from the DB.
    Includes both full titles and common short forms.
    """
    conn = db.get_connection()
    rows = conn.execute("SELECT tmdb_id, title FROM films").fetchall()
    conn.close()

    index: dict[str, int] = {}
    for r in rows:
        key = r["title"].lower().strip()
        index[key] = r["tmdb_id"]
        # Also index without "The " prefix
        if key.startswith("the "):
            index[key[4:]] = r["tmdb_id"]

    return index


_COMMON_WORDS = {
    "the thing", "the call", "kill", "dark", "mother", "soni", "connect",
    "memories", "as one", "encounter", "run", "love", "home", "the host",
    "life", "the mist", "alone", "the gift", "the hunt", "missing",
    "hope", "fire", "the wall", "signal", "wanted", "us", "them",
    "split", "taken", "fury", "crash", "the one", "control", "alive",
    "fell", "her", "duel", "it", "up", "raw", "call", "thing",
    "helpless", "burning", "detachment", "jaws", "logan", "the shining",
    "the departed", "satya",
}


def _find_mentioned_films(title: str, body: str, film_index: dict[str, int]) -> list[int]:
    """
    Find tmdb_ids of films mentioned in the entry text.
    Uses word-boundary matching against DB titles. Short/common-word titles
    are only matched when they appear in a film-referencing context
    (e.g. quoted, italicised, or near words like "film", "movie", "watch").
    """
    full_text = f"{title}\n{body}"
    found_ids: set[int] = set()

    sorted_titles = sorted(film_index.keys(), key=len, reverse=True)

    for film_title in sorted_titles:
        if len(film_title) < 3:
            continue

        # Word boundary match
        pattern = re.compile(
            r"(?<![a-zA-Z])" + re.escape(film_title) + r"(?![a-zA-Z])",
            re.IGNORECASE,
        )
        match = pattern.search(full_text)
        if not match:
            continue

        # For common-word titles, require explicit film-naming context nearby
        if film_title.lower() in _COMMON_WORDS:
            start = max(0, match.start() - 60)
            end = min(len(full_text), match.end() + 60)
            context = full_text[start:end].lower()
            film_signals = ["film", "movie", "watch", "drama", "series", "anime",
                            "season", "episode", "show", "recommend",
                            "rated", "review", "masterpiece"]
            if not any(s in context for s in film_signals):
                continue

        found_ids.add(film_index[film_title])

    return sorted(found_ids)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def parse_journal_dump(filepath: str | Path) -> list[dict]:
    """
    Parse a raw markdown journal dump into structured entries ready for DB insert.

    Returns list of dicts with keys:
        title, content, type, linked_film_ids, tags, source_url
    """
    filepath = Path(filepath)
    text = filepath.read_text(encoding="utf-8")

    raw_entries = _split_into_entries(text)
    film_index = _build_film_index()

    parsed: list[dict] = []
    for entry in raw_entries:
        title = _clean_title(entry["raw_title"])
        url = _extract_url(entry["raw_title"])
        body = _clean_body(entry["raw_body"])

        if not body:
            continue

        entry_type = _detect_type(title, body, url)
        tags = _extract_tags(title, body, entry_type)
        film_ids = _find_mentioned_films(title, body, film_index)

        # Add source URL as first line of content if it's a newsletter
        content = body
        if url:
            content = f"*Originally published: [{url}]({url})*\n\n{body}"

        parsed.append({
            "title": title,
            "content": content,
            "type": entry_type,
            "linked_film_ids": film_ids,
            "tags": tags,
        })

    return parsed


def ingest_journal_dump(filepath: str | Path, dry_run: bool = False) -> list[dict]:
    """
    Parse and import a journal dump into the database.

    Args:
        filepath: Path to the raw markdown file
        dry_run: If True, parse and return entries without writing to DB

    Returns:
        List of parsed entries (with 'id' key added if written to DB)
    """
    entries = parse_journal_dump(filepath)

    if dry_run:
        return entries

    for entry in entries:
        entry_id = db.create_journal_entry(
            title=entry["title"],
            content=entry["content"],
            entry_type=entry["type"],
            linked_film_ids=entry["linked_film_ids"],
            tags=entry["tags"],
        )
        entry["id"] = entry_id

    print(f"✓ Imported {len(entries)} journal entries")
    return entries
