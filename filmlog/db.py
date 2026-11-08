"""
db.py — database connection, schema setup, and all read/write operations.

All other modules import from here. Nothing else touches sqlite3 directly.
"""

import sqlite3
import json
import os
from pathlib import Path
from datetime import datetime, timezone

# Database lives at the repo root, next to taste_log.md and taste_model.md
# Can be overridden via FILMLOG_DATA_DIR (e.g. /data on Fly.io) or VERCEL environment
DATA_DIR = Path(os.getenv("FILMLOG_DATA_DIR", Path(__file__).parent.parent))

if os.getenv("VERCEL"):
    import shutil
    TMP_DB_PATH = Path("/tmp/filmlog.db")
    if not TMP_DB_PATH.exists() and (DATA_DIR / "filmlog.db").exists():
        shutil.copy2(DATA_DIR / "filmlog.db", TMP_DB_PATH)
    DB_PATH = TMP_DB_PATH
else:
    DB_PATH = DATA_DIR / "filmlog.db"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection() -> sqlite3.Connection:
    """Return a sqlite3 connection with row_factory set for dict-like access."""
    target_path = DB_PATH
    if os.getenv("VERCEL"):
        import shutil
        tmp_path = Path("/tmp/filmlog.db")
        if not tmp_path.exists() and (DATA_DIR / "filmlog.db").exists():
            shutil.copy2(DATA_DIR / "filmlog.db", tmp_path)
        if tmp_path.exists():
            target_path = tmp_path

    conn = sqlite3.connect(target_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # safe for concurrent reads
    return conn


# ---------------------------------------------------------------------------
# Schema setup
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS films (
    tmdb_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    year INTEGER,
    runtime INTEGER,
    language TEXT,
    country TEXT,
    poster_url TEXT,
    backdrop_url TEXT,
    tmdb_genres TEXT,               -- JSON array, raw TMDB data, reference only
    default_taste_attributes TEXT,  -- JSON array, Claude-inferred baseline attributes
    media_type TEXT DEFAULT 'movie' -- 'movie', 'tv', 'anime'
);

CREATE TABLE IF NOT EXISTS user_films (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id INTEGER NOT NULL REFERENCES films(tmdb_id),
    status TEXT NOT NULL CHECK(status IN ('watched', 'watchlist')),
    rewatch INTEGER NOT NULL DEFAULT 0,  -- 1 = want to rewatch
    date_added TEXT,
    date_watched TEXT,
    personal_notes TEXT,
    jellyfin_notes TEXT,
    watch_context TEXT CHECK(watch_context IN ('solo', 'family', 'partner', 'friends')),
    source TEXT,                    -- 'mubi_import', 'keep_import', 'manual', etc.
    rating INTEGER,
    hall_of_fame INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS taste_attributes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attribute_name TEXT NOT NULL,
    confidence_score REAL DEFAULT 0.5 CHECK(confidence_score >= 0 AND confidence_score <= 1),
    evidence_film_ids TEXT,         -- JSON array of tmdb_ids
    cluster TEXT,                   -- 'solo_late_night', 'family', 'weekend_afternoon', etc.
    polarity TEXT NOT NULL DEFAULT 'positive' CHECK(polarity IN ('positive', 'negative'))
);

CREATE TABLE IF NOT EXISTS taste_model_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_number INTEGER NOT NULL,
    generated_at TEXT NOT NULL,
    model_summary TEXT NOT NULL,
    trigger TEXT CHECK(trigger IN ('manual', '10_films_added', 'user_edit'))
);

CREATE TABLE IF NOT EXISTS recommendation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    generated_at TEXT NOT NULL,
    taste_model_version INTEGER REFERENCES taste_model_versions(id),
    issue_text TEXT NOT NULL,
    films_referenced TEXT           -- JSON array of tmdb_ids
);

CREATE TABLE IF NOT EXISTS watchlist_unresolved (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_text TEXT NOT NULL,
    source TEXT CHECK(source IN ('handwritten', 'screenshot', 'text')),
    confidence_score REAL,
    resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS journals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    type TEXT CHECK(type IN ('musing', 'newsletter', 'note')) DEFAULT 'musing',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    linked_film_ids TEXT,    -- JSON array of tmdb_ids mentioned in the entry
    tags TEXT                -- JSON array of freeform tag strings
);

-- FTS5 virtual tables — indexes film titles, notes, taste attributes, journal entries.
-- These are regular (content-storing) FTS5 tables; we sync them manually.
-- NOTE: fts_films and fts_journals are managed by _ensure_fts_tables() below,
--       not by SCHEMA, because virtual table recreation needs DROP first.

"""


_FTS_FILMS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_films USING fts5(
    tmdb_id,
    title,
    personal_notes,
    taste_attrs,
    tokenize='porter unicode61'
);
"""

_FTS_JOURNALS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_journals USING fts5(
    journal_id,
    title,
    content,
    tags,
    tokenize='porter unicode61'
);
"""


def _ensure_fts_tables(conn) -> None:
    """
    Create FTS tables if they don't exist yet.
    If an old contentless version exists (content=''), drop and recreate it.
    """
    for tbl, ddl in [("fts_films", _FTS_FILMS_DDL), ("fts_journals", _FTS_JOURNALS_DDL)]:
        # Check if table exists
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
        ).fetchone()
        if row is not None:
            # If the existing DDL has content= (old contentless schema), drop it
            if "content=" in (row[0] or ""):
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
                conn.executescript(ddl)
        else:
            conn.executescript(ddl)


def _migrate_jellyfin_comments(conn) -> None:
    """Move Jellyfin-generated descriptions out of personal_notes into jellyfin_notes."""
    from pathlib import Path
    import json
    comments_path = Path.home() / "Documents" / "Claude Code" / "Jellyfin Mac" / "jellyfin-mac-setup" / "film_comments.json"
    if not comments_path.exists():
        return
    try:
        with open(comments_path) as f:
            data = json.load(f)
        jellyfin_texts = set()
        for cat, fdict in data.items():
            for ftitle, comm in fdict.items():
                if comm and comm.strip():
                    jellyfin_texts.add(comm.strip())

        rows = conn.execute("SELECT id, personal_notes FROM user_films WHERE personal_notes IS NOT NULL").fetchall()
        for r in rows:
            pnote = (r["personal_notes"] or "").strip()
            if pnote in jellyfin_texts:
                conn.execute(
                    "UPDATE user_films SET jellyfin_notes = ?, personal_notes = NULL WHERE id = ?",
                    (pnote, r["id"])
                )
    except Exception as e:
        print("Note: Jellyfin comments migration skipped:", e)


def _run_migrations(conn) -> None:
    """Add columns that may not exist in older databases."""
    film_cols = {r[1] for r in conn.execute("PRAGMA table_info(films)").fetchall()}
    if "media_type" not in film_cols:
        conn.execute("ALTER TABLE films ADD COLUMN media_type TEXT DEFAULT 'movie'")
    if "cognitive_load" not in film_cols and "engagement" not in film_cols:
        conn.execute("ALTER TABLE films ADD COLUMN engagement TEXT")
    elif "cognitive_load" in film_cols and "engagement" not in film_cols:
        conn.execute("ALTER TABLE films RENAME COLUMN cognitive_load TO engagement")

    if "overview" not in film_cols:
        conn.execute("ALTER TABLE films ADD COLUMN overview TEXT")

    if "backdrop_url" not in film_cols:
        conn.execute("ALTER TABLE films ADD COLUMN backdrop_url TEXT")

    uf_cols = {r[1] for r in conn.execute("PRAGMA table_info(user_films)").fetchall()}
    if "rewatch" not in uf_cols:
        conn.execute("ALTER TABLE user_films ADD COLUMN rewatch INTEGER NOT NULL DEFAULT 0")
        conn.execute("""
            UPDATE user_films SET rewatch = 1, status = 'watched'
            WHERE status = 'rewatch_candidate'
        """)
    if "rating" not in uf_cols:
        conn.execute("ALTER TABLE user_films ADD COLUMN rating INTEGER")  # 1-5 stars, NULL = unrated
        # Auto-rate ALL existing watched films as 5 stars — the watched list IS the Hall of Fame
        # (Prashil's watched list only contains films he loved, not everything he's seen)
        conn.execute("""
            UPDATE user_films SET rating = 5
            WHERE status = 'watched' AND rating IS NULL
        """)

    f_cols = {r[1] for r in conn.execute("PRAGMA table_info(films)").fetchall()}
    if "secondary_backdrop_url" not in f_cols:
        conn.execute("ALTER TABLE films ADD COLUMN secondary_backdrop_url TEXT")

    ri_cols = {r[1] for r in conn.execute("PRAGMA table_info(recommendation_issues)").fetchall()}
    if "archived" not in ri_cols:
        conn.execute("ALTER TABLE recommendation_issues ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")

    # Unarchive all existing recommendation issues so past editorial issues are never hidden
    conn.execute("UPDATE recommendation_issues SET archived = 0")

    if "updated_at" not in uf_cols:
        conn.execute("ALTER TABLE user_films ADD COLUMN updated_at TEXT")
        conn.execute("UPDATE user_films SET updated_at = date_added WHERE updated_at IS NULL")

    if "hall_of_fame" not in uf_cols:
        conn.execute("ALTER TABLE user_films ADD COLUMN hall_of_fame INTEGER NOT NULL DEFAULT 0")
        # Initialize existing watched 5-star / loved films as Hall of Fame
        conn.execute("""
            UPDATE user_films SET hall_of_fame = 1
            WHERE status = 'watched' AND (rating = 5 OR source LIKE 'loved_%')
        """)

    if "jellyfin_notes" not in uf_cols:
        conn.execute("ALTER TABLE user_films ADD COLUMN jellyfin_notes TEXT")
        # Migrate Jellyfin generated comments to jellyfin_notes so personal_notes stays pure
        _migrate_jellyfin_comments(conn)

    j_cols = {r[1] for r in conn.execute("PRAGMA table_info(journals)").fetchall()}
    if "public" not in j_cols:
        conn.execute("ALTER TABLE journals ADD COLUMN public INTEGER NOT NULL DEFAULT 0")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS enrichment_skips (
            tmdb_id INTEGER PRIMARY KEY,
            skipped_at TEXT NOT NULL
        )
    """)


def init_db() -> None:
    """Create all tables if they don't already exist."""
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _run_migrations(conn)
        _ensure_fts_tables(conn)
    print(f"✓ Database ready at {DB_PATH}")


# ---------------------------------------------------------------------------
# films table
# ---------------------------------------------------------------------------

def upsert_film(
    tmdb_id: int,
    title: str,
    year: int | None = None,
    runtime: int | None = None,
    language: str | None = None,
    country: str | None = None,
    poster_url: str | None = None,
    tmdb_genres: list | None = None,
    default_taste_attributes: list | None = None,
    media_type: str = "movie",
    overview: str | None = None,
    backdrop_url: str | None = None,
) -> None:
    """Insert a film record, or update it if it already exists."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO films (
                tmdb_id, title, year, runtime, language, country,
                poster_url, backdrop_url, tmdb_genres, default_taste_attributes, media_type, overview
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tmdb_id) DO UPDATE SET
                title = excluded.title,
                year = excluded.year,
                runtime = excluded.runtime,
                language = excluded.language,
                country = excluded.country,
                poster_url = excluded.poster_url,
                backdrop_url = COALESCE(excluded.backdrop_url, films.backdrop_url),
                tmdb_genres = excluded.tmdb_genres,
                default_taste_attributes = excluded.default_taste_attributes,
                media_type = excluded.media_type,
                overview = COALESCE(excluded.overview, films.overview)
            """,
            (
                tmdb_id,
                title,
                year,
                runtime,
                language,
                country,
                poster_url,
                backdrop_url,
                json.dumps(tmdb_genres) if tmdb_genres else None,
                json.dumps(default_taste_attributes) if default_taste_attributes else None,
                media_type,
                overview,
            ),
        )


def update_film_backdrop(tmdb_id: int, backdrop_url: str) -> None:
    """Update backdrop_url for a film."""
    with get_connection() as conn:
        conn.execute("UPDATE films SET backdrop_url = ? WHERE tmdb_id = ?", (backdrop_url, tmdb_id))
        _fts_index_film(conn, tmdb_id)


def update_film_backdrops(tmdb_id: int, backdrop_url: str, secondary_backdrop_url: str | None = None) -> None:
    """Update primary and optional secondary backdrop_url for a film."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE films SET backdrop_url = ?, secondary_backdrop_url = ? WHERE tmdb_id = ?",
            (backdrop_url, secondary_backdrop_url, tmdb_id),
        )
        _fts_index_film(conn, tmdb_id)


def set_film_taste_attributes(tmdb_id: int, attributes: list[str]) -> None:
    """Update only the default_taste_attributes column for a film."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE films SET default_taste_attributes = ? WHERE tmdb_id = ?",
            (json.dumps(attributes), tmdb_id),
        )
        _fts_index_film(conn, tmdb_id)


def get_films_without_attributes(limit: int = 100) -> list[dict]:
    """Return films that have no default_taste_attributes populated."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT tmdb_id, title, year, runtime, language, tmdb_genres, overview, media_type
        FROM films
        WHERE default_taste_attributes IS NULL
           OR default_taste_attributes = '[]'
           OR default_taste_attributes = 'null'
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_film(tmdb_id: int) -> dict | None:
    """Return a film record by tmdb_id, or None if not found."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM films WHERE tmdb_id = ?", (tmdb_id,)
        ).fetchone()
    if row is None:
        return None
    film = dict(row)
    film["tmdb_genres"] = json.loads(film["tmdb_genres"]) if film["tmdb_genres"] else []
    film["default_taste_attributes"] = (
        json.loads(film["default_taste_attributes"])
        if film["default_taste_attributes"]
        else []
    )
    return film


def swap_film_tmdb_id(old_tmdb_id: int, new_tmdb_id: int) -> None:
    """Replace a film's tmdb_id across all tables (wrong TMDB match correction)."""
    with get_connection() as conn:
        conn.execute("UPDATE user_films SET tmdb_id = ? WHERE tmdb_id = ?", (new_tmdb_id, old_tmdb_id))
        conn.execute("DELETE FROM fts_films WHERE tmdb_id = ?", (old_tmdb_id,))
        conn.execute("DELETE FROM films WHERE tmdb_id = ?", (old_tmdb_id,))
        _fts_index_film(conn, new_tmdb_id)


def delete_film(tmdb_id: int) -> None:
    """Remove a film and scrub its ID from taste_attributes evidence."""
    with get_connection() as conn:
        conn.execute("DELETE FROM user_films WHERE tmdb_id = ?", (tmdb_id,))
        conn.execute("DELETE FROM fts_films WHERE tmdb_id = ?", (tmdb_id,))
        conn.execute("DELETE FROM films WHERE tmdb_id = ?", (tmdb_id,))

        # Scrub this film from taste_attributes evidence and degrade confidence.
        #
        # evidence_film_ids is a sample Claude cited — not every supporting film.
        # We can't know exactly how much this film contributed to the confidence
        # score without re-running Claude. But we can proportionally degrade:
        # if the attribute had N evidence films and we remove 1, reduce confidence
        # by 1/N. If all cited evidence is gone, drop confidence hard (0.15) —
        # the attribute might still be valid from un-cited films, but it's now
        # weakly supported and the next rescan will either confirm or kill it.
        rows = conn.execute(
            "SELECT id, evidence_film_ids, confidence_score FROM taste_attributes"
        ).fetchall()
        for row in rows:
            ids = json.loads(row["evidence_film_ids"]) if row["evidence_film_ids"] else []
            if tmdb_id not in ids:
                continue
            original_count = len(ids)
            ids = [i for i in ids if i != tmdb_id]
            if ids:
                degradation = 1.0 / original_count
                new_confidence = max(0.1, row["confidence_score"] - (row["confidence_score"] * degradation))
                conn.execute(
                    "UPDATE taste_attributes SET evidence_film_ids = ?, confidence_score = ? WHERE id = ?",
                    (json.dumps(ids), round(new_confidence, 3), row["id"]),
                )
            else:
                conn.execute(
                    "UPDATE taste_attributes SET evidence_film_ids = '[]', confidence_score = 0.15 WHERE id = ?",
                    (row["id"],),
                )


def search_films_local(query: str) -> list[dict]:
    """Case-insensitive title search against the local DB."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM films WHERE title LIKE ? ORDER BY year DESC",
            (f"%{query}%",),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# user_films table
# ---------------------------------------------------------------------------

def add_user_film(
    tmdb_id: int,
    status: str,
    personal_notes: str | None = None,
    watch_context: str | None = None,
    source: str = "manual",
    date_watched: str | None = None,
    rewatch: bool = False,
    rating: int | None = None,
    hall_of_fame: int = 0,
    jellyfin_notes: str | None = None,
) -> int:
    """Add a user_film entry. Returns the new row id."""
    if status == "rewatch_candidate":
        status = "watched"
        rewatch = True
    now = datetime.now(timezone.utc).date().isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO user_films
                (tmdb_id, status, rewatch, date_added, date_watched, personal_notes, jellyfin_notes, watch_context, source, rating, hall_of_fame)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tmdb_id, status, int(rewatch), now, date_watched, personal_notes, jellyfin_notes, watch_context, source, rating, int(hall_of_fame)),
        )
        return cursor.lastrowid


def update_user_film(row_id: int, **kwargs) -> None:
    """Update specific fields on a user_film row."""
    allowed = {
        "status", "rewatch", "date_watched", "personal_notes", "jellyfin_notes", "watch_context", "source", "rating", "hall_of_fame"
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [row_id]
    with get_connection() as conn:
        conn.execute(
            f"UPDATE user_films SET {set_clause} WHERE id = ?", values
        )
        # Re-index this film if personal_notes changed (FTS indexes notes)
        if "personal_notes" in updates:
            uf_row = conn.execute("SELECT tmdb_id FROM user_films WHERE id = ?", (row_id,)).fetchone()
            if uf_row:
                _fts_index_film(conn, uf_row["tmdb_id"])


def get_user_film_by_tmdb(tmdb_id: int) -> dict | None:
    """Return the most recent user_film entry for a tmdb_id, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM user_films WHERE tmdb_id = ? ORDER BY date_added DESC LIMIT 1",
            (tmdb_id,),
        ).fetchone()
    return dict(row) if row else None


def get_watchlist() -> list[dict]:
    """Return all films currently on the watchlist, joined with film metadata."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT f.*, uf.id as user_film_id, uf.status, uf.date_added,
                   uf.personal_notes, uf.watch_context, uf.source
            FROM user_films uf
            JOIN films f ON uf.tmdb_id = f.tmdb_id
            WHERE uf.status = 'watchlist'
            ORDER BY uf.date_added DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_watched_films() -> list[dict]:
    """Return all watched films, joined with film metadata."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT f.*, uf.id as user_film_id, uf.status, uf.date_added,
                   uf.date_watched, uf.personal_notes, uf.watch_context, uf.source
            FROM user_films uf
            JOIN films f ON uf.tmdb_id = f.tmdb_id
            WHERE uf.status = 'watched'
            ORDER BY uf.date_watched DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_hall_of_fame() -> list[dict]:
    """Return all films designated as Hall of Fame (deeply moving taste anchors)."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT f.*, uf.id as user_film_id, uf.status, uf.rewatch, uf.date_added,
                   uf.date_watched, uf.personal_notes, uf.jellyfin_notes, uf.watch_context,
                   uf.source, uf.rating, uf.hall_of_fame
            FROM user_films uf
            JOIN films f ON uf.tmdb_id = f.tmdb_id
            WHERE uf.hall_of_fame = 1
              AND uf.id = (
                  SELECT MAX(id) FROM user_films u2
                  WHERE u2.tmdb_id = uf.tmdb_id
              )
            ORDER BY uf.date_added DESC
            """,
        ).fetchall()
    return [dict(r) for r in rows]


def toggle_hall_of_fame(tmdb_id: int) -> bool:
    """Toggle the Hall of Fame status for a film. Returns the new state (True/False)."""
    with get_connection() as conn:
        row = conn.execute("SELECT hall_of_fame FROM user_films WHERE tmdb_id = ? ORDER BY id DESC LIMIT 1", (tmdb_id,)).fetchone()
        if not row:
            return False
        current = bool(row["hall_of_fame"])
        new_val = 0 if current else 1
        conn.execute("UPDATE user_films SET hall_of_fame = ? WHERE tmdb_id = ?", (new_val, tmdb_id))
        return bool(new_val)


def get_rated_films(min_rating: int = 1) -> list[dict]:
    """Return all rated films (with opinions), joined with film metadata."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT f.*, uf.id as user_film_id, uf.status, uf.rewatch, uf.date_added,
                   uf.date_watched, uf.personal_notes, uf.watch_context, uf.source, uf.rating
            FROM user_films uf
            JOIN films f ON uf.tmdb_id = f.tmdb_id
            WHERE uf.rating IS NOT NULL AND uf.rating >= ?
              AND uf.id = (
                  SELECT MAX(id) FROM user_films u2
                  WHERE u2.tmdb_id = uf.tmdb_id
              )
            ORDER BY uf.rating DESC, uf.date_added DESC
            """,
            (min_rating,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_user_films() -> dict:
    """Return counts per status."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM user_films GROUP BY status"
        ).fetchall()
    return {row["status"]: row["n"] for row in rows}


# ---------------------------------------------------------------------------
# taste_attributes table
# ---------------------------------------------------------------------------

def upsert_taste_attribute(
    attribute_name: str,
    confidence_score: float,
    evidence_film_ids: list[int],
    cluster: str | None,
    polarity: str = "positive",
) -> None:
    """Insert or update a taste attribute by name + cluster + polarity."""
    with get_connection() as conn:
        existing = conn.execute(
            """
            SELECT id FROM taste_attributes
            WHERE attribute_name = ? AND cluster IS ? AND polarity = ?
            """,
            (attribute_name, cluster, polarity),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE taste_attributes
                SET confidence_score = ?, evidence_film_ids = ?
                WHERE id = ?
                """,
                (
                    confidence_score,
                    json.dumps(evidence_film_ids),
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO taste_attributes
                    (attribute_name, confidence_score, evidence_film_ids, cluster, polarity)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    attribute_name,
                    confidence_score,
                    json.dumps(evidence_film_ids),
                    cluster,
                    polarity,
                ),
            )


def get_taste_attributes(cluster: str | None = None, polarity: str = "positive") -> list[dict]:
    """Return taste attributes, optionally filtered by cluster and polarity."""
    with get_connection() as conn:
        if cluster:
            rows = conn.execute(
                """
                SELECT * FROM taste_attributes
                WHERE polarity = ? AND cluster = ?
                ORDER BY confidence_score DESC
                """,
                (polarity, cluster),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM taste_attributes WHERE polarity = ? ORDER BY confidence_score DESC",
                (polarity,),
            ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["evidence_film_ids"] = json.loads(d["evidence_film_ids"]) if d["evidence_film_ids"] else []
        result.append(d)
    return result


def get_films_changed_since(since_iso: str) -> list[dict]:
    """
    Return films added or updated since a given ISO timestamp.
    Used by diff-rescan to send only new/changed data to Claude.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT f.*, uf.status, uf.personal_notes, uf.watch_context,
                   uf.source, uf.date_added, uf.updated_at, uf.rewatch, uf.rating
            FROM user_films uf
            JOIN films f ON uf.tmdb_id = f.tmdb_id
            WHERE uf.date_added > ? OR uf.updated_at > ?
            ORDER BY uf.updated_at DESC
            """,
            (since_iso, since_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_taste_attributes() -> None:
    """Wipe all taste attributes — called at the start of a rescan."""
    with get_connection() as conn:
        conn.execute("DELETE FROM taste_attributes")


# ---------------------------------------------------------------------------
# taste_model_versions table
# ---------------------------------------------------------------------------

def save_taste_model_version(model_summary: str, trigger: str) -> int:
    """Save a new taste model version. Returns the version id."""
    with get_connection() as conn:
        last = conn.execute(
            "SELECT MAX(version_number) as v FROM taste_model_versions"
        ).fetchone()
        next_version = (last["v"] or 0) + 1
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            """
            INSERT INTO taste_model_versions (version_number, generated_at, model_summary, trigger)
            VALUES (?, ?, ?, ?)
            """,
            (next_version, now, model_summary, trigger),
        )
        return cursor.lastrowid


def get_latest_taste_model() -> dict | None:
    """Return the most recent taste model version."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM taste_model_versions ORDER BY version_number DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# recommendation_issues table
# ---------------------------------------------------------------------------

def save_recommendation_issue(
    issue_text: str,
    films_referenced: list[int],
    taste_model_version_id: int | None = None,
) -> int:
    """Save a recommendation issue. Returns the issue id."""
    with get_connection() as conn:
        last = conn.execute(
            "SELECT MAX(issue_number) as n FROM recommendation_issues"
        ).fetchone()
        next_issue = (last["n"] or 0) + 1
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            """
            INSERT INTO recommendation_issues
                (issue_number, generated_at, taste_model_version, issue_text, films_referenced)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                next_issue,
                now,
                taste_model_version_id,
                issue_text,
                json.dumps(films_referenced),
            ),
        )
        return cursor.lastrowid


def get_recommendation_history(limit: int = 10, include_archived: bool = False) -> list[dict]:
    """Return the most recent recommendation issues (non-archived by default)."""
    with get_connection() as conn:
        if include_archived:
            rows = conn.execute(
                "SELECT * FROM recommendation_issues ORDER BY generated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM recommendation_issues WHERE archived = 0 ORDER BY generated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["films_referenced"] = json.loads(d["films_referenced"]) if d["films_referenced"] else []
        d.setdefault("archived", 0)
        result.append(d)
    return result


def get_archived_issues(limit: int = 50) -> list[dict]:
    """Return archived recommendation issues."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM recommendation_issues WHERE archived = 1 ORDER BY generated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["films_referenced"] = json.loads(d["films_referenced"]) if d["films_referenced"] else []
        result.append(d)
    return result


def archive_issue(issue_id: int) -> None:
    """Archive a single issue."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE recommendation_issues SET archived = 1 WHERE id = ?",
            (issue_id,),
        )


def unarchive_issue(issue_id: int) -> None:
    """Restore an archived issue."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE recommendation_issues SET archived = 0 WHERE id = ?",
            (issue_id,),
        )


def auto_archive_issues(keep: int = 2) -> int:
    """
    Auto-archive all but the most recent `keep` non-archived issues.
    Returns the number of issues archived.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM recommendation_issues WHERE archived = 0 ORDER BY generated_at DESC"
        ).fetchall()
        to_archive = [r["id"] for r in rows[keep:]]
        if to_archive:
            placeholders = ",".join("?" * len(to_archive))
            conn.execute(
                f"UPDATE recommendation_issues SET archived = 1 WHERE id IN ({placeholders})",
                to_archive,
            )
        return len(to_archive)


def update_recommendation_issue(issue_id: int, issue_text: str) -> None:
    """Overwrite the editorial text of an existing issue (keeps films_referenced)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE recommendation_issues SET issue_text = ? WHERE id = ?",
            (issue_text, issue_id),
        )


# ---------------------------------------------------------------------------
# watchlist_unresolved table
# ---------------------------------------------------------------------------

def add_unresolved(raw_text: str, source: str, confidence_score: float | None = None) -> int:
    """Add an unresolved watchlist entry. Returns the new row id."""
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO watchlist_unresolved (raw_text, source, confidence_score) VALUES (?, ?, ?)",
            (raw_text, source, confidence_score),
        )
        return cursor.lastrowid


def get_unresolved(resolved: bool = False) -> list[dict]:
    """Return unresolved (or resolved) watchlist entries."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM watchlist_unresolved WHERE resolved = ? ORDER BY id ASC",
            (1 if resolved else 0,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_resolved(row_id: int) -> None:
    """Mark an unresolved entry as resolved."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE watchlist_unresolved SET resolved = 1 WHERE id = ?", (row_id,)
        )


# ---------------------------------------------------------------------------
# Web UI helpers
# ---------------------------------------------------------------------------

def get_all_films_with_status(status_filter: str | None = None) -> list[dict]:
    """
    Return all films joined with their latest user_film row.
    Optionally filter by status ('watched', 'watchlist').
    """
    with get_connection() as conn:
        if status_filter and status_filter != "all":
            if status_filter == "watchlist":
                rows = conn.execute(
                    """
                    SELECT f.*, uf.id as user_film_id, uf.status, uf.rewatch, uf.date_added,
                           uf.date_watched, uf.personal_notes, uf.watch_context, uf.source, uf.rating
                    FROM user_films uf
                    JOIN films f ON uf.tmdb_id = f.tmdb_id
                    WHERE (uf.status = 'watchlist' OR uf.rewatch = 1)
                      AND uf.id = (
                          SELECT MAX(id) FROM user_films u2
                          WHERE u2.tmdb_id = uf.tmdb_id
                      )
                    ORDER BY uf.date_added DESC
                    """,
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT f.*, uf.id as user_film_id, uf.status, uf.rewatch, uf.date_added,
                           uf.date_watched, uf.personal_notes, uf.watch_context, uf.source, uf.rating
                    FROM user_films uf
                    JOIN films f ON uf.tmdb_id = f.tmdb_id
                    WHERE uf.status = ?
                      AND uf.id = (
                          SELECT MAX(id) FROM user_films u2
                          WHERE u2.tmdb_id = uf.tmdb_id
                      )
                    ORDER BY uf.date_added DESC
                    """,
                    (status_filter,),
                ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT f.*, uf.id as user_film_id, uf.status, uf.rewatch, uf.date_added,
                       uf.date_watched, uf.personal_notes, uf.watch_context, uf.source, uf.rating
                FROM user_films uf
                JOIN films f ON uf.tmdb_id = f.tmdb_id
                WHERE uf.id = (
                    SELECT MAX(id) FROM user_films u2
                    WHERE u2.tmdb_id = uf.tmdb_id
                )
                ORDER BY uf.date_added DESC
                """,
            ).fetchall()
    return [dict(r) for r in rows]


def get_recommendations_for_film(tmdb_id: int, limit: int = 5) -> list[dict]:
    """Return recommendation issues that reference a given film."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM recommendation_issues
            WHERE films_referenced LIKE ?
            ORDER BY generated_at DESC
            LIMIT ?
            """,
            (f"%{tmdb_id}%", limit),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["films_referenced"] = (
            json.loads(d["films_referenced"]) if d["films_referenced"] else []
        )
        result.append(d)
    # LIKE can match partial integers — verify the actual id is in the list
    return [r for r in result if tmdb_id in r["films_referenced"]]


# ---------------------------------------------------------------------------
# journals table
# ---------------------------------------------------------------------------

def create_journal_entry(
    title: str,
    content: str,
    entry_type: str = "musing",
    linked_film_ids: list[int] | None = None,
    tags: list[str] | None = None,
) -> int:
    """Create a new journal entry. Returns the new row id."""
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO journals (title, content, type, created_at, updated_at,
                                  linked_film_ids, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                content,
                entry_type,
                now,
                now,
                json.dumps(linked_film_ids or []),
                json.dumps(tags or []),
            ),
        )
        new_id = cursor.lastrowid
        _fts_index_journal(conn, new_id)
        return new_id


def update_journal_entry(entry_id: int, **kwargs) -> None:
    """Update fields on a journal entry. Allowed: title, content, type, linked_film_ids, tags."""
    allowed = {"title", "content", "type", "linked_film_ids", "tags"}
    updates = {}
    for k, v in kwargs.items():
        if k not in allowed:
            continue
        if k in ("linked_film_ids", "tags") and isinstance(v, list):
            updates[k] = json.dumps(v)
        else:
            updates[k] = v
    if not updates:
        return
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [entry_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE journals SET {set_clause} WHERE id = ?", values)
        _fts_index_journal(conn, entry_id)


def delete_journal_entry(entry_id: int) -> None:
    """Delete a journal entry by id."""
    with get_connection() as conn:
        conn.execute("DELETE FROM journals WHERE id = ?", (entry_id,))
        conn.execute("DELETE FROM fts_journals WHERE journal_id = ?", (entry_id,))


def toggle_journal_public(entry_id: int, public: int) -> None:
    """Set the public flag on a journal entry (1 = visible to visitors, 0 = private)."""
    with get_connection() as conn:
        conn.execute("UPDATE journals SET public = ? WHERE id = ?", (public, entry_id))


def get_journal_entry(entry_id: int) -> dict | None:
    """Return a single journal entry by id, or None."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM journals WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["linked_film_ids"] = json.loads(d["linked_film_ids"]) if d["linked_film_ids"] else []
    d["tags"] = json.loads(d["tags"]) if d["tags"] else []
    return d


def get_all_journal_entries(limit: int = 50, offset: int = 0, *, public_only: bool = False) -> list[dict]:
    """Return all journal entries, newest first. If public_only, filter to public=1."""
    with get_connection() as conn:
        if public_only:
            rows = conn.execute(
                "SELECT * FROM journals WHERE public = 1 ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM journals ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["linked_film_ids"] = json.loads(d["linked_film_ids"]) if d["linked_film_ids"] else []
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
        result.append(d)
    return result


def get_journals_for_film(tmdb_id: int) -> list[dict]:
    """Return journal entries that mention a given film."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM journals WHERE linked_film_ids LIKE ? ORDER BY created_at DESC",
            (f"%{tmdb_id}%",),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["linked_film_ids"] = json.loads(d["linked_film_ids"]) if d["linked_film_ids"] else []
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
        result.append(d)
    # Verify the tmdb_id is actually in the list (LIKE can false-positive)
    return [e for e in result if tmdb_id in e["linked_film_ids"]]


# ---------------------------------------------------------------------------
# Full-text search (FTS5)
# ---------------------------------------------------------------------------

def rebuild_fts_index() -> None:
    """
    Rebuild the FTS5 indexes from scratch. Called after bulk ingestion or rescan.
    Safe to call at any time — clears and repopulates both tables.
    """
    with get_connection() as conn:
        # ── films ────────────────────────────────────────────────────────────
        conn.execute("DELETE FROM fts_films")

        rows = conn.execute(
            """
            SELECT f.tmdb_id, f.title, uf.personal_notes,
                   f.default_taste_attributes
            FROM films f
            LEFT JOIN user_films uf ON uf.tmdb_id = f.tmdb_id
              AND uf.id = (SELECT MAX(id) FROM user_films u2 WHERE u2.tmdb_id = f.tmdb_id)
            """
        ).fetchall()

        for r in rows:
            tmdb_id, title, notes, attrs_json = r
            attrs = ""
            if attrs_json:
                try:
                    attrs = " ".join(json.loads(attrs_json))
                except Exception:
                    pass
            conn.execute(
                "INSERT INTO fts_films (tmdb_id, title, personal_notes, taste_attrs) VALUES (?,?,?,?)",
                (tmdb_id, title or "", notes or "", attrs),
            )

        # ── journals ─────────────────────────────────────────────────────────
        conn.execute("DELETE FROM fts_journals")

        jrows = conn.execute("SELECT id, title, content, tags FROM journals").fetchall()
        for r in jrows:
            jid, title, content, tags_json = r
            tags = ""
            if tags_json:
                try:
                    tags = " ".join(json.loads(tags_json))
                except Exception:
                    pass
            conn.execute(
                "INSERT INTO fts_journals (journal_id, title, content, tags) VALUES (?,?,?,?)",
                (jid, title or "", content or "", tags),
            )


def _fts_index_film(conn, tmdb_id: int) -> None:
    """Insert or replace a single film row in fts_films (called after upsert)."""
    row = conn.execute(
        """
        SELECT f.tmdb_id, f.title, uf.personal_notes, f.default_taste_attributes
        FROM films f
        LEFT JOIN user_films uf ON uf.tmdb_id = f.tmdb_id
          AND uf.id = (SELECT MAX(id) FROM user_films u2 WHERE u2.tmdb_id = f.tmdb_id)
        WHERE f.tmdb_id = ?
        """,
        (tmdb_id,),
    ).fetchone()
    if not row:
        return
    _, title, notes, attrs_json = row
    attrs = ""
    if attrs_json:
        try:
            attrs = " ".join(json.loads(attrs_json))
        except Exception:
            pass
    # Delete existing and re-insert
    conn.execute("DELETE FROM fts_films WHERE tmdb_id = ?", (tmdb_id,))
    conn.execute(
        "INSERT INTO fts_films (tmdb_id, title, personal_notes, taste_attrs) VALUES (?,?,?,?)",
        (tmdb_id, title or "", notes or "", attrs),
    )


def _fts_index_journal(conn, journal_id: int) -> None:
    """Insert or replace a single journal row in fts_journals."""
    row = conn.execute("SELECT id, title, content, tags FROM journals WHERE id = ?", (journal_id,)).fetchone()
    if not row:
        return
    jid, title, content, tags_json = row
    tags = ""
    if tags_json:
        try:
            tags = " ".join(json.loads(tags_json))
        except Exception:
            pass
    conn.execute("DELETE FROM fts_journals WHERE journal_id = ?", (journal_id,))
    conn.execute(
        "INSERT INTO fts_journals (journal_id, title, content, tags) VALUES (?,?,?,?)",
        (jid, title or "", content or "", tags),
    )


def fts_search(query: str, limit: int = 20) -> dict:
    """
    Run a full-text search across films and journals.
    Returns {"films": [...], "journals": [...]} each with ranked results.
    """
    if not query or not query.strip():
        return {"films": [], "journals": []}

    # Sanitise query — escape special FTS5 chars to avoid parse errors
    safe_q = query.strip().replace('"', '""')
    # Wrap in quotes for phrase search, fall back to prefix match
    fts_q = f'"{safe_q}"*'

    films: list[dict] = []
    journals: list[dict] = []

    with get_connection() as conn:
        # ── Films ─────────────────────────────────────────────────────────────
        try:
            # FTS5 stores tmdb_id as a column; join to films for poster/year/status
            rows = conn.execute(
                """
                SELECT CAST(ff.tmdb_id AS INTEGER) AS tmdb_id,
                       f.title, f.year, f.language, f.poster_url,
                       uf.status, uf.personal_notes
                FROM fts_films ff
                JOIN films f ON CAST(ff.tmdb_id AS INTEGER) = f.tmdb_id
                LEFT JOIN user_films uf ON uf.tmdb_id = f.tmdb_id
                  AND uf.id = (SELECT MAX(id) FROM user_films u2 WHERE u2.tmdb_id = f.tmdb_id)
                WHERE fts_films MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_q, limit),
            ).fetchall()
            films = [dict(r) for r in rows]
        except Exception:
            # Fallback: plain LIKE search
            rows = conn.execute(
                """
                SELECT f.tmdb_id, f.title, f.year, f.language, f.poster_url,
                       uf.status, uf.personal_notes
                FROM films f
                LEFT JOIN user_films uf ON uf.tmdb_id = f.tmdb_id
                  AND uf.id = (SELECT MAX(id) FROM user_films u2 WHERE u2.tmdb_id = f.tmdb_id)
                WHERE f.title LIKE ?
                LIMIT ?
                """,
                (f"%{query}%", limit),
            ).fetchall()
            films = [dict(r) for r in rows]

        # ── Journals ──────────────────────────────────────────────────────────
        try:
            rows = conn.execute(
                """
                SELECT CAST(fj.journal_id AS INTEGER) AS journal_id,
                       j.title, j.type, j.created_at, j.content
                FROM fts_journals fj
                JOIN journals j ON CAST(fj.journal_id AS INTEGER) = j.id
                WHERE fts_journals MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_q, limit // 2),
            ).fetchall()
            journals = [dict(r) for r in rows]
        except Exception:
            rows = conn.execute(
                """
                SELECT id as journal_id, title, type, created_at, content
                FROM journals
                WHERE title LIKE ? OR content LIKE ?
                LIMIT ?
                """,
                (f"%{query}%", f"%{query}%", limit // 2),
            ).fetchall()
            journals = [dict(r) for r in rows]

    return {"films": films, "journals": journals}


def infer_engagement(
    runtime: int | None,
    genres: list[str] | None,
    taste_attrs: list[str] | None,
    media_type: str = "movie",
) -> str:
    """
    Infer engagement level (low/medium/high) from film metadata.
    Engagement = how easy it is to get hooked once you press play.
    No API call — pure heuristic based on genre, runtime, and attributes.

    - High engagement: grips you from the first scene (Fight Club, Mr. Nobody, Duel)
    - Medium engagement: takes 15–20 min to settle in but rewards attention
    - Low engagement: slow build, requires patience and the right mood
    """
    score = 0.0  # positive = higher engagement (more hookable)

    genres_lower = {g.lower() for g in (genres or [])}
    attrs_lower = " ".join(a.lower() for a in (taste_attrs or []))

    # Genre signals — hookability
    high_hook_genres = {"thriller", "action", "horror", "crime", "mystery", "adventure"}
    mid_hook_genres = {"drama", "science fiction", "fantasy", "animation", "comedy"}
    low_hook_genres = {"documentary", "history"}

    if genres_lower & high_hook_genres:
        score += 1.0
    if genres_lower & mid_hook_genres:
        score += 0.3
    if genres_lower & low_hook_genres:
        score -= 0.5

    # Runtime signals — shorter films tend to hook faster
    if runtime:
        if runtime < 95:
            score += 0.3
        elif runtime > 150:
            score -= 0.5
        elif runtime > 130:
            score -= 0.2

    # Attribute signals — hookability keywords
    high_hook_words = ["relentless", "gripping", "tension", "adrenaline", "frenetic",
                       "fast-paced", "propulsive", "edge-of-seat", "suspense",
                       "grips you", "locked in", "can't look away", "action-packed",
                       "riveting", "gut-punch", "high-stakes"]
    low_hook_words = ["meditative", "contemplative", "slow", "patient", "demands patience",
                      "hypnotic", "stillness", "quiet", "measured", "languid",
                      "deliberate pacing", "rewards patience"]

    for w in high_hook_words:
        if w in attrs_lower:
            score += 0.4
    for w in low_hook_words:
        if w in attrs_lower:
            score -= 0.4

    # Anime/series get a small boost — episodic format hooks differently
    if media_type in ("anime", "tv"):
        score += 0.2

    if score >= 1.0:
        return "high"
    elif score <= -0.3:
        return "low"
    return "medium"


def compute_all_engagement() -> int:
    """Compute and store engagement for all films. Returns count updated."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT tmdb_id, runtime, tmdb_genres, default_taste_attributes, media_type FROM films"
        ).fetchall()

        updated = 0
        for r in rows:
            genres = json.loads(r["tmdb_genres"]) if r["tmdb_genres"] else []
            attrs = json.loads(r["default_taste_attributes"]) if r["default_taste_attributes"] else []
            level = infer_engagement(r["runtime"], genres, attrs, r["media_type"] or "movie")
            conn.execute(
                "UPDATE films SET engagement = ? WHERE tmdb_id = ?",
                (level, r["tmdb_id"]),
            )
            updated += 1

    return updated


def get_library_stats() -> dict:
    """Return aggregate library statistics for the UI nav/header."""
    with get_connection() as conn:
        count_rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM user_films GROUP BY status"
        ).fetchall()
        total_row = conn.execute("SELECT COUNT(*) as n FROM films").fetchone()
        model_row = conn.execute(
            """SELECT version_number, generated_at FROM taste_model_versions
               ORDER BY version_number DESC LIMIT 1"""
        ).fetchone()

    counts = {row["status"]: row["n"] for row in count_rows}
    rewatch_row = conn.execute(
        "SELECT COUNT(*) as n FROM user_films WHERE rewatch = 1"
    ).fetchone() if False else None
    return {
        "watched": counts.get("watched", 0),
        "watchlist": counts.get("watchlist", 0),
        "total": total_row["n"] if total_row else 0,
        "taste_model_version": dict(model_row) if model_row else None,
    }


# ---------------------------------------------------------------------------
# Enrichment queue
# ---------------------------------------------------------------------------

def get_enrichment_count() -> int:
    """Return count of watched films that have no personal notes."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT COUNT(*) FROM user_films
        WHERE status = 'watched'
          AND (personal_notes IS NULL OR TRIM(personal_notes) = '')
        """
    ).fetchone()
    return row[0] if row else 0


def get_enrichment_queue(
    limit: int = 100,
    exclude_tmdb_id: int | None = None,
    exclude_ids: list[int] | None = None,
    active_prompt: str | None = None,
) -> list[dict]:
    """
    Return films that have no personal notes, prioritized by taste relevance & status:
    1. Un-annotated watched films (Hall of Fame, 5-star, 4-star, unrated)
    2. Relevance boost from stored taste attributes and active recommendation themes
    3. Skipped films are deprioritized to a 'later pile' (oldest skipped first),
       avoiding repetitive cycling while resurfacing organically if relevant to active queries.
    4. Watchlist films as fallback when watched queue is exhausted.
    """
    import random
    from datetime import datetime, timezone

    conn = get_connection()
    excludes = set(exclude_ids or [])
    if exclude_tmdb_id:
        excludes.add(exclude_tmdb_id)

    # 1. Gather taste relevance keywords
    taste_keywords = set()
    try:
        attr_rows = conn.execute(
            "SELECT attribute_name FROM taste_attributes WHERE polarity = 'positive' ORDER BY confidence_score DESC LIMIT 15"
        ).fetchall()
        for ar in attr_rows:
            for word in (ar["attribute_name"] or "").lower().split():
                if len(word) > 3:
                    taste_keywords.add(word)

        issue_row = conn.execute(
            "SELECT issue_text FROM recommendation_issues WHERE archived = 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if issue_row and issue_row["issue_text"]:
            for word in issue_row["issue_text"].lower().split():
                if len(word) > 4:
                    taste_keywords.add(word)
    except Exception:
        pass

    prompt_words = {w.lower() for w in (active_prompt or "").split() if len(w) > 3}

    exclude_clause = ""
    params = []
    if excludes:
        placeholders = ",".join(["?"] * len(excludes))
        exclude_clause = f"AND f.tmdb_id NOT IN ({placeholders})"
        params.extend(list(excludes))

    # 2. Fetch un-annotated watched films
    watched_rows = conn.execute(
        f"""
        SELECT f.tmdb_id, f.title, f.year, f.language, f.poster_url, f.overview,
               f.tmdb_genres, f.default_taste_attributes,
               uf.rating, uf.personal_notes, uf.jellyfin_notes, uf.status, uf.hall_of_fame,
               es.skipped_at
        FROM user_films uf
        JOIN films f ON uf.tmdb_id = f.tmdb_id
        LEFT JOIN enrichment_skips es ON es.tmdb_id = f.tmdb_id
        WHERE uf.status = 'watched'
          AND (uf.personal_notes IS NULL OR TRIM(uf.personal_notes) = '')
          AND uf.id = (
              SELECT MAX(id) FROM user_films u2
              WHERE u2.tmdb_id = uf.tmdb_id
          )
          {exclude_clause}
        """,
        params,
    ).fetchall()

    now_utc = datetime.now(timezone.utc)

    def score_candidate(film_row, is_watched: bool) -> float:
        # Base status score
        if is_watched:
            if film_row["hall_of_fame"]:
                score = 100.0
            elif film_row["rating"] == 5:
                score = 90.0
            elif film_row["rating"] == 4:
                score = 75.0
            elif film_row["rating"]:
                score = 60.0
            else:
                score = 50.0
        else:
            score = 15.0  # Watchlist baseline

        # Relevance scoring from taste keywords & active prompt
        film_text = f"{film_row['title'] or ''} {film_row['overview'] or ''} {film_row['tmdb_genres'] or ''} {film_row['default_taste_attributes'] or ''}".lower()
        relevance_hits = sum(1 for kw in taste_keywords if kw in film_text)
        prompt_hits = sum(1 for pw in prompt_words if pw in film_text)

        score += min(relevance_hits * 3.0, 25.0)
        score += min(prompt_hits * 15.0, 45.0)

        # Skip penalty ("later pile")
        skipped_at = film_row["skipped_at"]
        if skipped_at:
            score -= 200.0
            try:
                # Parsed ISO timestamp
                dt = datetime.fromisoformat(skipped_at.replace("Z", "+00:00"))
                age_hours = (now_utc - dt).total_seconds() / 3600.0
                # Old skips slowly regain priority; very recent skips stay at the bottom
                score += min(age_hours * 0.5, 30.0)
            except Exception:
                pass

        # Small jitter for rotation among identical scores
        score += random.uniform(0.0, 2.0)
        return score

    scored_watched = []
    for r in watched_rows:
        d = dict(r)
        d["_score"] = score_candidate(d, is_watched=True)
        scored_watched.append(d)

    scored_watched.sort(key=lambda x: x["_score"], reverse=True)
    results = scored_watched[:limit]

    # Fallback to watchlist if needed
    if len(results) < limit:
        needed = limit - len(results)
        wl_rows = conn.execute(
            f"""
            SELECT f.tmdb_id, f.title, f.year, f.language, f.poster_url, f.overview,
                   f.tmdb_genres, f.default_taste_attributes,
                   uf.rating, uf.personal_notes, uf.jellyfin_notes, uf.status, uf.hall_of_fame,
                   es.skipped_at
            FROM user_films uf
            JOIN films f ON uf.tmdb_id = f.tmdb_id
            LEFT JOIN enrichment_skips es ON es.tmdb_id = f.tmdb_id
            WHERE uf.status = 'watchlist'
              AND (uf.personal_notes IS NULL OR TRIM(uf.personal_notes) = '')
              AND uf.id = (
                  SELECT MAX(id) FROM user_films u2
                  WHERE u2.tmdb_id = uf.tmdb_id
              )
              {exclude_clause}
            """,
            params,
        ).fetchall()

        scored_wl = []
        for r in wl_rows:
            d = dict(r)
            d["_score"] = score_candidate(d, is_watched=False)
            scored_wl.append(d)

        scored_wl.sort(key=lambda x: x["_score"], reverse=True)
        results.extend(scored_wl[:needed])

    conn.close()
    return results


def record_enrichment_skip(tmdb_id: int) -> None:
    """Record a skip with the current UTC timestamp so the film rotates into cooldown."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO enrichment_skips (tmdb_id, skipped_at)
            VALUES (?, ?)
            ON CONFLICT(tmdb_id) DO UPDATE SET skipped_at = excluded.skipped_at
            """,
            (tmdb_id, now),
        )


def get_recently_skipped_tmdb_ids(days: int = 7) -> set[int]:
    """Return tmdb_ids skipped within the past N days."""
    from datetime import datetime, timezone, timedelta
    with get_connection() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='enrichment_skips'"
        ).fetchone()
        if not row:
            return set()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        rows = conn.execute(
            "SELECT tmdb_id FROM enrichment_skips WHERE skipped_at > ?",
            (cutoff.isoformat(),)
        ).fetchall()
        return {r["tmdb_id"] for r in rows}


def get_taste_calibration_stats() -> dict:
    """Compute active calibration level, progress %, and signal counts based on watched films annotated."""
    with get_connection() as conn:
        n_watched = conn.execute("SELECT COUNT(*) FROM user_films WHERE status = 'watched'").fetchone()[0]
        n_notes = conn.execute("SELECT COUNT(*) FROM user_films WHERE status = 'watched' AND personal_notes IS NOT NULL AND trim(personal_notes) != ''").fetchone()[0]
        n_ratings = conn.execute("SELECT COUNT(*) FROM user_films WHERE status = 'watched' AND rating IS NOT NULL").fetchone()[0]
        n_journals = conn.execute("SELECT COUNT(*) FROM journals").fetchone()[0]
        n_attrs = conn.execute("SELECT COUNT(*) FROM taste_attributes").fetchone()[0]

    # Active calibration is the ratio of watched films with personal notes
    n_unannotated = max(0, n_watched - n_notes)
    if n_watched > 0:
        base_pct = int((n_notes / n_watched) * 100)
        pct = 100 if n_unannotated == 0 else min(98, max(25, base_pct))
    else:
        pct = 100

    # Progressive Cinephile Tiers
    if n_notes >= 150:
        level_title = "Grand Auteur"
        level_num = 5
        next_hint = f"{n_unannotated} uncalibrated films waiting · Log notes to maintain peak fidelity" if n_unannotated > 0 else "Vault fully calibrated · Next film logged sharpens your brain"
    elif n_notes >= 100:
        level_title = "Master Cinephile"
        level_num = 4
        needed = max(1, 150 - n_notes)
        next_hint = f"{n_unannotated} uncalibrated films waiting · {needed} notes until Grand Auteur" if n_unannotated > 0 else f"{needed} notes until Grand Auteur"
    elif n_notes >= 50:
        level_title = "Auteur Critic"
        level_num = 3
        needed = max(1, 100 - n_notes)
        next_hint = f"{n_unannotated} uncalibrated films waiting · {needed} notes until Master Cinephile"
    elif n_notes >= 20:
        level_title = "Keen Observer"
        level_num = 2
        needed = max(1, 50 - n_notes)
        next_hint = f"{needed} notes until Auteur Critic"
    else:
        level_title = "Film Explorer"
        level_num = 1
        needed = max(1, 20 - n_notes)
        next_hint = f"Enrich {needed} more films to calibrate"

    return {
        "percentage": pct,
        "level_title": level_title,
        "level_num": level_num,
        "next_hint": next_hint,
        "n_notes": n_notes,
        "n_unannotated": n_unannotated,
        "n_watched": n_watched,
        "n_ratings": n_ratings,
        "n_journals": n_journals,
        "n_attrs": n_attrs,
    }


def get_random_taste_insight() -> dict | None:
    """
    Pull a polished, mindful, and self-contained cinephile insight from past journals or personal notes.
    Strictly filters out any NSFW, sexual, vulgar, or fragmented thoughts.
    """
    import random
    import re

    SENSITIVE_WORDS = {
        "sex", "sexual", "sexuality", "nudity", "naked", "erotic", "lesbian",
        "gay", "porn", "pervert", "vagina", "penis", "boob", "breast",
        "masturbat", "climax", "intercourse", "lust", "pussy", "dick",
        "fetish", "horny", "intimate scene", "bed scene"
    }

    DISQUALIFIED_PATTERNS = [
        re.compile(r'\b(honourable|honorable)\s+mention\b', re.IGNORECASE),
        re.compile(r'\bhall\s+of\s+fame\b', re.IGNORECASE),
        re.compile(r'\bon\s+hard\s+drive\b', re.IGNORECASE),
        re.compile(r'\bsole\s+focus\b', re.IGNORECASE),
        re.compile(r'\badult\s+parody\b', re.IGNORECASE),
    ]

    CURATED_FALLBACKS = [
        {
            "source_type": "Journal",
            "title": "Thoughts on Jigra",
            "quote": "Jigra has soul. And soul supersedes everything in a film.",
            "link": "/journal/20",
        },
        {
            "source_type": "Journal",
            "title": "Reverse Chekhov's Gun",
            "quote": "If you want to build suspense without losing audience attention, introduce the unfamiliar element quietly before it goes guns blazing.",
            "link": "/journal/21",
        },
        {
            "source_type": "Journal",
            "title": "Voiceless Characters",
            "quote": "To build memorable heroes, you must recognize that audiences have a moral code of their own that trumps any legal one.",
            "link": "/journal/22",
        },
        {
            "source_type": "Personal Note",
            "title": "Days of Heaven (1978)",
            "quote": "Cinematographer Néstor Almendros was going blind while filming, yet delivered what remains one of the most visually stunning films ever made.",
            "link": "/films/16642",
        },
        {
            "source_type": "Personal Note",
            "title": "Burning (2018)",
            "quote": "A slow-burn psychological thriller exploring class divide and obsession — a true masterclass in simmering, unresolved tension.",
            "link": "/films/491584",
        },
        {
            "source_type": "Personal Note",
            "title": "Hereditary (2018)",
            "quote": "A devastating family drama disguised as horror; Toni Collette's performance makes the descent into grief deeply visceral.",
            "link": "/films/493922",
        },
        {
            "source_type": "Personal Note",
            "title": "Sairat (2016)",
            "quote": "A powerful social commentary disguised as a romantic drama; the tonal shift in the second half hits hard and stays with you.",
            "link": "/films/395642",
        },
        {
            "source_type": "Personal Note",
            "title": "Fight Club (1999)",
            "quote": "Fincher builds a world out of IKEA catalogs and testosterone, then asks what it's actually made of. The visual grammar is baked into every cut.",
            "link": "/films/550",
        },
        {
            "source_type": "Personal Note",
            "title": "Inception (2010)",
            "quote": "A heist film set inside the architecture of dreams. Nolan at his most ambitious — every frame is a puzzle box you want to live inside.",
            "link": "/films/27205",
        },
        {
            "source_type": "Personal Note",
            "title": "Everything Everywhere All at Once (2022)",
            "quote": "The Daniels use multiverse theory to tell an intimate story about family. The visual invention is relentless — every universe has its own distinct grammar.",
            "link": "/films/545611",
        },
        {
            "source_type": "Personal Note",
            "title": "Jagga Jasoos (2017)",
            "quote": "Anurag Basu makes a musical detective adventure entirely in song. The ambition is wild — a Tintin film made in Bollywood.",
            "link": "/films/378236",
        },
        {
            "source_type": "Personal Note",
            "title": "Hometown Cha-Cha-Cha",
            "quote": "Never stopped smiling. Even the clichés were endearing because the emotional warmth was completely earned.",
            "link": "/films/129888",
        },
    ]

    with get_connection() as conn:
        # 1. Search journals (exclude explicitly NSFW journal IDs and titles)
        journal_rows = conn.execute(
            """
            SELECT id, title, content, created_at
            FROM journals
            WHERE length(content) > 60
              AND id NOT IN (32, 36, 37)
              AND LOWER(title) NOT LIKE '%handmaiden%'
              AND LOWER(title) NOT LIKE '%sex%'
              AND LOWER(title) NOT LIKE '%shinji%'
            ORDER BY RANDOM()
            LIMIT 10
            """
        ).fetchall()

        for j in journal_rows:
            raw = j["content"]
            cleaned = re.sub(r'https?://\S+', '', raw)
            cleaned = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', cleaned)
            cleaned = re.sub(r'\\([+*_\-\[\]()])', r'\1', cleaned)
            cleaned = re.sub(r'[#*>`_]', '', cleaned).strip()

            paragraphs = [p.strip() for p in cleaned.split('\n\n') if p.strip()]
            for para in paragraphs:
                para_lower = para.lower()
                if any(w in para_lower for w in SENSITIVE_WORDS):
                    continue

                sentences = [
                    s.strip() for s in re.split(r'(?<=[.!?])\s+', para)
                    if 45 <= len(s.strip()) <= 230
                    and not s.strip().startswith(('1.', '2.', '3.', '-', '•', 'http', 'And ', 'Because ', 'Or ', 'So if ', 'Which '))
                    and not (s.strip().endswith('?') and len(s.strip()) < 90)
                    and s.strip()[0].isupper()
                    and any(w in s.lower() for w in ['soul', 'cinema', 'tension', 'visual', 'frame', 'craft', 'character', 'heart', 'story', 'pacing', 'world', 'moment', 'structure', 'hero', 'moral'])
                    and not any(w in s.lower() for w in SENSITIVE_WORDS)
                ]
                if sentences:
                    quote = re.sub(r'\s+', ' ', sentences[0]).strip()
                    if not quote.endswith(('.', '!', '?')):
                        quote += '.'
                    return {
                        "source_type": "Journal",
                        "title": j["title"],
                        "quote": quote,
                        "date": j["created_at"][:10] if j["created_at"] else "",
                        "link": f"/journal/{j['id']}",
                    }

        # 2. Search personal notes
        note_rows = conn.execute(
            """
            SELECT f.tmdb_id, f.title, f.year, uf.personal_notes
            FROM user_films uf
            JOIN films f ON uf.tmdb_id = f.tmdb_id
            WHERE uf.personal_notes IS NOT NULL AND length(uf.personal_notes) > 40
              AND LOWER(f.title) NOT LIKE '%handmaiden%'
              AND LOWER(f.title) NOT LIKE '%jan dara%'
              AND LOWER(f.title) NOT LIKE '%intimacy%'
            ORDER BY RANDOM()
            LIMIT 15
            """
        ).fetchall()

        for n in note_rows:
            raw = n["personal_notes"]
            cleaned = re.sub(r'https?://\S+', '', raw)
            cleaned = re.sub(r'[#*>`_]', '', cleaned).strip()

            if any(p.search(cleaned) for p in DISQUALIFIED_PATTERNS):
                continue
            if any(w in cleaned.lower() for w in SENSITIVE_WORDS):
                continue

            sentences = [
                s.strip() for s in re.split(r'(?<=[.!?])\s+', cleaned)
                if 42 <= len(s.strip()) <= 230
                and not s.strip().startswith(('1.', '2.', '3.', '-', '•', 'Honourable', 'Mention', 'Hall of Fame', 'And ', 'Because '))
                and s.strip()[0].isupper()
                and any(w in s.lower() for w in ['film', 'director', 'scene', 'visual', 'craft', 'story', 'frame', 'pacing', 'camera', 'moment', 'feel', 'ambition', 'acting', 'script', 'genius', 'dread', 'tension', 'performance'])
                and not any(w in s.lower() for w in SENSITIVE_WORDS)
            ]
            if sentences:
                quote = re.sub(r'\s+', ' ', sentences[0]).strip()
                if not quote.endswith(('.', '!', '?')):
                    quote += '.'
                return {
                    "source_type": "Personal Note",
                    "title": f"{n['title']} ({n['year']})" if n['year'] else n['title'],
                    "quote": quote,
                    "date": "",
                    "link": f"/films/{n['tmdb_id']}",
                }

    return random.choice(CURATED_FALLBACKS)


def get_taste_knowledge_graph() -> dict:
    """
    Construct the thematic knowledge graph for the Taste Profile Brain.
    Returns:
      {
        "nodes": [...],
        "links": [...]
      }
    Zero ongoing Gemini calls — queries SQLite relational models directly.
    """
    import json

    CLUSTER_CONFIG = {
        "solo_late_night": {
            "id": "cluster_solo",
            "label": "Solo & Visceral Thrillers",
            "desc": "High-intensity, constrained settings, real-time dread, stylized action, Korean genre",
            "color": "#ef4444",
            "radius": 18,
        },
        "general": {
            "id": "cluster_general",
            "label": "Humanist Ensemble & Pathos",
            "desc": "Slice-of-life with dignity, comedy protecting pathos, grounded grief, Indian realism",
            "color": "#f59e0b",
            "radius": 18,
        },
        "partner": {
            "id": "cluster_partner",
            "label": "Earned Tenderness & Warmth",
            "desc": "Warmth without sentimentality, heartfelt coming-of-age, comforting human connections",
            "color": "#10b981",
            "radius": 18,
        },
        "intellect": {
            "id": "cluster_intellect",
            "label": "High-Concept & Formal Cleverness",
            "desc": "Mind-bending structures, recontextualizing reveals, puzzle boxes that deliver emotion",
            "color": "#06b6d4",
            "radius": 18,
        },
    }

    nodes = [
        {
            "id": "core_brain",
            "label": "Taste Brain",
            "sub": "137 Watched · 18 Active Signals",
            "type": "core",
            "color": "#e0743a",
            "radius": 24,
            "desc": "The central distillation of Prashil's cinematic taste, combining emotional precision, structural cleverness, and visceral commitment.",
        }
    ]

    links = []

    # Add cluster nodes and connect to core
    for ckey, cinfo in CLUSTER_CONFIG.items():
        nodes.append({
            "id": cinfo["id"],
            "label": cinfo["label"],
            "desc": cinfo["desc"],
            "type": "cluster",
            "color": cinfo["color"],
            "radius": cinfo["radius"],
            "cluster_key": ckey,
        })
        links.append({
            "source": "core_brain",
            "target": cinfo["id"],
            "strength": 0.8,
            "type": "core_link",
        })

    with get_connection() as conn:
        attr_rows = conn.execute("""
            SELECT id, attribute_name, confidence_score, cluster, polarity, evidence_film_ids
            FROM taste_attributes
            WHERE polarity = 'positive'
            ORDER BY confidence_score DESC
        """).fetchall()

        # Cache film details to avoid redundant queries
        all_film_ids = set()
        for ar in attr_rows:
            if ar["evidence_film_ids"]:
                try:
                    all_film_ids.update(json.loads(ar["evidence_film_ids"]))
                except Exception:
                    pass

        films_by_id = {}
        if all_film_ids:
            placeholders = ",".join("?" for _ in all_film_ids)
            f_rows = conn.execute(f"""
                SELECT f.tmdb_id, f.title, f.year, f.poster_url, f.backdrop_url, uf.rating, uf.personal_notes
                FROM films f
                LEFT JOIN user_films uf ON f.tmdb_id = uf.tmdb_id
                WHERE f.tmdb_id IN ({placeholders})
            """, list(all_film_ids)).fetchall()

            for fr in f_rows:
                films_by_id[fr["tmdb_id"]] = {
                    "tmdb_id": fr["tmdb_id"],
                    "title": fr["title"],
                    "year": fr["year"],
                    "poster_url": fr["poster_url"] or "",
                    "backdrop_url": fr["backdrop_url"] or "",
                    "rating": fr["rating"],
                    "personal_notes": fr["personal_notes"] or "",
                }

        # Build attribute theme nodes
        for ar in attr_rows:
            node_id = f"attr_{ar['id']}"
            ckey = ar["cluster"] or "general"
            # Route high-concept attributes to intellect cluster
            name_lower = ar["attribute_name"].lower()
            if any(k in name_lower for k in ["high-concept", "twist", "puzzle", "weird", "procedural"]):
                ckey = "intellect"
            elif ckey not in CLUSTER_CONFIG:
                ckey = "general"

            cinfo = CLUSTER_CONFIG[ckey]

            ev_ids = []
            if ar["evidence_film_ids"]:
                try:
                    ev_ids = json.loads(ar["evidence_film_ids"])
                except Exception:
                    ev_ids = []

            evidence_films = [films_by_id[fid] for fid in ev_ids if fid in films_by_id]

            nodes.append({
                "id": node_id,
                "label": ar["attribute_name"],
                "short_label": ar["attribute_name"][:32] + ("…" if len(ar["attribute_name"]) > 32 else ""),
                "type": "theme",
                "cluster_key": ckey,
                "color": cinfo["color"],
                "confidence": ar["confidence_score"] or 0.8,
                "radius": 12 + int((ar["confidence_score"] or 0.5) * 6),
                "film_count": len(evidence_films),
                "films": evidence_films,
            })

            links.append({
                "source": cinfo["id"],
                "target": node_id,
                "strength": 0.5,
                "type": "theme_link",
            })

    return {
        "nodes": nodes,
        "links": links,
        "total_nodes": len(nodes),
        "total_links": len(links),
    }
