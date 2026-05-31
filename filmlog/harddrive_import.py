"""
harddrive_import.py — Import films and TV shows from a hard drive catalog JSON.

Reads data/harddrive_catalog.json, looks up each title on TMDB, and adds
new entries to the FilmLog database with source='harddrive_import'.

Deduplicates against existing entries. Uses alt_title as TMDB search fallback.
TV shows use search_tv() and get the right media_type (tv or anime).
"""

import json
import time
from pathlib import Path

import click

from filmlog import db, tmdb
from filmlog import attributes as attr_module


CATALOG_PATH = Path(__file__).parent.parent / "data" / "harddrive_catalog.json"

# Titles to skip: short films, stage adaptations, concert films, non-feature content
SKIP_KEYWORDS = {
    "concert", "live performance", "behind the scenes", "making of",
    "bonus features", "deleted scenes", "featurette",
}


def _best_movie_match(title: str, year: int | None, alt_title: str | None = None) -> dict | None:
    """
    Search TMDB for a movie. Try primary title first, then alt_title.
    Returns the best candidate dict or None.
    """
    candidates = tmdb.search_movies(title, year)

    # If year search returned nothing, try without year
    if not candidates and year:
        candidates = tmdb.search_movies(title)

    # Fallback to alt_title
    if not candidates and alt_title:
        candidates = tmdb.search_movies(alt_title, year)
        if not candidates and year:
            candidates = tmdb.search_movies(alt_title)

    if not candidates:
        return None

    # If we have a year, prefer exact year match
    if year:
        for c in candidates:
            if c.get("year") == year:
                return c
        # Accept ±1 year (release date discrepancies)
        for c in candidates:
            if c.get("year") and abs(c["year"] - year) <= 1:
                return c

    # Otherwise return the first result (highest relevance)
    return candidates[0] if candidates else None


def _best_tv_match(title: str, year: int | None = None) -> dict | None:
    """Search TMDB for a TV show. Returns the best candidate or None."""
    candidates = tmdb.search_tv(title, year)
    if not candidates and year:
        candidates = tmdb.search_tv(title)
    if not candidates:
        return None

    if year:
        for c in candidates:
            if c.get("year") == year:
                return c
    return candidates[0] if candidates else None


def _should_skip(film: dict) -> bool:
    """Check if this entry should be skipped (non-feature content)."""
    note = (film.get("note") or "").lower()
    title = film.get("title", "").lower()
    for kw in SKIP_KEYWORDS:
        if kw in note or kw in title:
            return True
    return False


def load_catalog() -> dict:
    """Load and return the hard drive catalog JSON."""
    if not CATALOG_PATH.exists():
        raise FileNotFoundError(f"Catalog not found at {CATALOG_PATH}")
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)


def import_harddrive(dry_run: bool = False, infer_attrs: bool = True) -> dict:
    """
    Main import function. Returns a summary dict:
    {added, skipped_existing, skipped_notfound, skipped_filter, errors, total}
    """
    catalog = load_catalog()

    # Collect all film entries from all categories
    all_films = []
    for cat_key, cat in catalog["movies"]["categories"].items():
        category_label = cat.get("label", cat_key)
        for film in cat.get("films", []):
            film["_category"] = category_label
            film["_is_tv"] = False
            all_films.append(film)

    # Collect TV shows
    all_tv = []
    for show in catalog.get("tv_shows", {}).get("shows", []):
        show["_is_tv"] = True
        all_tv.append(show)

    total = len(all_films) + len(all_tv)
    added = 0
    skipped_existing = 0
    skipped_notfound = 0
    skipped_filter = 0
    errors = 0

    click.echo(f"\n── Hard drive import: {len(all_films)} films + {len(all_tv)} TV shows ──────────────\n")

    # ── Process movies ──────────────────────────────────────────────────────
    for i, film in enumerate(all_films, 1):
        title = film["title"]
        year = film.get("year")
        alt_title = film.get("alt_title")
        note = film.get("note", "")
        category = film.get("_category", "")

        prefix = f"  [{i}/{len(all_films)}]"

        # Skip non-feature content
        if _should_skip(film):
            click.echo(f"{prefix} SKIP (filter): {title} — {note}")
            skipped_filter += 1
            continue

        # Search TMDB
        try:
            match = _best_movie_match(title, year, alt_title)
        except Exception as e:
            click.echo(f"{prefix} ERROR: {title} — {e}")
            errors += 1
            continue

        if not match:
            click.echo(f"{prefix} NOT FOUND: {title}" + (f" ({year})" if year else ""))
            skipped_notfound += 1
            continue

        tmdb_id = match["tmdb_id"]

        # Check for existing entry
        existing = db.get_user_film_by_tmdb(tmdb_id)
        if existing:
            click.echo(f"{prefix} EXISTS: {title} → {match['title']} ({match.get('year', '?')})")
            skipped_existing += 1
            continue

        # Fetch full details
        try:
            details = tmdb.get_movie_details(tmdb_id)
        except Exception as e:
            click.echo(f"{prefix} ERROR fetching details: {title} — {e}")
            errors += 1
            continue

        if dry_run:
            click.echo(f"{prefix} WOULD ADD: {title} → {details['title']} ({details.get('year', '?')}) [{details.get('language', '?')}]")
            added += 1
            continue

        # Determine media_type (anime movies stay as 'movie' in TMDB, but we tag them)
        media_type = "movie"
        if category == "Anime movies":
            media_type = "anime"

        # Upsert film record
        db.upsert_film(
            tmdb_id=details["tmdb_id"],
            title=details["title"],
            year=details.get("year"),
            runtime=details.get("runtime"),
            language=details.get("language"),
            country=details.get("country"),
            poster_url=details.get("poster_url"),
            tmdb_genres=details.get("tmdb_genres"),
            media_type=media_type,
            overview=details.get("overview"),
        )

        # Add user_film entry
        db.add_user_film(
            tmdb_id=tmdb_id,
            status="watchlist",
            source="harddrive_import",
            personal_notes=f"On hard drive. {note}".strip() if note else "On hard drive.",
        )

        # Infer taste attributes
        if infer_attrs:
            try:
                attrs = attr_module.infer_single(tmdb_id)
                if attrs:
                    click.echo(f"{prefix} ✓ {details['title']} ({details.get('year', '?')}) + {len(attrs)} attrs")
                else:
                    click.echo(f"{prefix} ✓ {details['title']} ({details.get('year', '?')})")
            except Exception:
                click.echo(f"{prefix} ✓ {details['title']} ({details.get('year', '?')}) (attrs failed)")
        else:
            click.echo(f"{prefix} ✓ {details['title']} ({details.get('year', '?')})")

        added += 1

        # Rate limit: ~0.25s between TMDB calls to stay under 40 req/s
        time.sleep(0.25)

    # ── Process TV shows ────────────────────────────────────────────────────
    if all_tv:
        click.echo(f"\n── TV shows ({len(all_tv)}) ──────────────\n")

    for i, show in enumerate(all_tv, 1):
        title = show["title"]
        prefix = f"  [{i}/{len(all_tv)}]"

        # Search TMDB TV
        try:
            match = _best_tv_match(title)
        except Exception as e:
            click.echo(f"{prefix} ERROR: {title} — {e}")
            errors += 1
            continue

        if not match:
            click.echo(f"{prefix} NOT FOUND: {title}")
            skipped_notfound += 1
            continue

        tmdb_id = match["tmdb_id"]

        # Check existing
        existing = db.get_user_film_by_tmdb(tmdb_id)
        if existing:
            click.echo(f"{prefix} EXISTS: {title}")
            skipped_existing += 1
            continue

        # Fetch full details
        try:
            details = tmdb.get_tv_details(tmdb_id)
        except Exception as e:
            click.echo(f"{prefix} ERROR: {title} — {e}")
            errors += 1
            continue

        # Determine media_type from catalog
        media_type = show.get("media_type", "tv")  # catalog already tags anime

        if dry_run:
            click.echo(f"{prefix} WOULD ADD: {title} → {details['title']} ({details.get('year', '?')}) [{media_type}]")
            added += 1
            continue

        db.upsert_film(
            tmdb_id=details["tmdb_id"],
            title=details["title"],
            year=details.get("year"),
            runtime=details.get("runtime"),
            language=details.get("language"),
            country=details.get("country"),
            poster_url=details.get("poster_url"),
            tmdb_genres=details.get("tmdb_genres"),
            media_type=media_type,
            overview=details.get("overview"),
        )

        note = show.get("note", "")
        db.add_user_film(
            tmdb_id=tmdb_id,
            status="watchlist",
            source="harddrive_import",
            personal_notes=f"On hard drive. {note}".strip() if note else "On hard drive.",
        )

        # Infer taste attributes for TV too
        if infer_attrs:
            try:
                attrs = attr_module.infer_single(tmdb_id)
                if attrs:
                    click.echo(f"{prefix} ✓ {details['title']} ({details.get('year', '?')}) [{media_type}] + {len(attrs)} attrs")
                else:
                    click.echo(f"{prefix} ✓ {details['title']} ({details.get('year', '?')}) [{media_type}]")
            except Exception:
                click.echo(f"{prefix} ✓ {details['title']} ({details.get('year', '?')}) [{media_type}] (attrs failed)")
        else:
            click.echo(f"{prefix} ✓ {details['title']} ({details.get('year', '?')}) [{media_type}]")

        added += 1
        time.sleep(0.25)

    # ── Summary ─────────────────────────────────────────────────────────────
    action = "Would add" if dry_run else "Added"
    click.echo(f"\n── Summary ──────────────────────────────")
    click.echo(f"  {action}:          {added}")
    click.echo(f"  Already existed:  {skipped_existing}")
    click.echo(f"  Not found (TMDB): {skipped_notfound}")
    click.echo(f"  Filtered out:     {skipped_filter}")
    click.echo(f"  Errors:           {errors}")
    click.echo(f"  Total processed:  {total}")
    click.echo()

    return {
        "added": added,
        "skipped_existing": skipped_existing,
        "skipped_notfound": skipped_notfound,
        "skipped_filter": skipped_filter,
        "errors": errors,
        "total": total,
    }
