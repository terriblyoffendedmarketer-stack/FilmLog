# verify_drive_snapshot.py — Prove a batch job on the hard drive only ADDED files.
#
# Compares the drive now against a snapshot taken before (lines of "<size> <mtime> ./<path>", made with:
#   cd "/Volumes/Backup Plus/All Movies" && find . -type f -print0 | xargs -0 stat -f "%z %m %N" | sort -k3 > snap.txt )
# and reports: files that disappeared (must be 0), existing files whose size/date changed, and new files —
# opening every new image fully to catch half-written downloads.
#
# Usage: .venv/bin/python scripts/verify_drive_snapshot.py drive_snapshot_2026-11-11.txt
# Requires: the drive mounted at /Volumes/Backup Plus; Pillow (uv pip install pillow).
#
# Gotchas:
# - Jellyfin itself rewrites folder.jpg / backdrop.jpg / landscape.jpg / logo.png / movie.nfo when it refreshes
#   metadata, and macOS rewrites ._* and .DS_Store. Changes to those are listed separately — they are not ours
#   (the exporter opens files with 'xb', which cannot touch an existing file).
# - Paths contain spaces, so split each line into exactly 3 parts (size, mtime, rest).

import subprocess
import sys
from pathlib import Path

ROOT = Path("/Volumes/Backup Plus/All Movies")
SYSTEM_MANAGED = {"folder.jpg", "backdrop.jpg", "landscape.jpg", "logo.png", "movie.nfo", ".DS_Store", "Thumbs.db"}
IMAGE_EXT = {".jpg", ".jpeg", ".png"}


def load(lines):
    out = {}
    for line in lines:
        parts = line.rstrip("\n").split(" ", 2)
        if len(parts) == 3:
            out[parts[2]] = (parts[0], parts[1])
    return out


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__ or "usage: verify_drive_snapshot.py <snapshot.txt>")
    before = load(open(sys.argv[1], encoding="utf-8", errors="surrogateescape"))
    now_txt = subprocess.run(
        'find . -type f -print0 | xargs -0 stat -f "%z %m %N"', shell=True, cwd=ROOT,
        capture_output=True, text=True, errors="surrogateescape").stdout
    now = load(now_txt.splitlines())

    gone = sorted(set(before) - set(now))
    new = sorted(set(now) - set(before))
    changed = [p for p in before if p in now and before[p] != now[p]]
    ours_changed = [p for p in changed if Path(p).name not in SYSTEM_MANAGED and not Path(p).name.startswith("._")]
    sys_changed = [p for p in changed if p not in ours_changed]

    print(f"Before: {len(before)} files   Now: {len(now)} files")
    print(f"\nDISAPPEARED: {len(gone)}")
    for p in gone:
        print("  !", p)
    print(f"\nCHANGED (not Jellyfin/macOS-managed): {len(ours_changed)}")
    for p in ours_changed:
        print("  !", p, before[p], "->", now[p])
    print(f"CHANGED by Jellyfin/macOS (folder.jpg, movie.nfo, ._*, .DS_Store …): {len(sys_changed)}")

    from PIL import Image
    kinds, broken = {}, []
    for p in new:
        name = Path(p).name
        if name.startswith("._"):
            continue
        key = name if "-" not in name else "<video>-" + name.rsplit("-", 1)[-1]
        kinds[key] = kinds.get(key, 0) + 1
        if Path(p).suffix.lower() in IMAGE_EXT:
            try:
                with Image.open(ROOT / p) as im:
                    im.load()
            except Exception as e:
                broken.append((p, str(e)[:80]))
    print(f"\nNEW files: {sum(kinds.values())} (plus macOS ._ companions)")
    for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])[:25]:
        print(f"  {v:5d}  {k}")
    print(f"\nNEW images that fail to open (half-written?): {len(broken)}")
    for p, e in broken:
        print("  !", p, e)


if __name__ == "__main__":
    main()
