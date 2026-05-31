"""
main.py — CLI entry point for Filmlog.

This file is a thin shell only. All logic lives in the filmlog/ package.
"""

import click
from filmlog import db
from filmlog import tmdb
from filmlog import ingest as ingest_module
from filmlog import log as log_module
from filmlog import model as model_module
from filmlog import recommend as recommend_module
from filmlog import browse as browse_module
from filmlog import journal_ingest
from filmlog import attributes as attr_module
from filmlog import harddrive_import


@click.group()
def cli():
    """Filmlog — a personal taste engine for films."""
    pass


@cli.command()
def setup():
    """Initialise the database. Run this once before anything else."""
    db.init_db()


@cli.command()
@click.argument("title")
def add(title):
    """Add a film to your log or watchlist."""
    log_module.add_film_interactive(title)


@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--yes", "-y", is_flag=True, help="Skip batch confirmation (save ambiguous as unresolved).")
@click.option("--offline", is_flag=True, help="Use Claude for metadata instead of TMDB (when TMDB is unavailable).")
def ingest(file, yes, offline):
    """Import a watchlist from a text file."""
    try:
        ingest_module.ingest_file(file, non_interactive=yes, offline=offline)
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)


@cli.command("ingest-all")
@click.option("--yes", "-y", is_flag=True, help="Skip batch confirmation.")
@click.option("--offline", is_flag=True, help="Use Claude for metadata instead of TMDB (when TMDB is unavailable).")
def ingest_all(yes, offline):
    """Ingest all raw source files from data/raw/ in one go."""
    try:
        ingest_module.ingest_all_raw(non_interactive=yes, offline=offline)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)


@cli.command("export-unresolved")
def export_unresolved():
    """Print all unresolved watchlist entries to the terminal."""
    items = db.get_unresolved(resolved=False)
    if not items:
        click.echo("No unresolved entries.")
        return
    click.echo(f"\n── {len(items)} unresolved entries ──────────────")
    for item in items:
        click.echo(f"  [{item['id']}] {item['raw_text']}  (confidence: {item['confidence_score']:.2f})")
    click.echo()


@cli.command()
@click.option("--trigger", default="manual",
              type=click.Choice(["manual", "user_edit"]),
              help="What triggered this rescan.")
@click.option("--full", is_flag=True, default=False,
              help="Force a full rescan instead of diff-based.")
@click.option("--precache", is_flag=True, default=False,
              help="Pre-generate 2 recommendation issues after rescan.")
def rescan(trigger, full, precache):
    """Regenerate the taste model (diff-based by default, --full for complete rebuild)."""
    model_module.rescan(trigger=trigger, force_full=full)
    if precache:
        click.echo("\nPre-caching recommendation issues…")
        results = recommend_module.precache_issues(count=2)
        click.echo(f"  Generated {len(results)} pre-cached issue(s).")


@cli.command()
def recommend():
    """Mood calibration → personalised recommendation issue."""
    recommend_module.generate_issue()


@cli.command()
@click.option("--language", "-l", default=None, help="Filter by language code (ko, ja, ml, en, hi…)")
@click.option("--context", "-c", default=None,
              type=click.Choice(["solo", "family", "partner", "friends"]),
              help="Filter by watch context.")
@click.option("--mood", "-m", default=None, help="Mood keyword matched against taste attributes.")
@click.option("--length", "-d", default=None,
              type=click.Choice(["short", "medium", "long"]),
              help="short <90min / medium 90–140 / long >140.")
@click.option("--era", "-e", default=None,
              type=click.Choice(["50s","60s","70s","80s","90s","2000s","2010s","2020s"]),
              help="Filter by decade.")
@click.option("--query", "-q", default=None,
              help="Natural language query — parsed by Claude Haiku.")
@click.option("--limit", "-n", default=15, help="Number of results to show.")
@click.option("--watched", "-w", is_flag=True, help="Include watched films too.")
def browse(language, context, mood, length, era, query, limit, watched):
    """Browse and filter your film library.

    \b
    Examples:
      python main.py browse
      python main.py browse --language ko
      python main.py browse --context family --length short
      python main.py browse --query "Malayalam film I can watch with family"
      python main.py browse --mood "slow burn" --era 2010s
    """
    browse_module.browse(
        language=language,
        context=context,
        mood=mood,
        length=length,
        era=era,
        query=query,
        limit=limit,
        show_watched=watched,
    )


@cli.command("compute-engagement")
def compute_engagement():
    """Compute engagement levels for all films (heuristic, no API needed)."""
    total = db.compute_all_engagement()
    click.echo(f"✓ Updated engagement for {total} films.")


@cli.command("backfill-attributes")
@click.option("--batch-size", default=10, help="Films per API call (default 10).")
@click.option("--max-batches", default=100, help="Maximum number of batches to process.")
def backfill_attributes(batch_size, max_batches):
    """Infer taste attributes for all films missing them (uses Claude Haiku)."""
    remaining = len(db.get_films_without_attributes(limit=9999))
    click.echo(f"\n── Backfilling attributes for {remaining} films ──────────────")
    click.echo(f"   Batch size: {batch_size}, max batches: {max_batches}\n")

    total = attr_module.backfill_attributes(
        batch_size=batch_size,
        max_batches=max_batches,
    )

    click.echo(f"\n✓ Updated {total} films with taste attributes.")
    new_remaining = len(db.get_films_without_attributes(limit=9999))
    if new_remaining:
        click.echo(f"  {new_remaining} films still need attributes.")
    else:
        click.echo("  All films now have taste attributes.")
    click.echo()


@cli.command("import-journal")
@click.argument("file", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Parse and preview without importing.")
def import_journal(file, dry_run):
    """Import a raw markdown journal dump into structured entries."""
    entries = journal_ingest.ingest_journal_dump(file, dry_run=dry_run)
    if dry_run:
        click.echo(f"\n── Dry run: {len(entries)} entries parsed ──────────────")
        for e in entries:
            films_n = len(e.get("linked_film_ids", []))
            tags = ", ".join(e.get("tags", [])) or "—"
            click.echo(f"  [{e['type']:10s}] {e['title'][:55]}")
            click.echo(f"             {len(e['content']):,} chars · {films_n} films · {tags}")
        click.echo("\nRun without --dry-run to import.")


@cli.command()
@click.argument("title")
@click.option("--year", "-y", default=None, type=int, help="Narrow search by release year.")
def search(title, year):
    """Search TMDB for a film by title."""
    click.echo(f"\nSearching for: {title}" + (f" ({year})" if year else "") + " …\n")

    try:
        result = tmdb.search_and_pick(title, year)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        return

    if result is None:
        click.echo("No results found. Try a different title or add a year with --year.")
        return

    # Multiple candidates — list them
    if "candidates" in result:
        click.echo(f"Found {len(result['candidates'])} results:\n")
        for i, c in enumerate(result["candidates"][:8], 1):  # cap at 8
            year_str = f" ({c['year']})" if c["year"] else ""
            lang_str = f"  [{c['language']}]" if c["language"] else ""
            click.echo(f"  {i}.  {c['title']}{year_str}{lang_str}")
            if c["overview"]:
                click.echo(f"       {c['overview'][:120]}…" if len(c["overview"]) > 120 else f"       {c['overview']}")
            click.echo()
        click.echo("To add one: python main.py add \"<title>\" --year <year>")
        return

    # Single result — show the details
    click.echo(f"  {result['title']} ({result['year']})")
    click.echo(f"  Language : {result['language']}  |  Country: {result['country']}")
    click.echo(f"  Runtime  : {result['runtime']} min")
    click.echo(f"  Genres   : {', '.join(result['tmdb_genres'])}")
    if result["poster_url"]:
        click.echo(f"  Poster   : {result['poster_url']}")
    click.echo(f"\n  TMDB ID  : {result['tmdb_id']}")


@cli.command()
def status():
    """Show a quick summary of the current database state."""
    counts = db.count_user_films()
    model = db.get_latest_taste_model()

    click.echo("\n── Filmlog status ──────────────────────")
    click.echo(f"  Watched        {counts.get('watched', 0)}")
    click.echo(f"  Watchlist      {counts.get('watchlist', 0)}")
    click.echo(f"  Rewatch queue  {counts.get('rewatch_candidate', 0)}")

    if model:
        click.echo(f"\n  Taste model    v{model['version_number']} ({model['generated_at'][:10]})")
    else:
        click.echo("\n  Taste model    not generated yet — run: python main.py rescan")

    unresolved = db.get_unresolved(resolved=False)
    if unresolved:
        click.echo(f"\n  Unresolved imports: {len(unresolved)} — run: python main.py ingest")

    click.echo("")


@cli.command("import-harddrive")
@click.option("--dry-run", is_flag=True, help="Preview what would be added without importing.")
@click.option("--no-attrs", is_flag=True, help="Skip taste attribute inference (faster).")
def import_harddrive_cmd(dry_run, no_attrs):
    """Import films from the hard drive catalog (data/harddrive_catalog.json)."""
    try:
        harddrive_import.import_harddrive(dry_run=dry_run, infer_attrs=not no_attrs)
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)


if __name__ == "__main__":
    cli()
