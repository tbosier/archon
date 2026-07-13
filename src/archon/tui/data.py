"""View-model builders for the Textual TUI.

Pure functions that read the Archon SQLite DB and produce plain dataclasses the
widgets render. Keeping the DB reads here (out of the widgets) makes the shape of
the dashboard testable without spinning up a Textual app.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .. import budget, db
from ..models import health_of

# Job status → sort bucket (lower sorts first). Attention overrides everything.
_JOB_BUCKET = {
    "running": 1,
    "verifying": 1,
    "planning": 1,
    "attention_required": 0,
    "awaiting_plan_approval": 2,
    "intake": 2,
    "queued": 2,
    "complete": 4,
    "done": 4,
    "cancelled": 5,
}


@dataclass
class TaskNode:
    id: str
    phase: str
    status: str
    name: str
    tool: str
    tier: str
    model: str | None
    cost_usd: float
    run_id: str | None = None
    session_id: str | None = None
    session_name: str | None = None
    worktree_path: str | None = None
    branch: str | None = None

    @property
    def glyph(self) -> tuple[str, str]:
        glyph, color, _ = health_of(self.status)
        return glyph, color


@dataclass
class JobNode:
    id: str
    title: str
    repo_name: str
    status: str
    open_attention: int
    tasks: list[TaskNode] = field(default_factory=list)


@dataclass
class AttentionRow:
    id: str
    kind: str
    severity: str
    title: str
    summary: str | None
    job_id: str | None
    job_title: str | None
    task_run_id: str | None
    session_id: str | None
    session_name: str | None
    options: list[str]
    recommended: str | None

    @property
    def is_plan_approval(self) -> bool:
        return self.kind == "plan_approval"


@dataclass
class Snapshot:
    jobs: list[JobNode]
    attention: list[AttentionRow]
    budget_line: str
    budget_action: str
    cost_usd: float
    hard_usd: float | None

    @property
    def header_budget(self) -> str:
        if self.hard_usd is not None:
            return f"budget ${self.cost_usd:.2f}/${self.hard_usd:.0f}"
        return f"spend ${self.cost_usd:.2f}"


def _runs_by_task(conn: sqlite3.Connection) -> dict[str, dict]:
    """Map task_id → {cost, latest run row}. Rows arrive newest-first."""
    acc: dict[str, dict] = {}
    for run in db.list_task_runs(conn):
        entry = acc.setdefault(run["task_id"], {"cost": 0.0, "latest": run})
        entry["cost"] += float(run["cost_usd"] or 0.0)
    return acc


def _task_nodes(conn: sqlite3.Connection, job_id: str, runs: dict[str, dict]) -> list[TaskNode]:
    nodes: list[TaskNode] = []
    for t in db.list_job_tasks(conn, job_id):
        run = runs.get(t["id"])
        latest = run["latest"] if run else None
        nodes.append(
            TaskNode(
                id=t["id"],
                phase=t["phase"] or "-",
                status=t["status"],
                name=t["name"],
                tool=t["provider_id"] or "-",
                tier=t["model_tier"] or "-",
                model=t["model"],
                cost_usd=run["cost"] if run else 0.0,
                run_id=latest["id"] if latest else None,
                session_id=latest["provider_session_id"] if latest else None,
                session_name=latest["provider_session_name"] if latest else None,
                worktree_path=latest["worktree_path"] if latest else None,
                branch=latest["branch"] if latest else None,
            )
        )
    return nodes


def _job_sort_key(job: JobNode) -> tuple[int, str]:
    if job.open_attention:
        bucket = 0
    else:
        bucket = _JOB_BUCKET.get(job.status, 3)
    return (bucket, job.id)


def build_jobs(conn: sqlite3.Connection) -> list[JobNode]:
    runs = _runs_by_task(conn)
    jobs = [
        JobNode(
            id=j["id"],
            title=j["title"],
            repo_name=j["repo_name"] or "-",
            status=j["status"],
            open_attention=j["open_attention_count"],
            tasks=_task_nodes(conn, j["id"], runs),
        )
        for j in db.list_jobs(conn)
    ]
    jobs.sort(key=_job_sort_key)
    return jobs


def build_attention(conn: sqlite3.Connection) -> list[AttentionRow]:
    rows: list[AttentionRow] = []
    for item in db.list_attention_items(conn, status="open"):
        try:
            options = json.loads(item["options_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            options = []
        session_id = session_name = None
        if item["task_run_id"]:
            run = db.find_task_run(conn, item["task_run_id"])
            if run is not None:
                session_id = run["provider_session_id"]
                session_name = run["provider_session_name"]
        rows.append(
            AttentionRow(
                id=item["id"],
                kind=item["kind"],
                severity=item["severity"],
                title=item["title"],
                summary=item["summary"],
                job_id=item["job_id"],
                job_title=item["job_title"],
                task_run_id=item["task_run_id"],
                session_id=session_id,
                session_name=session_name,
                options=options,
                recommended=item["recommended_option"],
            )
        )
    return rows


def build_snapshot(conn: sqlite3.Connection, config) -> Snapshot:
    status = budget.evaluate(conn, config)
    return Snapshot(
        jobs=build_jobs(conn),
        attention=build_attention(conn),
        budget_line=budget.describe(status),
        budget_action=status.action,
        cost_usd=status.total_cost_usd,
        hard_usd=status.hard_usd,
    )
