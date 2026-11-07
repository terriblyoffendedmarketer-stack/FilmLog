# Learnings & Gotchas

- Vercel 404 on all Flask routes | Vercel rewrite `/(.*)` rewrote `PATH_INFO` to destination path `/api/index` which Flask routes didn't match | Created `VercelPathMiddleware` in `app.py` and rewrote to `/api/index.py/$1` to cleanly restore original request path
- Enrich cycling between two films (Maidaan & Jawan) | `exclude_tmdb_id` only excluded the single immediate film, causing random pick between remaining 5-star films to bounce between the same two | Implemented session-level `seen_enrich` tracking plus persistent `enrichment_skips` table with 7-day cooldown
- Static CSS/assets 404 on Vercel | Vercel serverless Python container doesn't serve Flask static assets via CDN unless placed in root `public/` directory | Created `public/` and `public/static/` mirroring CSS, JS, manifest, and icons with zero CDN latency
- Query form hidden on Tonight | `{% if is_owner %}` guarded the entire query form, rendering the page as non-interactive past issues for any unauthenticated visitor | Made query bar always visible for everyone, with seamless inline owner password entry (`8544`) that authenticates and generates in one click
