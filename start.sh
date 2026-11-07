#!/bin/bash
set -e

DATA_DIR="${FILMLOG_DATA_DIR:-/data}"

if [ -d "$DATA_DIR" ]; then
    if [ ! -f "$DATA_DIR/filmlog.db" ] && [ -f "/app/filmlog.db" ]; then
        echo "Seeding initial database to $DATA_DIR..."
        cp /app/filmlog.db "$DATA_DIR/filmlog.db"
    fi
    if [ ! -f "$DATA_DIR/taste_log.md" ] && [ -f "/app/taste_log.md" ]; then
        echo "Copying taste_log.md to $DATA_DIR..."
        cp /app/taste_log.md "$DATA_DIR/taste_log.md"
    fi
    if [ ! -f "$DATA_DIR/taste_model.md" ] && [ -f "/app/taste_model.md" ]; then
        echo "Copying taste_model.md to $DATA_DIR..."
        cp /app/taste_model.md "$DATA_DIR/taste_model.md"
    fi
fi

exec gunicorn -w 2 -b 0.0.0.0:${PORT:-8080} 'app:app'
