#!/bin/bash
cd "$(dirname "$0")"

echo "📂 Checking for changes..."
echo ""

# Check if there are any changes
if git diff --quiet HEAD && [ -z "$(git ls-files --others --exclude-standard)" ]; then
    echo "✓ No changes found. Everything is already up to date."
    echo ""
    echo "Press any key to close."
    read -n 1
    exit 0
fi

# Show what changed
echo "Changes found:"
echo ""

# Modified files
MODIFIED=$(git diff --name-only 2>/dev/null)
if [ -n "$MODIFIED" ]; then
    echo "  Modified:"
    echo "$MODIFIED" | while read f; do echo "    ~ $f"; done
fi

# New files
NEW=$(git ls-files --others --exclude-standard 2>/dev/null)
if [ -n "$NEW" ]; then
    echo "  New:"
    echo "$NEW" | while read f; do echo "    + $f"; done
fi

# Deleted files
DELETED=$(git diff --name-only --diff-filter=D 2>/dev/null)
if [ -n "$DELETED" ]; then
    echo "  Deleted:"
    echo "$DELETED" | while read f; do echo "    - $f"; done
fi

echo ""
echo "Pushing to GitHub..."
git add -A
git commit -m "Update filmlog data and code"
git push origin main

if [ $? -eq 0 ]; then
    echo ""
    echo "✓ Pushed successfully!"
else
    echo ""
    echo "✗ Push failed. Check your internet connection."
fi

echo ""
echo "Press any key to close."
read -n 1
