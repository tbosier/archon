"""Reconcile live worker state into the DB and advance the task graph.

A background loop in the TUI calls :func:`reconcile_once` on an interval. One
pass:

1. polls the execution backend for every live run and syncs status/cost,
2. detects completion — either the backend reporting ``done`` or a provider
   Stop/SessionEnd hook having already flipped a run to ``done`` in the DB — and
   runs the existing handoff (execute → review → test) via
   :func:`archon.dispatcher.complete_task`,
3. ticks the scheduler so newly-ready work dispatches.

This is what makes the orchestrator self-advancing: no second terminal running
``archon schedule --watch`` and no manual ``archon complete`` are required.

Honest limitation: long-lived agent-deck sessions report ``idle``/``running``
(not ``done``) when an agent finishes a *turn*, so backend polling alone cannot
tell "the agent is waiting for me" from "the task is finished". Reliable
completion therefore depends on the Stop/SessionEnd hook (step 2). Backend
polling still catches crashes (``error``) and cost updates.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import budget, db, dispatcher, jobs, scheduler
from .backends import WorkerHandle

_LIVE = ("running", "starting")
_TERMINAL = ("done", "failed")


@dataclass
class ReconcileResult:
    reconciled: list[str] = field(default_factory=list)  # run ids whose status/cost changed
    completed: list[str] = field(default_factory=list)   # task ids marked done this pass
    failed: list[str] = field(default_factory=list)      # run ids marked failed this pass
    dispatched: list[str] = field(default_factory=list)  # task ids dispatched by the tick


def reconcile_once(conn: sqlite3.Connection, config, *, backend, launch=None) -> ReconcileResult:
    """Run one reconcile pass. Never raises on backend errors."""
    result = ReconcileResult()
    completed_tasks: set[str] = set()

    runs = db.list_task_runs(conn)

    # 1. Poll live runs against the backend.
    for run in runs:
        if run["status"] not in _LIVE or not run["provider_session_id"]:
            continue
        handle = WorkerHandle(
            backend_id=run["provider_session_id"],
            title=run["provider_session_name"] or run["provider_session_id"],
        )
        try:
            status = backend.status(handle)
        except Exception:
            # Backend hiccup — leave the run untouched, try again next pass.
            continue

        state = (status.state or "").lower()
        run_id = run["id"]
        task_id = run["task_id"]

        if state == "done":
            if _complete_task_once(conn, config, task_id, completed_tasks):
                result.completed.append(task_id)
                _mark(result.reconciled, run_id)
        elif state == "error":
            db.set_task_run_status(conn, run_id, "failed")
            result.failed.append(run_id)
            _mark(result.reconciled, run_id)
        # running / waiting / idle / missing → leave as-is (completion comes from
        # 'done' above or the hook-driven path below).

        if status.cost_usd is not None and float(status.cost_usd) != float(run["cost_usd"] or 0.0):
            db.update_task_run(conn, run_id, cost_usd=float(status.cost_usd))
            _mark(result.reconciled, run_id)

    # 2. Pick up hook-driven completion: a Stop/SessionEnd hook may have set a run
    #    to 'done' while its task is still queued/running. Advance those too.
    for run in db.list_task_runs(conn):
        if run["status"] != "done":
            continue
        task = db.get_task(conn, run["task_id"])
        if task is None or task["status"] in _TERMINAL:
            continue
        if _complete_task_once(conn, config, run["task_id"], completed_tasks):
            result.completed.append(run["task_id"])

    # 3. Advance the queue.
    launch = launch or dispatcher.make_scheduler_launch(dry_run=False)
    decision = scheduler.tick(conn, config, launch=launch, budget_policy=budget.policy)
    result.dispatched = list(decision.dispatched)
    return result


def _complete_task_once(conn, config, task_id: str, seen: set[str]) -> bool:
    """Complete a task at most once per pass; skip if already terminal."""
    if task_id in seen:
        return False
    task = db.get_task(conn, task_id)
    if task is None or task["status"] in _TERMINAL:
        seen.add(task_id)
        return False
    _finish_task(conn, task_id)
    seen.add(task_id)
    return True


def _finish_task(conn, task_id: str) -> None:
    """Mark a task (and its live runs) done and close the job when all are done.

    Unlike ``dispatcher.complete_task`` this deliberately does NOT run
    ``handoff.on_feature_done``: planner-built jobs already carry explicit
    review/test tasks with dependency edges, so the scheduler releases them once
    this task is ``done``. Synthesising more here would duplicate them.
    """
    for r in db.list_task_runs(conn):
        if r["task_id"] == task_id and r["status"] in ("running", "starting", "blocked", "stale"):
            db.set_task_run_status(conn, r["id"], "done")
    db.set_task_status(conn, task_id, "done")

    task = db.get_task(conn, task_id)
    job_id = task["job_id"] if task is not None else None
    if job_id:
        remaining = [
            t for t in db.list_job_tasks(conn, job_id)
            if t["status"] not in _TERMINAL
        ]
        if not remaining:
            jobs.mark_finished(conn, job_id, "complete")


def _mark(bucket: list[str], value: str) -> None:
    if value not in bucket:
        bucket.append(value)
