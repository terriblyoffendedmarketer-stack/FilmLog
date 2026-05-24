---
name: Watched list is Hall of Fame only, not full history
description: The DB's "watched" films are only ones Prashil loved, not everything he's seen. The taste model should account for this incompleteness.
type: feedback
---

The 126 "watched" films in the DB are Hall of Fame / loved films only. Prashil has seen far more films than this. The taste model and recommendations should be humble about what they "know" — absence from the watched list doesn't mean he hasn't seen it.

**Why:** Journal entries reference films (like All of Us Are Dead, Hellbound) that Prashil clearly watched but aren't in the DB because they weren't Hall of Fame material.

**How to apply:**
- When journal entries mention films with opinions (positive or negative), those films should be added to the DB as watched
- Anti-signals from journal entries (films he watched and didn't like) are valuable taste data
- Don't assume unwatched just because a film isn't in user_films
