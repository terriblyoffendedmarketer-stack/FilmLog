---
name: Project status as of 2026-05-25
description: Current state of Filmlog — what's built, what's next, what's unresolved. Read this first in any new session.
type: project
---

## Current state (2026-05-25)

**Database:** 515 entries — 137 watched (128 rated 5★, 9 unrated from journal import), 376 watchlist. SQLite at `filmlog.db`. Rating column added to `user_films`.

**Journal:** 19 entries imported. 18/19 have film links (1 is pure writing theory). Journal film scan completed — 10 new films added from journal mentions, 11 existing films newly linked to journal entries.

**Taste model:** v1 generated with 23 taste signals.

**Media types in DB:** 368 movies, 101 TV series (K-drama etc), 34 anime.

**Rewatch model:** Rewatch is a boolean flag on `user_films`, NOT a separate status.

**Rating system:** 1-5 stars on `user_films`. Personal feeling, not standardized. 5★ = Hall of Fame. All existing watched films auto-rated 5★.

**Unresolved imports:** 117 entries in `watchlist_unresolved`.

**FTS index:** Rebuilt to include journal entries.

**GitHub:** `prashil3k/FilmLog` — main branch.

**Flask server:** Runs on port 5001.

## Web UI pages built

- **Tonight** — REDESIGNED: simplified to "who's watching" + optional freeform text. Generates editorial issue via Claude Opus with markdown rendering. Bold film titles, spoiler-aware sells, "why you specifically" connections. Past issues shown below.
- **Films** — tile grid with poster cards, status/language/era/length/media_type filters, taste-based sorting
- **Film detail** — poster, metadata, **inline star rating (HTMX)**, notes (HTMX edit-in-place), status toggle, taste attributes, linked recommendations, linked journal entries
- **Hall of Fame** — NEW: dedicated page for 5★ films with gold star badges. Nav link added.
- **Add Film** — NEW: TMDB search → confirm/log page with status, star rating, watch context, notes. "+" button in nav.
- **Taste Profile** — confidence bars, clusters, evidence films, anti-signals
- **Journal** — card list with type badges, tag chips, film counts. Detail view renders markdown. New/edit forms.
- **Search** — FTS5 full-text search across films and journals
- **Settings** — API key entry and test

## Key changes this session

### Tonight page redesign
- Removed mood/energy/attention/ending questions (Prashil said they don't match how he picks films)
- Kept only "Who's watching?" + optional freeform text field
- Completely rewritten Claude prompt: spoiler awareness, markdown formatting, bold film titles, ability to suggest outside watchlist, Hall of Fame connections
- Editorial now renders as proper HTML (markdown → HTML via Python `markdown` library)
- Film titles rendered in accent color via `<strong>` styling

### Add Film flow (NEW)
- `/add` — TMDB search with live results via HTMX
- `/add/confirm/<tmdb_id>` — log page: poster, metadata, status toggle, star rating, watch context, notes
- Films auto-fetched from TMDB and saved to local DB on first view
- Supports updating existing films

### Hall of Fame (NEW)
- `/hall-of-fame` — dedicated page showing all 5★ rated films
- Gold star badge overlay on poster cards
- Nav link added between Films and Taste

### Rating system (NEW)
- `rating` INTEGER column added to `user_films` (1-5, NULL = unrated)
- Inline star rating on film detail page (HTMX, click to rate)
- Star rating on add/log page
- 5★ = "loved it, hall of fame" / 1★ = "actively disliked"
- All 128 existing watched films auto-rated 5★

### Recommendation prompt rewrite (v2)
- Simplified prompt: "sell me nicely, that's it"
- Fixed: no longer mentions films it's NOT recommending ("save Upstream Color for later" problem)
- Connections to Hall of Fame films are optional, not forced for every pick
- Spoiler-aware but not bland — sell the feeling, craft, texture
- Past issues now render as proper HTML (was raw markdown before)

### Rating system removed from UI
- Rating column kept in DB (128 films rated 5★) but rating UI removed from film detail and add film pages
- Prashil: "rating system is a very broken concept" — optional, expressed through notes instead

### Rewatch flow fixed
- Clicking Rewatch on a watchlist film now auto-sets status to Watched + Rewatch
- Previously required manually setting Watched first

### Film card taglines (NEW)
- `overview` column added to `films` table, backfilled from TMDB (468/515 films)
- Film cards in grid now show first sentence of TMDB overview as small dimmed tagline
- Provides anchor/context for why a film was added without cluttering the view

## What's next

1. ~~**Fix unlinked journal films**~~ ✅ DONE — 10 new films added (Jigra, Eternals, Vincenzo, Light in Your Eyes, Mr. Nobody, Scrubs, MAL·MO·E, Masaan, Call Me by Your Name, Blue Is the Warmest Color). 11 existing films newly linked. 18/19 journals now have links.
2. **Fix wrong TMDB matches** — e.g. The Lunchbox. Add "wrong film?" correction flow in film detail UI
3. **AI cleanup pass on journal text** — fix dictation typos via Haiku
4. **Context-aware film matching** — character names vs film titles
5. **Resolve 117 unresolved imports**
6. **Cognitive load scoring refinement**
7. **Taste-based mixed views** — cross-media recommendations by theme
8. **Phone access via Tailscale** — PWA manifest for mobile homescreen
9. **Watch provider data** — TMDB watch provider API, "is this on Netflix?" filter
10. **Pre-cached recommendation issues** — generate 2-3 at rescan time for Tier 2

## API keys

Stored in `.env` (gitignored). All modules use `load_dotenv(override=True)` because Prashil's shell has an empty `ANTHROPIC_API_KEY` from Claude Code itself.
