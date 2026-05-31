# Filmlog — Claude project memory

## What this is

Filmlog is a personal taste engine for films. It solves a specific problem: spending an hour deciding what to watch and still not feeling good about the choice. The fix is a system that gets to know taste at a granular level — not "you like horror" but "you like slow-building existential dread over jump scares" — and uses that model to give one well-argued recommendation rather than an infinite scroll of options.

The secondary problem it solves is the watchlist trap: a watchlist loses all emotional spark the moment something gets added to it. Filmlog treats the watchlist as a live candidate pool that's filtered through a real taste model in real time, so every recommendation from it comes with a reason *why tonight*.

**Important:** `DECISIONS.md` contains the *why* behind design decisions — user preferences, product philosophy, what was tried and rejected. Read it alongside this file in any new session.

---

## Architecture

### Language and runtime
**Python.** Reasons: `sqlite3` is in the stdlib, the Anthropic SDK is Python-first, it's the simplest path for a local personal tool, and it's easy to extend.

### The library-first constraint
**All business logic lives in the `filmlog/` package as importable Python modules.** Both `app.py` (Flask web UI) and `main.py` (CLI) import from this package. Neither contains business logic — `app.py` handles routes and template rendering, `main.py` handles argument parsing.

### Interface
**Primary:** Flask + HTMX web UI (`app.py`). Dark, cinematic design. All pages built and functional.
**Secondary:** Terminal CLI (`main.py`). Available for scripting and batch operations.

### Database
SQLite (`filmlog.db`). Local, no server, no setup, fast, portable. Accessed via Python's built-in `sqlite3`. FTS5 virtual tables provide full-text search.

### The three-file model
- **`filmlog.db`** — the engine. All structured data. Never edit directly.
- **`taste_log.md`** — the journal. Human-readable, written in plain language. Prashil can open and read this at any time and recognise himself in it. When he edits an entry and runs `rescan`, the database updates.
- **`taste_model.md`** — the distillation. A compact versioned summary of what the log reveals about taste. Gets regenerated on rescan. This is what Claude reads at recommendation time — not the full log.

### External APIs
- **TMDB** — all structured metadata: title, year, runtime, language, country, poster URL.
  - Poster fetch: `GET https://api.themoviedb.org/3/search/movie?query=...` → `poster_path` → `https://image.tmdb.org/t/p/w500/{poster_path}`
  - Key stored in `.env` as `TMDB_API_KEY`
- **Claude API (Anthropic)** — taste model generation, recommendation issue generation, attribute inference.
  - Key stored in `.env` as `ANTHROPIC_API_KEY`
  - Use `claude-opus-4-7` for taste model generation and recommendation issues. Use `claude-haiku-4-5-20251001` for attribute extraction fallback.

### AI degradation tiers

| Tier | Requires | Quality | When it applies |
|---|---|---|---|
| **Tier 1 — Full Claude** | Live API | Best | Normal use |
| **Tier 2 — Pre-computed model** | Nothing (reads stored data) | Good | API down / credits exhausted |
| **Tier 3 — Raw SQL** | Nothing | Functional | Always |

**Tier 2 recommendation logic (no live AI):**
Query `taste_attributes` for the active cluster + polarity=positive, cross-reference against `user_films` where status=watchlist, rank by confidence_score, return top 3. No prose, but correct picks.

---

## Project structure

```
filmlog/                     ← repo root
├── CLAUDE.md                ← this file — project memory for Claude sessions
├── ENRICHMENT_SPEC.md       ← spec for the enrichment screen (not yet built)
├── filmlog.db               ← SQLite database
├── taste_log.md             ← human-readable journal (committed)
├── taste_model.md           ← generated taste model (committed)
├── .env                     ← API keys (gitignored)
├── requirements.txt
├── app.py                   ← Flask web server — routes + template rendering only
├── main.py                  ← CLI entry point — argument parsing only
├── wsgi.py                  ← PythonAnywhere WSGI entry point
├── deploy.md                ← PythonAnywhere deployment guide
├── static/
│   ├── style.css            ← all web UI styles
│   ├── manifest.json        ← PWA manifest
│   ├── sw.js                ← service worker (offline caching)
│   ├── icon-192.png         ← PWA icon 192×192
│   └── icon-512.png         ← PWA icon 512×512
├── templates/               ← Jinja2 templates (HTMX partials + full pages)
│   ├── base.html
│   ├── tonight.html         ← recommendation generator
│   ├── tonight_archived.html
│   ├── films.html           ← tile grid browse
│   ├── film_detail.html     ← single film view with editable notes
│   ├── hall_of_fame.html
│   ├── taste.html           ← taste profile report card
│   ├── journal.html / journal_entry.html / journal_form.html
│   ├── search.html          ← full-text search
│   ├── settings.html        ← API key management
│   ├── enrich.html          ← enrichment screen (default landing page)
│   ├── add_film.html / add_film_confirm.html
│   ├── unresolved.html      ← unmatched import management
│   └── partials/            ← HTMX swap targets (incl. enrich_card.html)
└── filmlog/
    ├── __init__.py
    ├── db.py                ← schema, migrations, all DB read/write operations
    ├── tmdb.py              ← TMDB API client (search, metadata, poster URL)
    ├── ingest.py            ← watchlist ingestion: text/screenshot → confidence tier → DB
    ├── log.py               ← taste log: write/rebuild taste_log.md from DB
    ├── model.py             ← taste model generation (rescan — diff-based or full)
    ├── recommend.py         ← mood calibration + recommendation issue generation + rewrite
    ├── attributes.py        ← taste attribute inference (Claude Haiku) + backfill
    ├── journal_ingest.py    ← bulk journal import from markdown
    └── browse.py            ← browse/filter mode (CLI)
```

---

## Database schema

### `films` — canonical film records
```sql
CREATE TABLE films (
    tmdb_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    year INTEGER,
    runtime INTEGER,
    language TEXT,
    country TEXT,
    poster_url TEXT,
    tmdb_genres TEXT,                    -- raw JSON array, for reference only
    default_taste_attributes TEXT,       -- JSON: Claude-inferred baseline attributes (Haiku-inferred, backfilled)
    media_type TEXT DEFAULT 'movie',     -- 'movie', 'tv', 'anime'
    engagement TEXT,                     -- hookability level: 'high' (grips you immediately), 'medium', 'low' (slow build)
    overview TEXT                        -- TMDB synopsis
);
```

### `user_films` — Prashil's relationship to each film
```sql
CREATE TABLE user_films (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id INTEGER REFERENCES films(tmdb_id),
    status TEXT CHECK(status IN ('watched', 'watchlist', 'rewatch_candidate')),
    date_added TEXT,
    date_watched TEXT,
    personal_notes TEXT,                 -- free text, Prashil's own words
    watch_context TEXT CHECK(watch_context IN ('solo', 'family', 'partner', 'friends')),
    source TEXT,                         -- 'mubi_import', 'keep_import', 'manual', etc.
    rewatch INTEGER NOT NULL DEFAULT 0,  -- boolean flag (rewatch is a flag, not a status)
    rating INTEGER,                      -- 1–5 stars, NULL = unrated
    updated_at TEXT                      -- ISO timestamp, set on every edit
);
```

### `taste_attributes` — extracted taste signals
```sql
CREATE TABLE taste_attributes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attribute_name TEXT NOT NULL,        -- e.g. 'slow-building dread', 'survival stakes'
    confidence_score REAL,               -- 0.0–1.0, weighted by quality signal
    evidence_film_ids TEXT,              -- JSON array of tmdb_ids (sample, not exhaustive)
    cluster TEXT,                        -- 'solo_late_night', 'family', 'partner', 'general'
    polarity TEXT CHECK(polarity IN ('positive', 'negative'))
);
```

### `taste_model_versions` — versioned snapshots
```sql
CREATE TABLE taste_model_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_number INTEGER NOT NULL,
    generated_at TEXT NOT NULL,
    model_summary TEXT NOT NULL,
    trigger TEXT CHECK(trigger IN ('manual', '10_films_added', 'user_edit'))
);
```

### `recommendation_issues` — every recommendation ever generated
```sql
CREATE TABLE recommendation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    generated_at TEXT NOT NULL,
    taste_model_version INTEGER REFERENCES taste_model_versions(id),
    issue_text TEXT NOT NULL,
    films_referenced TEXT,               -- JSON array of tmdb_ids
    archived INTEGER NOT NULL DEFAULT 0  -- auto-archived when >2 active issues exist
);
```

### `watchlist_unresolved` — imports that couldn't be matched
```sql
CREATE TABLE watchlist_unresolved (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_text TEXT NOT NULL,
    source TEXT CHECK(source IN ('handwritten', 'screenshot', 'text')),
    confidence_score REAL,
    resolved INTEGER DEFAULT 0
);
```

### `journals` — musings and newsletter entries
```sql
CREATE TABLE journals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    type TEXT CHECK(type IN ('musing', 'newsletter', 'note')) DEFAULT 'musing',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    linked_film_ids TEXT,                -- JSON array of tmdb_ids
    tags TEXT                            -- JSON array of freeform tag strings
);
```

### FTS5 virtual tables (search layer)
- `fts_films` — indexes tmdb_id, title, personal_notes, taste_attrs
- `fts_journals` — indexes journal_id, title, content, tags

---

## CLI commands

```bash
python3 main.py setup                    # initialise DB (run once)
python3 main.py add "The Wailing"        # TMDB search → add film
python3 main.py ingest <file>            # watchlist from text file
python3 main.py ingest-all <dir>         # bulk ingest all files in a directory
python3 main.py rescan                   # diff-based taste model update (only sends changes)
python3 main.py rescan --full            # full rebuild from entire library
python3 main.py rescan --precache        # rescan + pre-generate 2 recommendation issues
python3 main.py recommend               # mood calibration → recommendation issue
python3 main.py browse                   # interactive browse/filter mode (CLI)
python3 main.py import-journal <file>    # bulk import journal entries from markdown
python3 main.py export-unresolved        # export unresolved items
python3 main.py backfill-attributes      # infer taste attributes for all films (Claude Haiku)
python3 main.py compute-engagement       # compute engagement levels for all films (heuristic)
```

---

## Web UI pages (all built)

| Page | Route | What it does |
|---|---|---|
| **Enrich** | `/enrich` | Default landing page. Full-screen poster card, one watched film at a time, notes field front and center. Queue ordered by rating tier. |
| **Tonight** | `/tonight` | Recommendation generator — calibration form → Claude Opus editorial with inline posters. Past issues below (auto-archived, keep 2). |
| **Archived Issues** | `/tonight/archived` | Older recommendation issues with restore button. |
| **Films** | `/films` | Tile/card grid with posters, status badges. Click → film detail. |
| **Film Detail** | `/films/<id>` | Editable notes, rating, status, rewatch toggle, film correction. |
| **Hall of Fame** | `/hall-of-fame` | Top-rated films (5-star). |
| **Add Film** | `/add` | TMDB search → confirm → add to watchlist or watched. |
| **Taste Profile** | `/taste` | Report card — top attributes with confidence bars, clusters, anti-signals. |
| **Journal** | `/journal` | Musings and entries. CRUD. Searchable. |
| **Search** | `/search` | Full-text search across films, journals, attributes. |
| **Settings** | `/settings` | API key entry with test connection buttons. |
| **Unresolved** | `/unresolved` | Resolve or dismiss unmatched watchlist imports. |

---

## Key mechanics

### Rescan — diff-based by default
When fewer than 30 films have changed since the last model version, rescan sends only the diff + existing model to Claude. Much cheaper than a full rebuild. Uses `updated_at` on `user_films` to detect changes. Full rebuild available via `--full` flag.

### Film deletion and confidence degradation
When a film is deleted, its ID is scrubbed from `taste_attributes.evidence_film_ids` and the attribute's confidence score is proportionally degraded (1/N reduction where N was the evidence count). If all evidence is gone, confidence drops to 0.15 — the attribute survives weakly until the next rescan confirms or kills it. Attributes are never deleted at deletion time because `evidence_film_ids` is a sample, not an exhaustive list.

### Recommendation issues
- **Inline posters**: poster images float beside the sell text for each bold film title
- **Rewrite**: regenerate an issue's editorial with the current prompt, same films
- **Archive**: manual archive button + auto-archive (keep only 2 most recent visible)
- **Format**: 300–500 word editorial, 2–4 films, bold titles, one paragraph per film

### Quality signal weighting (for model.py)
| Quality signal | Source | Weight multiplier |
|---|---|---|
| `loved` | Hall of fame, 5-star rating | 3.0 |
| `recommended` | "must watch", "highly recommended" in notes | 1.5 |
| `watchlist` | Added with no notes | 0.5 |
| `anti` | Dogshit / explicitly hated | −2.0 (negative polarity) |

### Taste clusters
Prashil-watching-alone-late-at-night is a different viewer from Prashil-with-family on Sunday afternoon. The model discovers these clusters from `watch_context` and tags attributes accordingly. Recommendations are made *to a cluster*, not to an averaged-out profile.

### Recommendation prompt location
The main prompt is `RECOMMENDATION_PROMPT` in `filmlog/recommend.py` (line ~271). The rewrite prompt is `REWRITE_PROMPT` in the same file (line ~774). The taste model generation prompt is `MODEL_PROMPT` in `filmlog/model.py` (line ~144). Safe to edit the voice/sell sections; don't touch `{placeholders}` or the `**bold titles**` instruction (film extraction depends on it).

---

## Design principles (non-negotiable)

- **Sharpening over accumulating.** More data should make recommendations more precise, not more generic.
- **Watchlist first, always.** The recommendation issue always exhausts the watchlist before suggesting new films.
- **Sell the film.** Not a title and a genre tag — a reason *this* film is right for *this person* on *this evening.*
- **No paradox of choice.** The recommendation issue surfaces 2–4 films, not 10.
- **The file is always readable.** `taste_log.md` and `taste_model.md` are openable by Prashil at any point and immediately legible.
- **Edits are first-class inputs.** Changing a note in `taste_log.md` is new signal. The rescan loop is how the system gets smarter.
- **Language is a filter, never an organizer.** All films live in one unified pool. No language quotas, no region quotas, no type-of-film quotas. If the top 3 are all Korean, that's correct.
- **Quality over quantity in taste signals.** A hall-of-fame film contributes more evidence than ten unseen watchlist entries. Confidence scores are weighted by quality signal, not raw count.
- **No artificial balancing.** Recommendation variety comes from the methodology being good (the prompt instructs varied picks across tone/origin/energy), not from code-level boosts or quotas.

---

## Open questions (resolved and remaining)

| Question | Status |
|---|---|
| Recommendation issue trigger | **Resolved.** On demand via web UI and CLI. |
| Rescan trigger | **Resolved.** Manual, diff-based by default. `--full` flag available. |
| Watch providers | **Resolved.** Not implementing — decided against. |
| Pre-cached recommendation issues | **Done.** CLI `--precache` flag + frontend 3s reveal button on Tonight page for solo/no-free-text. |
| "None of these click" fallback | Parked for V2. Soft negative signal + regenerate avoiding those films. |
| `default_taste_attributes` backfill | **Done.** All 514 films have Haiku-inferred attributes. `filmlog/attributes.py` handles batch + single-film inference. |
| Engagement/hookability reframe | **Done.** Renamed `cognitive_load` → `engagement`. New hookability rubric. All 514 films computed. |
| Enrichment screen | **Done.** Default landing page at `/enrich`. Full-screen poster card, notes field, HTMX card swap. |
| Deployment | **Config ready.** `wsgi.py` + `deploy.md` for PythonAnywhere free tier. Needs manual account setup. |
| PWA manifest | **Done.** `manifest.json`, icons, service worker (caches app shell + posters), apple-mobile-web-app meta. |
| Relational navigation depth | **Mostly done.** Attribute chips → search. Past issues → model version. Remaining: dedicated attribute→films page. |

---

## Remaining build order

1. ~~**Enrichment screen**~~ — **Done.**
2. ~~**`default_taste_attributes` backfill**~~ — **Done.** All 514 films have attributes. Auto-infer on add-film.
3. ~~**Engagement reframe**~~ — **Done.** `cognitive_load` → `engagement`. New hookability rubric.
4. ~~**Pre-cached issue reveal**~~ — **Done.** CLI `--precache` + 3s delayed reveal on Tonight page.
5. ~~**Relational navigation**~~ — **Mostly done.** Attribute chips → search. Model version on issues.
6. ~~**Recommendation prompt variety**~~ — **Done.**
7. ~~**Deployment config**~~ — **Done.** `wsgi.py` + `deploy.md`. Needs manual PythonAnywhere setup.
8. ~~**PWA manifest**~~ — **Done.** Service worker, manifest, icons, apple-mobile-web-app meta.

### Still remaining
- **PythonAnywhere account setup** — manual step, follow `deploy.md`
- **Relational navigation polish** — dedicated attribute→films page, journal→film poster chips
- **"None of these click" fallback** — V2
