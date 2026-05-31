"""
attributes.py — infer taste attributes for films using Claude Haiku.

Each film gets 4–6 short taste attributes that describe its viewing experience
(not plot, not genre — what it *feels like* to watch it). These power the
candidate scoring in recommend.py.

Uses claude-haiku-4-5-20251001 for speed and cost.
"""

import json
import os
import time

from filmlog import db


ATTRIBUTE_PROMPT = """You are a film taste analyst. For each film below, provide 4–6 short taste attributes that describe the *viewing experience* — not plot, not genre labels. Think about:

- Emotional texture (e.g. "slow-building dread", "cathartic release", "bittersweet nostalgia")
- Energy and pacing (e.g. "relentless tension", "contemplative stillness", "frenetic energy")
- Sensory qualities (e.g. "sun-drenched visuals", "claustrophobic framing", "atmospheric sound design")
- Thematic feel (e.g. "existential weight", "dark absurdist humour", "survival stakes")
- Hookability (e.g. "grips you from minute one", "slow reveal that rewards patience")

Bad attributes: "drama", "foreign film", "critically acclaimed", "award-winning"
Good attributes: "mounting paranoia", "lived-in warmth", "gut-punch ending", "dreamlike pacing"

Return ONLY valid JSON. Format: a JSON object where each key is the TMDB ID (as a string) and the value is an array of 4–6 attribute strings.

Films:
{film_list}

Return JSON only, no markdown fencing, no explanation."""


def _format_films_for_prompt(films: list[dict]) -> str:
    """Format a batch of films for the inference prompt."""
    lines = []
    for f in films:
        genres = f.get("tmdb_genres") or "[]"
        if isinstance(genres, str):
            try:
                genres = json.loads(genres)
            except Exception:
                genres = []
        genre_str = ", ".join(genres[:4]) if genres else "unknown"

        overview = (f.get("overview") or "")[:200]
        line = f"- TMDB ID {f['tmdb_id']}: {f['title']} ({f.get('year', '?')}) [{genre_str}] {f.get('language', '').upper()} {f.get('runtime', '?')}min"
        if overview:
            line += f" — {overview}"
        lines.append(line)
    return "\n".join(lines)


def infer_attributes_batch(films: list[dict]) -> dict[int, list[str]]:
    """
    Call Claude Haiku to infer taste attributes for a batch of films.
    Returns {tmdb_id: [attr1, attr2, ...]} for successfully inferred films.
    Batch size should be 10–15 for best results.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    film_list = _format_films_for_prompt(films)
    prompt = ATTRIBUTE_PROMPT.format(film_list=film_list)

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()

    # Strip markdown fencing if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  ✗ Failed to parse JSON response: {raw[:200]}")
        return {}

    # Convert string keys to int
    result = {}
    for key, attrs in parsed.items():
        try:
            tmdb_id = int(key)
            if isinstance(attrs, list) and all(isinstance(a, str) for a in attrs):
                result[tmdb_id] = attrs[:6]  # cap at 6
        except (ValueError, TypeError):
            continue

    return result


def backfill_attributes(batch_size: int = 10, max_batches: int = 100, delay: float = 1.0) -> int:
    """
    Backfill default_taste_attributes for all films that don't have them.
    Processes in batches to stay within API limits.

    Returns total number of films updated.
    """
    total_updated = 0

    for batch_num in range(max_batches):
        films = db.get_films_without_attributes(limit=batch_size)
        if not films:
            break

        print(f"  Batch {batch_num + 1}: inferring attributes for {len(films)} films…")

        try:
            results = infer_attributes_batch(films)
        except Exception as e:
            print(f"  ✗ Batch {batch_num + 1} failed: {e}")
            break

        for tmdb_id, attrs in results.items():
            db.set_film_taste_attributes(tmdb_id, attrs)
            total_updated += 1
            film_title = next((f["title"] for f in films if f["tmdb_id"] == tmdb_id), "?")
            print(f"    ✓ {film_title}: {', '.join(attrs[:3])}…")

        # Rate limiting
        if batch_num < max_batches - 1 and films:
            time.sleep(delay)

    return total_updated


def infer_single(tmdb_id: int) -> list[str] | None:
    """
    Infer attributes for a single film and save to DB.
    Used when adding a new film. Returns the attributes list or None on failure.
    """
    film = db.get_film(tmdb_id)
    if not film:
        return None

    # Skip if already has attributes
    existing = film.get("default_taste_attributes")
    if existing and existing not in ("[]", "null", None):
        try:
            parsed = json.loads(existing) if isinstance(existing, str) else existing
            if parsed:
                return parsed
        except Exception:
            pass

    try:
        results = infer_attributes_batch([film])
    except Exception:
        return None

    attrs = results.get(tmdb_id)
    if attrs:
        db.set_film_taste_attributes(tmdb_id, attrs)
    return attrs
