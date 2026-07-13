"""Task queue: enqueue work, find what's ready, cancel, count pending.

Thin, boring wrappers over :mod:`archon.db`. The queue never touches Zellij or
Git — it only manipulates rows, so it is fully testable in-memory.
"""

from __future__ import annotations

import sqlite3

from . import db
from .models import Task
from .util import new_task_id


def enqueue_task(
    conn: sqlite3.Connection,
    *,
    repo_id: int,
    type: str,
    name: str,
    prompt: str,
    phase: str = "execute",
    priority: int = 0,
    parent_task_id: str | None = None,
    provider_id: str | None = None,
    provider_policy: str = "single",
    pr_number: int | None = None,
    job_id: str | None = None,
    model_tier: str | None = None,
    model: str | None = None,
    depends_on: list[str] | None = None,
) -> Task:
    """Insert a fresh ``queued`` task and wire up its dependencies.

    Mints a unique id, inserts the row, then records one dependency edge for each
    id in ``depends_on``. Returns the in-memory :class:`Task`.
    """
    task = Task(
        id=new_task_id(),
        repo_id=repo_id,
        type=type,
        name=name,
        status="queued",
        prompt=prompt,
        provider_policy=provider_policy,
        priority=priority,
        pr_number=pr_number,
        phase=phase,
        parent_task_id=parent_task_id,
        provider_id=provider_id,
        job_id=job_id,
        model_tier=model_tier,
        model=model,
    )
    db.insert_task(conn, task)
    for dep_id in depends_on or []:
        db.add_dependency(conn, task.id, dep_id)
    return task


def ready_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Full task rows for every queued task whose dependencies are all ``done``.

    Preserves the scheduler ordering from :func:`db.ready_task_ids`
    (priority DESC, then created_at ASC).
    """
    rows = []
    for task_id in db.ready_task_ids(conn):
        row = db.get_task(conn, task_id)
        if row is not None:
            rows.append(row)
    return rows


def pending_count(conn: sqlite3.Connection) -> int:
    """How many tasks are still ``queued`` (waiting to run)."""
    row = conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE status='queued'"
    ).fetchone()
    return int(row["c"])


def cancel_task(conn: sqlite3.Connection, task_id: str) -> None:
    """Mark a task ``failed`` unless it has already finished (``done``)."""
    row = db.get_task(conn, task_id)
    if row is None or row["status"] == "done":
        return
    db.set_task_status(conn, task_id, "failed")
