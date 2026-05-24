---
name: Project status as of 2026-05-25
description: Current state of Filmlog — what's built, what's next, what's unresolved. Read this first in any new session.
type: project
---

## Current state (2026-05-25)

**Database:** 503 entries — 126 watched, 377 watchlist. SQLite at `filmlog.db`.

**Journal:** 19 entries imported from `data/raw/journal_dump.md` via `filmlog/journal_ingest.py`. 3 newsletters (ThreeK #1, #2, #4), 16 musings/notes. Auto-linked films via title matching with common-word filtering. Tags extracted (craft, diary, newsletter, kdrama, anime, threek-N).

**Taste model:** v1 generated with 23 taste signals.

**Media types in DB:** 368 movies, 101 TV series (K-drama etc), 34 anime. Column `media_type` on `films` table.

**Rewatch model:** Rewatch is a boolean flag on `user_films`, NOT a separate status.

**Unresolved imports:** 117 entries in `watchlist_unresolved`.

**GitHub:** `prashil3k/FilmLog` — main branch, up to date.

**Flask server:** Runs on port 5001. All 13 build steps + journal import complete.

## Web UI pages built

- **Films** — tile grid with poster cards, status/language/era/length/media_type filters, taste-based sorting
- **Film detail** — poster, metadata, notes (HTMX edit-in-place), status toggle, taste attributes, linked recommendations, linked journal entries
- **Tonight** — mood calibration form → Claude-generated recommendation editorial
- **Taste Profile** — confidence bars, clusters, evidence films, anti-signals
- **Journal** — card list with type badges, tag chips, film counts. Detail view renders markdown. New/edit forms with film search picker.
- **Search** — FTS5 full-text search across films and journals
- **Settings** — API key entry and test

## Key files added this session

- `filmlog/journal_ingest.py` — journal dump parser (split on H1 headers + bold-link-after-separator, common-word film title filtering, tag extraction)
- `strip_md` Jinja filter in `app.py` — strips markdown for card previews
- `import-journal` CLI command in `main.py`

## What's next

1. **Taste-based mixed views** — cross-media recommendations by theme
2. **The "sell" refinements** — mood calibration detecting tired-but-not-mindless
3. **Cognitive load scoring** — per-film `cognitive_load: low/medium/high` attribute
4. **Phone access via Tailscale** — PWA manifest for mobile homescreen
5. **FTS index rebuild** — run `db.rebuild_fts_index()` to index the new journal entries

## API keys

Stored in `.env` (gitignored). All modules use `load_dotenv(override=True)` because Prashil's shell has an empty `ANTHROPIC_API_KEY` from Claude Code itself.
