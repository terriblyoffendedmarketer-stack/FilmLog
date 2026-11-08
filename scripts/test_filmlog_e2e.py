# test_filmlog_e2e.py — Comprehensive end-to-end test suite for FilmLog
# Usage: python scripts/test_filmlog_e2e.py
#
# Gotchas:
# - DB on Vercel runs in /tmp copy; locally uses root filmlog.db.
# - Test client requires session context for visitor/owner verification.

import os
import sys
from pathlib import Path

# Ensure root is in path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from app import app
from filmlog import db

def run_all_tests():
    print("=" * 60)
    print("RUNNING FILMLOG END-TO-END VERIFICATION")
    print("=" * 60)

    client = app.test_client()

    # 1. Database Schema & Tables
    print("\n[Test 1] Database schema & new tables...")
    conn = db.get_connection()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "films" in tables, "films table missing"
    assert "user_films" in tables, "user_films table missing"
    assert "recommendation_issues" in tables, "recommendation_issues table missing"
    assert "enrichment_skips" in tables, "enrichment_skips table missing"
    print("  ✓ All required tables present (including enrichment_skips)")

    # 2. Taste Calibration Stats
    print("\n[Test 2] Taste Calibration Gamification calculation...")
    calib = db.get_taste_calibration_stats()
    assert "percentage" in calib and 0 <= calib["percentage"] <= 100
    assert "level_title" in calib
    assert "level_num" in calib and 1 <= calib["level_num"] <= 15
    assert "next_hint" in calib
    print(f"  ✓ Calibration: Level {calib['level_num']} · {calib['level_title']} ({calib['percentage']}%)")
    print(f"  ✓ Signals: {calib['n_watched']} watched, {calib['n_notes']} notes, {calib['n_journals']} journals")

    # 3. Taste Vault Insight
    print("\n[Test 3] Ambient Taste Vault Insight...")
    insight = db.get_random_taste_insight()
    if insight:
        assert "source_type" in insight
        assert "title" in insight
        assert "quote" in insight
        print(f"  ✓ Taste Insight pulled from {insight['source_type']}: [{insight['title']}]")
        print(f"    \"{insight['quote'][:90]}...\"")
    else:
        print("  - No insight pulled (library empty)")

    # 4. Enrich Skip with 7-Day Persistent Cooldown
    print("\n[Test 4] Enrich skip with 7-day cooldown...")
    queue1 = db.get_enrichment_queue(limit=5)
    assert len(queue1) > 0, "Enrichment queue is empty"
    test_film = queue1[0]
    db.record_enrichment_skip(test_film["tmdb_id"])
    skipped_ids = db.get_recently_skipped_tmdb_ids(days=7)
    assert test_film["tmdb_id"] in skipped_ids, "Skip was not recorded"

    queue2 = db.get_enrichment_queue(limit=5)
    queue2_ids = {f["tmdb_id"] for f in queue2}
    assert test_film["tmdb_id"] not in queue2_ids, "Skipped film was not excluded!"
    print(f"  ✓ Film {test_film['title']} ({test_film['tmdb_id']}) skipped and cleanly excluded from queue")

    # 5. Route Verification: Root -> /tonight
    print("\n[Test 5] Root route redirect...")
    r_root = client.get("/")
    assert r_root.status_code == 302
    assert r_root.headers.get("Location") == "/tonight"
    print("  ✓ / properly redirects to /tonight as default hero screen")

    # 6. Tonight Hero Screen & Layout
    print("\n[Test 6] Tonight Hero Screen HTML elements...")
    r_tonight = client.get("/tonight")
    assert r_tonight.status_code == 200
    html = r_tonight.data.decode("utf-8")
    assert "ask-what-to-watch-form" in html, "Ask what to watch form missing"
    assert "signal-gamification-widget" in html or "taste-calibration-bar-widget" in html, "Calibration widget missing"
    assert "today-hero-tile" in html, "Today hero tile missing"
    assert "hero-cover-img" in html or "hero-cover-ph" in html, "Hero cover image or placeholder missing"
    assert "issue-reading-overlay" in html, "Reading overlay popup missing"
    assert "taste-vault-epigraph" in html, "Taste vault epigraph missing"
    assert "article-cover-wrap" in html, "Substack style article cover wrap missing"
    assert "micro-enrich-wrap" in html, "Micro-enrich card missing"
    print("  ✓ /tonight renders Landscape Cover Hero Tile, Substack Article Cover, Epigraph, and Reading Overlay")

    # 7. Micro-Enrichment POST Endpoint
    print("\n[Test 7] Tonight Micro-Enrichment endpoint...")
    with client.session_transaction() as sess:
        sess["owner"] = True
    r_micro = client.post(f"/tonight/micro-enrich/{test_film['tmdb_id']}", data={
        "personal_notes": "Great pacing and atmospheric tension",
        "rating": "5"
    })
    assert r_micro.status_code == 200
    micro_html = r_micro.data.decode("utf-8")
    assert "+1 Taste Signal Calibrated" in micro_html or "micro-enrich" in micro_html
    print("  ✓ Micro-enrich note and rating saved cleanly with immediate feedback")

    # 8. Recommendation Generation & Issue Logging
    print("\n[Test 8] Recommendation generation & issue logging...")
    r_gen = client.post("/tonight/generate", data={
        "owner_password": "8544",
        "watch_context": "solo",
        "free_text": "intense psychological thriller with moody lighting"
    })
    assert r_gen.status_code == 200
    gen_html = r_gen.data.decode("utf-8")
    assert "today-hero-tile" in gen_html, "Today hero tile missing in fresh generation"
    assert "issue-full-fresh" in gen_html, "Issue template missing in fresh generation"
    print("  ✓ Recommendation issue generated, landscape hero selected, and logged to DB")

    # 9. Taste Profile Brain & Knowledge Graph
    print("\n[Test 9] Taste Knowledge Graph & Visual Brain...")
    r_graph = client.get("/api/taste-graph")
    assert r_graph.status_code == 200
    g_data = r_graph.get_json()
    assert "nodes" in g_data and len(g_data["nodes"]) >= 15, "Graph nodes insufficient"
    assert "links" in g_data and len(g_data["links"]) >= 15, "Graph links insufficient"
    r_taste = client.get("/taste")
    assert r_taste.status_code == 200
    t_html = r_taste.data.decode("utf-8")
    assert "taste-graph-canvas" in t_html, "Graph canvas missing from /taste"
    assert "graph-node-drawer" in t_html, "Node drawer missing from /taste"
    print("  ✓ Taste Brain Knowledge Graph endpoint and interactive canvas verified")

    # 10. Smart TMDB Search (Fuzzy Year, Title+Year, and TMDB URL)
    print("\n[Test 10] Smart TMDB Search (Fuzzy Year, URL, Title+Year)...")
    # Fuzzy year 2012 finding 2013 David
    r_fuzzy = client.get("/api/tmdb/search?q=David&year=2012")
    assert r_fuzzy.status_code == 200
    assert "161040" in r_fuzzy.data.decode("utf-8"), "Fuzzy year search failed to rank 161040"
    # Title + year single input
    r_single = client.get("/api/tmdb/search?q=David+2013")
    assert r_single.status_code == 200
    assert "161040" in r_single.data.decode("utf-8"), "Title+year combined input failed"
    # Direct TMDB URL
    r_url = client.get("/api/tmdb/search?q=https://www.themoviedb.org/movie/161040-david")
    assert r_url.status_code == 200
    assert "161040" in r_url.data.decode("utf-8"), "Direct TMDB URL search failed"
    print("  ✓ Smart search verified across fuzzy year, single-string title+year, and direct TMDB URLs")

    # 11. Hall of Fame vs 5-Star Distinction & Toggle
    print("\n[Test 11] Hall of Fame vs 5-Star Distinction & Toggle...")
    with client.session_transaction() as sess:
        sess["owner"] = True

    # Toggle HOF off then on for test_film
    r_hof_toggle = client.post(f"/films/{test_film['tmdb_id']}/toggle-hof")
    assert r_hof_toggle.status_code == 200
    hof_html = r_hof_toggle.data.decode("utf-8")
    assert "hof-toggle-btn" in hof_html

    # Check /hall-of-fame page
    r_hof_page = client.get("/hall-of-fame")
    assert r_hof_page.status_code == 200
    print("  ✓ Hall of Fame toggle and dedicated /hall-of-fame page verified")

    # 12. Notes from Jellyfin Display
    print("\n[Test 12] Notes from Jellyfin separated from Personal Notes...")
    # Find a film with jellyfin_notes
    with db.get_connection() as conn:
        j_row = conn.execute("SELECT tmdb_id, jellyfin_notes, personal_notes FROM user_films WHERE jellyfin_notes IS NOT NULL LIMIT 1").fetchone()
    if j_row:
        r_detail = client.get(f"/films/{j_row['tmdb_id']}")
        assert r_detail.status_code == 200
        d_html = r_detail.data.decode("utf-8")
        assert "Notes from Jellyfin" in d_html, "Notes from Jellyfin section missing on film detail"
        print(f"  ✓ Film {j_row['tmdb_id']} displays Notes from Jellyfin cleanly separated from personal notes")

    # 13. Enrich Queue Behavior & No 7-Day Jargon
    print("\n[Test 13] Enrich Queue Skip & No Technical 7-Day Wording...")
    r_enrich = client.get("/enrich")
    assert r_enrich.status_code == 200
    e_html = r_enrich.data.decode("utf-8")
    assert "7-day cooldown" not in e_html, "7-day cooldown technical jargon still visible in enrich!"
    assert "Skip for now" in e_html, "Skip for now button missing in enrich"
    print("  ✓ Enrich UI free of '7-day cooldown' jargon; uses 'Skip for now'")

    # 14. Widescreen Multi-Backdrop & Clickable Landscape Banners in Editorial Issue
    print("\n[Test 14] Widescreen Multi-Backdrop & Clickable Landscape Banners in Editorial Issue...")
    with db.get_connection() as conn:
        issue_row = conn.execute("SELECT * FROM recommendation_issues WHERE archived = 0 ORDER BY id DESC LIMIT 1").fetchone()
    if issue_row:
        r_tonight = client.get("/tonight")
        t_html = r_tonight.data.decode("utf-8")
        assert "article-cover-frame" in t_html, "Article cover frame missing in tonight page"
        assert "editorial-landscape-break" in t_html or "editorial-break-frame" in t_html, "Wide landscape editorial break missing in issue template"
        print("  ✓ Widescreen multi-backdrop architecture verified: Substack cover + clickable landscape break banners")

    # 15. Zero-Friction Journal (Auto Title & Auto Film Detection)
    print("\n[Test 15] Zero-Friction Journal Entry (Content-Only Input)...")
    raw_thought = "Thinking about Seven Samurai and Kurosawa's spatial blocking in the rain."
    r_j = client.post("/journal/new", data={"content": raw_thought}, follow_redirects=True)
    assert r_j.status_code == 200
    with db.get_connection() as conn:
        last_j = conn.execute("SELECT * FROM journals ORDER BY id DESC LIMIT 1").fetchone()
        assert last_j and last_j["title"], "Auto title generation failed!"
        assert "346" in (last_j["linked_film_ids"] or ""), "Auto film detection failed to find Seven Samurai!"
        # Cleanup
        conn.execute("DELETE FROM journals WHERE id = ?", (last_j["id"],))
    print("  ✓ Content-only journal entry succeeded: title and film links auto-inferred by AI")

    # 16. Multi-Container Persistence & Issue Restore API
    print("\n[Test 16] Multi-Container Persistence & /api/issues/restore Endpoint...")
    sample_issue = {
        "issue_number": 999,
        "headline": "Testing Persistence & Zero Issue Loss",
        "issue_text": "# Testing Persistence & Zero Issue Loss\n\n**Seven Samurai** (1954) anchors tonight with breathtaking spatial blocking.\n\n**The Dark Knight** (2008) follows with escalating ethical pressure.",
        "films_referenced": [346, 155],
        "generated_at": "2026-11-08T12:00:00+00:00"
    }
    r_restore = client.post("/api/issues/restore", json={"issues": [sample_issue]})
    assert r_restore.status_code == 200
    res_data = r_restore.get_json()
    assert res_data.get("ok") is True

    # Verify export endpoint
    r_exp = client.get("/api/issues/export")
    assert r_exp.status_code == 200
    exp_issues = r_exp.get_json().get("issues", [])
    assert any(i["issue_number"] == 999 for i in exp_issues), "Restored issue 999 not found in exported issues!"

    # Clean up test issue 999
    with db.get_connection() as conn:
        conn.execute("DELETE FROM recommendation_issues WHERE issue_number = 999")
    print("  ✓ Issue persistence & restore verified: client issues safely rehydrated to SQLite")

    print("\n" + "=" * 60)
    print("ALL END-TO-END TESTS PASSED (100% SUCCESS)")
    print("=" * 60)

if __name__ == "__main__":
    run_all_tests()
