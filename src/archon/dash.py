"""Read-only Archon dashboard entrypoint."""

from __future__ import annotations

import time

from rich.console import Console
from rich.live import Live

from . import db
from .paths import resolve_paths
from .sessions import default_registry
from .tui.tables import render_sessions


def main() -> None:
    """Watch the unified session registry without launching/interacting."""
    console = Console()
    conn = db.connect(resolve_paths().ensure())
    registry = default_registry(conn)
    try:
        with Live(render_sessions(registry.snapshot()), console=console, refresh_per_second=4, screen=False) as live:
            while True:
                time.sleep(2.0)
                live.update(render_sessions(registry.snapshot()))
    except KeyboardInterrupt:
        console.print("\n[dim]archon-dash closed[/dim]")
