"""The scheduler: one ``tick`` decides what to launch given concurrency + budget.

The scheduler is deliberately side-effect-light and injectable. Real dispatch
(Zellij panes, worktrees, provider processes) is handled by the ``launch``
callable passed in, so the whole engine is testable with a stub.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from . import db, queue

# launch(conn, config, task_row) -> None. Performs the real dispatch.
LaunchFn = Callable[[sqlite3.Connection, object, sqlite3.Row], None]
# budget_policy(conn, config) -> one of the actions below.
BudgetFn = Callable[[sqlite3.Connection, object], str]

_BUDGET_ACTIONS = ("allow", "prefer_small", "no_new_impl", "pause")


@dataclass
class SchedulerDecision:
    dispatched: list[str] = field(default_factory=list)   # task ids launched this tick
    skipped: list[str] = field(default_factory=list)      # ready but not launched
    paused: bool = False
    reason: str = ""
    budget_action: str = "allow"


def tick(
    conn: sqlite3.Connection,
    config,
    *,
    launch: LaunchFn,
    budget_policy: BudgetFn | None = None,
) -> SchedulerDecision:
    """Advance the queue by one step: dispatch ready tasks within the budget.

    See the module/build-spec contract for the exact ordering of checks.
    """
    if db.get_scheduler_state(conn, "paused") == "1":
        return SchedulerDecision(paused=True, reason="paused")

    action = budget_policy(conn, config) if budget_policy else "allow"
    if action == "pause":
        return SchedulerDecision(paused=True, reason="budget", budget_action="pause")

    decision = SchedulerDecision(budget_action=action)

    slots = max(0, config.scheduler.max_concurrency - db.count_running_runs(conn))
    per_provider = config.scheduler.per_provider_concurrency

    for row in queue.ready_tasks(conn):
        if slots <= 0:
            decision.skipped.append(row["id"])
            continue

        if not _budget_allows(action, row):
            decision.skipped.append(row["id"])
            continue

        provider_id = row["provider_id"]
        if provider_id and db.count_running_runs(conn, provider_id) >= per_provider:
            decision.skipped.append(row["id"])
            continue

        launch(conn, config, row)
        # Mark running unless launch already advanced the task itself (e.g. a
        # synchronous test launch that runs the work to 'done' inline). This
        # keeps real async launches at 'running' while letting run_until_idle
        # drain a chain when launch completes work eagerly.
        current = db.get_task(conn, row["id"])
        if current is not None and current["status"] == "queued":
            db.set_task_status(conn, row["id"], "running")
        decision.dispatched.append(row["id"])
        slots -= 1

    return decision


def _budget_allows(action: str, row: sqlite3.Row) -> bool:
    """Whether the current budget action permits dispatching ``row``."""
    if action == "prefer_small":
        # Only cheap analytical follow-ups: review/test.
        return row["phase"] in ("review", "test")
    if action == "no_new_impl":
        # Hold back new implementation work.
        if row["phase"] in ("plan", "execute"):
            return False
        if row["type"] == "feature":
            return False
    return True


def pause(conn: sqlite3.Connection) -> None:
    db.set_scheduler_state(conn, "paused", "1")


def resume(conn: sqlite3.Connection) -> None:
    db.set_scheduler_state(conn, "paused", "0")


def is_paused(conn: sqlite3.Connection) -> bool:
    return db.get_scheduler_state(conn, "paused") == "1"


def run_until_idle(
    conn: sqlite3.Connection,
    config,
    *,
    launch: LaunchFn,
    budget_policy: BudgetFn | None = None,
    max_ticks: int = 100,
) -> list[SchedulerDecision]:
    """Tick repeatedly until nothing dispatches (or ``max_ticks`` reached).

    Intended for tests/automation where ``launch`` also advances the task to
    ``done`` so dependents become ready on the next tick.
    """
    decisions: list[SchedulerDecision] = []
    for _ in range(max_ticks):
        decision = tick(conn, config, launch=launch, budget_policy=budget_policy)
        decisions.append(decision)
        if not decision.dispatched:
            break
    return decisions
