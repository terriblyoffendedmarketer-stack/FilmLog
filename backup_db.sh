#!/bin/bash
# Daily backup: commit and push filmlog.db to GitHub.
# Runs as a PythonAnywhere scheduled task.

cd ~/FilmLog || exit 1

# Only push if the DB actually changed
if git diff --quiet filmlog.db 2>/dev/null; then
    echo "$(date): No DB changes, skipping."
    exit 0
fi

git add filmlog.db
git commit -m "Auto-backup filmlog.db $(date +%Y-%m-%d)"
git push

echo "$(date): DB backed up to GitHub."
