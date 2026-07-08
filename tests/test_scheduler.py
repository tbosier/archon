"""Tests for the scheduler tick loop: ordering, concurrency, pause, budget."""

from __future__ import annotations

import pytest

from archon import db, queue, scheduler, taskgraph
from archon.config import Config, SchedulerConfig
from archon.models import Repo, TaskRun
from archon.util import run_id_for


@pytest.fixture()
def conn():
    c = db.connect_memory()
    yield c
    c.close()


@pytest.fixture()
def repo_id(conn):
    return db.upsert_repo(conn, Repo(name="demo", root_path="/tmp/demo", zellij_session="demo"))


def _cfg(**scheduler_kw) -> Config:
    return Config(scheduler=SchedulerConfig(**scheduler_kw))


def _enqueue(conn, repo_id, name, **kw):
    return queue.enqueue_task(
        conn, repo_id=repo_id, type="feature", name=name, prompt=f"do {name}", **kw,
    )


def _done_launch(order: list[str]):
    """A launch stub that records dispatch order and completes work inline."""
    def launch(conn, config, row):
        order.append(row["id"])
        db.set_task_status(conn, row["id"], "done")
    return launch


def _running_run_launch():
    """A launch stub that opens a 'running' task_run for the task's provider."""
    def launch(conn, config, row):
        run = TaskRun(
            id=run_id_for(row["id"], row["provider_id"] or "x"),
            task_id=row["id"],
            provider_id=row["provider_id"] or "x",
            status="running",
        )
        db.insert_task_run(conn, run)
    return launch


def test_run_until_idle_drains_chain_in_order(conn, repo_id):
    a = _enqueue(conn, repo_id, "a")
    b = _enqueue(conn, repo_id, "b", depends_on=[a.id])
    c = _enqueue(conn, repo_id, "c", depends_on=[b.id])

    order: list[str] = []
    decisions = scheduler.run_until_idle(conn, _cfg(), launch=_done_launch(order))

    assert order == [a.id, b.id, c.id]
    assert db.get_task(conn, c.id)["status"] == "done"
    # last decision dispatched nothing (loop terminator)
    assert decisions[-1].dispatched == []


def test_max_concurrency_limits_dispatch_per_tick(conn, repo_id):
    # 5 independent ready tasks, but only 2 slots.
    ids = [_enqueue(conn, repo_id, f"t{i}").id for i in range(5)]

    def launch(conn, config, row):  # no state change: stays 'running'
        pass

    decision = scheduler.tick(conn, _cfg(max_concurrency=2), launch=launch)
    assert len(decision.dispatched) == 2
    assert len(decision.skipped) == 3
    assert set(decision.dispatched) | set(decision.skipped) == set(ids)


def test_pause_and_resume_gate_dispatch(conn, repo_id):
    _enqueue(conn, repo_id, "a")
    scheduler.pause(conn)
    assert scheduler.is_paused(conn) is True

    def launch(conn, config, row):
        raise AssertionError("should not launch while paused")

    decision = scheduler.tick(conn, _cfg(), launch=launch)
    assert decision.paused is True
    assert decision.reason == "paused"
    assert decision.dispatched == []

    scheduler.resume(conn)
    assert scheduler.is_paused(conn) is False
    order: list[str] = []
    decision = scheduler.tick(conn, _cfg(), launch=_done_launch(order))
    assert len(order) == 1


def test_budget_policy_pause(conn, repo_id):
    _enqueue(conn, repo_id, "a")

    def launch(conn, config, row):
        raise AssertionError("should not launch when budget pauses")

    decision = scheduler.tick(
        conn, _cfg(), launch=launch, budget_policy=lambda c, cfg: "pause"
    )
    assert decision.paused is True
    assert decision.reason == "budget"
    assert decision.budget_action == "pause"


def test_budget_prefer_small_skips_execute_dispatches_review(conn, repo_id):
    execute = _enqueue(conn, repo_id, "impl", phase="execute")
    review = _enqueue(conn, repo_id, "rev", phase="review")

    dispatched: list[str] = []

    def launch(conn, config, row):
        dispatched.append(row["id"])

    decision = scheduler.tick(
        conn, _cfg(), launch=launch, budget_policy=lambda c, cfg: "prefer_small"
    )
    assert review.id in decision.dispatched
    assert execute.id in decision.skipped
    assert decision.budget_action == "prefer_small"


def test_budget_no_new_impl_skips_feature(conn, repo_id):
    feat = _enqueue(conn, repo_id, "feat", phase="execute")
    test = queue.enqueue_task(
        conn, repo_id=repo_id, type="test", name="t", prompt="p", phase="test",
    )

    decision = scheduler.tick(
        conn, _cfg(), launch=lambda *a: None, budget_policy=lambda c, cfg: "no_new_impl"
    )
    assert feat.id in decision.skipped
    assert test.id in decision.dispatched


def test_per_provider_concurrency_blocks_second_run(conn, repo_id):
    a = _enqueue(conn, repo_id, "a", provider_id="claude")
    b = _enqueue(conn, repo_id, "b", provider_id="claude")

    decision = scheduler.tick(
        conn, _cfg(max_concurrency=5, per_provider_concurrency=1),
        launch=_running_run_launch(),
    )
    # First one launches (opening a running run for 'claude'); second is blocked.
    assert len(decision.dispatched) == 1
    assert len(decision.skipped) == 1
    assert decision.dispatched[0] in (a.id, b.id)
