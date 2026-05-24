"""
recommend.py — mood calibration + recommendation issue generation.

Flow:
  1. Load taste model from taste_model.md + taste_attributes from DB
  2. 2–3 indirect mood calibration questions (feel like a conversation)
  3. Determine active cluster + tonight's context from answers
  4. Score full watchlist → select 5–8 best candidates
  5. Call Claude Opus → 300–400 word editorial recommendation issue
  6. Display + save to recommendation_issues table

Tier 2 fallback (no live API):
  Use stored taste_attributes to rank watchlist by attribute overlap.
  Present as a simple ranked list — correct picks, no prose.
"""

import os
import json
from pathlib import Path
from datetime import datetime, timezone

import click
from dotenv import load_dotenv

from filmlog import db
from filmlog.model import recommend_from_model

load_dotenv(override=True)

TASTE_MODEL_PATH = Path(__file__).parent.parent / "taste_model.md"


# ---------------------------------------------------------------------------
# Mood calibration
# ---------------------------------------------------------------------------

def _ask_calibration_questions() -> dict:
    """
    2–3 indirect questions that reveal tonight's context without asking
    directly about genre or mood. Returns a context dict.
    """
    answers = {}

    click.echo()

    # ── Q1: Who's watching ───────────────────────────────────────────────────
    click.echo("Quick one — who's watching tonight?")
    click.echo("  1. Just me")
    click.echo("  2. With my partner")
    click.echo("  3. With family")
    click.echo("  4. With friends")
    q1 = click.prompt("  ", default="1").strip()

    watch_map = {"1": "solo", "2": "partner", "3": "family", "4": "friends"}
    answers["watch_context"] = watch_map.get(q1, "solo")

    click.echo()

    # ── Q2: Mood/energy — adapts to context ─────────────────────────────────
    ctx = answers["watch_context"]

    if ctx == "solo":
        click.echo("What kind of night is it?")
        click.echo("  1. I want to feel something — a film that stays with me")
        click.echo("  2. I want to switch off — entertained but not challenged")
        click.echo("  3. Somewhere in between")
        q2 = click.prompt("  ", default="1").strip()
        mood_map = {"1": "engaged", "2": "casual", "3": "mid"}
        answers["mood"] = mood_map.get(q2, "engaged")

    elif ctx == "family":
        click.echo("Who's in the room?")
        click.echo("  1. Mixed ages — needs to work for everyone")
        click.echo("  2. Just adults, can go anywhere")
        q2 = click.prompt("  ", default="1").strip()
        answers["mood"] = "family_mixed" if q2 == "1" else "family_adults"

    else:  # partner or friends
        click.echo("What's the energy?")
        click.echo("  1. We're up for something real — weight and darkness are fine")
        click.echo("  2. Something we can enjoy together without it being heavy")
        click.echo("  3. Pure fun — entertainment, nothing more")
        q2 = click.prompt("  ", default="2").strip()
        mood_map = {"1": "engaged", "2": "mid", "3": "casual"}
        answers["mood"] = mood_map.get(q2, "mid")

    click.echo()

    # ── Q3: Attention level (optional-feeling) ───────────────────────────────
    click.echo("How much of you is in the room tonight?")
    click.echo("  1. Full attention — I'm watching, nothing else")
    click.echo("  2. Half-watching — it's on while I wind down")
    q3 = click.prompt("  (Enter to skip)", default="").strip()
    if q3 == "1":
        answers["attention"] = "full"
    elif q3 == "2":
        answers["attention"] = "half"
    else:
        answers["attention"] = "full" if answers["mood"] == "engaged" else "half"

    click.echo()

    # ── Q4: Open ending preference (optional) ────────────────────────────────
    click.echo("Do you want something that wraps up cleanly, or okay with an open ending?")
    click.echo("  1. Clean resolution tonight")
    click.echo("  2. Open ending is fine — ambiguity is part of it")
    q4 = click.prompt("  (Enter to skip)", default="").strip()
    if q4 == "1":
        answers["ending"] = "clean"
    elif q4 == "2":
        answers["ending"] = "open"
    else:
        answers["ending"] = None

    return answers


def _determine_cluster(answers: dict) -> str | None:
    """Map calibration answers to a taste cluster for DB queries."""
    ctx = answers.get("watch_context", "solo")
    mood = answers.get("mood", "engaged")

    if ctx == "solo" and mood == "engaged":
        return "solo_late_night"
    if ctx == "solo" and mood == "casual":
        return None  # general pool
    if ctx == "family":
        return "family"
    if ctx in ("partner", "friends"):
        return "partner" if ctx == "partner" else None
    return None


def _context_summary(answers: dict) -> str:
    """One-paragraph summary of tonight's context for the Claude prompt."""
    ctx = answers.get("watch_context", "solo")
    mood = answers.get("mood", "engaged")
    attention = answers.get("attention", "full")
    ending = answers.get("ending")

    parts = []

    if ctx == "solo":
        parts.append("Watching alone tonight.")
    elif ctx == "partner":
        parts.append("Watching with a partner.")
    elif ctx == "family":
        parts.append("Watching with family.")
    else:
        parts.append("Watching with friends.")

    if mood == "engaged":
        parts.append("In the mood to feel something — a film that lands and stays.")
    elif mood == "casual":
        parts.append("Wants to switch off — entertained but not challenged.")
    elif mood == "mid":
        parts.append("Somewhere between engaged and casual — something enjoyable without being heavy.")
    elif mood == "family_mixed":
        parts.append("Mixed ages in the room — needs to work for everyone.")
    elif mood == "family_adults":
        parts.append("Family but all adults — can go anywhere.")

    if attention == "half":
        parts.append("Half-watching — doesn't need full concentration.")
    else:
        parts.append("Full attention available.")

    if ending == "clean":
        parts.append("Wants something that wraps up cleanly tonight.")
    elif ending == "open":
        parts.append("Fine with ambiguity and open endings.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def _score_candidates(cluster: str | None, answers: dict) -> list[dict]:
    """
    Score the full watchlist against tonight's context.
    Returns up to 8 best candidates for Claude to choose from.
    """
    watchlist = db.get_watchlist()
    rewatch = [f for f in db.get_watched_films()
               if f.get("status") == "rewatch_candidate"]
    candidates = watchlist + rewatch

    if not candidates:
        return []

    # Get positive taste attributes for tonight's cluster
    pos_attrs = db.get_taste_attributes(cluster=cluster, polarity="positive")
    neg_attrs = db.get_taste_attributes(cluster=cluster, polarity="negative")

    # Also pull general (non-cluster) attributes
    if cluster:
        pos_attrs += db.get_taste_attributes(cluster=None, polarity="positive")
        neg_attrs += db.get_taste_attributes(cluster=None, polarity="negative")

    pos_map = {a["attribute_name"].lower(): a["confidence_score"] for a in pos_attrs}
    neg_set = {a["attribute_name"].lower() for a in neg_attrs}

    # Get recent recommendation history (avoid repeating)
    recent_issues = db.get_recommendation_history(limit=5)
    recently_recommended_ids = set()
    for issue in recent_issues:
        recently_recommended_ids.update(issue.get("films_referenced", []))

    # Attention filter — for half-watching, deprioritise very long or very demanding films
    attention = answers.get("attention", "full")
    mood = answers.get("mood", "engaged")

    scored = []
    for film in candidates:
        raw_attrs = film.get("default_taste_attributes") or "[]"
        if isinstance(raw_attrs, str):
            try:
                film_attrs = json.loads(raw_attrs)
            except Exception:
                film_attrs = []
        else:
            film_attrs = raw_attrs or []

        score = 0.0

        # Attribute matching
        for fa in film_attrs:
            fa_lower = fa.lower()
            for taste_attr, confidence in pos_map.items():
                if taste_attr in fa_lower or fa_lower in taste_attr:
                    score += confidence
            for neg_attr in neg_set:
                if neg_attr in fa_lower or fa_lower in neg_attr:
                    score -= 0.4

        # Personal notes boost (film has context = more thought went into adding it)
        if film.get("personal_notes"):
            score += 0.3

        # Penalise recently recommended films slightly
        if film.get("tmdb_id") in recently_recommended_ids:
            score -= 0.5

        # Runtime consideration for half-watching
        runtime = film.get("runtime") or 0
        if attention == "half" and runtime > 150:
            score -= 0.4

        # Cognitive load matching
        cog_load = film.get("cognitive_load") or "medium"
        if mood == "casual" and cog_load == "high":
            score -= 0.6
        elif mood == "casual" and cog_load == "low":
            score += 0.3
        elif mood == "engaged" and cog_load == "high":
            score += 0.2
        if attention == "half" and cog_load == "high":
            score -= 0.5

        # Rewatch candidates get a small boost (they stuck for a reason)
        if film.get("status") == "rewatch_candidate":
            score += 0.3

        if score > 0:
            scored.append((score, film))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [film for _, film in scored[:8]]


# ---------------------------------------------------------------------------
# Claude Opus recommendation issue
# ---------------------------------------------------------------------------

RECOMMENDATION_PROMPT = """You are writing a personalised film recommendation for Prashil.

You know his taste deeply — you have his full taste model and his watch history.
Your job is to write a recommendation issue: a 300–400 word editorial, written like a
recommendation from a close friend who has seen all these films and knows exactly what lands
for Prashil.

NOT a list. Flowing prose. The voice is warm, specific, and direct.

For each film you recommend (2–3 maximum):
1. The sell — 3–4 sentences about the film itself. Non-spoilery. Written like someone who loves it.
2. The why-you-specifically — reference a specific film from Prashil's log and the *exact quality*
   that connects them. Not the genre — the actual quality. "Like the way X worked for you, this
   has that same [specific thing]."
3. A brief context note — who it's right for, what energy it needs.

PRIORITY ORDER:
1. Films from his watchlist that match tonight's context — always check watchlist first
2. Rewatch candidates if the mood is right (frame it as: "you've seen this and it stuck —
   tonight might be exactly the right time to go back")
3. Only suggest films outside his watchlist if nothing fits

End with one sentence that is an honest read of tonight — why these picks, why now.

---

TONIGHT'S CONTEXT:
{context_summary}

---

TASTE MODEL:
{taste_model}

---

CANDIDATE FILMS FROM WATCHLIST:
{candidates}

---

RECENT WATCHES (for context and avoiding repetition):
{recent_watches}

---

Write the recommendation issue now. Remember: prose, not a list. Specific, not generic.
The "why you specifically" for each film is the most important sentence you will write."""


def _format_candidates(candidates: list[dict]) -> str:
    """Format candidate films for the Claude prompt."""
    lines = []
    for film in candidates:
        title = film.get("title", "Unknown")
        year = film.get("year", "")
        lang = film.get("language", "")
        runtime = film.get("runtime", "")
        status = film.get("status", "watchlist")
        notes = film.get("personal_notes", "") or ""

        raw_attrs = film.get("default_taste_attributes") or "[]"
        if isinstance(raw_attrs, str):
            try:
                attrs = json.loads(raw_attrs)
            except Exception:
                attrs = []
        else:
            attrs = raw_attrs or []

        cog_load = film.get("cognitive_load") or "medium"
        line = f"• {title} ({year})"
        if lang:
            line += f" [{lang}]"
        if runtime:
            line += f" {runtime}min"
        line += f" [energy: {cog_load}]"
        if status == "rewatch_candidate":
            line += " [REWATCH CANDIDATE]"
        if attrs:
            line += f"\n  Attributes: {', '.join(attrs[:5])}"
        if notes:
            short = notes[:100] + "…" if len(notes) > 100 else notes
            line += f"\n  Notes: {short}"

        lines.append(line)

    return "\n\n".join(lines) if lines else "No candidates available."


def _format_recent_watches(limit: int = 5) -> str:
    """Format recent watch history for context."""
    watched = db.get_watched_films()
    recent = watched[:limit]

    if not recent:
        return "No watch history yet."

    lines = []
    for film in recent:
        title = film.get("title", "Unknown")
        year = film.get("year", "")
        notes = film.get("personal_notes", "") or ""
        short_notes = f' — "{notes[:80]}…"' if len(notes) > 80 else (f' — "{notes}"' if notes else "")
        lines.append(f"• {title} ({year}){short_notes}")

    return "\n".join(lines)


def _load_taste_model() -> str:
    """Read taste_model.md. Returns empty string if not yet generated."""
    if not TASTE_MODEL_PATH.exists():
        return ""
    return TASTE_MODEL_PATH.read_text(encoding="utf-8")


def _call_claude_opus(prompt: str) -> str:
    """Call Claude Opus to generate the recommendation issue."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text.strip()


# ---------------------------------------------------------------------------
# Tier 2 fallback (no live API)
# ---------------------------------------------------------------------------

def _tier2_recommendation(cluster: str | None) -> None:
    """
    Offline recommendation using stored taste_attributes.
    No prose — just the correct picks with attribute reasoning.
    """
    click.echo("\n── Offline recommendation (pre-computed model) ─────────")
    click.echo("(Claude API unavailable — using stored taste model)\n")

    picks = recommend_from_model(cluster=cluster, limit=3)

    if not picks:
        click.echo("Not enough data yet. Add some films and run: python main.py rescan\n")
        return

    for i, film in enumerate(picks, 1):
        title = film.get("title", "Unknown")
        year = film.get("year", "")
        raw_attrs = film.get("default_taste_attributes") or "[]"
        if isinstance(raw_attrs, str):
            try:
                attrs = json.loads(raw_attrs)
            except Exception:
                attrs = []
        else:
            attrs = raw_attrs or []

        click.echo(f"  {i}. {title} ({year})")
        if attrs:
            click.echo(f"     {' · '.join(attrs[:3])}")
        click.echo()

    click.echo("Run 'python main.py recommend' when API is available for the full editorial.\n")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_issue() -> None:
    """
    Full recommendation flow: calibration → candidate selection →
    Claude Opus editorial → display → save to DB.
    """
    click.echo("\n── What are we watching tonight? ───────────────────────")

    # ── 1. Calibration ───────────────────────────────────────────────────────
    answers = _ask_calibration_questions()
    cluster = _determine_cluster(answers)
    context_summary = _context_summary(answers)

    # ── 2. Load taste model ──────────────────────────────────────────────────
    taste_model = _load_taste_model()
    if not taste_model:
        click.echo("⚠  No taste model found. Run: python main.py rescan")
        click.echo("   Falling back to attribute-based scoring.\n")

    # ── 3. Score candidates ──────────────────────────────────────────────────
    candidates = _score_candidates(cluster, answers)

    if not candidates:
        click.echo("Your watchlist is empty — nothing to recommend from.")
        click.echo("Run: python main.py ingest-all   to import your saved lists.\n")
        return

    # ── 4. Try Claude Opus — fall back to Tier 2 if unavailable ─────────────
    api_key = os.getenv("ANTHROPIC_API_KEY")

    if not api_key or not taste_model:
        _tier2_recommendation(cluster)
        return

    # ── 5. Build prompt ──────────────────────────────────────────────────────
    prompt = RECOMMENDATION_PROMPT.format(
        context_summary=context_summary,
        taste_model=taste_model[:3000],  # cap at 3k chars — it's a summary, not the full log
        candidates=_format_candidates(candidates),
        recent_watches=_format_recent_watches(),
    )

    # ── 6. Generate ──────────────────────────────────────────────────────────
    click.echo("Generating recommendation …\n")
    try:
        issue_text = _call_claude_opus(prompt)
    except RuntimeError as e:
        click.echo(f"API error: {e}\n", err=True)
        _tier2_recommendation(cluster)
        return

    # ── 7. Display ───────────────────────────────────────────────────────────
    click.echo("─" * 54)
    click.echo()
    click.echo(issue_text)
    click.echo()
    click.echo("─" * 54)
    click.echo()

    # ── 8. Save to DB ────────────────────────────────────────────────────────
    model_version = db.get_latest_taste_model()
    version_id = model_version["id"] if model_version else None

    film_ids = [f["tmdb_id"] for f in candidates if f.get("tmdb_id")]

    issue_id = db.save_recommendation_issue(
        issue_text=issue_text,
        films_referenced=film_ids,
        taste_model_version_id=version_id,
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    click.echo(f"Issue #{issue_id} saved — {now}")
    click.echo("Run 'python main.py status' to see your history.\n")


# ---------------------------------------------------------------------------
# Web API (library-first — called by Flask, not the CLI)
# ---------------------------------------------------------------------------

def generate_web_recommendation(answers: dict) -> dict:
    """
    Generate a recommendation from web form answers. Returns a result dict
    so Flask routes get structured data, not printed output.

    answers keys:
      watch_context  — 'solo' | 'partner' | 'family' | 'friends'
      mood           — 'engaged' | 'casual' | 'mid'
      attention      — 'full' | 'half'
      ending         — 'clean' | 'open' | None

    Returns:
      {
        "issue_text":       str,         # editorial prose (or fallback list)
        "issue_number":     int | None,  # saved issue number
        "films_referenced": list[int],   # tmdb_ids mentioned
        "tier2":            bool,        # True if offline fallback was used
        "error":            str | None,  # human-readable error if failed
      }
    """
    cluster = _determine_cluster(answers)
    context_summary = _context_summary(answers)
    taste_model = _load_taste_model()
    candidates = _score_candidates(cluster, answers)

    # ── No watchlist ─────────────────────────────────────────────────────────
    if not candidates:
        return {
            "issue_text": "",
            "issue_number": None,
            "films_referenced": [],
            "tier2": False,
            "error": (
                "Your watchlist is empty — there's nothing to recommend from. "
                "Run python main.py ingest-all to import your film lists."
            ),
        }

    # ── No taste model ────────────────────────────────────────────────────────
    if not taste_model:
        return {
            "issue_text": "",
            "issue_number": None,
            "films_referenced": [],
            "tier2": False,
            "error": (
                "No taste model found. Run python main.py rescan to build it "
                "from your film log."
            ),
        }

    api_key = os.getenv("ANTHROPIC_API_KEY")

    # ── Tier 2 fallback — no live API ────────────────────────────────────────
    if not api_key:
        picks = recommend_from_model(cluster=cluster, limit=3)
        if not picks:
            return {
                "issue_text": "",
                "issue_number": None,
                "films_referenced": [],
                "tier2": True,
                "error": "API key not configured and no stored taste attributes found.",
            }

        lines = []
        for i, film in enumerate(picks, 1):
            title = film.get("title", "Unknown")
            year = film.get("year", "")
            raw_attrs = film.get("default_taste_attributes") or "[]"
            if isinstance(raw_attrs, str):
                try:
                    attrs = json.loads(raw_attrs)
                except Exception:
                    attrs = []
            else:
                attrs = raw_attrs or []
            attr_str = " · ".join(attrs[:3]) if attrs else ""
            lines.append(
                f"{i}. {title} ({year})\n   {attr_str}"
                if attr_str else f"{i}. {title} ({year})"
            )

        issue_text = (
            "Recommendation based on stored taste model (offline mode):\n\n"
            + "\n\n".join(lines)
        )
        film_ids = [f["tmdb_id"] for f in picks if f.get("tmdb_id")]
        model_version = db.get_latest_taste_model()
        db.save_recommendation_issue(
            issue_text=issue_text,
            films_referenced=film_ids,
            taste_model_version_id=model_version["id"] if model_version else None,
        )
        issues = db.get_recommendation_history(limit=1)
        issue_number = issues[0]["issue_number"] if issues else 1

        return {
            "issue_text": issue_text,
            "issue_number": issue_number,
            "films_referenced": film_ids,
            "tier2": True,
            "error": None,
        }

    # ── Tier 1 — full Claude Opus ─────────────────────────────────────────────
    prompt = RECOMMENDATION_PROMPT.format(
        context_summary=context_summary,
        taste_model=taste_model[:3000],
        candidates=_format_candidates(candidates),
        recent_watches=_format_recent_watches(),
    )

    try:
        issue_text = _call_claude_opus(prompt)
    except Exception as e:
        return {
            "issue_text": "",
            "issue_number": None,
            "films_referenced": [],
            "tier2": False,
            "error": f"Claude API error: {e}",
        }

    film_ids = [f["tmdb_id"] for f in candidates if f.get("tmdb_id")]
    model_version = db.get_latest_taste_model()
    db.save_recommendation_issue(
        issue_text=issue_text,
        films_referenced=film_ids,
        taste_model_version_id=model_version["id"] if model_version else None,
    )
    issues = db.get_recommendation_history(limit=1)
    issue_number = issues[0]["issue_number"] if issues else 1

    return {
        "issue_text": issue_text,
        "issue_number": issue_number,
        "films_referenced": film_ids,
        "tier2": False,
        "error": None,
    }
