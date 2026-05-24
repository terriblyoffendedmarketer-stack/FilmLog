---
name: Journal entries need film linking expansion and AI cleanup
description: Action items for journal quality — add unlinked films to DB, sanitize text, fix context-aware matching
type: project
---

## Unlinked films in journal entries
Films that are the primary subject of a journal entry but aren't in the 503-film DB get silently skipped. Example: "All of Us Are Dead" and "Hellbound" are the main subjects of their entry but have no linked_film_ids because they weren't in the DB.

**Fix:** Scan all journal entries for film/show mentions not in the DB. Look up on TMDB, add to films table (as watched, since Prashil clearly saw them), and update linked_film_ids. This is a one-time bulk operation + should happen automatically on future journal imports.

## AI cleanup pass on journal content
Raw journal text has bad dictation, typos, broken grammar from rough drafts. Needs a light Claude Haiku pass — fix grammar/typos, preserve voice and meaning. Not a rewrite.

## Context-aware film matching
Character names can collide with film titles. "Satya" in the Jigra entry refers to a character, not the 1998 film Satya. The common-word filter partially handles this but named characters need awareness of which films they appear in.

## Wrong TMDB matches (data quality)
The Lunchbox (2013, Irrfan Khan, Indian) was matched to a Japanese film "Lunch Box" (2004). Need a "wrong film?" correction flow in the film detail UI — re-search TMDB and re-link.

**Why:** Prashil's Hall of Fame films were imported first, so incorrect matches on beloved films are especially bad. The system should have caught this because Lunchbox was in the Indian films list.
