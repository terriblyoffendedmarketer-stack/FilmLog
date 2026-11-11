# curate_film_images.py — Backfill curated, de-duplicated TMDB artwork for every film.
#
# For each film: fetch all TMDB backdrops/posters/logos, prefer textless landscape
# stills, drop near-duplicates, and store the result in `film_images` plus
# films.backdrop_url / secondary_backdrop_url / alt_poster_url. Films referenced by
# Tonight issues go first so past issues are fixed even if you stop it early.
#
# Usage:
#   .venv/bin/python scripts/curate_film_images.py            # only films not yet curated
#   .venv/bin/python scripts/curate_film_images.py --all      # redo everything
#   .venv/bin/python scripts/curate_film_images.py --ids 1091 10494
#
# Requires: TMDB_API_KEY in .env; Pillow + numpy in the venv
#   (uv pip install pillow numpy — intentionally NOT in requirements.txt, Vercel never runs this)
#
# Gotchas:
# - ~6-10 s per film (downloads ~28 thumbnails + similarity search). 6 workers ≈ 30 min for 1000.
# - SQLite writes are serialized through a lock; parallel writers hit "database is locked".
# - After running, commit filmlog.db — Vercel serves the committed DB, so un-committed
#   curation never reaches the live site.

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from filmlog import db, images  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="re-curate films already done")
    ap.add_argument("--ids", nargs="*", type=int, help="only these tmdb_ids")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    db.init_db()
    with db.get_connection() as conn:
        films = [dict(r) for r in conn.execute(
            "SELECT tmdb_id, title, media_type, poster_url, language, images_curated_at FROM films"
        ).fetchall()]
        issue_ids = []
        for (fr,) in conn.execute("SELECT films_referenced FROM recommendation_issues ORDER BY id DESC"):
            issue_ids += json.loads(fr or "[]")

    if args.ids:
        films = [f for f in films if f["tmdb_id"] in set(args.ids)]
    elif not args.all:
        films = [f for f in films if not f["images_curated_at"]]
    prio = {t: i for i, t in enumerate(dict.fromkeys(issue_ids))}
    films.sort(key=lambda f: prio.get(f["tmdb_id"], 10**6))

    print(f"Curating {len(films)} films with {args.workers} workers…", flush=True)
    lock = threading.Lock()
    done = fail = 0
    t0 = time.time()

    def work(f):
        c = images.curate_film(f["tmdb_id"], f["media_type"] or "movie", f["title"],
                               f["poster_url"], f["language"])
        with lock:
            db.save_curated_images(f["tmdb_id"], c)
        return f, c

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, f) for f in films]
        for fut in as_completed(futs):
            try:
                f, c = fut.result()
                done += 1
                if done % 25 == 0 or done == len(films):
                    rate = done / (time.time() - t0)
                    print(f"  {done}/{len(films)}  ({rate:.2f}/s)  last: {f['title']} "
                          f"[{len(c['backdrops'])} stills, {len(c['posters'])} posters]", flush=True)
            except Exception as e:
                fail += 1
                print(f"  ! failed: {e}", flush=True)

    print(f"Done: {done} curated, {fail} failed in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
