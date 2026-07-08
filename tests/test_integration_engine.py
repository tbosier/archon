"""Integration tests for the queue-driven engine wired through the dispatcher.

Exercises enqueue → scheduler dispatch → model tiering → completion handoff,
all in dry-run so nothing touches Zellij or Git for real.
"""

import subprocess

import pytest

from archon import budget, db, dispatcher, scheduler
from archon.config import default_config


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "ci_amplify_ai"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=repo, check=True,
    )
    return repo


@pytest.fixture()
def ctx(conn, git_repo):
    return dispatcher.register_repo(conn, dispatcher.resolve_repo_context(git_repo))


@pytest.fixture()
def cfg():
    c = default_config()
    c.providers["claude"].enabled = True
    return c


def test_enqueue_feature_creates_plan_execute_chain(conn, cfg, ctx):
    chain = dispatcher.enqueue_feature(conn, cfg, ctx, feature_name="btn", provider_id="claude")
    assert chain["plan"] is not None and chain["execute"] is not None
    # execute depends on plan
    assert chain["plan"].id in db.dependencies_of(conn, chain["execute"].id)
    # only the plan is ready initially
    from archon import queue
    ready = [t["id"] for t in queue.ready_tasks(conn)]
    assert ready == [chain["plan"].id]


def test_scheduler_dispatches_plan_with_strong_model(conn, cfg, ctx):
    dispatcher.enqueue_feature(conn, cfg, ctx, feature_name="btn", provider_id="claude")
    launch = dispatcher.make_scheduler_launch(dry_run=True)
    decision = scheduler.tick(conn, cfg, launch=launch, budget_policy=budget.policy)
    assert len(decision.dispatched) == 1
    run = next(r for r in db.list_task_runs(conn) if r["phase"] == "plan")
    assert run["model"] == "claude-opus-4-8"          # plan → strong tier


def test_execute_uses_cheaper_model_after_plan(conn, cfg, ctx):
    chain = dispatcher.enqueue_feature(conn, cfg, ctx, feature_name="btn", provider_id="claude")
    dispatcher.complete_task(conn, cfg, chain["plan"].id)          # plan done → execute ready
    launch = dispatcher.make_scheduler_launch(dry_run=True)
    scheduler.tick(conn, cfg, launch=launch, budget_policy=budget.policy)
    run = next(r for r in db.list_task_runs(conn) if r["phase"] == "execute")
    assert run["model"] == "claude-sonnet-5"          # execute → cheaper tier


def test_completion_triggers_reviewer_tester_handoff(conn, cfg, ctx):
    chain = dispatcher.enqueue_feature(conn, cfg, ctx, feature_name="btn", provider_id="claude")
    result = dispatcher.complete_task(conn, cfg, chain["execute"].id)
    handoff = result["handoff"]
    assert set(handoff) == {"review", "test"}
    # review depends on execute; test depends on review
    assert chain["execute"].id in db.dependencies_of(conn, handoff["review"].id)
    assert handoff["review"].id in db.dependencies_of(conn, handoff["test"].id)
    # review carries the review phase (→ strong analytical model tier)
    assert handoff["review"].phase == "review"
    assert handoff["test"].phase == "test"


def test_full_chain_drains_in_order(conn, cfg, ctx):
    """plan → execute → review → test dispatch in dependency order when each
    completes (launch marks the run, complete_task advances the task)."""
    chain = dispatcher.enqueue_feature(conn, cfg, ctx, feature_name="btn", provider_id="claude")
    launch = dispatcher.make_scheduler_launch(dry_run=True)

    order = []
    # plan
    scheduler.tick(conn, cfg, launch=launch, budget_policy=budget.policy)
    order.append("plan")
    dispatcher.complete_task(conn, cfg, chain["plan"].id)
    # execute
    scheduler.tick(conn, cfg, launch=launch, budget_policy=budget.policy)
    order.append("execute")
    res = dispatcher.complete_task(conn, cfg, chain["execute"].id)
    # review + test now queued via handoff
    scheduler.tick(conn, cfg, launch=launch, budget_policy=budget.policy)
    dispatcher.complete_task(conn, cfg, res["handoff"]["review"].id)
    scheduler.tick(conn, cfg, launch=launch, budget_policy=budget.policy)

    phases = [r["phase"] for r in sorted(db.list_task_runs(conn), key=lambda r: r["id"])]
    assert set(phases) == {"plan", "execute", "review", "test"}


def test_budget_pause_blocks_dispatch(conn, cfg, ctx):
    cfg.scheduler.budget.hard_usd = 0.0     # already over budget → pause
    dispatcher.enqueue_feature(conn, cfg, ctx, feature_name="btn", provider_id="claude")
    launch = dispatcher.make_scheduler_launch(dry_run=True)
    decision = scheduler.tick(conn, cfg, launch=launch, budget_policy=budget.policy)
    assert decision.paused and decision.reason == "budget"
    assert not decision.dispatched
