"""Command-bar planning glue for the TUI.

Wraps the exact same pipeline `archon do` uses — ``planner.plan_with_llm`` →
``policy.validate_plan`` → ``planner.persist_plan`` → ``scheduler.tick`` — so the
interactive command bar and the headless CLI stay behaviourally identical.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .. import budget, dispatcher, planner, policy, scheduler
from ..planner import PlanProposal


@dataclass
class DispatchOutcome:
    job_id: str
    task_count: int
    dispatched: list[str]
    skipped: list[str]


def propose(
    conn: sqlite3.Connection,
    config,
    ctx: dispatcher.RepoContext,
    message: str,
    *,
    planner_command: list[str] | None = None,
) -> PlanProposal:
    """Plan a natural-language outcome and run governance checks.

    Raises on planning or policy failure (callers surface it in the UI).
    """
    proposal = planner.plan_with_llm(
        message,
        repo_path=ctx.root,
        config=config,
        recent_job_titles=planner.recent_job_titles(conn, ctx.repo_id or 0),
        planner_command=planner_command,
    )
    policy.validate_plan(proposal, config)
    return proposal


def dispatch(
    conn: sqlite3.Connection,
    config,
    ctx: dispatcher.RepoContext,
    proposal: PlanProposal,
) -> DispatchOutcome:
    """Persist an approved plan and advance the scheduler one tick."""
    policy.validate_plan(proposal, config)
    job, tasks = planner.persist_plan(conn, config, ctx, proposal)
    launch = dispatcher.make_scheduler_launch(dry_run=False)
    decision = scheduler.tick(conn, config, launch=launch, budget_policy=budget.policy)
    return DispatchOutcome(
        job_id=job.id,
        task_count=len(tasks),
        dispatched=list(decision.dispatched),
        skipped=list(decision.skipped),
    )


def render_preview(proposal: PlanProposal) -> str:
    """Reuse the CLI's plan rendering so preview text matches ``archon do``."""
    return planner.render_plan(proposal)
