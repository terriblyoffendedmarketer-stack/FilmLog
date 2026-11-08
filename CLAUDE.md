# Filmlog — Personal Taste Engine for Films

## Status
- **Current Phase:** Phase 5 — Substack Editorial Issues & Taste Brain Knowledge Graph Live
- **In Progress:** PWA installation verification, interactive knowledge graph exploration
- **What's Next:** Automated daily GitHub sync for notes written on web
- **Live URL:** https://film-log-tau.vercel.app/ (Owner password: `8544`)
- **Active AI:** Google Gemini API (REST, $0/month, zero external SDK dependencies)

---

## Roadmap
- [x] Phase 1: Database & Core Engine (SQLite, TMDB metadata, FTS5 search)
- [x] Phase 2: Web UI (Flask, HTMX, dark cinematic styling, Enrich queue, Tonight issues)
- [x] Phase 3: AI Migration (Anthropic Claude -> Google Gemini REST via `filmlog/ai.py`)
- [x] Phase 4: Cloud Deployment & PWA (Vercel serverless Python runtime, Service Worker, web manifest)
- [x] Phase 5: Editorial Redesign & Knowledge Graph (Substack edge-to-edge covers, reading overlay, interactive Taste Brain canvas)
- [ ] Phase 6: Sync web updates back to local Jellyfin & GitHub automatically

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
- `filmlog.db` — SQLite database with 137 watched and 897 watchlist films
- `public/` & `static/` — CSS, `visitor.js`, `manifest.json`, `sw.js`, and PWA icons
- `scripts/sync_jellyfin.py` — Matches local Jellyfin library & merges personal comments into DB
- `scripts/test_filmlog_e2e.py` — Comprehensive end-to-end verification test suite
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
```

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
