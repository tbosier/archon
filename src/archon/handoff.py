"""Reviewer/tester handoff: build the plan→execute→review→test task chain.

These helpers translate config policy (``plan_before_execute``, ``auto_handoff``)
into concrete queued tasks plus the dependency edges that let the scheduler run
the chain in order. No Zellij/Git here — only queue + graph writes.
"""

from __future__ import annotations

import sqlite3

from . import queue, taskgraph
from .models import Task


def _first_enabled_provider(config) -> str | None:
    ids = config.enabled_provider_ids()
    return ids[0] if ids else None


def enqueue_feature_chain(
    conn: sqlite3.Connection,
    config,
    *,
    repo_id: int,
    feature_name: str,
    prompt: str,
    provider_id: str,
    base_execute_task_id: str | None = None,
) -> dict:
    """Queue a feature as ``plan -> execute`` (or just ``execute``).

    When ``config.scheduler.plan_before_execute`` is set, a plan task is created
    first and the execute task is made to depend on it. Both tasks are type
    ``feature`` and share ``parent_task_id`` = the plan task's id (or the execute
    task's own id when there is no plan). Returns ``{"plan": Task|None,
    "execute": Task}``.
    """
    plan_task: Task | None = None

    if config.scheduler.plan_before_execute:
        plan_task = queue.enqueue_task(
            conn,
            repo_id=repo_id,
            type="feature",
            name=f"{feature_name} (plan)",
            prompt=prompt,
            phase="plan",
            provider_id=provider_id,
        )
        parent_id = plan_task.id
    else:
        parent_id = None

    execute_task = queue.enqueue_task(
        conn,
        repo_id=repo_id,
        type="feature",
        name=feature_name,
        prompt=prompt,
        phase="execute",
        provider_id=provider_id,
        parent_task_id=parent_id,
        depends_on=[base_execute_task_id] if base_execute_task_id else None,
    )

    if plan_task is not None:
        # execute waits for plan; parent grouping already set above.
        taskgraph.chain(conn, [plan_task.id, execute_task.id])

    return {"plan": plan_task, "execute": execute_task}


def on_feature_done(conn: sqlite3.Connection, config, execute_task_row) -> dict:
    """After a feature's execute task finishes, queue review then test.

    Only fires when ``config.scheduler.auto_handoff`` is set; otherwise returns
    ``{}`` and enqueues nothing. review depends on the execute task; test depends
    on review. Both reuse the execute task's provider (falling back to the first
    enabled provider) and are grouped under the feature's parent task.
    """
    if not config.scheduler.auto_handoff:
        return {}

    execute_id = execute_task_row["id"]
    repo_id = execute_task_row["repo_id"]
    provider_id = execute_task_row["provider_id"] or _first_enabled_provider(config)
    parent_id = execute_task_row["parent_task_id"] or execute_id
    name = execute_task_row["name"]

    review_task = queue.enqueue_task(
        conn,
        repo_id=repo_id,
        type="review",
        name=f"{name} (review)",
        prompt=f"Review the changes produced by task {execute_id}.",
        phase="review",
        provider_id=provider_id,
        parent_task_id=parent_id,
        depends_on=[execute_id],
    )

    test_task = queue.enqueue_task(
        conn,
        repo_id=repo_id,
        type="test",
        name=f"{name} (test)",
        prompt=f"Run and verify tests for task {execute_id}.",
        phase="test",
        provider_id=provider_id,
        parent_task_id=parent_id,
        depends_on=[review_task.id],
    )

    return {"review": review_task, "test": test_task}
