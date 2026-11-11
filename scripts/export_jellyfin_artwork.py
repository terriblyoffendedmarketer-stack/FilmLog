# export_jellyfin_artwork.py — Write curated TMDB artwork into each movie's folder for Jellyfin.
#
# Per movie folder it writes Jellyfin's local-artwork filenames:
#   poster.jpg                      primary poster
#   backdrop.jpg, backdrop1.jpg …   distinct, mostly textless landscape stills (de-duplicated)
#   landscape.jpg                   "Thumb" image (a titled still if TMDB has one)
#   logo.png                        clear logo
# Movies that share a folder with other videos get <videofile>-poster.jpg / -fanart.jpg /
# -landscape.jpg / -logo.png instead (Jellyfin's per-file naming).
#
# Usage (hard drive must be mounted at /Volumes/Backup Plus):
#   .venv/bin/python scripts/export_jellyfin_artwork.py                # dry run: prints the plan
#   .venv/bin/python scripts/export_jellyfin_artwork.py --write        # download into folders
#   .venv/bin/python scripts/export_jellyfin_artwork.py --write --refresh   # then ask Jellyfin to rescan
#   options: --backdrops 4 (target total backdrop*.jpg per folder), --only "Oldboy", --limit 5,
#            --library some.json (skip the Jellyfin API; used for testing with a fake tree)
#
# Requires: TMDB_API_KEY in .env; curated images in filmlog.db (scripts/curate_film_images.py)
#   for films FilmLog knows; others are curated on the fly (Pillow + numpy in the venv).
#   Jellyfin running on localhost:8096 gives exact TMDB ids; otherwise falls back to
#   ~/Documents/Claude Code/Jellyfin Mac/jellyfin-mac-setup/library.json + title/year matching.
#   For exact ids set JELLYFIN_USER / JELLYFIN_PASS (and optionally JELLYFIN_URL) in .env —
#   without them it uses library.json matching.
#
# Gotchas:
# - Jellyfin shows ONE primary poster per movie; extra poster files are ignored, so only
#   one poster is written. Multiple backdrops ARE supported (backdrop1.jpg, backdrop2.jpg …).
# - ADD-ONLY by design: never replaces, renames or deletes. Jellyfin already saves folder.jpg /
#   backdrop.jpg / landscape.jpg / logo.png into most folders; adding poster.jpg next to
#   folder.jpg could flip the poster Jellyfin shows, so no poster is written there, and new
#   backdrops are compared to the existing ones so Jellyfin's backdrop.jpg isn't duplicated.
# - Jellyfin only notices new local images on a library scan / metadata refresh (--refresh).

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from filmlog import db, images  # noqa: E402

LIBRARY_JSON = Path.home() / "Documents" / "Claude Code" / "Jellyfin Mac" / "jellyfin-mac-setup" / "library.json"
JF_URL = os.getenv("JELLYFIN_URL", "http://localhost:8096")
JF_USER = os.getenv("JELLYFIN_USER", "")
JF_PASS = os.getenv("JELLYFIN_PASS", "")  # never hard-code: this file is in a public-ish repo
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".webm"}


# ── Which movies, and where ─────────────────────────────────────────────────

def jellyfin_session():
    if not JF_USER:
        return None
    try:
        requests.get(f"{JF_URL}/System/Ping", timeout=3).raise_for_status()
        r = requests.post(
            f"{JF_URL}/Users/authenticatebyname",
            headers={"X-Emby-Authorization": 'MediaBrowser Client="FilmLog", Device="CLI", '
                                             'DeviceId="filmlog-artwork", Version="1.0"'},
            json={"Username": JF_USER, "Pw": JF_PASS}, timeout=10,
        )
        r.raise_for_status()
        a = r.json()
        return a["User"]["Id"], a["AccessToken"]
    except Exception:
        return None


def movies_from_jellyfin(uid, token) -> list[dict]:
    r = requests.get(
        f"{JF_URL}/Users/{uid}/Items",
        params={"Recursive": "true", "IncludeItemTypes": "Movie", "Fields": "ProviderIds,Path"},
        headers={"X-Emby-Token": token}, timeout=60,
    )
    r.raise_for_status()
    out = []
    for it in r.json().get("Items", []):
        tid = (it.get("ProviderIds") or {}).get("Tmdb")
        out.append({"name": it.get("Name"), "year": it.get("ProductionYear"), "path": it.get("Path"),
                    "tmdb_id": int(tid) if tid and tid.isdigit() else None})
    return out


def movies_from_library_json(path: Path = LIBRARY_JSON) -> list[dict]:
    from sync_jellyfin import clean_title
    data = json.loads(Path(path).read_text())
    with db.get_connection() as conn:
        rows = conn.execute("SELECT tmdb_id, title, year FROM films").fetchall()
    by_key = {}
    for t, title, year in rows:
        n = images._norm(title)
        by_key.setdefault((n, year), t)
        by_key.setdefault((n, None), t)
    out = []
    for it in data.get("items", []):
        if it.get("type") != "Movie":
            continue
        title, y2 = clean_title(it.get("name", ""))
        year = it.get("year") or y2
        if year and len(str(year)) > 4:
            year = int(str(year)[:4])
        n = images._norm(title)
        tid = by_key.get((n, year)) or by_key.get((n, None))
        for p in it.get("paths") or [it.get("path")]:
            out.append({"name": it.get("name"), "year": year, "path": p, "tmdb_id": tid})
    return out


# ── Artwork selection ───────────────────────────────────────────────────────

def artwork_for(tmdb_id: int, name: str) -> dict | None:
    rows = db.get_film_images(tmdb_id)
    if rows:
        pick = lambda k: [{"file_path": r["file_path"], "iso_639_1": r["lang"]} for r in rows if r["kind"] == k]
        logo = pick("logo")
        return {"backdrops": pick("backdrop"), "posters": pick("poster"), "logo": logo[0] if logo else None}
    film = db.get_film(tmdb_id)
    try:
        return images.curate_film(tmdb_id, (film or {}).get("media_type") or "movie",
                                  (film or {}).get("title") or name,
                                  (film or {}).get("poster_url"), (film or {}).get("language"))
    except Exception as e:
        print(f"    ! could not curate {name}: {e}")
        return None


POSTER_NAMES = ("folder.jpg", "poster.jpg", "cover.jpg", "folder.png", "poster.png")
LANDSCAPE_RE = re.compile(r"^(backdrop\d*|fanart\d*|background\d*|landscape|thumb)\.(jpg|jpeg|png)$", re.I)


def _local_thumb(path: Path):
    from PIL import Image, ImageFilter
    try:
        with Image.open(path) as im:
            im = im.convert("L")
            im.thumbnail((300, 300))
            return im.filter(ImageFilter.GaussianBlur(1.5))
    except Exception:
        return None


def _is_dup(thumb, others) -> bool:
    return thumb is not None and any(
        o is not None and images._similarity(thumb, o, 63, 36) >= images.DUP_THRESHOLD for o in others
    )


def plan_files(video_path: Path, art: dict, n_backdrops: int) -> list[tuple[Path, str]]:
    """
    ADD-ONLY plan. Respects what Jellyfin already saved in the folder:
      - no poster if folder.jpg/poster.jpg/cover.jpg (or <video>-poster.jpg) exists
      - new backdrops are compared against every existing landscape image in the folder
        (backdrop*.jpg, fanart*, landscape.jpg …) and skipped if they're the same picture
      - new backdrops take the next FREE number; nothing is renamed or replaced
    """
    folder = video_path.parent
    names = {p.name for p in folder.iterdir() if not p.name.startswith("._")}
    videos = [n for n in names if Path(n).suffix.lower() in VIDEO_EXT]
    shared = len(videos) > 1
    stem = video_path.stem
    backs, posters, logo = art.get("backdrops") or [], art.get("posters") or [], art.get("logo")

    if shared:
        existing_land = [folder / n for n in names
                         if n.startswith(stem + "-") and re.search(r"-(fanart|backdrop\d*|landscape)\.(jpg|png)$", n)]
        has_poster = f"{stem}-poster.jpg" in names
        has_logo = f"{stem}-logo.png" in names
        has_landscape = f"{stem}-landscape.jpg" in names
        slots = [f"{stem}-fanart.jpg"] if not any(n in names for n in (f"{stem}-fanart.jpg", f"{stem}-backdrop.jpg")) else []
    else:
        existing_land = [folder / n for n in names if LANDSCAPE_RE.match(n)]
        has_poster = any(n.lower() in POSTER_NAMES for n in names)
        has_logo = "logo.png" in names
        has_landscape = "landscape.jpg" in names
        n_existing = sum(1 for n in names if re.match(r"^backdrop\d*\.(jpg|png)$", n, re.I))
        free = (f"backdrop{i}.jpg" if i else "backdrop.jpg" for i in range(0, 100))
        free = [n for n in free if n not in names]
        slots = free[: max(0, n_backdrops - n_existing)]

    files: list[tuple[Path, str]] = []
    if posters and not has_poster:
        files.append((folder / (f"{stem}-poster.jpg" if shared else "poster.jpg"),
                      images.url(posters[0]["file_path"], "w780")))
    if logo and not has_logo:
        files.append((folder / (f"{stem}-logo.png" if shared else "logo.png"),
                      images.url(logo["file_path"], "w500")))

    need_landscape = not has_landscape
    if not slots and not need_landscape:
        return files

    # Compare candidates against what's already there and against each other.
    seen = [_local_thumb(p) for p in existing_land]
    titled = next((b for b in backs if b.get("iso_639_1")), None)
    for b in backs:
        if not slots and not need_landscape:
            break
        th = images._safe_thumb(b["file_path"], "w300")
        if th is None or _is_dup(th, seen):
            continue
        seen.append(th)
        if need_landscape and (b is titled or not slots):
            files.append((folder / (f"{stem}-landscape.jpg" if shared else "landscape.jpg"),
                          images.url(b["file_path"], "w1280")))
            need_landscape = False
        elif slots:
            files.append((folder / slots.pop(0), images.url(b["file_path"], "w1280")))
    return files


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="actually download (default: dry run)")
    ap.add_argument("--refresh", action="store_true", help="ask Jellyfin to rescan libraries afterwards")
    ap.add_argument("--backdrops", type=int, default=4)
    ap.add_argument("--only", help="substring filter on movie name")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--library", help="use this library.json instead of asking Jellyfin (testing)")
    args = ap.parse_args()

    db.init_db()
    sess = None if args.library else jellyfin_session()
    if sess:
        movies = movies_from_jellyfin(*sess)
        print(f"Jellyfin: {len(movies)} movies (exact TMDB ids)")
    else:
        movies = movies_from_library_json(args.library or LIBRARY_JSON)
        print(f"Jellyfin offline — library.json: {len(movies)} movie paths (title/year matched)")

    if args.only:
        movies = [m for m in movies if args.only.lower() in (m["name"] or "").lower()]
    if args.limit:
        movies = movies[: args.limit]

    wrote = skipped = missing = unmatched = 0
    for m in movies:
        vp = Path(m["path"] or "")
        if not m["tmdb_id"]:
            unmatched += 1
            print(f"  ? no TMDB id: {m['name']}")
            continue
        if not vp.parent.exists():
            missing += 1
            continue
        art = artwork_for(m["tmdb_id"], m["name"])
        if not art:
            continue
        print(f"  {m['name']} ({m['year']}) → {vp.parent}")
        for dest, src in plan_files(vp, art, args.backdrops):
            if dest.exists():  # never replace anything on the drive
                skipped += 1
                print(f"      = {dest.name} (exists)")
                continue
            print(f"      + {dest.name}")
            if not args.write:
                wrote += 1
            else:
                try:
                    r = requests.get(src, timeout=30)
                    r.raise_for_status()
                    with open(dest, "xb") as fh:  # 'x' = create only, fails if the file exists
                        fh.write(r.content)
                    wrote += 1
                except Exception as e:
                    print(f"      ! {dest.name}: {e}")

    print(f"\n{'Wrote' if args.write else 'Would write'}: {wrote} files  "
          f"skipped existing: {skipped}  folder not found (drive unplugged?): {missing}  "
          f"no TMDB match: {unmatched}")
    if not args.write:
        print("Dry run — add --write to download.")
    if args.write and args.refresh and sess:
        requests.post(f"{JF_URL}/Library/Refresh", headers={"X-Emby-Token": sess[1]}, timeout=30)
        print("Asked Jellyfin to rescan libraries.")


if __name__ == "__main__":
    main()
