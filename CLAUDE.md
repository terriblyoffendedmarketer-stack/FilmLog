# Filmlog — Claude project memory

## What this is

Filmlog is a personal taste engine for films. It solves a specific problem: spending an hour deciding what to watch and still not feeling good about the choice. The fix is a system that gets to know taste at a granular level — not "you like horror" but "you like slow-building existential dread over jump scares" — and uses that model to give one well-argued recommendation rather than an infinite scroll of options.

The secondary problem it solves is the watchlist trap: a watchlist loses all emotional spark the moment something gets added to it. Filmlog treats the watchlist as a live candidate pool that's filtered through a real taste model in real time, so every recommendation from it comes with a reason *why tonight*.

The full spec lives in `FILMLOG_SPEC.md` (or in the original upload). This file is the working memory for Claude sessions.

---

## Architecture

### Language and runtime
**Python.** Reasons: `sqlite3` is in the stdlib, the Anthropic SDK is Python-first, it's the simplest path for a local personal tool, and it's easy to extend. No framework overhead.

### The library-first constraint
**All business logic lives in the `filmlog/` package as importable Python modules.** `main.py` (the CLI) is a thin shell that calls into this package. This is the foundational decision that makes a future web UI possible without rewriting anything — a Flask or FastAPI server can import the same `filmlog.recommend`, `filmlog.db`, `filmlog.model` functions directly.

Do not put logic in `main.py`. Keep it to argument parsing and calling into `filmlog/`.

### Interface
Terminal CLI for V1. Clean, fast, works everywhere. The library-first design means a web UI can be layered on top later.

### Database
SQLite (`filmlog.db`). Local, no server, no setup, fast, portable. Accessed via Python's built-in `sqlite3`.

### The three-file model
- **`filmlog.db`** — the engine. All structured data. Never edit directly.
- **`taste_log.md`** — the journal. Human-readable, written in plain language. Prashil can open and read this at any time and recognise himself in it. When he edits an entry and runs `rescan`, the database updates. This is the primary sharpening mechanism.
- **`taste_model.md`** — the distillation. A compact versioned summary of what the log reveals about taste. Gets regenerated on rescan. This is what Claude reads at recommendation time — not the full log. At 500 films, parsing every log entry would be slow and noisy; the model is the compressed signal.

### External APIs
- **TMDB** — all structured metadata: title, year, runtime, language, country, poster URL. Free, comprehensive, well-documented.
  - Poster fetch: `GET https://api.themoviedb.org/3/search/movie?query=...` → `poster_path` → `https://image.tmdb.org/t/p/w500/{poster_path}`
  - Watch providers: `GET https://api.themoviedb.org/3/movie/{id}/watch/providers` → which streaming platforms carry a film. Store in `films` table so "is this on Netflix?" is a SQL filter, zero AI, always works.
  - Key stored in `.env` as `TMDB_API_KEY`
- **Claude API (Anthropic)** — taste inference, default attribute generation, taste model distillation, mood calibration, recommendation issue generation.
  - Key stored in `.env` as `ANTHROPIC_API_KEY`
  - Use `claude-opus-4-7` for taste model generation and recommendation issues (quality matters). Use `claude-haiku-4-5-20251001` for fast lookups and batch attribute inference.

### AI degradation tiers — the system must work at all three levels

The Claude API is a quality amplifier, not a hard dependency. Design every feature against this stack:

| Tier | Requires | Quality | When it applies |
|---|---|---|---|
| **Tier 1 — Full Claude** | Live API | Best | Normal use |
| **Tier 2 — Pre-computed model** | Nothing (reads stored data) | Good | API down / credits exhausted |
| **Tier 3 — Raw SQL** | Nothing | Functional | Always |

**What gets pre-computed and stored (Tier 2 always available):**
- `default_taste_attributes` per film — inferred once when added, stored in `films` table. Never re-inferred.
- `taste_attributes` table — fully populated at rescan time, persisted in SQLite.
- `taste_model.md` — written at rescan time. A Tier 2 recommendation reads this file + queries the DB directly, skipping live Claude entirely.
- `recommendation_issues` — consider pre-generating 2–3 issues at rescan time and caching them. Stale by a few days but still taste-correct.

**Tier 2 recommendation logic (no live AI):**
Query `taste_attributes` for the active cluster + polarity=positive, cross-reference against `user_films` where status=watchlist, rank by confidence_score, return top 3. No prose, but correct picks.

---

## Project structure

```
filmlog/                     ← repo root
├── CLAUDE.md
├── FILMLOG_SPEC.md          ← original full spec (reference)
├── filmlog.db               ← SQLite database (gitignored)
├── taste_log.md             ← human-readable journal (committed)
├── taste_model.md           ← generated taste model (committed)
├── .env                     ← API keys (gitignored)
├── .gitignore
├── requirements.txt
├── main.py                  ← CLI entry point only — no logic here
└── filmlog/
    ├── __init__.py
    ├── db.py                ← schema setup + all DB read/write operations
    ├── tmdb.py              ← TMDB API client (search, metadata, poster URL)
    ├── ingest.py            ← watchlist ingestion: screenshot/text → confidence tier → DB
    ├── log.py               ← taste log: manual entry, parse log file, write entries
    ├── model.py             ← taste model generation (rescan command)
    ├── recommend.py         ← mood calibration + recommendation issue generation
    └── browse.py            ← browse/filter mode
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
    default_taste_attributes TEXT        -- JSON: Claude's inferred baseline attributes
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
    source TEXT                          -- 'mubi_import', 'keep_import', 'manual', etc.
);
```

### `taste_attributes` — extracted taste signals
```sql
CREATE TABLE taste_attributes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attribute_name TEXT NOT NULL,        -- e.g. 'slow-building dread', 'survival stakes'
    confidence_score REAL,               -- 0.0–1.0, increases with more evidence
    evidence_film_ids TEXT,             -- JSON array of tmdb_ids
    cluster TEXT,                        -- 'solo_late_night', 'family', 'weekend_afternoon', etc.
    polarity TEXT CHECK(polarity IN ('positive', 'negative'))
);
```

### `taste_model_versions` — versioned snapshots of the taste model
```sql
CREATE TABLE taste_model_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_number INTEGER,
    generated_at TEXT,
    model_summary TEXT,                  -- full taste_model.md text for this version
    trigger TEXT                         -- 'manual', '10_films_added', 'user_edit'
);
```

### `recommendation_issues` — every recommendation ever generated
```sql
CREATE TABLE recommendation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER,
    generated_at TEXT,
    taste_model_version INTEGER,
    issue_text TEXT,                     -- full editorial text
    films_referenced TEXT               -- JSON array of tmdb_ids
);
```

### `watchlist_unresolved` — imports that couldn't be matched
```sql
CREATE TABLE watchlist_unresolved (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_text TEXT,
    source TEXT CHECK(source IN ('handwritten', 'screenshot')),
    confidence_score REAL,
    resolved INTEGER DEFAULT 0          -- boolean
);
```

---

## CLI commands

```bash
python main.py setup                    # initialise DB (run once)
python main.py add "The Wailing"        # manual entry — opens interactive log flow
python main.py ingest <file>            # watchlist from screenshot or text file
python main.py rescan                   # regenerate taste model from full log
python main.py recommend                # mood calibration → recommendation issue
python main.py browse                   # interactive browse/filter mode
```

Alternatively with module syntax:
```bash
python -m filmlog <command>
```

---

## V1 build order

Work through these in sequence. Each step is a dependency for the next.

1. **DB schema setup** — `filmlog/db.py`, all tables, `python main.py setup`
2. **TMDB API integration** — `filmlog/tmdb.py`, search + metadata + poster URL fetch
3. **Watchlist ingestion** — `filmlog/ingest.py`, confidence-tiered (auto / batch-confirm / unresolved)
4. **Taste log + manual entry** — `filmlog/log.py`, format spec, `python main.py add`
5. **Taste model generation** — `filmlog/model.py`, rescan command, writes `taste_model.md`
6. **Mood calibration + recommendation issue** — `filmlog/recommend.py`, 2–3 indirect questions → 300–400 word editorial
7. **Browse/filter mode** — `filmlog/browse.py`, filters: language, watch context, mood, length, era

**Out of scope for V1:** quick-capture vs deep-dive split, rewatch candidate logic, "none of these click" fallback, books and music.

---

## Design principles (non-negotiable)

These are constraints on every decision, not preferences.

- **Sharpening over accumulating.** More data should make recommendations more precise, not more generic. If a feature makes recommendations noisier at scale, it's wrong.
- **Watchlist first, always.** Before surfacing anything new, check what's already been saved. The recommendation issue always exhausts the watchlist before suggesting new films.
- **Sell the film.** Not a title and a genre tag — a reason *this* film is right for *this person* on *this evening.* The why-you-specifically is the product.
- **No paradox of choice.** Reduce options to the point of excitement. The recommendation issue surfaces 2–3 films, not 10.
- **The file is always readable.** `taste_log.md` and `taste_model.md` should be openable by Prashil at any point and immediately legible. No internal codes, no DB IDs, no jargon.
- **Edits are first-class inputs.** When Prashil goes back and changes a note in `taste_log.md`, that's not a correction — it's new signal. The rescan loop is how the system gets smarter over time.
- **Language is a filter, never an organizer.** All films — Indian, Korean, Japanese, English, French — live in one unified pool. Language and country are searchable attributes, not buckets. The default recommendation and browse views show everything. Siloing by language recreates the graveyard problem the tool exists to solve.
- **Quality over quantity in taste signals.** A film in the hall of fame contributes more evidence to the taste model than ten unseen watchlist entries. Confidence scores are weighted by quality signal (loved > recommended > watchlist-only > no signal), not by raw count. This prevents volume imbalance (300 Indian films vs 50 Asian films) from distorting the model toward whichever language Prashil has catalogued most.

---

## Key mechanics to understand before touching recommend.py or model.py

### Default attributes (low-friction baseline)
Every film gets a set of default taste attributes inferred by Claude — what most people who love this film love about it. A film with no personal notes still contributes to the taste model via these defaults, at medium confidence.

### Personal layer
Some preferences are invisible to any default system (e.g. "I liked this film because the lead was attractive"). The `personal_notes` field captures exactly these — not required for every film, but critical for edge cases and depth.

### Contradiction surfacing
If Prashil loved The Exorcist and hated Hereditary, the model doesn't average those out. It surfaces the tension: "Both are elevated horror — what was different?" One answer to that sharpens the model more than ten new data points. This is the core loop.

### Taste clusters
Prashil-watching-alone-late-at-night is a different viewer from Prashil-with-family on Sunday afternoon. The model discovers these clusters from the log via `watch_context` and tags attributes accordingly. Recommendations are made *to a cluster*, not to an averaged-out profile.

### Anti-recommendation signal
Skips, abandonments, and negative ratings are tracked with `polarity = 'negative'` in `taste_attributes`. A consistent pattern of bailing on a specific quality — even if those films are acclaimed — is real signal.

### Quality signal weighting (for model.py)
When building taste attributes from the log, weight each film's contribution by its quality signal:

| Quality signal | Source | Weight multiplier |
|---|---|---|
| `loved` | Hall of fame, explicit ranking, 5-star | 3.0 |
| `recommended` | "must watch", "highly recommended" | 1.5 |
| `watchlist` | Added with no notes | 0.5 |
| `anti` | Dogshit / explicitly hated | −2.0 (negative polarity) |

This means 10 unseen watchlist films ≠ 1 loved film. The model reflects depth of feeling, not depth of list.

### Unified pool enforcement (for recommend.py and browse.py)
- Never group or filter by language/country in default views
- When scoring watchlist films for recommendation, score each film individually on taste attributes — no language quota, no language boost
- If the top 3 films all happen to be Korean, that's correct — it means Korean films best match tonight's taste profile, not that the system is biased
- Language filter is available in browse mode but never applied silently
- Apply a mild **recency-of-surfacing** boost: if a language/region hasn't appeared in the last 5 recommendation issues, bump films from that region slightly. This counteracts the graveyard problem without distorting taste signals.

---

## Watchlist ingestion tiers

When ingesting a screenshot or text file, classify each item:

| Tier | Condition | Action |
|---|---|---|
| Auto-import | Clean digital source, exact/near-exact TMDB match | Write directly to DB |
| Batch confirm | Ambiguous handwriting, or TMDB returns multiple plausible matches | Present as single scrollable batch — confirm or correct |
| Unresolved | Completely illegible, or no TMDB match at all | Save to `watchlist_unresolved`, optionally export to `unresolved.md` |

A 500-film ingestion should require active attention on 40–80 items at most, presented in one fast batch — not 500 sequential prompts.

---

## Recommendation issue format

Not a list. An editorial. ~300–400 words, written like a recommendation from a friend who actually knows the person's taste.

Each film mention includes:
1. **The sell** — 3–4 sentences, non-spoilery, written like a human who loves film
2. **The why-you-specifically** — referencing a film already in the log and the *specific attribute* that connects them (not the genre — the actual quality)
3. **Context tag** — who's watching, what energy level

Priority: watchlist candidates → rewatch candidates → new discoveries. The watchlist is always checked first.

---

## Mood calibration (before generating an issue)

2–3 indirect questions that reveal preference through contrast or scenario, not introspection. Examples:
- "Do you want something that wraps up cleanly tonight, or are you okay sitting with an open ending?"
- "Are you watching alone or with someone?"
- "Do you want to feel something or switch off?"
- "How much attention do you want to give this — full focus or half watching?"

These should feel like a small natural conversation, not a form. The answers identify the active taste cluster.

---

## Open questions (unresolved, carry forward)

| Question | Current default |
|---|---|
| Recommendation issue trigger — on demand, daily, or both | On demand for V1 |
| Rescan trigger — manual only or auto after N new films | Manual + auto after 5–10 additions |
| "None of these click" fallback | Parked for V2 |
| Watch providers storage schema | Add `watch_providers` TEXT (JSON) column to `films` table — fetch at add time, store per-country |
| Pre-cached recommendation issues | Generate 2–3 at rescan time for Tier 2 offline use — implement in model.py |

---

## Web UI (confirmed — builds after Step 7)

The terminal CLI is the engine. The web UI is the daily interface. Both use the same `filmlog/` package — nothing gets rewritten.

### Stack
- **Flask** — lightweight Python web server, imports `filmlog/` directly
- **HTMX** — interactive UI in plain HTML, no React/npm/build tools
- **SQLite FTS5** — full-text search added to schema; indexes film titles, notes, attributes, journal entries, recommendation issues. This is the retrieval layer.

### Pages / sections

| Section | What it does |
|---|---|
| **Tonight** | Recommendation generator + pre-generated issues ready to open |
| **Films** | Tile/card grid with posters, status badge, attribute chips. Click → film detail with editable notes, linked recommendations, related films |
| **Taste Profile** | Visual report card — top attributes with confidence bars, clusters, anti-signals, contradictions to surface |
| **Journal** | Musings and newsletter entries as cards. Searchable. Links to films. Separate from the film log. |
| **History** | Past recommendation issues, what got watched from them |

### Relational navigation (the retrieval layer)
Every piece of data is connected. Click paths that should work:
- Attribute → all films that have it
- Film → which recommendations referenced it → which attributes it contributed
- Recommendation → which taste model version generated it → which films were referenced
- Journal entry → films mentioned in it → attributes those films share

The taste model (`taste_model.md`) is the *compressed intelligence* — what Claude reads. The web UI displays the underlying graph it was built from. Users never need to read a 15,000-word text file; they navigate the indexed relationships instead.

### Markdown files in this context
`taste_log.md` and `taste_model.md` remain as human-editable exports. They are not the UI. The web UI reads from the DB. The markdown files are for: direct editing, version control, offline reading. They stay.

### Build order (after Step 7)
- Step 8: Flask server + Films page (tile grid, film detail, inline edit)
- Step 9: Tonight page (recommendation generator, pre-generated issues)
- Step 10: Taste Profile page (report card view, attribute navigation)
- Step 11: Journal / musings section
- Step 12: Full-text search + relational navigation across all sections
- Step 13: Settings page (API key entry, stored in browser localStorage)

### Settings page (Step 13)
A settings page in the web UI where the user enters both API keys.
Keys stored in browser `localStorage` — safe for a localhost personal tool (keys never leave the machine).
The Flask server reads keys from localStorage (passed via request headers or a config endpoint) rather than requiring a `.env` file.
This means the tool works out of the box after cloning — no manual `.env` setup needed.
Both keys should be enterable, testable (a "test connection" button per key), and clearable.

### Journal notes — stated vs revealed preference (Step 11)
Journal entries are *stated preferences* (what Prashil thinks/values about film). Film log is *revealed preferences* (what his behaviour shows he enjoys). Both go into the model but with different epistemological weight. Journal notes never directly generate taste_attributes. They inform the prose layer of recommendations. The interesting value: surfacing divergences — "you've written about valuing X but consistently finish films without it — tonight's pick might be the version where both align."
