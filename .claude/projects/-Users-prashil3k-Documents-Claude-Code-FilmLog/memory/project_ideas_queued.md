---
name: Queued feature ideas from first session
description: Feature ideas Prashil described in conversation — organized by category with his reasoning. These aren't hypothetical, they reflect real problems he faces.
type: project
---

## Dedicated views that don't become silos
Anime/K-drama get dedicated filter views (already built). But the Tonight recommendation page should ignore media_type entirely — pull from the full pool based on taste + mood. A Tuesday recommendation could be an anime, a Korean film, and an Indian thriller together because taste matched, not because of category.

## The masterpiece avoidance trap
Films like Schindler's List get mentally tagged as "need the right mindframe" — but that mindframe never comes. The sell must reframe: "this is actually more of a thriller than a history lesson." Never group by prestige. Never make "masterpiece" a category.

## The tired-but-not-mindless gradient
Prashil's most common state: tired, wants fun but never mindless. This is a narrow band. Mood calibration should detect energy level on a spectrum, not a binary. Map to `cognitive_load` per film.

## The Mubi/Netflix trap
Had Mubi for 2 years, watched 2 films. Too much choice = watch nothing. Filmlog's answer: 2-3 films max with specific reasons why tonight. Prioritize realistically watchable films, not aspirationally watchable ones.

## Journal bulk import
Prashil has film thoughts scattered across Notion, Google Keep, and published Substack. He wants to dump raw unstructured content and have the tool sort it into journal entries, auto-link films, extract tags. File goes in `data/raw/`.

## Watchlist as live candidate pool
Watchlist loses emotional spark the moment something gets added. Filmlog treats it as a live pool filtered through the taste model — every recommendation from it comes with a reason "why tonight."

## Phone access
Tailscale (recommended) or cloud deploy. Keep in mind during builds — don't hardcode localhost. Consider PWA manifest for mobile homescreen.
