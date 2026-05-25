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
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

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
