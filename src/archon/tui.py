"""Rich-based dashboards for `archon status` and `archon tui`.

Renders a provider readiness table and a task-run table, colour-coded by status
and sorted by urgency (blocked first). Pure rendering — no external calls — so it
works against any populated Archon DB.
"""

from __future__ import annotations

import sqlite3
import time

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import db
from .models import STATUS_COLORS, health_of, run_urgency


def _status_text(status: str) -> Text:
    return Text(status, style=STATUS_COLORS.get(status, "white"))


def _health_text(status: str) -> Text:
    glyph, color, _ = health_of(status)
    return Text(glyph, style=f"bold {color}")


def _yesno(value: bool) -> Text:
    return Text("yes", style="green") if value else Text("no", style="dim")


def providers_table(conn: sqlite3.Connection) -> Table:
    table = Table(title="PROVIDERS", title_style="bold cyan", expand=True, header_style="bold")
    for col in ("Provider", "Enabled", "Installed", "Auth", "Mode", "Command", "Last checked"):
        table.add_column(col)
    for p in db.list_providers(conn):
        table.add_row(
            p.id,
            _yesno(p.enabled),
            _yesno(p.installed),
            _status_text(p.auth_status),
            p.default_mode,
            p.command,
            p.last_checked_at or "-",
        )
    if not table.rows:
        table.add_row("(none configured)", "", "", "", "", "", "")
    return table


def _fmt_cost_tokens(row: sqlite3.Row) -> str:
    parts = []
    if row["cost_usd"]:
        parts.append(f"${row['cost_usd']:.2f}")
    if row["total_tokens"]:
        parts.append(f"{row['total_tokens'] // 1000}k tok")
    return "  ".join(parts) or "-"


def task_runs_table(conn: sqlite3.Connection) -> Table:
    table = Table(title="TASK RUNS", title_style="bold cyan", expand=True, header_style="bold")
    for col in ("", "Repo", "Task", "Provider", "Phase", "Model", "State", "Branch"):
        table.add_column(col, overflow="fold")
    rows = sorted(db.list_task_runs(conn), key=lambda r: run_urgency(r["status"]))
    for r in rows:
        table.add_row(
            _health_text(r["status"]),
            r["repo_name"],
            r["task_name"],
            r["provider_id"],
            r["phase"] or "-",
            r["model"] or "-",
            _status_text(r["status"]),
            r["branch"] or "-",
        )
    if not table.rows:
        table.add_row("", "(no task runs yet)", "", "", "", "", "", "")
    return table


def health_legend() -> Text:
    return Text.assemble(
        ("  ● ", "bold green"), ("working   ", "dim"),
        ("● ", "bold yellow"), ("needs help   ", "dim"),
        ("● ", "bold red"), ("problem   ", "dim"),
        ("✓ ", "bold green"), ("done   ", "dim"),
        ("○ ", "bold dim"), ("waiting", "dim"),
    )


def workers_table(conn: sqlite3.Connection) -> Table:
    table = Table(title="WORKER POOL", title_style="bold cyan", expand=True, header_style="bold")
    for col in ("Worker", "Provider", "State", "Current run"):
        table.add_column(col)
    for w in db.list_workers(conn):
        table.add_row(
            w["id"], w["provider_id"], _status_text(w["state"]),
            w["current_task_run_id"] or "-",
        )
    if not table.rows:
        table.add_row("(no workers)", "", "", "")
    return table


def render(conn: sqlite3.Connection) -> Group:
    return Group(
        Panel.fit(
            Text("ARCHON", style="bold cyan") + Text("  ·  parallel AI coding cockpit", style="dim"),
            border_style="cyan",
        ),
        providers_table(conn),
        Text(""),
        workers_table(conn),
        Text(""),
        task_runs_table(conn),
        health_legend(),
        Text("live · auto-refreshes every 2s · Ctrl-C to exit", style="dim"),
    )


def show_once(conn: sqlite3.Connection, console: Console | None = None) -> None:
    (console or Console()).print(render(conn))


def watch(conn: sqlite3.Connection, *, interval: float = 2.0, console: Console | None = None) -> None:
    console = console or Console()
    try:
        with Live(render(conn), console=console, refresh_per_second=4, screen=False) as live:
            while True:
                time.sleep(interval)
                live.update(render(conn))
    except KeyboardInterrupt:
        console.print("\n[dim]archon: dashboard closed[/dim]")
