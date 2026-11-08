# Filmlog — Design Decisions & Context

This file captures the *why* behind decisions. CLAUDE.md describes *what* is built and *what's next*. Read both in a new session.

---

## About Prashil (the user)

Not a coder — thinks in product/UX terms. Explain technical concepts plainly. Values clear progress indicators, thoughtful defaults, and persistence of intelligence across sessions.

Film notes are scattered across Notion, Google Keep, and a published Substack. Prefers bulk import over manual entry.

Silent processes with no progress feedback are unacceptable. Any batch operation must show "N of M done" with elapsed time.

---

## The core problem

The gap between "want to watch" and "actually watching."

**The masterpiece avoidance trap:** Films like Schindler's List get mentally categorized as "need to be in the right mindframe" — but that mindframe never arrives. The tool must not reinforce this by grouping films by prestige/difficulty. It should find the bridge: "yes this is a great film, but here's why it works for tonight."

**Fun but not mindless:** Prashil's most common viewing state. He can never watch something truly mindless — it feels like a waste of time. But he also doesn't always want heavy engagement. The sweet spot is engaging-but-accessible.

**The Mubi/Netflix trap:** Had Mubi for 2 years, watched 2 films. Too much choice = watch nothing. Filmlog's answer: 2–3 films max with specific reasons why tonight.

**Measure every decision against:** "Does this make it more likely he actually watches something tonight?"

---

## Recommendation philosophy

Recommendations are NOT limited to the watchlist. Watchlist is one source among many. Claude should use its full intelligence (taste model, journal, hall of fame, anti-signals) to suggest any film — including ones Prashil has never heard of.

**The product is a personalized magazine issue.** Imagine coming home from work, picking up a magazine where the writer knows you deeply — your opinions, your journal entries, your taste profile. That writer recommends 2–3 films with real editorial prose. It's a Claude chat that happens to be about film.

### No quotas, ever
No language quotas, region quotas, or type-of-film quotas. If the top 3 are all Korean, that's correct. Variety comes from the recommendation methodology being good (the prompt instructs varied picks across tone/origin/energy), not from code-level boosts or artificial balancing. The 2–4 film recommendation spread being well-crafted IS the solution.

### Journal influence shouldn't bias recommendations
If Prashil writes extensively about artsy/Mubi films in the journal, recommendations shouldn't become overwhelmingly indie. Some indie presence is natural, but the pool shouldn't look biased toward what he writes about vs what he watches. The methodology being balanced solves this — not a separate stated-vs-revealed weighting system.

### Spoiler awareness
Some films are better watched blind — even the premise can be a spoiler. For these, sell the feeling and craft, not the premise. Never reveal plot turns, twists, or structural surprises. When in doubt, be vaguer.

### Rating is personal, not standardized
Stars are personal feeling (5 = loved, 4 = great but one thing held it back). Never compare ratings across films.

---

## Engagement, not cognitive load

The `cognitive_load` column needs reframing. Cognitive load is the wrong axis. A film like Mr. Nobody or Fight Club is technically high cognitive load, but you'd easily watch either because they hook you immediately.

**What matters:** How easy it is to get locked in once you press play — not how "hard" or "demanding" the film is intellectually.

- High engagement: locked in from the first scene (Fight Club, Mr. Nobody, Duel)
- Medium engagement: takes 15–20 mins to settle in but rewards attention
- Low engagement: slow build, requires patience and the right mood

This should be ONE attribute that informs the Tonight calibration ("how much of you is in the room"), not the primary sorting axis.

---

## Tonight page calibration

The original multi-question mood calibration was wrong. Prashil never decides based on genre, ending preference, or attention level. "Half watching" is never a real state — he's always fully engaged. Nobody sits down and decides "I want an open ending tonight."

**Current design:** Two inputs only — "who's watching" (solo/partner/family/friends) and an optional freeform text field. No other questions.

---

## Pre-cached recommendations

`precache_issues()` exists but must follow these rules:

- **Never surfaced proactively.** The "Generate issue" button click is the reward moment.
- Only serve a cached issue when: user clicks Generate, parameters are default (solo, no free text), and a cached issue exists.
- Add a **3-second delay** before revealing the cached result — makes it feel like it's being written, not instant.
- If parameters aren't default, always generate live.
- Never show "you have a recommendation waiting" or auto-surface on page load.

---

## Watched list is Hall of Fame only

The 137 "watched" films in the DB are films Prashil loved, not everything he's ever seen. He's watched far more. The taste model should be humble about what it "knows" — absence from the watched list doesn't mean he hasn't seen it.

When journal entries mention films with opinions (positive or negative), those films should be added to the DB as watched. Anti-signals from journal entries are valuable taste data.

---

## Film deletion and taste model

When a film is deleted, taste attributes are degraded proportionally — not deleted. Because `evidence_film_ids` is a sample (what Claude cited), not an exhaustive list. An attribute may be supported by many films but only cite 3–4 as examples. Removing one example doesn't invalidate the attribute.

If all cited evidence is gone, confidence drops to 0.15 (weakly supported). The next rescan makes the final call.

---

## Data quality notes

- **Wrong TMDB matches happen.** Film detail page has a correction flow (re-search TMDB, re-link).
- **Journal text quality:** Raw journal text has dictation errors, typos, broken grammar. A light Haiku cleanup pass may be needed — fix grammar/typos, preserve voice and meaning.
- **Character name collisions:** "Satya" in a journal entry about Jigra refers to a character, not the 1998 film. Context-aware matching needed.

---

## Future Vision & Multi-User Architecture (Phase 8)

### Local-First Architecture vs. Cloud Server
- **Can it live entirely on the user's phone? YES.** A Local-First PWA architecture using browser IndexedDB / SQLite-WASM stores the user's entire library, ratings, and notes on their device.
- **Why Local-First is ideal:**
  - 100% private: User notes and tastes never touch an external database.
  - $0 server cost: Zero infrastructure overhead for the project or the user.
  - Blazing speed: Sub-millisecond queries run on device.
  - Works offline: Browsing and logging works without internet; only Gemini API calls need network.
- **Trade-offs of Phone-Only:**
  - No automatic multi-device sync (iPhone ↔ Mac) without an export/import file or cloud sync bridge (e.g., iCloud/Google Drive backup).
  - Browser cache eviction risk mitigated by requesting persistent storage (`navigator.storage.persist()`) and providing one-click JSON backup/restore.

### Ephemeral Bring-Your-Own-Key (BYOK) for Visitors
- Visitors can input their personal Google Gemini API key, stored exclusively in their device's `localStorage`.
- When generating Tonight issues or recommendations, the server utilizes their key via temporary request headers (`X-Gemini-Key`).
- Generated issues are rendered and cached in the visitor's local browser storage, with zero writes to Prashil's server database.
- Owner API quota is 100% protected ($0 cost); owner taste model remains uncorrupted.

### Cold Start Onboarding (Hybrid Approach)
To eliminate blank-slate friction for new users:
- **Option A (Seed Vault):** Pre-populate 30 curated classic/Criterion cornerstone films from Prashil's vault. The user reviews the list and quickly checks which ones they've seen.
- **Option B (Cornerstone Calibration):** Pick and rate 5 favorite films immediately to establish Level 1.0 baseline taste vectors.
- Both options work together in the onboarding flow.

### Multimodal Ingestion Pipeline
- **Photo/Screenshot OCR:** Direct upload of handwritten notebook pages or Google Keep screenshots parsed by Gemini Flash Multimodal.
- **Raw Text Dump:** Messy, unpunctuated film lists and stream-of-consciousness rants parsed by Gemini to extract titles, dates, ratings, and sentiments.
- **Universal Importer:** One-click CSV ingestion for Letterboxd (`watched.csv`), IMDb exports, and Jellyfin API connections.

---

## Technical preferences

- **Don't use Chrome automation tools** (Claude in Chrome extension) for testing. They drain API usage heavily. Use curl or ask Prashil to check the browser.
- **Diagnose before brute-forcing.** When something fails, read the code and understand why before retrying. Never brute-force past interactive prompts.
- **Progress indicators always.** Any batch operation must show "N of M done" with elapsed time. CLI and web UI both.
