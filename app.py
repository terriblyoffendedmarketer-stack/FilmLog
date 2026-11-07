"""
app.py — Flask web UI for Filmlog.

Imports directly from the filmlog/ package. No logic lives here — only routes,
template context preparation, and the thin web layer.

Dev:   flask --app app run --debug --port 5001
Prod:  gunicorn -w 2 'app:app'
"""

import json
import os
import re
from functools import wraps
from pathlib import Path

import markdown as md_lib
from flask import Flask, abort, redirect, render_template, request, session, url_for
from markupsafe import Markup
from dotenv import load_dotenv

from filmlog import db, tmdb, attributes as attr_module
from filmlog.browse import _apply_filters, _score_by_taste

load_dotenv(override=True)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(32).hex())
app.permanent_session_lifetime = 60 * 60 * 24 * 365  # 1 year


# Middleware to handle Vercel serverless / proxy prefix rewriting
class VercelPathMiddleware:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        for prefix in ["/app.py", "/app", "/api/index.py", "/api/index"]:
            if path == prefix:
                path = "/"
                break
            elif path.startswith(prefix + "/"):
                path = path[len(prefix):]
                break

        if not path or not path.startswith("/"):
            path = "/" + path

        environ["PATH_INFO"] = path
        environ["SCRIPT_NAME"] = ""
        return self.wsgi_app(environ, start_response)


app.wsgi_app = VercelPathMiddleware(app.wsgi_app)


# ---------------------------------------------------------------------------
# Auth — read-only for visitors, full access for owner
# ---------------------------------------------------------------------------

def _is_owner() -> bool:
    """Check if the current session belongs to the owner."""
    return session.get("owner") is True


@app.before_request
def _set_visitor_flag():
    """Set visitor mode flag. Visitors can interact — their changes go to localStorage."""
    from flask import g
    g.visitor_mode = not _is_owner()


@app.context_processor
def _inject_auth():
    """Make is_owner available in all templates."""
    return {"is_owner": _is_owner()}

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

    # One-liner tagline from overview (first sentence, capped)
    overview = f.get("overview") or ""
    if overview:
        # Take first sentence (up to first period, question mark, or exclamation)
        first_sentence = re.split(r'(?<=[.!?])\s', overview, maxsplit=1)[0]
        if len(first_sentence) > 85:
            first_sentence = first_sentence[:82].rsplit(" ", 1)[0] + "…"
        f["tagline"] = first_sentence
    else:
        f["tagline"] = ""

    return f


def _render_md(text: str) -> Markup:
    """Convert markdown text to safe HTML for Jinja2 templates."""
    return Markup(md_lib.markdown(text, extensions=["nl2br", "sane_lists"]))


def _inject_inline_posters(html: str, film_cards: list[dict]) -> Markup:
    """
    Replace the first <strong>Title</strong> occurrence for each recommended film
    with a small inline poster float + the bold title. Creates the visual pull
    of seeing the poster in context with the sell text.
    """
    if not film_cards:
        return html

    for film in film_cards:
        title = film.get("title", "")
        poster = film.get("poster_url", "")
        tmdb_id = film.get("tmdb_id", "")
        if not title:
            continue

        bold_pattern = re.compile(
            r"<strong>" + re.escape(title) + r"</strong>", re.IGNORECASE
        )

        if poster:
            replacement = (
                f'<span class="inline-poster-wrap">'
                f'<a href="/films/{tmdb_id}" class="inline-poster">'
                f'<img src="{poster}" alt="{title}" loading="lazy">'
                f'</a>'
                f'<strong>{title}</strong>'
                f'</span>'
            )
        else:
            replacement = f"<strong>{title}</strong>"

        html = bold_pattern.sub(replacement, html, count=1)

    return Markup(html)


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

@app.route("/login", methods=["GET", "POST"])
def login():
    """Owner login — sets session cookie."""
    if request.method == "POST":
        password = request.form.get("password", "")
        correct = os.getenv("FILMLOG_PASSWORD", "")
        if correct and password == correct:
            session["owner"] = True
            session.permanent = True
            return redirect(request.args.get("next", "/"))
        return render_template("login.html", error="Wrong password.", stats=_get_stats())
    return render_template("login.html", stats=_get_stats())


@app.route("/logout")
def logout():
    session.pop("owner", None)
    return redirect("/films")


@app.route("/sw.js")
def service_worker():
    """Serve the service worker from root so it can control the full scope."""
    return app.send_static_file("sw.js"), 200, {
        "Content-Type": "application/javascript",
        "Service-Worker-Allowed": "/",
    }

@app.route("/")
def index():
    return redirect("/tonight")


# ---------------------------------------------------------------------------
# Enrichment — default landing page
# ---------------------------------------------------------------------------

import random

ENRICH_PLACEHOLDERS = [
    "What did you love about this?",
    "What stayed with you after watching?",
    "Who would you recommend this to?",
    "What mood were you in when you watched it?",
    "Any scenes that stuck with you?",
    "Would you rewatch this? Why or why not?",
    "What made this one stand out?",
    "How did it make you feel?",
]


@app.route("/enrich")
def enrich():
    seen = list(session.get("seen_enrich", []))
    queue = db.get_enrichment_queue(limit=1, exclude_ids=seen)
    if not queue and seen:
        seen = []
        session["seen_enrich"] = []
        queue = db.get_enrichment_queue(limit=1)

    film = queue[0] if queue else None
    if film and film["tmdb_id"] not in seen:
        seen.append(film["tmdb_id"])
        session["seen_enrich"] = seen
    remaining = db.get_enrichment_count()
    placeholder = random.choice(ENRICH_PLACEHOLDERS)

    return render_template(
        "enrich.html",
        page="enrich",
        title="Enrich",
        stats=_get_stats(),
        film=film,
        remaining=remaining,
        placeholder=placeholder,
    )


@app.route("/enrich/<int:tmdb_id>", methods=["POST"])
def enrich_save(tmdb_id: int):
    """Save notes from enrichment card, return next card."""
    from flask import g
    notes = request.form.get("personal_notes", "").strip()
    user_film = db.get_user_film_by_tmdb(tmdb_id)
    if user_film and notes and not g.get("visitor_mode"):
        db.update_user_film(user_film["id"], personal_notes=notes)

    seen = list(session.get("seen_enrich", []))
    if tmdb_id not in seen:
        seen.append(tmdb_id)
    session["seen_enrich"] = seen

    queue = db.get_enrichment_queue(limit=1, exclude_ids=seen)
    if not queue:
        seen = [tmdb_id]
        session["seen_enrich"] = seen
        queue = db.get_enrichment_queue(limit=1, exclude_ids=seen)

    film = queue[0] if queue else None
    if film and film["tmdb_id"] not in seen:
        seen.append(film["tmdb_id"])
        session["seen_enrich"] = seen

    remaining = db.get_enrichment_count()
    placeholder = random.choice(ENRICH_PLACEHOLDERS)

    return render_template(
        "partials/enrich_card.html",
        film=film,
        remaining=remaining,
        placeholder=placeholder,
    )


@app.route("/enrich/<int:tmdb_id>/skip", methods=["POST"])
def enrich_skip(tmdb_id: int):
    """Skip without saving, return next card (advancing through the entire queue)."""
    seen = list(session.get("seen_enrich", []))
    if tmdb_id not in seen:
        seen.append(tmdb_id)
    session["seen_enrich"] = seen

    queue = db.get_enrichment_queue(limit=1, exclude_ids=seen)
    if not queue:
        seen = [tmdb_id]
        session["seen_enrich"] = seen
        queue = db.get_enrichment_queue(limit=1, exclude_ids=seen)

    film = queue[0] if queue else None
    if film and film["tmdb_id"] not in seen:
        seen.append(film["tmdb_id"])
        session["seen_enrich"] = seen

    remaining = db.get_enrichment_count()
    placeholder = random.choice(ENRICH_PLACEHOLDERS)

    return render_template(
        "partials/enrich_card.html",
        film=film,
        remaining=remaining,
        placeholder=placeholder,
    )


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


# ── Film correction: swap wrong TMDB match ─────────────────────────────────

@app.route("/films/<int:tmdb_id>/correct", methods=["POST"])
def correct_film(tmdb_id: int):
    """Swap a wrong TMDB match with the correct one. Owner only."""
    from flask import g
    if g.get("visitor_mode"):
        return redirect(url_for("film_detail", tmdb_id=tmdb_id))
    new_tmdb_id = int(request.form.get("new_tmdb_id", 0))
    media_type = request.form.get("media_type", "movie")
    if not new_tmdb_id:
        abort(400)

    # Fetch the correct film from TMDB and save it
    try:
        if media_type == "tv":
            details = tmdb.fetch_tv_data(new_tmdb_id)
        else:
            details = tmdb.fetch_film_data(new_tmdb_id)
        db.upsert_film(**details)
    except Exception:
        abort(500)

    # Swap the old tmdb_id to the new one across all tables
    db.swap_film_tmdb_id(tmdb_id, new_tmdb_id)

    return redirect(url_for("film_detail", tmdb_id=new_tmdb_id))


# ── Film delete ────────────────────────────────────────────────────────────

@app.route("/films/<int:tmdb_id>/delete", methods=["POST"])
def delete_film(tmdb_id: int):
    """Remove a film from the library entirely. Owner only."""
    from flask import g
    if g.get("visitor_mode"):
        return redirect(url_for("film_detail", tmdb_id=tmdb_id))
    film = db.get_film(tmdb_id)
    if not film:
        abort(404)
    db.delete_film(tmdb_id)
    return redirect(url_for("films"))


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
    from flask import g
    notes = request.form.get("personal_notes", "").strip()
    user_film = db.get_user_film_by_tmdb(tmdb_id)
    if user_film and not g.get("visitor_mode"):
        db.update_user_film(user_film["id"], personal_notes=notes)
    return render_template("partials/notes_view.html", tmdb_id=tmdb_id, notes=notes)


# ── HTMX partial: status change ─────────────────────────────────────────────

@app.route("/films/<int:tmdb_id>/status", methods=["POST"])
def update_status(tmdb_id: int):
    """Change film status or toggle rewatch flag (HTMX partial)."""
    user_film = db.get_user_film_by_tmdb(tmdb_id)
    if not user_film:
        abort(404)

    from flask import g
    new_status = request.form.get("status")
    rewatch_val = request.form.get("rewatch")

    if not g.get("visitor_mode"):
        if new_status and new_status in {"watched", "watchlist"}:
            db.update_user_film(user_film["id"], status=new_status)
        if rewatch_val is not None:
            rewatch_int = int(rewatch_val)
            db.update_user_film(user_film["id"], rewatch=rewatch_int)
            if rewatch_int:
                db.update_user_film(user_film["id"], status="watched")

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
    past_issues = db.get_recommendation_history(limit=10)
    # Pre-render markdown and resolve film posters for past issues
    for issue in past_issues:
        film_cards = []
        for tmdb_id in issue.get("films_referenced", [])[:4]:
            film = db.get_film(tmdb_id)
            if film:
                film_cards.append(_parse_film(film))
        raw_html = _render_md(issue.get("issue_text", ""))
        issue["issue_html"] = _inject_inline_posters(raw_html, film_cards)
        issue["film_cards"] = film_cards

    latest_issue = past_issues[0] if past_issues else None
    hero_film = None
    companion_films = []
    if latest_issue and latest_issue.get("film_cards"):
        hero_film = latest_issue["film_cards"][0]
        companion_films = latest_issue["film_cards"][1:4]

    older_issues = past_issues[1:] if len(past_issues) > 1 else []

    return render_template(
        "tonight.html",
        page="tonight",
        title="Tonight",
        stats=_get_stats(),
        latest_issue=latest_issue,
        hero_film=hero_film,
        companion_films=companion_films,
        older_issues=older_issues,
        past_issues=past_issues,
    )


@app.route("/tonight/generate", methods=["POST"])
def tonight_generate():
    """HTMX endpoint: receive calibration answers, stream back the editorial."""
    from flask import g
    owner_pwd = request.form.get("owner_password", "").strip()
    if owner_pwd and owner_pwd == os.getenv("FILMLOG_PASSWORD", "8544"):
        session["owner"] = True
        session.permanent = True
        g.visitor_mode = False
    elif g.get("visitor_mode"):
        return '<div class="rec-result-error"><p>Enter the owner password (8544) above to ask FilmLog.</p></div>'

    from filmlog.recommend import generate_web_recommendation

    answers = {
        "watch_context": request.form.get("watch_context", "solo") or "solo",
        "free_text": request.form.get("free_text", "").strip(),
    }

    result = generate_web_recommendation(answers)

    # Auto-archive: keep only the 2 most recent issues visible
    db.auto_archive_issues(keep=2)

    # Fetch referenced films for inline posters + hero pick + companion doors
    film_cards = []
    for tmdb_id in result.get("films_referenced", [])[:4]:
        film = db.get_film(tmdb_id)
        if film:
            film_cards.append(_parse_film(film))

    hero_film = film_cards[0] if film_cards else None
    companion_films = film_cards[1:4] if len(film_cards) > 1 else []

    # Render editorial markdown then inject inline posters beside bold titles
    issue_html = ""
    if result.get("issue_text"):
        raw_html = _render_md(result["issue_text"])
        issue_html = _inject_inline_posters(raw_html, film_cards)

    return render_template(
        "partials/recommendation_result.html",
        issue_html=issue_html,
        film_cards=film_cards,
        hero_film=hero_film,
        companion_films=companion_films,
        issue_number=result.get("issue_number"),
        error=result.get("error"),
        tier2=result.get("tier2", False),
    )


@app.route("/tonight/cached")
def tonight_cached():
    """Check if a recent pre-cached issue exists for instant reveal."""
    issues = db.get_recommendation_history(limit=1, include_archived=False)
    if not issues:
        return "", 204  # No content

    issue = issues[0]
    # Only serve cached if it's less than 48 hours old
    from datetime import datetime, timezone
    try:
        gen_time = datetime.fromisoformat(issue["generated_at"].replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - gen_time).total_seconds() / 3600
        if age_hours > 48:
            return "", 204
    except Exception:
        pass

    # Render the issue
    film_cards = []
    for tmdb_id in issue.get("films_referenced", [])[:4]:
        film = db.get_film(tmdb_id)
        if film:
            film_cards.append(film)

    raw_html = _render_md(issue.get("issue_text", ""))
    issue_html = _inject_inline_posters(raw_html, film_cards)

    return render_template(
        "partials/recommendation_result.html",
        issue_html=issue_html,
        film_cards=film_cards,
        issue_number=issue.get("issue_number"),
        cached=True,
    )


@app.route("/tonight/rewrite/<int:issue_id>", methods=["POST"])
def tonight_rewrite(issue_id: int):
    """HTMX endpoint: rewrite a past issue with the current prompt, same films."""
    from flask import g
    if g.get("visitor_mode"):
        return ""
    from filmlog.recommend import rewrite_issue

    result = rewrite_issue(issue_id)

    if result.get("error"):
        return f'<div class="rec-result-error"><p>{result["error"]}</p></div>'

    # Re-fetch the updated issue for rendering
    history = db.get_recommendation_history(limit=50)
    issue = next((i for i in history if i["id"] == issue_id), None)
    if not issue:
        return '<div class="rec-result-error"><p>Issue not found after rewrite.</p></div>'

    film_cards = []
    for tmdb_id in issue.get("films_referenced", [])[:4]:
        film = db.get_film(tmdb_id)
        if film:
            film_cards.append(film)

    raw_html = _render_md(result["issue_text"])
    issue_html = _inject_inline_posters(raw_html, film_cards)

    return render_template(
        "partials/recommendation_result.html",
        issue_html=issue_html,
        film_cards=film_cards,
        issue_number=issue.get("issue_number"),
        tier2=False,
        error=None,
    )


@app.route("/tonight/archive/<int:issue_id>", methods=["POST"])
def tonight_archive(issue_id: int):
    """HTMX endpoint: archive a past issue."""
    from flask import g
    if g.get("visitor_mode"):
        return ""
    db.archive_issue(issue_id)
    return ""


@app.route("/tonight/unarchive/<int:issue_id>", methods=["POST"])
def tonight_unarchive(issue_id: int):
    """HTMX endpoint: restore an archived issue."""
    from flask import g
    if g.get("visitor_mode"):
        return ""
    db.unarchive_issue(issue_id)
    return ""


@app.route("/tonight/archived")
def tonight_archived():
    """View archived recommendation issues."""
    archived = db.get_archived_issues(limit=50)
    for issue in archived:
        film_cards = []
        for tmdb_id in issue.get("films_referenced", [])[:4]:
            film = db.get_film(tmdb_id)
            if film:
                film_cards.append(film)
        raw_html = _render_md(issue.get("issue_text", ""))
        issue["issue_html"] = _inject_inline_posters(raw_html, film_cards)
        issue["film_cards"] = film_cards
    return render_template(
        "tonight_archived.html",
        page="tonight",
        title="Archived Issues",
        stats=_get_stats(),
        archived_issues=archived,
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
    from flask import g
    entries = db.get_all_journal_entries(limit=50, public_only=g.get("visitor_mode", False))
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
    from flask import g
    if g.get("visitor_mode"):
        return redirect(url_for("journal"))
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
    from flask import g
    entry = db.get_journal_entry(entry_id)
    if entry is None:
        abort(404)
    if g.get("visitor_mode") and not entry.get("public"):
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
    from flask import g
    if g.get("visitor_mode"):
        return redirect(url_for("journal_entry", entry_id=entry_id))
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


@app.route("/journal/<int:entry_id>/toggle-public", methods=["POST"])
def journal_toggle_public(entry_id: int):
    """Toggle public/private visibility for a journal entry. Owner only."""
    from flask import g
    if g.get("visitor_mode"):
        return "", 403
    entry = db.get_journal_entry(entry_id)
    if entry is None:
        abort(404)
    new_val = 0 if entry.get("public") else 1
    db.toggle_journal_public(entry_id, new_val)
    label = "🌐 Public" if new_val else "🔒 Private"
    active = " btn-public-active" if new_val else ""
    return f'''<button class="btn-ghost btn-sm{active}"
              hx-post="/journal/{entry_id}/toggle-public"
              hx-target="this"
              hx-swap="outerHTML">{label}</button>'''


@app.route("/journal/<int:entry_id>/delete", methods=["POST"])
def journal_delete(entry_id: int):
    from flask import g
    if g.get("visitor_mode"):
        return redirect(url_for("journal"))
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


# ── Unresolved imports ──────────────────────────────────────────────────────

@app.route("/unresolved")
def unresolved():
    items = db.get_unresolved(resolved=False)
    return render_template(
        "unresolved.html",
        page="unresolved",
        title="Unresolved imports",
        stats=_get_stats(),
        items=items,
    )


@app.route("/unresolved/<int:item_id>/resolve", methods=["POST"])
def resolve_item(item_id: int):
    """Resolve an unresolved import by adding the matched film to the library."""
    tmdb_id = int(request.form.get("tmdb_id", 0))
    media_type = request.form.get("media_type", "movie")
    skip_add = request.form.get("skip_add")

    if not tmdb_id:
        abort(400)

    if not skip_add:
        # Fetch from TMDB and add to library
        try:
            if media_type == "tv":
                details = tmdb.fetch_tv_data(tmdb_id)
            else:
                details = tmdb.fetch_film_data(tmdb_id)
            db.upsert_film(**details)
        except Exception:
            abort(500)

        # Add as watchlist if not already in user_films
        if not db.get_user_film_by_tmdb(tmdb_id):
            db.add_user_film(tmdb_id=tmdb_id, status="watchlist", source="resolved_import")

    db.mark_resolved(item_id)

    # Return empty div — card disappears via hx-swap="outerHTML"
    return '<div class="unresolved-resolved">✓ Resolved</div>'


@app.route("/unresolved/<int:item_id>/dismiss", methods=["POST"])
def dismiss_item(item_id: int):
    """Dismiss an unresolved import (mark as resolved without adding)."""
    db.mark_resolved(item_id)
    return ""


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


# ── Add Film ─────────────────────────────────────────────────────────────────

@app.route("/add")
def add_film_page():
    return render_template(
        "add_film.html",
        page="add",
        title="Add a film",
        stats=_get_stats(),
    )


@app.route("/api/tmdb/search")
def api_tmdb_search():
    """HTMX endpoint: search TMDB for films/TV, return HTML results."""
    q = request.args.get("q", "").strip()
    media = request.args.get("media_type", "movie")
    correct_for = request.args.get("correct_for", type=int)
    resolve_for = request.args.get("resolve_for", type=int)
    if not q or len(q) < 2:
        return ""
    try:
        if media == "tv":
            results = tmdb.search_tv(q)[:8]
        else:
            results = tmdb.search_multi(q)[:8]
    except Exception:
        results = []

    # Mark which ones are already in the local DB
    for r in results:
        r["in_db"] = db.get_film(r["tmdb_id"]) is not None

    if resolve_for:
        return render_template("partials/resolve_results.html", results=results, resolve_for=resolve_for)
    return render_template("partials/tmdb_search_results.html", results=results, correct_for=correct_for)


@app.route("/add/confirm/<int:tmdb_id>")
def add_film_confirm(tmdb_id: int):
    """Show the add/log form for a specific TMDB film, fetching details if needed."""
    media_type = request.args.get("media_type", "movie")

    # Check if already in local DB
    film = db.get_film(tmdb_id)

    if not film:
        # Fetch from TMDB
        try:
            if media_type == "tv":
                details = tmdb.fetch_tv_data(tmdb_id)
            else:
                details = tmdb.fetch_film_data(tmdb_id)
            # Save to films table
            db.upsert_film(**details)
            film = db.get_film(tmdb_id)
        except Exception as e:
            abort(404)

    film = _parse_film(film)
    user_film = db.get_user_film_by_tmdb(tmdb_id)

    return render_template(
        "add_film_confirm.html",
        page="add",
        title=f"Log — {film['title']}",
        stats=_get_stats(),
        film=film,
        user_film=user_film,
    )


@app.route("/add/save", methods=["POST"])
def add_film_save():
    """Save/update a film log entry with status, rating, and notes."""
    from flask import g
    if g.get("visitor_mode"):
        return redirect(url_for("films"))

    tmdb_id = int(request.form.get("tmdb_id", 0))
    status = request.form.get("status", "watched")
    rating_str = request.form.get("rating", "")
    notes = request.form.get("personal_notes", "").strip()
    watch_context = request.form.get("watch_context") or None

    rating = int(rating_str) if rating_str and rating_str.isdigit() else None

    film = db.get_film(tmdb_id)
    if not film:
        abort(404)

    user_film = db.get_user_film_by_tmdb(tmdb_id)
    if user_film:
        # Update existing
        db.update_user_film(
            user_film["id"],
            status=status,
            rating=rating,
            personal_notes=notes,
            watch_context=watch_context,
        )
    else:
        # New entry
        db.add_user_film(
            tmdb_id=tmdb_id,
            status=status,
            personal_notes=notes or None,
            watch_context=watch_context,
            source="manual",
            rating=rating,
        )

    # Infer taste attributes if missing (non-blocking — Haiku is fast)
    film_data = db.get_film(tmdb_id)
    if film_data:
        existing_attrs = film_data.get("default_taste_attributes")
        if not existing_attrs or existing_attrs in ("[]", "null"):
            try:
                attr_module.infer_single(tmdb_id)
            except Exception:
                pass  # Don't block save on attribute inference failure

    return redirect(url_for("film_detail", tmdb_id=tmdb_id))


# ── Hall of Fame ─────────────────────────────────────────────────────────────

@app.route("/hall-of-fame")
def hall_of_fame():
    try:
        hof_films = db.get_hall_of_fame()
    except Exception:
        hof_films = []

    hof_films = [_parse_film(f) for f in hof_films]

    return render_template(
        "hall_of_fame.html",
        page="hall_of_fame",
        title="Hall of Fame",
        stats=_get_stats(),
        films=hof_films,
    )


# ── Film detail: rating update (HTMX) ───────────────────────────────────────

@app.route("/films/<int:tmdb_id>/rating", methods=["POST"])
def update_rating(tmdb_id: int):
    """Update film rating via HTMX."""
    user_film = db.get_user_film_by_tmdb(tmdb_id)
    if not user_film:
        abort(404)

    from flask import g
    rating_str = request.form.get("rating", "")
    rating = int(rating_str) if rating_str and rating_str.isdigit() else None
    if not g.get("visitor_mode"):
        db.update_user_film(user_film["id"], rating=rating)

    return render_template(
        "partials/rating_control.html",
        tmdb_id=tmdb_id,
        current_rating=rating,
    )


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
