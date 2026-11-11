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
#   options: --overwrite (replace existing files), --backdrops 4, --only "Oldboy", --limit 5,
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
# - Existing files are skipped unless --overwrite: Jellyfin may already have saved its own
#   poster.jpg there ("Save artwork into media folders"), and we don't clobber it silently.
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


def plan_files(video_path: Path, art: dict, n_backdrops: int) -> list[tuple[Path, str]]:
    folder = video_path.parent
    videos = [p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXT] if folder.exists() else [video_path]
    shared = len(videos) > 1
    backs, posters, logo = art.get("backdrops") or [], art.get("posters") or [], art.get("logo")
    # Every file must be a different picture: the landscape/thumb is a titled still if
    # there is one, else a still NOT used as a backdrop, else omitted (Jellyfin then
    # falls back to the backdrop on its own).
    titled = next((b for b in backs if b.get("iso_639_1")), None)
    clean = [b for b in backs if b is not titled]
    limit = 1 if shared else n_backdrops
    landscape = titled or (clean[limit] if len(clean) > limit else None)
    backs = clean

    files: list[tuple[Path, str]] = []
    if shared:
        stem = video_path.stem
        if posters:
            files.append((folder / f"{stem}-poster.jpg", images.url(posters[0]["file_path"], "w780")))
        if backs:
            files.append((folder / f"{stem}-fanart.jpg", images.url(backs[0]["file_path"], "w1280")))
        if landscape:
            files.append((folder / f"{stem}-landscape.jpg", images.url(landscape["file_path"], "w1280")))
        if logo:
            files.append((folder / f"{stem}-logo.png", images.url(logo["file_path"], "w500")))
        return files

    if posters:
        files.append((folder / "poster.jpg", images.url(posters[0]["file_path"], "w780")))
    for i, b in enumerate(backs[:n_backdrops]):
        files.append((folder / ("backdrop.jpg" if i == 0 else f"backdrop{i}.jpg"),
                      images.url(b["file_path"], "w1280")))
    if landscape:
        files.append((folder / "landscape.jpg", images.url(landscape["file_path"], "w1280")))
    if logo:
        files.append((folder / "logo.png", images.url(logo["file_path"], "w500")))
    return files


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="actually download (default: dry run)")
    ap.add_argument("--overwrite", action="store_true")
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
            if dest.exists() and not args.overwrite:
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
                    dest.write_bytes(r.content)
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
