"""Typer CLI — the `dromos` command."""
from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from dromos.config import load_settings

app = typer.Typer(help="dromos — extract, shape and visualise running data.")
console = Console()


@app.callback()
def _utf8_output() -> None:
    # Activity names hold emoji and umlauts that Windows' cp1252 can't encode when
    # output is piped or redirected.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _count_files(directory: Path) -> tuple[int, int]:
    files = [p for p in directory.rglob("*") if p.is_file() and p.name != ".gitkeep"]
    return len(files), sum(p.stat().st_size for p in files)


@app.command()
def status():
    """Show what is in the local archive."""
    settings = load_settings()
    table = Table(title="dromos archive")
    table.add_column("Store")
    table.add_column("Path")
    table.add_column("Files", justify="right")
    table.add_column("MB", justify="right")
    for name, directory in (("raw", settings.raw_dir), ("processed", settings.processed_dir)):
        if directory.exists():
            count, size = _count_files(directory)
            table.add_row(name, str(directory), str(count), f"{size / 1e6:.1f}")
        else:
            table.add_row(name, str(directory), "missing", "-")
    console.print(table)


garmin_app = typer.Typer(help="Export from Garmin Connect (unofficial, throttled, resumable).")
app.add_typer(garmin_app, name="garmin")


def _credentials() -> tuple[str | None, str | None]:
    import os

    from dotenv import load_dotenv

    load_dotenv()
    return os.getenv("GARMIN_EMAIL") or None, os.getenv("GARMIN_PASSWORD") or None


@garmin_app.command("login")
def garmin_login():
    """Log in once (asks for the MFA code if needed) and save the session to tokens/."""
    from dromos import garmin

    email, password = _credentials()
    garmin.login(email, password, prompt_mfa=lambda: typer.prompt("Garmin MFA code"))
    console.print("[green]Logged in, session saved.[/green]")


def _session():
    """(settings, logged-in Fetcher) with file logging switched on."""
    from dromos import garmin

    raw = load_settings().raw_dir
    garmin.setup_logging(raw)
    return raw, garmin.Fetcher(garmin.login(*_credentials()))


def _run(step):
    from dromos import garmin

    try:
        step()
    except garmin.ExportAborted as e:
        garmin.log.error("STOPPED: %s (progress is saved; re-run to continue)", e)
        raise typer.Exit(1)
    except KeyboardInterrupt:
        garmin.log.warning("interrupted by user (everything fetched so far is saved)")
        raise typer.Exit(130)


@garmin_app.command("activities")
def garmin_activities():
    """Download every activity (original file + detail JSON) into data/raw/."""
    from dromos import garmin

    raw, f = _session()
    _run(lambda: garmin.download_activities(f, raw, garmin.list_activities(f, raw)))
    garmin.log_report(garmin.verify(raw, wellness=False))


@garmin_app.command("extras")
def garmin_extras():
    """Splits, weather, gear, power zones for runs; exercise sets for strength. Runs first."""
    from dromos import garmin

    raw, f = _session()
    _run(lambda: garmin.download_extras(f, raw, garmin.list_activities(f, raw)))
    garmin.log_report(garmin.verify(raw, wellness=False))


@garmin_app.command("wellness")
def garmin_wellness(since: str = typer.Option(None, help="YYYY-MM-DD; default: date of first activity")):
    """Download per-day wellness JSON (sleep, HRV, stats, training status, VO2max)."""
    from datetime import date

    from dromos import garmin

    raw, f = _session()

    def step():
        start = date.fromisoformat(since) if since else garmin.earliest_activity_date(garmin.list_activities(f, raw))
        garmin.download_wellness(f, raw, start)

    _run(step)
    garmin.log_report(garmin.verify(raw))


@garmin_app.command("all")
def garmin_all():
    """Activities, then wellness, then a completeness check. Re-run any time to fill gaps."""
    from dromos import garmin

    raw, f = _session()

    def step():
        acts = garmin.list_activities(f, raw)
        garmin.download_activities(f, raw, acts)
        garmin.download_extras(f, raw, acts)
        garmin.download_wellness(f, raw, garmin.earliest_activity_date(acts))

    _run(step)
    garmin.log_report(garmin.verify(raw))
    if f.failures:
        garmin.log.warning("%d items failed for good this run (see log); re-run to retry them", len(f.failures))


@garmin_app.command("verify")
def garmin_verify(fix: bool = typer.Option(False, help="Delete corrupt files so the next run refetches them")):
    """Check data/raw against the activity list: missing, corrupt and leftover files. No network."""
    from dromos import garmin

    raw = load_settings().raw_dir
    garmin.setup_logging(raw)
    garmin.log_report(garmin.verify(raw, fix=fix))
