"""
app.py — Flask web UI for Filmlog.

Imports directly from the filmlog/ package. No logic lives here — only routes,
template context preparation, and the thin web layer.

Dev:   flask --app app run --debug --port 5001
Prod:  gunicorn -w 2 'app:app'
"""

import json
import re
from pathlib import Path

import markdown as md_lib
from flask import Flask, abort, redirect, render_template, request, url_for
from markupsafe import Markup
from dotenv import load_dotenv

from filmlog import db
from filmlog.browse import _apply_filters, _score_by_taste

load_dotenv(override=True)

app = Flask(__name__)

# Initialise DB on startup — creates all tables if they don't exist
db.init_db()

TASTE_MODEL_PATH = Path(__file__).parent / "taste_model.md"


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def _parse_film(film: dict) -> dict:
    """
    Parse JSON-encoded fields in a film dict and add computed display helpers.
    Safe to call repeatedly — idempotent.
    """
    f = dict(film)

    # default_taste_attributes → list of strings
    raw_attrs = f.get("default_taste_attributes") or "[]"
    if isinstance(raw_attrs, str):
        try:
            f["taste_attrs"] = json.loads(raw_attrs)
        except Exception:
            f["taste_attrs"] = []
    else:
        f["taste_attrs"] = raw_attrs or []

    # tmdb_genres → list of strings
    raw_genres = f.get("tmdb_genres") or "[]"
    if isinstance(raw_genres, str):
        try:
            f["genres"] = json.loads(raw_genres)
        except Exception:
            f["genres"] = []
    else:
        f["genres"] = raw_genres or []

    # Human-readable status label
    status = f.get("status")
    f["status_label"] = {
        "watched": "Watched",
        "watchlist": "Watchlist",
        "rewatch_candidate": "Rewatch",
    }.get(status, status or "—")

    return f


def _render_md(text: str) -> Markup:
    """Convert markdown text to safe HTML for Jinja2 templates."""
    return Markup(md_lib.markdown(text, extensions=["nl2br", "sane_lists"]))


def _extract_film_ids_from_text(text: str) -> list[int]:
    """Parse tmdb_id integers from a comma/space-separated string input."""
    if not text:
        return []
    ids = []
    for token in re.split(r"[\s,;]+", text.strip()):
        try:
            ids.append(int(token))
        except ValueError:
            pass
    return ids


def _strip_md(text: str) -> str:
    """Strip markdown syntax for plain-text previews."""
    t = text
    t = re.sub(r"^\*Originally published:.*?\*\n*", "", t)
    t = re.sub(r"^#{1,6}\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", t)
    t = re.sub(r"[…\.·]{3,}", "", t)
    t = re.sub(r"\n{2,}", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


app.jinja_env.filters["strip_md"] = _strip_md


def _get_stats() -> dict:
    """Load library stats for the nav bar. Returns zeros on error."""
    try:
        return db.get_library_stats()
    except Exception:
        return {
            "watched": 0,
            "watchlist": 0,
            "rewatch_candidate": 0,
            "total": 0,
            "taste_model_version": None,
        }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("films"))


@app.route("/films")
def films():
    # ── Filter params from query string ─────────────────────────────────────
    status_filter = request.args.get("status", "watchlist")
    language = request.args.get("language") or None
    era = request.args.get("era") or None
    length_filter = request.args.get("length") or None
    mood = request.args.get("mood") or None
    sort = request.args.get("sort", "taste")
    media_type = request.args.get("media_type") or None

    # ── Build base film pool ─────────────────────────────────────────────────
    try:
        if status_filter in ("watched", "watchlist", "rewatch_candidate"):
            raw_films = db.get_all_films_with_status(status_filter)
        else:
            raw_films = db.get_all_films_with_status()
    except Exception:
        raw_films = []

    total_count = len(raw_films)

    # ── Apply browse filters ─────────────────────────────────────────────────
    if media_type:
        if media_type == "series":
            raw_films = [f for f in raw_films if f.get("media_type") in ("tv", "anime")]
        else:
            raw_films = [f for f in raw_films if f.get("media_type") == media_type]

    films_list = _apply_filters(
        raw_films,
        language=language,
        mood=mood,
        length=length_filter,
        era=era,
    )

    # ── Sort / rank ──────────────────────────────────────────────────────────
    if sort == "taste":
        films_list = _score_by_taste(films_list)
    elif sort == "year":
        films_list.sort(key=lambda f: f.get("year") or 0, reverse=True)
    elif sort == "title":
        films_list.sort(key=lambda f: (f.get("title") or "").lower())
    elif sort == "added":
        films_list.sort(key=lambda f: f.get("date_added") or "", reverse=True)

    # ── Parse JSON fields for templates ─────────────────────────────────────
    films_list = [_parse_film(f) for f in films_list]

    ctx = dict(
        page="films",
        title="Films",
        stats=_get_stats(),
        films=films_list,
        total_count=total_count,
        current_status=status_filter,
        current_language=language or "",
        current_era=era or "",
        current_length=length_filter or "",
        current_sort=sort,
        current_media_type=media_type or "",
    )

    return render_template("films.html", **ctx)


@app.route("/films/<int:tmdb_id>")
def film_detail(tmdb_id: int):
    film = db.get_film(tmdb_id)
    if film is None:
        abort(404)

    film = _parse_film(film)
    user_film = db.get_user_film_by_tmdb(tmdb_id)

    # Linked recommendation issues that mention this film
    try:
        linked_recs = db.get_recommendations_for_film(tmdb_id)
    except Exception:
        linked_recs = []

    # Linked journal entries that mention this film
    try:
        linked_journals = db.get_journals_for_film(tmdb_id)
    except Exception:
        linked_journals = []

    return render_template(
        "film_detail.html",
        page="films",
        title=f"{film['title']} ({film.get('year', '')})",
        stats=_get_stats(),
        film=film,
        user_film=user_film,
        linked_recs=linked_recs,
        linked_journals=linked_journals,
    )


# ── HTMX partials: notes edit-in-place ──────────────────────────────────────

@app.route("/films/<int:tmdb_id>/notes/edit")
def notes_edit_form(tmdb_id: int):
    """Return the inline notes edit form (HTMX partial)."""
    user_film = db.get_user_film_by_tmdb(tmdb_id)
    notes = (user_film.get("personal_notes") or "") if user_film else ""
    return render_template("partials/notes_edit.html", tmdb_id=tmdb_id, notes=notes)


@app.route("/films/<int:tmdb_id>/notes/view")
def notes_view(tmdb_id: int):
    """Return the notes display view (HTMX partial — cancel)."""
    user_film = db.get_user_film_by_tmdb(tmdb_id)
    notes = (user_film.get("personal_notes") or "") if user_film else ""
    return render_template("partials/notes_view.html", tmdb_id=tmdb_id, notes=notes)


@app.route("/films/<int:tmdb_id>/notes", methods=["POST"])
def update_notes(tmdb_id: int):
    """Save notes and return updated display (HTMX partial)."""
    notes = request.form.get("personal_notes", "").strip()
    user_film = db.get_user_film_by_tmdb(tmdb_id)
    if user_film:
        db.update_user_film(user_film["id"], personal_notes=notes)
    return render_template("partials/notes_view.html", tmdb_id=tmdb_id, notes=notes)


# ── HTMX partial: status change ─────────────────────────────────────────────

@app.route("/films/<int:tmdb_id>/status", methods=["POST"])
def update_status(tmdb_id: int):
    """Change film status or toggle rewatch flag (HTMX partial)."""
    user_film = db.get_user_film_by_tmdb(tmdb_id)
    if not user_film:
        abort(404)

    new_status = request.form.get("status")
    rewatch_val = request.form.get("rewatch")

    if new_status and new_status in {"watched", "watchlist"}:
        db.update_user_film(user_film["id"], status=new_status)
    if rewatch_val is not None:
        db.update_user_film(user_film["id"], rewatch=int(rewatch_val))

    user_film = db.get_user_film_by_tmdb(tmdb_id)
    return render_template(
        "partials/status_control.html",
        tmdb_id=tmdb_id,
        current_status=user_film["status"],
        current_rewatch=user_film.get("rewatch", 0),
    )


# ── Tonight — mood calibration + recommendation generator ────────────────────

@app.route("/tonight")
def tonight():
    past_issues = db.get_recommendation_history(limit=5)
    return render_template(
        "tonight.html",
        page="tonight",
        title="Tonight",
        stats=_get_stats(),
        past_issues=past_issues,
    )


@app.route("/tonight/generate", methods=["POST"])
def tonight_generate():
    """HTMX endpoint: receive calibration answers, stream back the editorial."""
    from filmlog.recommend import generate_web_recommendation

    answers = {
        "watch_context": request.form.get("watch_context", "solo"),
        "mood": request.form.get("mood", "engaged"),
        "attention": request.form.get("attention", "full"),
        "ending": request.form.get("ending") or None,
    }
    if answers["ending"] == "none":
        answers["ending"] = None

    result = generate_web_recommendation(answers)

    # Fetch referenced films for the mini-poster strip
    film_cards = []
    for tmdb_id in result.get("films_referenced", [])[:4]:
        film = db.get_film(tmdb_id)
        if film:
            film_cards.append(film)

    return render_template(
        "partials/recommendation_result.html",
        **result,
        film_cards=film_cards,
    )


@app.route("/taste")
def taste_profile():
    stats = _get_stats()

    # All taste signals
    positive_attrs = db.get_taste_attributes(polarity="positive")
    negative_attrs = db.get_taste_attributes(polarity="negative")

    # Group positive by cluster for the cluster view
    clusters: dict[str, list] = {}
    general_attrs = []
    for attr in positive_attrs:
        cluster = attr.get("cluster")
        if cluster:
            clusters.setdefault(cluster, []).append(attr)
        else:
            general_attrs.append(attr)

    # Resolve evidence film titles for relational links
    all_ids: set[int] = set()
    for attr in positive_attrs + negative_attrs:
        all_ids.update(attr.get("evidence_film_ids", []))

    evidence_films: dict[int, dict] = {}
    for tmdb_id in all_ids:
        film = db.get_film(tmdb_id)
        if film:
            evidence_films[tmdb_id] = film

    # Taste model prose
    taste_model_text = ""
    if TASTE_MODEL_PATH.exists():
        taste_model_text = TASTE_MODEL_PATH.read_text(encoding="utf-8")

    # Cluster display names
    cluster_labels = {
        "solo_late_night": "Solo · late night",
        "family":          "Family",
        "partner":         "With partner",
        "friends":         "With friends",
        "weekend_afternoon": "Weekend afternoon",
    }

    return render_template(
        "taste.html",
        page="taste",
        title="Taste Profile",
        stats=stats,
        positive_attrs=positive_attrs,
        negative_attrs=negative_attrs,
        general_attrs=general_attrs,
        clusters=clusters,
        cluster_labels=cluster_labels,
        evidence_films=evidence_films,
        taste_model_text=taste_model_text,
    )


# ── Journal ──────────────────────────────────────────────────────────────────

@app.route("/journal")
def journal():
    entries = db.get_all_journal_entries(limit=50)
    return render_template(
        "journal.html",
        page="journal",
        title="Journal",
        stats=_get_stats(),
        entries=entries,
    )


@app.route("/api/films/search")
def api_film_search():
    """HTMX endpoint: search films by title, return partial HTML."""
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return ""
    results = db.search_films_local(q)[:10]
    return render_template("partials/film_search_results.html", films=results)


@app.route("/journal/new", methods=["GET", "POST"])
def journal_new():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        entry_type = request.form.get("type", "musing")
        raw_ids = request.form.get("linked_film_ids", "")
        raw_tags = request.form.get("tags", "")

        if not title or not content:
            return render_template(
                "journal_form.html",
                page="journal",
                title="New entry",
                stats=_get_stats(),
                entry=None,
                error="Title and content are required.",
                form_values=request.form,
                prelinked_films=[],
            )

        film_ids = _extract_film_ids_from_text(raw_ids)
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

        entry_id = db.create_journal_entry(
            title=title,
            content=content,
            entry_type=entry_type,
            linked_film_ids=film_ids,
            tags=tags,
        )
        return redirect(url_for("journal_entry", entry_id=entry_id))

    return render_template(
        "journal_form.html",
        page="journal",
        title="New entry",
        stats=_get_stats(),
        entry=None,
        error=None,
        form_values={},
        prelinked_films=[],
    )


@app.route("/journal/<int:entry_id>")
def journal_entry(entry_id: int):
    entry = db.get_journal_entry(entry_id)
    if entry is None:
        abort(404)

    # Resolve linked films
    linked_films = []
    for tmdb_id in entry.get("linked_film_ids", []):
        film = db.get_film(tmdb_id)
        if film:
            linked_films.append(_parse_film(film))

    return render_template(
        "journal_entry.html",
        page="journal",
        title=entry["title"],
        stats=_get_stats(),
        entry=entry,
        content_html=_render_md(entry["content"]),
        linked_films=linked_films,
    )


@app.route("/journal/<int:entry_id>/edit", methods=["GET", "POST"])
def journal_edit(entry_id: int):
    entry = db.get_journal_entry(entry_id)
    if entry is None:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        entry_type = request.form.get("type", "musing")
        raw_ids = request.form.get("linked_film_ids", "")
        raw_tags = request.form.get("tags", "")

        if not title or not content:
            return render_template(
                "journal_form.html",
                page="journal",
                title=f"Edit — {entry['title']}",
                stats=_get_stats(),
                entry=entry,
                error="Title and content are required.",
                form_values=request.form,
            )

        film_ids = _extract_film_ids_from_text(raw_ids)
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

        db.update_journal_entry(
            entry_id,
            title=title,
            content=content,
            type=entry_type,
            linked_film_ids=film_ids,
            tags=tags,
        )
        return redirect(url_for("journal_entry", entry_id=entry_id))

    # Resolve linked films for the picker pre-population
    prelinked_films = []
    for tmdb_id in entry.get("linked_film_ids", []):
        film = db.get_film(tmdb_id)
        if film:
            prelinked_films.append(film)

    return render_template(
        "journal_form.html",
        page="journal",
        title=f"Edit — {entry['title']}",
        stats=_get_stats(),
        entry=entry,
        error=None,
        form_values=entry,
        prelinked_films=prelinked_films,
    )


@app.route("/journal/<int:entry_id>/delete", methods=["POST"])
def journal_delete(entry_id: int):
    entry = db.get_journal_entry(entry_id)
    if entry is None:
        abort(404)
    db.delete_journal_entry(entry_id)
    return redirect(url_for("journal"))


# ── Full-text search ─────────────────────────────────────────────────────────

@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    results = {"films": [], "journals": []}
    if q and len(q) >= 2:
        raw = db.fts_search(q, limit=30)
        # Parse films
        for f in raw.get("films", []):
            results["films"].append(_parse_film(f))
        results["journals"] = raw.get("journals", [])

    return render_template(
        "search.html",
        page="search",
        title=f"Search — {q}" if q else "Search",
        stats=_get_stats(),
        q=q,
        results=results,
    )


# ── Settings ─────────────────────────────────────────────────────────────────

@app.route("/settings")
def settings():
    return render_template(
        "settings.html",
        page="settings",
        title="Settings",
        stats=_get_stats(),
    )


@app.route("/api/test-key", methods=["POST"])
def api_test_key():
    """Test an API key. Expects JSON body with {type, key}."""
    data = request.get_json(force=True, silent=True) or {}
    key_type = data.get("type")
    key = (data.get("key") or "").strip()

    if not key:
        return {"ok": False, "error": "No key provided"}, 400

    if key_type == "anthropic":
        try:
            import anthropic as _anthropic
            client = _anthropic.Anthropic(api_key=key)
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=5,
                messages=[{"role": "user", "content": "hi"}],
            )
            return {"ok": True, "message": "Anthropic key valid ✓"}
        except ImportError:
            return {"ok": False, "error": "anthropic package not installed"}, 500
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}, 400

    elif key_type == "tmdb":
        import requests as req
        try:
            r = req.get(
                "https://api.themoviedb.org/3/configuration",
                params={"api_key": key},
                timeout=6,
            )
            if r.ok:
                return {"ok": True, "message": "TMDB key valid ✓"}
            return {"ok": False, "error": f"TMDB returned {r.status_code}"}, 400
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}, 400

    return {"ok": False, "error": "Unknown key type"}, 400


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html", page=None, title="Not Found", stats=_get_stats()), 404


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5001)
