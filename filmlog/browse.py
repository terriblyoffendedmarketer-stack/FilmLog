"""
browse.py — browse and filter the watchlist.

Default view: top films ranked by taste-attribute match, unified pool
(all languages together — language is a filter, never the default organiser).

Filters:
  --language  ko / ja / ml / ta / en / fr / hi / etc.
  --context   solo / family / partner / friends
  --mood      keyword matched against taste attributes (e.g. "slow burn", "eerie")
  --length    short (<90min) / medium (90–140) / long (>140)
  --era       70s / 80s / 90s / 2000s / 2010s / 2020s
  --query     natural language — Claude Haiku parses to filters + ranks

The natural language query path is the most powerful:
  "Malayalam film I can watch with family"
  "Something short and funny"
  "Japanese horror from the 80s"
  "Slow burn that doesn't need full attention"
"""

import json
import os
import re

import click
from dotenv import load_dotenv

from filmlog import db
from filmlog.model import _quality_signal

load_dotenv(override=True)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

ERA_MAP = {
    "50s": (1950, 1959), "60s": (1960, 1969), "70s": (1970, 1979),
    "80s": (1980, 1989), "90s": (1990, 1999), "2000s": (2000, 2009),
    "2010s": (2010, 2019), "2020s": (2020, 2029),
}

LENGTH_MAP = {
    "short": (0, 90),
    "medium": (90, 140),
    "long": (140, 9999),
}


def _get_film_attrs(film: dict) -> list[str]:
    """Extract default_taste_attributes as a list of strings."""
    raw = film.get("default_taste_attributes") or "[]"
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return []
    return raw or []


def _apply_filters(
    films: list[dict],
    language: str | None = None,
    context: str | None = None,
    mood: str | None = None,
    length: str | None = None,
    era: str | None = None,
) -> list[dict]:
    """Apply hard filters to a film list. Returns filtered list."""
    result = films

    if language:
        lang_lower = language.lower().strip()
        result = [
            f for f in result
            if (f.get("language") or "").lower() == lang_lower
            or lang_lower in (f.get("country") or "").lower()
        ]

    if length and length.lower() in LENGTH_MAP:
        lo, hi = LENGTH_MAP[length.lower()]
        result = [
            f for f in result
            if f.get("runtime") and lo <= (f.get("runtime") or 0) <= hi
        ]

    if era and era.lower() in ERA_MAP:
        lo, hi = ERA_MAP[era.lower()]
        result = [
            f for f in result
            if f.get("year") and lo <= (f.get("year") or 0) <= hi
        ]

    if mood:
        mood_lower = mood.lower()
        result = [
            f for f in result
            if any(mood_lower in attr.lower() for attr in _get_film_attrs(f))
        ]

    if context:
        # Context filter: cross-reference with cluster taste_attributes
        ctx_attrs = db.get_taste_attributes(cluster=context, polarity="positive")
        if ctx_attrs:
            ctx_attr_names = {a["attribute_name"].lower() for a in ctx_attrs}
            def _context_match(film):
                film_attrs = [a.lower() for a in _get_film_attrs(film)]
                return any(
                    any(ca in fa or fa in ca for ca in ctx_attr_names)
                    for fa in film_attrs
                )
            result = [f for f in result if _context_match(f)]

    return result


# ---------------------------------------------------------------------------
# Taste-attribute ranking
# ---------------------------------------------------------------------------

def _score_by_taste(films: list[dict], cluster: str | None = None) -> list[dict]:
    """
    Rank films by how well their attributes match the stored taste model.
    Returns the list sorted by score descending.
    """
    pos_attrs = db.get_taste_attributes(cluster=cluster, polarity="positive")
    neg_attrs = db.get_taste_attributes(cluster=cluster, polarity="negative")

    if cluster:
        pos_attrs += db.get_taste_attributes(cluster=None, polarity="positive")

    if not pos_attrs:
        return films  # No model yet — return as-is

    pos_map = {a["attribute_name"].lower(): a["confidence_score"] for a in pos_attrs}
    neg_set = {a["attribute_name"].lower() for a in neg_attrs}

    scored = []
    for film in films:
        score = 0.0
        for fa in _get_film_attrs(film):
            fa_lower = fa.lower()
            for taste_attr, confidence in pos_map.items():
                if taste_attr in fa_lower or fa_lower in taste_attr:
                    score += confidence
            for neg_attr in neg_set:
                if neg_attr in fa_lower or fa_lower in neg_attr:
                    score -= 0.3
        scored.append((score, film))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [film for _, film in scored]


# ---------------------------------------------------------------------------
# Natural language query (Claude Haiku)
# ---------------------------------------------------------------------------

NL_QUERY_PROMPT = """Parse this film search query into filter parameters.

Query: "{query}"

Return JSON with these optional fields (omit fields that aren't relevant):
{{
  "language": "two-letter language code or null — ko=Korean, ja=Japanese, ml=Malayalam, ta=Tamil, hi=Hindi, en=English, fr=French, zh=Chinese",
  "context": "solo | family | partner | friends | null",
  "mood": "a short phrase matching the mood/vibe (e.g. 'slow burn', 'funny', 'eerie', 'warm') or null",
  "length": "short | medium | long | null",
  "era": "50s | 60s | 70s | 80s | 90s | 2000s | 2010s | 2020s | null",
  "free_text": "any part of the query not captured by filters — for re-ranking"
}}

Examples:
"Malayalam film I can watch with family" → {{"language": "ml", "context": "family"}}
"Something short and funny" → {{"length": "short", "mood": "funny"}}
"Japanese horror from the 80s" → {{"language": "ja", "mood": "horror", "era": "80s"}}
"Slow burn that doesn't need full attention" → {{"mood": "slow burn"}}

Return ONLY the JSON object."""


def _parse_natural_language_query(query: str) -> dict:
    """
    Use Claude Haiku to parse a natural language query into filter params.
    Falls back to empty dict (no filters) if API is unavailable.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"free_text": query}

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": NL_QUERY_PROMPT.format(query=query)
            }]
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw).strip()
        return json.loads(raw)
    except Exception:
        return {"free_text": query}


def _rerank_by_free_text(films: list[dict], free_text: str) -> list[dict]:
    """
    Simple re-rank: boost films whose title, notes, or attributes
    contain words from the free text query.
    """
    if not free_text:
        return films

    words = set(free_text.lower().split())
    scored = []
    for film in films:
        boost = 0
        title = (film.get("title") or "").lower()
        notes = (film.get("personal_notes") or "").lower()
        attrs = " ".join(_get_film_attrs(film)).lower()
        combined = f"{title} {notes} {attrs}"
        boost = sum(1 for w in words if len(w) > 3 and w in combined)
        scored.append((boost, film))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in scored]


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _display_results(films: list[dict], total: int, filters_applied: list[str]) -> None:
    """Render the browse results in the terminal."""
    if not films:
        click.echo("\n  No films found matching those filters.")
        click.echo("  Try: python main.py browse   (no filters) to see everything.\n")
        return

    filter_str = "  ·  ".join(filters_applied) if filters_applied else "top by taste match"
    click.echo(f"\n── Browse: {filter_str} ──{'─' * max(0, 40 - len(filter_str))}")
    click.echo(f"   {len(films)} shown  ·  {total} total in watchlist\n")

    for i, film in enumerate(films, 1):
        title = film.get("title", "Unknown")
        year = film.get("year", "")
        lang = film.get("language", "")
        runtime = film.get("runtime")
        notes = film.get("personal_notes") or ""
        attrs = _get_film_attrs(film)
        status = film.get("status", "watchlist")

        # Title line
        meta = []
        if lang:
            meta.append(f"[{lang}]")
        if runtime:
            meta.append(f"{runtime}min")
        if status == "rewatch_candidate":
            meta.append("↩ rewatch")

        title_line = f"  {i:>2}. {title} ({year})"
        if meta:
            title_line += "  " + "  ".join(meta)
        click.echo(title_line)

        # Attributes (top 3)
        if attrs:
            click.echo(f"       {' · '.join(attrs[:3])}")

        # Personal note (first sentence only)
        if notes:
            first_sentence = notes.split(".")[0].strip()
            short = (first_sentence[:90] + "…") if len(first_sentence) > 90 else first_sentence
            click.echo(f"       \"{short}\"")

        click.echo()

    click.echo("─" * 54)
    click.echo("  To add notes or mark watched: python main.py add \"<title>\"")
    click.echo("  To get a recommendation:      python main.py recommend\n")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def browse(
    language: str | None = None,
    context: str | None = None,
    mood: str | None = None,
    length: str | None = None,
    era: str | None = None,
    query: str | None = None,
    limit: int = 15,
    show_watched: bool = False,
) -> None:
    """
    Browse the film library with optional filters.
    Default: unified pool, ranked by taste-attribute match.
    """
    # ── 1. Get base pool ─────────────────────────────────────────────────────
    films = db.get_watchlist()
    if show_watched:
        films += db.get_watched_films()

    total = len(films)

    if not films:
        click.echo("\nYour watchlist is empty.")
        click.echo("Run: python main.py ingest-all   to import your saved lists.\n")
        return

    # ── 2. Natural language query path ───────────────────────────────────────
    free_text = None
    if query:
        click.echo(f"\nParsing: \"{query}\" …")
        parsed = _parse_natural_language_query(query)
        # Let parsed params override explicit args
        language = language or parsed.get("language")
        context = context or parsed.get("context")
        mood = mood or parsed.get("mood")
        length = length or parsed.get("length")
        era = era or parsed.get("era")
        free_text = parsed.get("free_text")

    # ── 3. Apply hard filters ────────────────────────────────────────────────
    filters_applied = []
    if language:
        filters_applied.append(f"language={language}")
    if context:
        filters_applied.append(f"context={context}")
    if mood:
        filters_applied.append(f"mood~{mood!r}")
    if length:
        filters_applied.append(f"length={length}")
    if era:
        filters_applied.append(f"era={era}")
    if query:
        filters_applied.append(f'query="{query}"')

    films = _apply_filters(films, language, context, mood, length, era)

    # ── 4. Rank by taste match ───────────────────────────────────────────────
    films = _score_by_taste(films, cluster=context)

    # Re-rank by free text from NL query if present
    if free_text:
        films = _rerank_by_free_text(films, free_text)

    # ── 5. Display ───────────────────────────────────────────────────────────
    _display_results(films[:limit], total, filters_applied)
