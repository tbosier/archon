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

from .. import db
from ..models import STATUS_COLORS, health_of, run_urgency


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


def jobs_table(conn: sqlite3.Connection) -> Table:
    table = Table(title="JOBS", title_style="bold cyan", expand=True, header_style="bold")
    for col in ("Job", "Repo", "Status", "Open Decisions", "Title"):
        table.add_column(col, overflow="fold")
    for j in db.list_jobs(conn):
        table.add_row(
            j["id"],
            j["repo_name"] or "-",
            _status_text(j["status"]),
            str(j["open_attention_count"]),
            j["title"],
        )
    if not table.rows:
        table.add_row("(no jobs yet)", "", "", "", "")
    return table


def attention_table(conn: sqlite3.Connection) -> Table:
    table = Table(title="ATTENTION REQUIRED", title_style="bold yellow", expand=True, header_style="bold")
    for col in ("Item", "Kind", "Severity", "Job", "Decision"):
        table.add_column(col, overflow="fold")
    for item in db.list_attention_items(conn, status="open"):
        table.add_row(
            item["id"],
            item["kind"],
            _status_text(item["severity"]),
            item["job_title"] or "-",
            item["title"],
        )
    if not table.rows:
        table.add_row("(none)", "", "", "", "")
    return table


def render(conn: sqlite3.Connection) -> Group:
    return Group(
        Panel.fit(
            Text("ARCHON", style="bold cyan") + Text("  ·  parallel AI coding cockpit", style="dim"),
            border_style="cyan",
        ),
        jobs_table(conn),
        Text(""),
        attention_table(conn),
        Text(""),
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


# --------------------------------------------------------------------------- #
# Cross-provider session dashboard (the pivot's headline view).
# --------------------------------------------------------------------------- #

def _age(updated_at: str | None) -> str:
    if not updated_at:
        return "-"
    import datetime
    try:
        ts = datetime.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    secs = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()
    if secs < 0:
        return "now"
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs // 60)}m"
    return f"{int(secs // 3600)}h"


def render_sessions(sessions) -> Group:
    """Render the unified cross-provider session list (`archon sessions`)."""
    from ..sessions.model import summarize, usage_line

    counts = summarize(sessions)
    usage_bits: list[tuple[str, str]] = []
    total_cost = sum(s.cost_usd or 0 for s in sessions)
    total_credits = sum(s.ai_credits or 0 for s in sessions)
    total_tokens = sum(s.total_tokens or 0 for s in sessions)
    if total_cost:
        usage_bits.append((f"${total_cost:.2f}", "yellow"))
    if total_credits:
        usage_bits.append((f"{total_credits:g} cr", "yellow"))
    if total_tokens:
        from ..sessions.model import _compact_int
        usage_bits.append((f"{_compact_int(total_tokens)} tok", "yellow"))
    usage_fragments: list[tuple[str, str]] = []
    if usage_bits:
        usage_fragments.append(("usage ", "dim"))
        for i, bit in enumerate(usage_bits):
            if i:
                usage_fragments.append(("  ", "dim"))
            usage_fragments.append(bit)
    header = Text.assemble(
        ("ARCHON", "bold cyan"), ("   ", ""),
        (f"{counts['working']} working", "green"), ("   ", "dim"),
        (f"{counts['need_you']} need you", "bold yellow"), ("   ", "dim"),
        (f"{counts['idle']} idle", "dim"), ("   ", "dim"),
        (f"{counts['failed']} failed", "red"), ("   ", "dim"),
        (f"{counts['done']} done", "green"),
        ("   ", "dim"),
        *usage_fragments,
    )

    table = Table(show_header=True, header_style="bold", expand=True, box=None, pad_edge=False)
    table.add_column("", width=1, no_wrap=True)
    table.add_column("Session", min_width=20, max_width=42, overflow="ellipsis", no_wrap=True)
    table.add_column("Provider", width=8, overflow="ellipsis", no_wrap=True)
    table.add_column("State", width=10, overflow="ellipsis", no_wrap=True)
    table.add_column("Usage", width=18, overflow="ellipsis", no_wrap=True)
    table.add_column("Doing", min_width=28, ratio=1, overflow="ellipsis", no_wrap=True)
    table.add_column("Age", width=5, overflow="ellipsis", no_wrap=True)
    for s in sessions:
        glyph, color = s.glyph
        usage = usage_line(s)
        table.add_row(
            Text(glyph, style=f"bold {color}"),
            Text(s.title or s.repo or s.session_id, style="bold"),
            Text(s.provider.upper(), style="cyan"),
            Text(s.label, style=("bold yellow" if s.needs_attention else color)),
            Text(usage or "-", style=("yellow" if usage else "dim")),
            Text(s.summary or "-", style="dim"),
            Text(_age(s.updated_at), style="dim"),
        )
    if not sessions:
        table.add_row("", Text("(no agent sessions discovered)", style="dim"), "", "", "", "", "")

    return Group(
        Panel.fit(header, border_style="cyan"),
        table,
        Text("discovered from Claude / Codex / Copilot / Archon", style="dim"),
    )


def show_sessions(sessions, console: Console | None = None) -> None:
    (console or Console()).print(render_sessions(sessions))
