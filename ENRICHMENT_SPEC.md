# Enrichment Screen — Feature Spec

## Problem

Most watched films in the library have no personal notes. Without notes, the taste model relies entirely on default attributes (Claude's inferred baseline) — which miss personal, idiosyncratic signals. The result: recommendations are genre-accurate but lack the "you specifically" depth that makes Filmlog valuable.

Manual enrichment exists (edit notes on the film detail page) but has too much friction — you need to navigate to a film, click edit, type, save. Nobody sits down and retroactively annotates 200 films.

## Solution

A dedicated screen designed for idle moments — elevator rides, waiting rooms, bus stops. Shows one watched film at a time, full-screen poster, with a notes field front and center. Zero navigation, zero decision-making about *which* film to enrich. The system picks the next one that needs it.

## User flow

1. Open the enrichment screen (mobile: default landing; desktop: nav link)
2. See a single film: poster fills the viewport, title overlaid at bottom
3. A notes input is visible — tap to type or dictate
4. Save notes → card slides away, next film appears
5. Skip → card slides away without saving, film goes to the back of the queue
6. Session ends when the user navigates away — no forced completion

## Queue logic

Films are queued for enrichment if they meet ALL of:
- `status = 'watched'`
- `personal_notes IS NULL OR personal_notes = ''`
- Not linked in any journal entry beyond a bare mention (optional — start simple with just the notes check)

Queue ordering (most valuable to enrich first):
1. Films rated 5★ with no notes (Hall of Fame films — highest signal value)
2. Films rated 4★ with no notes
3. Films rated 3★ or below with no notes
4. Unrated watched films with no notes
5. Within each tier: random order (prevents predictable fatigue)

## UI design

### Mobile (primary target)
- Full-bleed poster background, edge to edge
- Dark gradient overlay at bottom (60% of screen)
- Film title: large, bold, white — over the gradient
- Year + language: smaller, muted — below title
- Notes textarea: full-width, translucent dark background, auto-focused
  - Placeholder: "What did you like about this one?" or similar prompts that rotate
  - Dictation-friendly: large tap target, iOS/Android microphone accessible
- Two action buttons at bottom:
  - **Save** (primary accent) — saves notes, advances to next
  - **Skip** (ghost/muted) — advances without saving
- Swipe gestures (optional enhancement):
  - Swipe right = save (if notes are non-empty)
  - Swipe left = skip
- Progress indicator: "12 of 47 remaining" — small, top-right

### Desktop
- Same layout but constrained to phone-width column (max 420px, centered)
- Keyboard shortcuts: Enter to save, Escape to skip

## Rotating placeholder prompts

To combat blank-screen paralysis, rotate through prompts like:
- "What did you love about this?"
- "What stayed with you after watching?"
- "Who would you recommend this to?"
- "What mood were you in when you watched it?"
- "Any scenes that stuck with you?"
- "Would you rewatch this? Why/why not?"

Pick randomly per film. These aren't stored — just UI nudges.

## Data model

No schema changes needed. Uses existing:
- `user_films.personal_notes` — the field being populated
- `user_films.rating` — for queue ordering
- `films` — poster, title, year, language for display

## Routes

```
GET  /enrich              → enrichment screen (loads first queued film)
GET  /enrich/next         → HTMX partial: next film card
POST /enrich/<tmdb_id>    → save notes, return next film card
POST /enrich/<tmdb_id>/skip → skip without saving, return next film card
```

## DB query for queue

```sql
SELECT f.tmdb_id, f.title, f.year, f.language, f.poster_url,
       uf.rating
FROM user_films uf
JOIN films f ON uf.tmdb_id = f.tmdb_id
WHERE uf.status = 'watched'
  AND (uf.personal_notes IS NULL OR uf.personal_notes = '')
  AND uf.id = (
      SELECT MAX(id) FROM user_films u2
      WHERE u2.tmdb_id = uf.tmdb_id
  )
ORDER BY
  CASE
    WHEN uf.rating = 5 THEN 1
    WHEN uf.rating = 4 THEN 2
    WHEN uf.rating IS NOT NULL AND uf.rating <= 3 THEN 3
    ELSE 4
  END,
  RANDOM()
```

## Build considerations

- **PWA-first**: this screen is the strongest argument for the PWA manifest (Task 8). Once Tailscale exposes the server, this screen on the phone homescreen is the enrichment loop.
- **Offline**: the poster and title are all that's needed — no API calls. Works fully offline against the local DB.
- **Animation**: card transitions should be fast and tactile. CSS transform + opacity, 200ms. No heavy libraries.
- **Session tracking**: consider tracking enrichment sessions (films enriched per session) for the taste model to weigh recently-enriched films slightly higher in the next rescan.

## Out of scope for V1
- Star rating on the enrichment card (just notes for now — rating can be done on film detail)
- AI-suggested notes ("Based on similar films you liked, you might have enjoyed the cinematography…")
- Batch dictation mode (record one long voice note covering multiple films)
- Rewatch marking from enrichment screen

## Build order
1. DB helper: `get_enrichment_queue()` in `filmlog/db.py`
2. Routes in `app.py`
3. Template: `templates/enrich.html` (full-screen card layout)
4. CSS: mobile-first card design with gradient overlay
5. HTMX: card swap on save/skip
6. Rotating prompts (JS array, random pick)
7. Swipe gesture support (optional, touch events)
