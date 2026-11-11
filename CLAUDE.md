# Filmlog — Personal Taste Engine for Films

## Status
- **Current Phase:** Phase 6 — Enrich "Haven't Seen This" Failsafe, In-Place Film Inspector, Spacious Thematic Knowledge Graph & Issue Generation 500 Diagnostics Fixed
- **In Progress:** Curated artwork pipeline (2026-11-11): textless covers, de-duplicated stills, distinct hero image, alt posters, Jellyfin artwork export
- **What's Next:** Run `scripts/export_jellyfin_artwork.py --write --refresh` with the hard drive mounted; then GitHub commit poller for web-generated notes
- **Known issues:** `scripts/test_filmlog_e2e.py` generates a REAL issue via Gemini and leaves it in `filmlog.db` — delete it after running. `public/style.css` is a stale orphan (the served file is `public/static/style.css`, a copy of `static/style.css`).
- **Live URL:** https://film-log-tau.vercel.app/ (Owner password: `8544`)
- **Active AI:** Google Gemini API (REST, $0/month, BYOK + server fallback)

---

## Roadmap
- [x] Phase 1: Database & Core Engine (SQLite, TMDB metadata, FTS5 search)
- [x] Phase 2: Web UI (Flask, HTMX, dark cinematic styling, Enrich queue, Tonight issues)
- [x] Phase 3: AI Migration (Anthropic Claude -> Google Gemini REST via `filmlog/ai.py`)
- [x] Phase 4: Cloud Deployment & PWA (Vercel serverless Python runtime, Service Worker, web manifest)
- [x] Phase 5: Editorial Redesign & Knowledge Graph (Substack edge-to-edge covers, reading overlay, interactive Taste Brain canvas)
- [x] Phase 6: Taste Refinements & Asset Architecture (Hall of Fame separation from 5★, Notes from Jellyfin segregation, widescreen multi-backdrop pipeline with distinct hero still and clickable banners, mindful NSFW-filtered quote engine with salvaged transcription stitching, zero-loss issue persistence via bidirectional localStorage sync, smart TMDB multi-entry search, anti-cycling enrich queue, zero-friction journal, 15-tier prestige ladder with 15 signals per tier, 3 metallic subgroups of 5 signals, 5-pip illuminated aperture diode gauge, clean signal stats, Pokémon-style Vault Badge Case, universal minimal AI API key switcher with status indicator dot, Enrich 'Haven't seen this' failsafe with watchlist reallocation, in-place film detail inspection modal, and spacious unbundled thematic knowledge graph physics)
- [ ] Phase 7: Sync web updates back to local Jellyfin & GitHub automatically
- [ ] Phase 8 (Vision): Multi-User Personal Taste Platform — independent user accounts/passwords, multimodal watchlist imports (TMDB/IMDb/Jellyfin APIs, screenshot OCR of Keep notes, raw text dumps), and individual Taste Brains & Knowledge Graphs

---

## File Map
- `app.py` — Flask web app entrypoint, routes, and `VercelPathMiddleware`
- `vercel.json` — Vercel rewrite rules routing `/(.*)` to `/api/index.py/$1`
- `filmlog/ai.py` — Native Python `urllib` client for Google Gemini models (`gemini-flash-lite-latest`, etc.)
- `filmlog/db.py` — SQLite connection, schema, queries, Vercel `/tmp` copy helper
- `filmlog/model.py` — Taste model generation (`taste_model.md`) from personal notes & ratings
- `filmlog/recommend.py` — Magazine editorial recommendation generator & anti-fatigue cooldowns
- `filmlog/attributes.py` — Taste attribute extraction via Gemini
- `filmlog/tmdb.py` — TMDB metadata and poster resolver
- `filmlog/images.py` — Artwork curation: ranks TMDB stills/posters (textless first), drops near-duplicates (offline; needs Pillow+numpy)
- `film_images` table — curated artwork per film (backdrop/poster/logo, rank 0 = best); films.backdrop_url = cover, secondary_backdrop_url = a DIFFERENT still (or NULL), alt_poster_url = a different poster
- `filmlog.db` — SQLite database with 137 watched and 897 watchlist films
- `public/` & `static/` — CSS, `visitor.js`, `manifest.json`, `sw.js`, and PWA icons
- `scripts/sync_jellyfin.py` — Matches local Jellyfin library & merges personal comments into DB
- `scripts/test_filmlog_e2e.py` — Comprehensive end-to-end verification test suite
- `scripts/curate_film_images.py` — Backfills curated artwork for every film (resumable; commit filmlog.db after)
- `scripts/export_jellyfin_artwork.py` — Writes poster/backdrops/landscape/logo into each movie folder for Jellyfin (dry run by default)
- `taste_model.md` — Synthesized markdown taste profile for recommendation context

---

## Setup & Run Instructions
```bash
# 1. Install dependencies via uv (shared cache)
uv venv && uv pip install -r requirements.txt

# 2. Local development
flask --app app run --debug --port 5001

# 3. Jellyfin sync
python3 scripts/sync_jellyfin.py

# 4. End-to-end verification
python3 scripts/test_filmlog_e2e.py

# 5. Artwork (offline tools; Pillow+numpy are deliberately NOT in requirements.txt)
uv pip install pillow numpy
.venv/bin/python scripts/curate_film_images.py              # new/uncurated films only
.venv/bin/python scripts/export_jellyfin_artwork.py         # dry run; add --write --refresh
```

### How issue images are chosen
Cover (top of issue + Tonight tile) = hero's `backdrop_url` (best textless still). The hero's own
section in the essay uses `secondary_backdrop_url` only if it differs, else `alt_poster_url` on a
blurred wash — never the cover again. Hero = `films_referenced[0]`, matched by id, not position.

### Environment Variables (.env / Vercel Settings)
- `GEMINI_API_KEY`: Google Gemini API key
- `TMDB_API_KEY`: TMDB v3 API key
- `FILMLOG_PASSWORD`: Owner password (`8544`)
- `FLASK_SECRET_KEY`: Random 32-char session encryption string

---

## How Writing, Notes & The Taste Brain Work

1. **Writing:**
   - **Enrich (`/enrich`):** Presents one film card at a time with poster backdrop and note input.
   - **Film Detail (`/films/<id>`):** Direct editing of personal thoughts, ratings (1-5★), and status.
   - **Journal (`/journal`):** Long-form rants, essays, or specific movie reflections.
2. **Saving:**
   - Log in with password `8544` at `/login` (or Settings). Notes save to `user_films.personal_notes` and `journals` in the database.
   - In visitor mode (unauthenticated), notes persist in browser `localStorage`.
3. **Parsing & Brain Synthesis:**
   - Sentiment signals are scanned: anti-signals ("dogshit", "boring") get negative weight (-2.0), loved signals ("masterpiece", "GOAT", 5★) get 3.0 weight.
   - Gemini parses prose to extract granular taste attributes (pacing, tone, thematic motifs).
   - In `filmlog/model.py`, your watched films, personal notes, and journal entries are synthesized into `taste_model.md`.
   - When generating Tonight issues, Gemini reads this taste distillation, applies an anti-fatigue penalty to films previously recommended, and writes a focused, high-conviction editorial issue.
