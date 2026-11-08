"""
tmdb.py — TMDB API client.

Handles all communication with The Movie Database API:
  - search_movies()     → find candidates by title (+ optional year)
  - get_movie_details() → full metadata for a known tmdb_id
  - build_poster_url()  → construct the full image URL from a poster_path
  - fetch_film_data()   → convenience wrapper: details dict ready for db.upsert_film()

API key must be set in .env as TMDB_API_KEY.
"""

import os
import re
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
TMDB_BACKDROP_BASE = "https://image.tmdb.org/t/p/w1280"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _api_key() -> str:
    key = os.getenv("TMDB_API_KEY")
    if not key:
        raise RuntimeError(
            "TMDB_API_KEY is not set.\n"
            "Add it to your .env file: TMDB_API_KEY=your_key_here\n"
            "Get a free key at https://www.themoviedb.org/settings/api"
        )
    return key


def _get(path: str, params: dict | None = None) -> dict:
    """Make a GET request to the TMDB API. Raises on HTTP errors."""
    url = f"{TMDB_BASE}{path}"
    params = params or {}
    params["api_key"] = _api_key()

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Could not reach TMDB — check your internet connection.")
    except requests.exceptions.Timeout:
        raise RuntimeError("TMDB request timed out — try again.")
    except requests.exceptions.HTTPError as e:
        if response.status_code == 401:
            raise RuntimeError("TMDB API key is invalid or expired.")
        if response.status_code == 429:
            raise RuntimeError("TMDB rate limit hit — wait a moment and try again.")
        raise RuntimeError(f"TMDB returned an error: {e}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_poster_url(poster_path: str | None) -> str | None:
    """Return the full poster image URL, or None if no poster_path."""
    if not poster_path:
        return None
    return f"{TMDB_IMAGE_BASE}{poster_path}"


def build_backdrop_url(backdrop_path: str | None) -> str | None:
    """Return the full landscape backdrop image URL, or None if no backdrop_path."""
    if not backdrop_path:
        return None
    return f"{TMDB_BACKDROP_BASE}{backdrop_path}"


def fetch_film_backdrops(tmdb_id: int, limit: int = 5, media_type: str = "movie") -> list[str]:
    """
    Fetch genuine widescreen landscape backdrop URLs from TMDB /images endpoint.
    Filters out missing paths and returns high-resolution w1280 URLs.
    """
    endpoint = f"/{media_type}/{tmdb_id}/images"
    try:
        data = _get(endpoint)
        backdrops = data.get("backdrops", [])
        urls = []
        for b in backdrops:
            fp = b.get("file_path")
            if fp:
                url = build_backdrop_url(fp)
                if url and url not in urls:
                    urls.append(url)
            if len(urls) >= limit:
                break
        return urls
    except Exception:
        return []


def search_movies(query: str, year: int | None = None) -> list[dict]:
    """
    Search TMDB for movies by title. Returns a list of candidate dicts.
    """
    params = {"query": query, "include_adult": "false"}
    if year:
        params["year"] = year

    data = _get("/search/movie", params)
    results = data.get("results", [])

    candidates = []
    for r in results:
        release_year = None
        if r.get("release_date"):
            try:
                release_year = int(r["release_date"][:4])
            except ValueError:
                pass

        candidates.append(
            {
                "tmdb_id": r["id"],
                "title": r.get("title", "Unknown"),
                "year": release_year,
                "language": r.get("original_language"),
                "overview": r.get("overview", ""),
                "poster_url": build_poster_url(r.get("poster_path")),
                "media_type": "movie",
            }
        )

    return candidates


def search_tv(query: str, year: int | None = None) -> list[dict]:
    """
    Search TMDB for TV series by title. Returns a list of candidate dicts.
    """
    params = {"query": query, "include_adult": "false"}
    if year:
        params["first_air_date_year"] = year

    data = _get("/search/tv", params)
    results = data.get("results", [])

    candidates = []
    for r in results:
        first_year = None
        if r.get("first_air_date"):
            try:
                first_year = int(r["first_air_date"][:4])
            except ValueError:
                pass

        candidates.append(
            {
                "tmdb_id": r["id"],
                "title": r.get("name", "Unknown"),
                "year": first_year,
                "language": r.get("original_language"),
                "overview": r.get("overview", ""),
                "poster_url": build_poster_url(r.get("poster_path")),
                "media_type": "tv",
            }
        )

    return candidates


def search_multi(query: str, year: int | None = None, prefer_tv: bool = False) -> list[dict]:
    """
    Search both movies and TV series. If prefer_tv is True, TV results rank first.
    """
    movies = search_movies(query, year)
    tv = search_tv(query, year)

    if prefer_tv:
        return tv + movies
    return movies + tv


def get_movie_details(tmdb_id: int) -> dict:
    """
    Fetch full metadata for a film by tmdb_id. Returns a dict with:
        tmdb_id, title, year, runtime, language, country,
        poster_url, tmdb_genres, overview, tagline

    Raises RuntimeError if the film is not found.
    """
    data = _get(f"/movie/{tmdb_id}")

    release_year = None
    if data.get("release_date"):
        try:
            release_year = int(data["release_date"][:4])
        except ValueError:
            pass

    # First production country, if present
    countries = data.get("production_countries", [])
    country = countries[0]["iso_3166_1"] if countries else None

    genres = [g["name"] for g in data.get("genres", [])]

    return {
        "tmdb_id": data["id"],
        "title": data.get("title", "Unknown"),
        "year": release_year,
        "runtime": data.get("runtime"),
        "language": data.get("original_language"),
        "country": country,
        "poster_url": build_poster_url(data.get("poster_path")),
        "backdrop_url": build_backdrop_url(data.get("backdrop_path")),
        "tmdb_genres": genres,
        "overview": data.get("overview", ""),
        "tagline": data.get("tagline", ""),
    }


def get_tv_details(tmdb_id: int) -> dict:
    """
    Fetch full metadata for a TV series by tmdb_id.
    """
    data = _get(f"/tv/{tmdb_id}")

    first_year = None
    if data.get("first_air_date"):
        try:
            first_year = int(data["first_air_date"][:4])
        except ValueError:
            pass

    countries = data.get("origin_country", [])
    country = countries[0] if countries else None

    genres = [g["name"] for g in data.get("genres", [])]

    episode_runtime = data.get("episode_run_time", [])
    runtime = episode_runtime[0] if episode_runtime else None

    return {
        "tmdb_id": data["id"],
        "title": data.get("name", "Unknown"),
        "year": first_year,
        "runtime": runtime,
        "language": data.get("original_language"),
        "country": country,
        "poster_url": build_poster_url(data.get("poster_path")),
        "backdrop_url": build_backdrop_url(data.get("backdrop_path")),
        "tmdb_genres": genres,
        "overview": data.get("overview", ""),
        "tagline": data.get("tagline", ""),
        "media_type": "tv",
    }


def fetch_tv_data(tmdb_id: int) -> dict:
    """
    Convenience wrapper for TV series: returns a dict ready for db.upsert_film().
    """
    details = get_tv_details(tmdb_id)
    return {
        "tmdb_id": details["tmdb_id"],
        "title": details["title"],
        "year": details["year"],
        "runtime": details["runtime"],
        "language": details["language"],
        "country": details["country"],
        "poster_url": details["poster_url"],
        "backdrop_url": details.get("backdrop_url"),
        "tmdb_genres": details["tmdb_genres"],
        "media_type": "tv",
        "overview": details.get("overview") or None,
    }


def fetch_film_data(tmdb_id: int) -> dict:
    """
    Convenience wrapper: returns a dict ready to pass directly to db.upsert_film().
    """
    details = get_movie_details(tmdb_id)
    return {
        "tmdb_id": details["tmdb_id"],
        "title": details["title"],
        "year": details["year"],
        "runtime": details["runtime"],
        "language": details["language"],
        "country": details["country"],
        "poster_url": details["poster_url"],
        "backdrop_url": details.get("backdrop_url"),
        "tmdb_genres": details["tmdb_genres"],
        "media_type": "movie",
        "overview": details.get("overview") or None,
    }


def search_and_pick(query: str, year: int | None = None) -> dict | None:
    """
    Search for a film and return the best single match.

    Rules:
    - If exactly one result: return it directly (no prompt needed).
    - If multiple: return the list so the caller can present choices.
      This function returns a dict with key 'candidates' in that case.
    - If none: return None.

    The caller is responsible for presenting multiple-match UI.
    """
    candidates = search_movies(query, year)

    if not candidates:
        return None

    if len(candidates) == 1:
        # One result — fetch full details and return
        return fetch_film_data(candidates[0]["tmdb_id"])

    # Multiple results — caller must pick; return the candidate list
    return {"candidates": candidates}


def search_smart(
    query: str,
    year: int | None = None,
    media_type: str = "movie"
) -> list[dict]:
    """
    Intelligent multi-entry TMDB search supporting:
      1. TMDB URL / ID:
         - e.g. https://www.themoviedb.org/movie/161040-david
         - e.g. https://www.themoviedb.org/tv/12345
         - e.g. tmdb:161040 or raw ID
      2. Title + Year combined:
         - e.g. "David 2013", "David (2012)"
      3. Explicit Year with Fuzzy Window (+-1 and +-2 years):
         - e.g. searching "David" with year 2012 surfaces 2013 films with high priority.
      4. Exact title boosting so exact matches rank above partial substring clutter.
    """
    q = (query or "").strip()
    if not q:
        return []

    # 1. Check for TMDB URL
    url_m = re.search(r"themoviedb\.org/(movie|tv)/(\d+)", q)
    if url_m:
        mtype = url_m.group(1)
        tmdb_id = int(url_m.group(2))
        try:
            if mtype == "tv":
                d = get_tv_details(tmdb_id)
            else:
                d = get_movie_details(tmdb_id)
            return [d]
        except Exception:
            return []

    # 2. Check for raw TMDB ID (e.g. tmdb:161040 or 161040)
    id_m = re.match(r"^(?:tmdb:)?(\d{4,9})$", q)
    if id_m:
        tmdb_id = int(id_m.group(1))
        try:
            if media_type == "tv":
                return [get_tv_details(tmdb_id)]
            return [get_movie_details(tmdb_id)]
        except Exception:
            try:
                return [get_tv_details(tmdb_id)]
            except Exception:
                pass

    # 3. Extract year from title if not given
    target_year = year
    clean_q = q
    if not target_year:
        ym = re.search(r"^(.*?)\s*[\(\[,]?\s*(19\d{2}|20\d{2})\s*[\)\]]?$", q)
        if ym and ym.group(1).strip():
            clean_q = ym.group(1).strip()
            try:
                target_year = int(ym.group(2))
            except ValueError:
                pass

    candidates = {}

    # 4. Fetch candidates with fuzzy year window
    if target_year:
        # Search target year, and adjacent +-1 year
        year_list = [target_year, target_year + 1, target_year - 1]
        for yr in year_list:
            if media_type == "tv":
                for r in search_tv(clean_q, year=yr):
                    candidates[r["tmdb_id"]] = r
            else:
                for r in search_movies(clean_q, year=yr):
                    candidates[r["tmdb_id"]] = r

        # If sparse (< 4 candidates), also pull general query
        if len(candidates) < 4:
            general_res = search_tv(clean_q) if media_type == "tv" else search_movies(clean_q)
            for r in general_res:
                if r["tmdb_id"] not in candidates:
                    candidates[r["tmdb_id"]] = r
    else:
        # No year specified
        if media_type == "tv":
            results = search_tv(clean_q)
        else:
            results = search_multi(clean_q)
        for r in results:
            candidates[r["tmdb_id"]] = r

    # 5. Score and Rank candidates
    scored = []
    clean_lower = clean_q.lower()
    for r in candidates.values():
        score = 0
        title_lower = (r.get("title") or "").strip().lower()

        # Exact title match
        if title_lower == clean_lower:
            score += 45
        elif title_lower.startswith(clean_lower):
            score += 20
        elif clean_lower in title_lower:
            score += 10

        # Year match / proximity
        yr = r.get("year")
        if target_year and yr:
            diff = abs(yr - target_year)
            if diff == 0:
                score += 35
            elif diff == 1:
                score += 24  # Fuzzy +-1 year
            elif diff == 2:
                score += 14  # Fuzzy +-2 years
            else:
                score -= min(15, diff * 2)

        if r.get("poster_url"):
            score += 5
        if r.get("overview"):
            score += 2

        scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in scored[:18]]
