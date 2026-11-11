"""
images.py — Curated TMDB artwork: textless landscape stills, distinct alt posters,
and near-duplicate removal.

What it does
  curate_film(tmdb_id, media_type, title, poster_url) asks TMDB for every backdrop,
  poster and logo of a film, ranks them, and drops near-duplicates by comparing small
  thumbnails. It returns ordered, visually distinct lists that the app (issue covers,
  hero breaks, alt posters) and the Jellyfin exporter both use.

Usage
  Offline only — run via scripts/curate_film_images.py (needs Pillow + numpy, which are
  NOT in requirements.txt so the Vercel bundle stays small). The app only reads the
  results from the `film_images` table and the films.* columns.

Gotchas
  - TMDB often holds the SAME still twice: once clean, once with the title burned in,
    sometimes zoomed/shifted (Black Friday, A Hard Day, Confessions). Different URLs,
    same picture. A plain dHash misses the zoomed ones (distance 30/64 ≈ "different").
    What works: 63x36 blurred grayscale, tile-median NCC (survives a text overlay
    covering a few tiles), searched over shifts and zooms 1.0-1.79x. On the test set
    dups score >= 0.72 (key art re-cropped 1.5x+ needs the wide zoom range), different
    stills <= 0.54, so DUP_THRESHOLD = 0.70. A narrower zoom misses The Thing's crops.
  - media_type 'anime' is OUR tag, not a TMDB namespace: /anime/{id}/images 404s
    silently. Anime can be a TMDB movie OR tv show, and ids collide across the two
    namespaces, so resolve by matching the returned title.
  - Backdrops with iso_639_1 == None are the textless ones; prefer them for covers
    (the UI overlays its own caption — burned-in titles like "HOTEL BOMBAY" clash).
  - Without include_image_language TMDB filters by the default language and you lose
    most images; pass "en,null,<original language>".
"""

from __future__ import annotations

import io
import re
from concurrent.futures import ThreadPoolExecutor

import requests

from filmlog import tmdb

IMG = "https://image.tmdb.org/t/p/"
DUP_THRESHOLD = 0.70
MAX_BACKDROPS = 6   # distinct backdrops kept per film
MAX_POSTERS = 4     # distinct posters kept per film (index 0 = the main poster)
CANDIDATES = 14     # how many ranked candidates we bother downloading per kind
# Zoom levels searched when comparing two images (same still re-cropped/zoomed by uploaders)
ZOOMS = tuple(round(1.06 ** i, 3) for i in range(11))   # 1.0 … 1.79


def url(file_path: str | None, size: str) -> str | None:
    return f"{IMG}{size}{file_path}" if file_path else None


def file_path_of(tmdb_url: str | None) -> str | None:
    """'https://image.tmdb.org/t/p/w500/abc.jpg' -> '/abc.jpg'."""
    if not tmdb_url:
        return None
    m = re.search(r"/t/p/[^/]+(/.+)$", tmdb_url)
    return m.group(1) if m else None


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ---------------------------------------------------------------------------
# TMDB fetch (with anime movie/tv resolution)
# ---------------------------------------------------------------------------

def _fetch(kind: str, tmdb_id: int, langs: str) -> dict | None:
    try:
        return tmdb._get(f"/{kind}/{tmdb_id}", {
            "append_to_response": "images",
            "include_image_language": langs,
        })
    except Exception:
        return None


def fetch_images(tmdb_id: int, media_type: str, title: str, orig_lang: str | None) -> tuple[str, dict]:
    """Return (resolved_kind, images_dict). resolved_kind is 'movie' or 'tv'."""
    langs = ",".join(dict.fromkeys(["en", "null", (orig_lang or "en").lower()]))
    if media_type == "tv":
        order = ["tv"]
    elif media_type == "anime":
        order = ["movie", "tv"]
    else:
        order = ["movie"]

    fallback = None
    for kind in order:
        data = _fetch(kind, tmdb_id, langs)
        if not data:
            continue
        names = {_norm(data.get(k)) for k in ("title", "original_title", "name", "original_name")}
        if _norm(title) in names or len(order) == 1:
            return kind, data.get("images") or {}
        fallback = fallback or (kind, data.get("images") or {})
    return fallback or (order[0], {})


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def rank_backdrops(entries: list[dict]) -> list[dict]:
    """Textless first, then by TMDB's own vote ordering; prefer >= 1280px wide."""
    def key(e):
        textless = e.get("iso_639_1") in (None, "", "xx")
        wide_enough = (e.get("width") or 0) >= 1280
        return (not textless, not wide_enough, -(e.get("vote_average") or 0), -(e.get("vote_count") or 0))
    return sorted(entries, key=key)


def rank_posters(entries: list[dict], orig_lang: str | None) -> list[dict]:
    """English first, then original language, then textless; by votes."""
    def key(e):
        lang = e.get("iso_639_1")
        tier = 0 if lang == "en" else 1 if (orig_lang and lang == orig_lang) else 2
        return (tier, -(e.get("vote_average") or 0), -(e.get("vote_count") or 0))
    return sorted(entries, key=key)


def pick_logo(entries: list[dict], orig_lang: str | None) -> dict | None:
    pngs = [e for e in entries if (e.get("file_path") or "").endswith(".png")] or entries
    ranked = rank_posters(pngs, orig_lang)
    return ranked[0] if ranked else None


# ---------------------------------------------------------------------------
# Near-duplicate detection (lazy numpy/Pillow imports — offline only)
# ---------------------------------------------------------------------------

def _thumb(file_path: str, size: str):
    from PIL import Image, ImageFilter
    r = requests.get(url(file_path, size), timeout=15)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("L").filter(ImageFilter.GaussianBlur(1.5))


def _similarity(ia, ib, W: int, H: int) -> float:
    """Max tile-median NCC between two images over shifts and zoom levels (0..1)."""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view as swv
    from PIL import Image

    P = 3

    def arr(im, w, h):
        return np.asarray(im.resize((w, h), Image.BILINEAR), dtype=np.float32)

    def tiles(x):
        s = x.shape[:-2]
        th, tw = H // 3, W // 3
        t = x.reshape(*s, 3, th, 3, tw).swapaxes(-3, -2).reshape(*s, 9, th * tw)
        t = t - t.mean(-1, keepdims=True)
        return t / np.maximum(np.linalg.norm(t, axis=-1, keepdims=True), 1e-3)

    ta, tb = tiles(arr(ia, W, H)), tiles(arr(ib, W, H))
    best = -1.0
    for s in ZOOMS:
        for big, small_t in ((ia, tb), (ib, ta)):
            A = np.pad(arr(big, round(W * s), round(H * s)), P, mode="edge")
            score = np.median((tiles(swv(A, (H, W))) * small_t).sum(-1), axis=-1)
            best = max(best, float(score.max()))
            if s == 1.0:
                break
    return best


def dedupe(entries: list[dict], limit: int, kind: str, seed: list[dict] | None = None) -> list[dict]:
    """
    Greedy: walk ranked candidates, keep one only if it isn't a near-duplicate of any
    kept image. `seed` entries are kept first unconditionally (e.g. the current poster).
    Falls back to plain ranking if Pillow/numpy are unavailable.
    """
    size, W, H = ("w300", 63, 36) if kind == "backdrop" else ("w185", 30, 45)
    pool = (seed or []) + [e for e in entries[:CANDIDATES]
                           if e.get("file_path") not in {s.get("file_path") for s in (seed or [])}]
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return pool[:limit]

    with ThreadPoolExecutor(max_workers=8) as ex:
        thumbs = list(ex.map(lambda e: _safe_thumb(e["file_path"], size), pool))

    kept, kept_thumbs = [], []
    for e, th in zip(pool, thumbs):
        if th is None:
            continue
        if any(_similarity(th, k, W, H) >= DUP_THRESHOLD for k in kept_thumbs):
            continue
        kept.append(e)
        kept_thumbs.append(th)
        if len(kept) >= limit:
            break
    return kept


def _safe_thumb(file_path, size):
    try:
        return _thumb(file_path, size)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def curate_film(tmdb_id: int, media_type: str, title: str, poster_url: str | None,
                orig_lang: str | None) -> dict:
    """
    Returns {
      'kind': 'movie'|'tv',
      'backdrops': [entry, ...]   distinct, best first (textless preferred)
      'posters':   [entry, ...]   distinct, index 0 = current main poster if present
      'logo':      entry | None
    }
    Each entry is a TMDB image dict (file_path, iso_639_1, width, height, vote_*).
    """
    kind, imgs = fetch_images(tmdb_id, media_type, title, orig_lang)
    backdrops = rank_backdrops(imgs.get("backdrops") or [])
    posters = rank_posters(imgs.get("posters") or [], orig_lang)

    main_fp = file_path_of(poster_url)
    seed = []
    if main_fp:
        seed = [next((p for p in posters if p.get("file_path") == main_fp),
                     {"file_path": main_fp, "iso_639_1": None})]

    return {
        "kind": kind,
        "backdrops": dedupe(backdrops, MAX_BACKDROPS, "backdrop"),
        "posters": dedupe(posters, MAX_POSTERS, "poster", seed=seed),
        "logo": pick_logo(imgs.get("logos") or [], orig_lang),
    }
