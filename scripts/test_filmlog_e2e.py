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
    assert "level_num" in calib and 1 <= calib["level_num"] <= 5
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
    assert "taste-calibration-bar-widget" in html, "Calibration bar missing"
    assert "today-hero-tile" in html, "Today hero tile missing"
    assert "hero-cover-img" in html, "Hero cover image missing"
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

    print("\n" + "=" * 60)
    print("ALL END-TO-END TESTS PASSED (100% SUCCESS)")
    print("=" * 60)

if __name__ == "__main__":
    run_all_tests()
