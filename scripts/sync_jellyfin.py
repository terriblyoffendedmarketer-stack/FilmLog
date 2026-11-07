# scripts/sync_jellyfin.py — Syncs Jellyfin Mac library & personal comments into FilmLog.
#
# Usage:
#   cd ~/Documents/"Claude Code"/FilmLog
#   .venv/bin/python3 scripts/sync_jellyfin.py
#
# Requires:
#   - TMDB_API_KEY in .env
#   - ~/Documents/Claude Code/Jellyfin Mac/jellyfin-mac-setup/library.json
#   - ~/Documents/Claude Code/Jellyfin Mac/jellyfin-mac-setup/film_comments.json
#
# Gotchas:
#   - Titles from Jellyfin filenames often contain release tags like "(Dub)", "[Movie]",
#     or resolution tags (".x264"). These must be stripped before TMDB search.
#   - Some titles have years embedded in the name string; extract the year if missing.
#   - When merging film_comments.json, only replace notes if existing notes are generic
#     (e.g., "Hall of Fame", "Hall of Fame #1") so user customizations are preserved.

import os
import re
import sys
import json
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from filmlog import db, tmdb


JELLYFIN_DIR = Path.home() / "Documents" / "Claude Code" / "Jellyfin Mac" / "jellyfin-mac-setup"
LIBRARY_JSON = JELLYFIN_DIR / "library.json"
COMMENTS_JSON = JELLYFIN_DIR / "film_comments.json"


def clean_title(raw_title: str) -> tuple[str, int | None]:
    """Clean filename/tag noise from Jellyfin titles and extract year if embedded."""
    title = raw_title.strip()

    # Extract year if at the end e.g. "Title (2014)"
    year = None
    m = re.search(r'\((\d{4})\)$', title)
    if m:
        year = int(m.group(1))
        title = title[:m.start()].strip()

    # Strip bracketed metadata like [1080p], [Dual-Audio], [V2], [Movie]
    title = re.sub(r'\[.*?\]', '', title)

    # Strip (Dub), (HD)
    title = re.sub(r'\((Dub|HD|Anime|Original)\)', '', title, flags=re.IGNORECASE)

    # Strip trailing codec junk
    title = re.sub(r'[\.\-_](x264|x265|HEVC|DVDrip|BD|1080p|720p).*$', '', title, flags=re.IGNORECASE)

    # Replace dots/underscores with spaces
    title = title.replace('.', ' ').replace('_', ' ')
    title = re.sub(r'\s+', ' ', title).strip()

    return title, year


def sync_library():
    print("=" * 60)
    print("🎬 FilmLog ⟷ Jellyfin Sync")
    print("=" * 60)

    if not LIBRARY_JSON.exists():
        print(f"❌ Error: {LIBRARY_JSON} not found!")
        return

    with open(LIBRARY_JSON) as f:
        lib_data = json.load(f)

    items = lib_data.get("items", [])
    print(f"📂 Found {len(items)} items in Jellyfin library.json")

    # Load existing films from DB
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT tmdb_id, lower(title), year FROM films")
        existing_films = {}
        for r in cursor.fetchall():
            existing_films[(r[1].strip(), r[2])] = r[0]
            existing_films[(r[1].strip(), None)] = r[0]

        cursor.execute("SELECT tmdb_id, id, personal_notes, status FROM user_films")
        user_films = {r[0]: {"row_id": r[1], "notes": r[2], "status": r[3]} for r in cursor.fetchall()}

    added_count = 0
    skipped_count = 0

    print("\n🔍 Checking for new Jellyfin films to add to watchlist...")
    for item in items:
        raw_name = item.get("name", "")
        item_year = item.get("year")
        if item_year and len(str(item_year)) > 4:
            # Handle YYYYMMDD style release dates e.g. 20200804
            item_year = int(str(item_year)[:4])

        title, extracted_year = clean_title(raw_name)
        year = item_year or extracted_year

        norm_title = title.lower().strip()
        tmdb_id = existing_films.get((norm_title, year)) or existing_films.get((norm_title, None))

        if tmdb_id:
            skipped_count += 1
            continue

        # Look up on TMDB
        print(f"  ➜ Searching TMDB for: '{title}' ({year or '?'})")
        candidates = tmdb.search_movies(title, year)
        if not candidates and year:
            candidates = tmdb.search_movies(title)

        match = None
        if candidates:
            # Prefer exact year match
            for c in candidates:
                if year and c.get("year") == year:
                    match = c
                    break
            if not match:
                match = candidates[0]

        if match:
            # Fetch full details
            details = tmdb.get_movie_details(match["tmdb_id"])
            if details:
                match = details

            db.upsert_film(
                tmdb_id=match["tmdb_id"],
                title=match["title"],
                year=match.get("year"),
                runtime=match.get("runtime"),
                language=match.get("language"),
                country=match.get("country"),
                poster_url=match.get("poster_url"),
                tmdb_genres=match.get("genres"),
                media_type="movie",
                overview=match.get("overview")
            )

            # Add to watchlist
            db.add_user_film(
                tmdb_id=match["tmdb_id"],
                status="watchlist",
                source="jellyfin"
            )

            existing_films[(norm_title, year)] = match["tmdb_id"]
            user_films[match["tmdb_id"]] = {"row_id": None, "notes": None, "status": "watchlist"}
            added_count += 1
            print(f"     ✓ Added: {match['title']} ({match.get('year')}) [TMDB: {match['tmdb_id']}]")
            time.sleep(0.2)  # TMDB rate limit friendliness
        else:
            print(f"     ⚠ No TMDB match found for: '{title}'")

    print(f"\n✅ Jellyfin library sync complete: {added_count} new films added to watchlist ({skipped_count} already existed).")

    # Step 2: Merge personal comments from film_comments.json
    if COMMENTS_JSON.exists():
        print("\n💬 Merging rich comments from film_comments.json...")
        with open(COMMENTS_JSON) as f:
            comments_data = json.load(f)

        updated_notes_count = 0
        with db.get_connection() as conn:
            cursor = conn.cursor()
            for category, film_dict in comments_data.items():
                for film_title_raw, comment in film_dict.items():
                    ctitle, cyear = clean_title(film_title_raw)
                    cnorm = ctitle.lower().strip()

                    # Find matching film in DB
                    match_id = existing_films.get((cnorm, cyear)) or existing_films.get((cnorm, None))
                    if not match_id:
                        # Fuzzy check
                        for (db_title, _), tid in existing_films.items():
                            if db_title == cnorm or cnorm in db_title or db_title in cnorm:
                                match_id = tid
                                break

                    if match_id and match_id in user_films:
                        current_notes = user_films[match_id].get("notes") or ""
                        # Only update if empty or generic placeholder
                        is_generic = current_notes.startswith("Hall of Fame") or not current_notes.strip()
                        if is_generic:
                            cursor.execute(
                                "UPDATE user_films SET personal_notes = ? WHERE tmdb_id = ?",
                                (comment.strip(), match_id)
                            )
                            user_films[match_id]["notes"] = comment.strip()
                            updated_notes_count += 1

            conn.commit()

        print(f"✅ Merged {updated_notes_count} rich personal comments into FilmLog personal notes!")


if __name__ == "__main__":
    sync_library()
