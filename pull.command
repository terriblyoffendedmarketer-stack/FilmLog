#!/bin/bash
cd "$(dirname "$0")"

echo "📥 Pulling latest from GitHub..."
echo ""

OUTPUT=$(git pull origin main 2>&1)
echo "$OUTPUT"

if echo "$OUTPUT" | grep -q "Already up to date"; then
    echo ""
    echo "✓ No new changes. You're up to date."
elif [ $? -eq 0 ]; then
    echo ""
    echo "✓ Updated successfully! Changes pulled from GitHub."
else
    echo ""
    echo "✗ Pull failed. Check your internet connection."
fi

echo ""
echo "Press any key to close."
read -n 1
