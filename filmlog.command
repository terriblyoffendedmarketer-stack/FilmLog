#!/bin/bash
cd "$(dirname "$0")"

PORT=5001

# Check if already running
if lsof -i :$PORT -sTCP:LISTEN >/dev/null 2>&1; then
    echo "filmlog is already running on port $PORT"
    echo "Opening in browser..."
    open "http://localhost:$PORT"
    sleep 1
    exit 0
fi

echo "Starting filmlog..."
echo ""

# Activate venv if it exists
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
elif [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

# Start Flask in the background
flask --app app run --port $PORT &
FLASK_PID=$!

# Wait for server to start
echo "Waiting for server..."
for i in $(seq 1 20); do
    if curl -s "http://localhost:$PORT" >/dev/null 2>&1; then
        echo ""
        echo "✓ filmlog running at http://localhost:$PORT"
        open "http://localhost:$PORT"
        echo ""
        echo "Press Ctrl+C or close this window to stop the server."
        wait $FLASK_PID
        exit 0
    fi
    sleep 0.5
done

echo "✗ Server failed to start. Check the logs above."
echo ""
echo "Press any key to close."
read -n 1
