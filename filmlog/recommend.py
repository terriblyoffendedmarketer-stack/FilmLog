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

DATA_DIR = Path(os.getenv("FILMLOG_DATA_DIR", Path(__file__).parent.parent))
TASTE_MODEL_PATH = DATA_DIR / "taste_model.md"


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

    if ctx == "solo":
        return "solo_late_night"
    if ctx == "family":
        return "family"
    if ctx == "partner":
        return "partner"
    return None


def _context_summary(answers: dict) -> str:
    """One-paragraph summary of tonight's context for the Claude prompt."""
    ctx = answers.get("watch_context", "solo")
    free_text = answers.get("free_text", "").strip()

    parts = []

    if ctx == "solo":
        parts.append("Watching alone tonight.")
    elif ctx == "partner":
        parts.append("Watching with a partner.")
    elif ctx == "family":
        parts.append("Watching with family.")
    else:
        parts.append("Watching with friends.")

    if free_text:
        parts.append(f"Additional context from Prashil: \"{free_text}\"")
    else:
        parts.append("No specific mood stated — surprise him with variety.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def _score_candidates(cluster: str | None, answers: dict) -> list[dict]:
    """
    Score the full watchlist against tonight's context.
    Returns up to 10 best candidates for Claude to choose from.
    Claude picks the final 2-3 — we give it a generous pool.
    """
    watchlist = db.get_watchlist()
    rewatch = [f for f in db.get_watched_films() if f.get("rewatch", 0)]
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

        # Rewatch candidates get a small boost (they stuck for a reason)
        if film.get("rewatch", 0):
            score += 0.3

        if score > 0:
            scored.append((score, film))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [film for _, film in scored[:10]]


def _format_hall_of_fame(limit: int = 20) -> str:
    """Format hall of fame films for the Claude prompt."""
    try:
        hof = db.get_hall_of_fame()
    except Exception:
        hof = db.get_watched_films()

    if not hof:
        return "No hall of fame films logged yet."

    lines = []
    for film in hof[:limit]:
        title = film.get("title", "Unknown")
        year = film.get("year", "")
        notes = film.get("personal_notes", "") or ""
        raw_attrs = film.get("default_taste_attributes") or "[]"
        if isinstance(raw_attrs, str):
            try:
                attrs = json.loads(raw_attrs)
            except Exception:
                attrs = []
        else:
            attrs = raw_attrs or []

        line = f"• {title} ({year})"
        if attrs:
            line += f" — {', '.join(attrs[:3])}"
        if notes:
            short = notes[:60] + "…" if len(notes) > 60 else notes
            line += f' ["{short}"]'
        lines.append(line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude Opus recommendation issue
# ---------------------------------------------------------------------------

RECOMMENDATION_PROMPT = """You are writing a personalised recommendation for Prashil — one reader, one evening.

Voice: a friend who watches a lot of film and knows what lands for this specific person. Warm, direct, zero filler. You're handing someone a film, not reviewing one.

Write 300–500 words. Recommend 2–4 films.

## FORMAT

- **Bold film titles** every time: **The Wailing**, **Coherence**, etc.
- One paragraph per film, with a blank line between them.
- *Italics* for emphasis sparingly.
- A short opening line setting up the evening, and a short closing line.
- No numbered lists, no bullet points, no subheadings.

## THE SELL

This is the most important part. Each film gets 3–4 sentences that make someone want to press play. Not a synopsis. Not a review. A sell.

What works: sensory details, the feeling the film creates, what makes it distinctive, a specific image or moment (without spoiling). Write like you're leaning across a table saying "okay but you have to watch this one."

What doesn't work: generic praise ("masterfully crafted", "compelling narrative"), plot summary disguised as enthusiasm, or connecting every single film to something in the hall of fame. If a connection to a film Prashil loves is genuine and specific, make it. If you're reaching, skip it entirely. Most films should sell on their own merits.

Bad sell: "This film explores themes of isolation and redemption through a masterful lens."
Good sell: "Ninety minutes of a man in a car being hunted by a truck whose driver you never see. That's it. And it's relentless."

## VARIETY

Your 2–4 picks should feel like a curated set, not a cluster. Vary across:
- **Energy**: mix intensities — don't recommend four slow burns or four adrenaline rides.
- **Origin**: draw from different countries/languages when the watchlist allows it. If the best three are all Korean, that's fine — but don't default to one region out of habit.
- **Tone**: pair something heavy with something lighter, or something contemplative with something visceral.
The variety should feel natural, not forced. You're assembling a set of options for one evening — give real range so at least one clicks no matter the mood.

## RULES

- Only write about films you are recommending. Don't mention films you're not picking.
- Never spoil twists, endings, or structural surprises.
- Pick primarily from the watchlist candidates. You can go outside the list if something fits perfectly.

---

TONIGHT'S CONTEXT:
{context_summary}

---

TASTE MODEL:
{taste_model}

---

HALL OF FAME:
{hall_of_fame}

---

WATCHLIST CANDIDATES:
{candidates}

---

RECENT WATCHES (avoid repeating):
{recent_watches}

---

Write the issue now."""


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

        engagement = film.get("engagement") or "medium"
        line = f"• {title} ({year})"
        if lang:
            line += f" [{lang}]"
        if runtime:
            line += f" {runtime}min"
        line += f" [engagement: {engagement}]"
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


from filmlog.ai import call_ai

def _call_claude_opus(prompt: str) -> str:
    """Generate the recommendation issue using configured AI (Gemini or Anthropic)."""
    return call_ai(prompt, max_tokens=1500)


import re as _re

def _extract_mentioned_films(issue_text: str, candidates: list[dict]) -> list[int]:
    """
    Parse the editorial text to find which films were actually recommended.
    Looks for **bold titles** (markdown) and matches against candidates + full DB.
    Returns list of tmdb_ids for films mentioned in the text.
    """
    # Extract bold text patterns: **Title** or **Title (Year)**
    bold_pattern = _re.compile(r'\*\*([^*]+)\*\*')
    bold_matches = bold_pattern.findall(issue_text)

    # Clean up: strip year suffixes like "Title (2023)"
    mentioned_titles = []
    for match in bold_matches:
        # Remove trailing year in parens
        clean = _re.sub(r'\s*\(\d{4}\)\s*$', '', match).strip()
        if clean:
            mentioned_titles.append(clean.lower())

    if not mentioned_titles:
        return []

    # Build lookup: title → tmdb_id from candidates
    title_to_id: dict[str, int] = {}
    for film in candidates:
        title = (film.get("title") or "").lower()
        if title and film.get("tmdb_id"):
            title_to_id[title] = film["tmdb_id"]

    # Also search the full DB for films Claude may have picked outside the candidate list
    matched_ids = []
    for mentioned in mentioned_titles:
        # Exact match in candidates
        if mentioned in title_to_id:
            tid = title_to_id[mentioned]
            if tid not in matched_ids:
                matched_ids.append(tid)
            continue

        # Fuzzy: check if mentioned title is contained in or contains a candidate title
        found = False
        for title, tid in title_to_id.items():
            if mentioned in title or title in mentioned:
                if tid not in matched_ids:
                    matched_ids.append(tid)
                found = True
                break

        if not found:
            # Search the full local DB
            results = db.search_films_local(mentioned)
            if results:
                # Pick the closest match
                for r in results:
                    r_title = (r.get("title") or "").lower()
                    if r_title == mentioned or mentioned in r_title or r_title in mentioned:
                        tid = r["tmdb_id"]
                        if tid not in matched_ids:
                            matched_ids.append(tid)
                        found = True
                        break

    return matched_ids


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

    # ── 4. Try AI — fall back to Tier 2 if unavailable ──────────────────────
    has_ai = bool(os.getenv("GEMINI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))

    if not has_ai or not taste_model:
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

    film_ids = _extract_mentioned_films(issue_text, candidates)

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
      free_text      — optional freeform context ("I'm tired but want something good")

    Returns:
      {
        "issue_text":       str,         # editorial prose (markdown formatted)
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

    has_ai = bool(os.getenv("GEMINI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))

    # ── Tier 2 fallback — no live API ────────────────────────────────────────
    if not has_ai:
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
                f"{i}. **{title}** ({year})\n   {attr_str}"
                if attr_str else f"{i}. **{title}** ({year})"
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
        hall_of_fame=_format_hall_of_fame(limit=20),
        candidates=_format_candidates(candidates) if candidates else "Watchlist is empty — recommend films from your knowledge that match the taste model.",
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

    # Extract only the films actually mentioned in the editorial
    film_ids = _extract_mentioned_films(issue_text, candidates)

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


# ---------------------------------------------------------------------------
# Pre-cached issues (generate in advance for Tier 2 offline use)
# ---------------------------------------------------------------------------

_PRECACHE_CONTEXTS = [
    {"watch_context": "solo", "free_text": ""},
    {"watch_context": "solo", "free_text": "something that stays with me"},
    {"watch_context": "partner", "free_text": ""},
]


def precache_issues(count: int = 2) -> list[dict]:
    """
    Pre-generate recommendation issues for common contexts.
    Call this after a rescan or periodically. Each issue is saved to the DB
    so it's available even without API credits.

    Returns list of generated issue dicts.
    """
    has_ai = bool(os.getenv("GEMINI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))
    if not has_ai:
        return []

    taste_model = _load_taste_model()
    if not taste_model:
        return []

    results = []
    for i, ctx in enumerate(_PRECACHE_CONTEXTS[:count]):
        try:
            result = generate_web_recommendation(ctx)
            if result.get("issue_text") and not result.get("error"):
                results.append(result)
        except Exception:
            continue

    return results


# ---------------------------------------------------------------------------
# Rewrite an existing issue (same films, fresh editorial)
# ---------------------------------------------------------------------------

REWRITE_PROMPT = """You are rewriting a personalised recommendation for Prashil — one reader, one evening.

Voice: a friend who watches a lot of film and knows what lands for this specific person. Warm, direct, zero filler. You're handing someone a film, not reviewing one.

Write 300–500 words. You MUST recommend exactly these films (all of them, no others):
{film_list}

## FORMAT

- **Bold film titles** every time: **The Wailing**, **Coherence**, etc.
- One paragraph per film, with a blank line between them.
- *Italics* for emphasis sparingly.
- A short opening line setting up the evening, and a short closing line.
- No numbered lists, no bullet points, no subheadings.

## THE SELL

This is the most important part. Each film gets 3–4 sentences that make someone want to press play. Not a synopsis. Not a review. A sell.

What works: sensory details, the feeling the film creates, what makes it distinctive, a specific image or moment (without spoiling). Write like you're leaning across a table saying "okay but you have to watch this one."

What doesn't work: generic praise ("masterfully crafted", "compelling narrative"), plot summary disguised as enthusiasm, or connecting every single film to something in the hall of fame. If a connection to a film Prashil loves is genuine and specific, make it. If you're reaching, skip it entirely. Most films should sell on their own merits.

Bad sell: "This film explores themes of isolation and redemption through a masterful lens."
Good sell: "Ninety minutes of a man in a car being hunted by a truck whose driver you never see. That's it. And it's relentless."

## RULES

- Write about ALL the films listed above. Don't skip any, don't add new ones.
- Never spoil twists, endings, or structural surprises.

---

TASTE MODEL:
{taste_model}

---

HALL OF FAME:
{hall_of_fame}

---

Write the issue now."""


def rewrite_issue(issue_id: int) -> dict:
    """
    Rewrite an existing recommendation issue: same films, fresh editorial
    using the current prompt and taste model.

    Returns:
      {"issue_text": str, "error": str | None}
    """
    history = db.get_recommendation_history(limit=50)
    issue = next((i for i in history if i["id"] == issue_id), None)
    if not issue:
        return {"issue_text": "", "error": "Issue not found."}

    film_ids = issue.get("films_referenced", [])
    if not film_ids:
        return {"issue_text": "", "error": "No films linked to this issue."}

    has_ai = bool(os.getenv("GEMINI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))
    if not has_ai:
        return {"issue_text": "", "error": "AI API key not set."}

    taste_model = _load_taste_model()

    films = []
    for tid in film_ids:
        film = db.get_film(tid)
        if film:
            films.append(film)

    if not films:
        return {"issue_text": "", "error": "Could not find films in the database."}

    film_lines = []
    for film in films:
        title = film.get("title", "Unknown")
        year = film.get("year", "")
        lang = film.get("language", "")
        runtime = film.get("runtime", "")
        raw_attrs = film.get("default_taste_attributes") or "[]"
        if isinstance(raw_attrs, str):
            try:
                attrs = json.loads(raw_attrs)
            except Exception:
                attrs = []
        else:
            attrs = raw_attrs or []

        line = f"• {title} ({year})"
        if lang:
            line += f" [{lang}]"
        if runtime:
            line += f" {runtime}min"
        if attrs:
            line += f" — {', '.join(attrs[:5])}"
        film_lines.append(line)

    prompt = REWRITE_PROMPT.format(
        film_list="\n".join(film_lines),
        taste_model=taste_model[:3000] if taste_model else "(no taste model available)",
        hall_of_fame=_format_hall_of_fame(limit=20),
    )

    try:
        new_text = _call_claude_opus(prompt)
    except Exception as e:
        return {"issue_text": "", "error": f"Claude API error: {e}"}

    db.update_recommendation_issue(issue_id, new_text)

    return {"issue_text": new_text, "error": None}
