---
name: Project status as of 2026-05-25
description: Current state of Filmlog — what's built, what's next, what's unresolved. Read this first in any new session.
type: project
---

## Current state (2026-05-25)

**Database:** 503 entries — 126 watched, 377 watchlist. SQLite at `filmlog.db` (committed to git).

**Taste model:** v1 generated with 23 taste signals. Top signals include "high-concept structure that delivers emotional payload" (0.97), "constrained-setting thriller intensity" (0.95), "ensemble slice-of-life with dignity" (0.94).

**Media types in DB:** 368 movies, 101 TV series (K-drama etc), 34 anime. Column `media_type` on `films` table.

**Rewatch model:** Rewatch is a boolean flag on `user_films`, NOT a separate status. Status is only `watched` or `watchlist`. A film can be watched AND flagged for rewatch. Purple ↩ badge on cards.

**Unresolved imports:** 117 entries in `watchlist_unresolved` table — genuinely ambiguous titles that need manual review.

**GitHub:** `prashil3k/FilmLog` — 2 commits on main, up to date. `push.command` and `pull.command` files for Prashil to double-click.

**Flask server:** Runs on port 5001 (`flask --app app run --debug --port 5001`). Auto-reloads on code changes.

## Key fixes made this session

- `load_dotenv(override=True)` across all modules — shell had empty `ANTHROPIC_API_KEY` that blocked `.env`
- Added TV series search to TMDB client (`search_tv`, `get_tv_details`, `fetch_tv_data`)
- Ingest pipeline uses TV search when `is_series=True` with scoring boost
- Progress bars with time estimates on TMDB matching and DB writing
- `ingest_all_raw` respects `non_interactive` flag to skip confirmation prompt
- `media_type` column + migration in db.py

## What's next

1. **Journal bulk import** — Prashil will drop a markdown file in `data/raw/`. Build a journal ingestion pipeline that parses unstructured notes (Notion, Keep, Substack) into journal entries, auto-links mentioned films, extracts tags.
2. **Phone access via Tailscale** — deferred, but keep in mind during builds (don't hardcode localhost assumptions). Also consider PWA manifest for "add to homescreen" on mobile.
3. **Taste-based mixed views** — cross-media recommendations by theme (an anime, a K-drama, and a film could appear together if they share taste attributes)
4. **The "sell" refinements** — mood calibration should detect tired-but-not-mindless state. The editorial should actively reframe masterpieces as tonight-watchable.
5. **Cognitive load scoring** — each film gets a `cognitive_load: low/medium/high` attribute so recommendations can match energy level

## API keys

Stored in `.env` (gitignored). Keys:
- `ANTHROPIC_API_KEY` — for Claude Haiku (parsing, metadata) and Opus (taste model, recommendations)
- `TMDB_API_KEY` — for film metadata and posters

**Why:** Prashil's shell has an empty `ANTHROPIC_API_KEY` env var (from Claude Code itself). All modules use `load_dotenv(override=True)` to work around this.
