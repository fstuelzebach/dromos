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
