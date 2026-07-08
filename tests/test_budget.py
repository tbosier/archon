"""Tests for the global budget + rate-limit policy (spec §14)."""

from __future__ import annotations

import uuid

import pytest

from archon import budget, db
from archon.config import default_config
from archon.models import Repo, Task, TaskRun


def _seed_run(conn, *, cost_usd: float = 0.0, five_hour_pct: float | None = None) -> str:
    """Insert a repo + task + task_run and set its cost / five-hour rate-limit."""
    repo_id = db.upsert_repo(
        conn,
        Repo(name="r", root_path=f"/tmp/{uuid.uuid4()}", zellij_session="s"),
    )
    task_id = uuid.uuid4().hex
    db.insert_task(
        conn,
        Task(
            id=task_id,
            repo_id=repo_id,
            type="feature",
            name="t",
            status="queued",
            prompt="do it",
        ),
    )
    run_id = uuid.uuid4().hex
    db.insert_task_run(
        conn,
        TaskRun(id=run_id, task_id=task_id, provider_id="claude"),
    )
    db.update_task_run(
        conn, run_id, cost_usd=cost_usd, rate_limit_five_hour_pct=five_hour_pct
    )
    return run_id


@pytest.fixture()
def conn():
    c = db.connect_memory()
    yield c
    c.close()


@pytest.fixture()
def config():
    c = default_config()
    c.scheduler.budget.hard_usd = 5.0
    c.scheduler.budget.soft_usd = 2.0
    return c


def test_fresh_db_allows(conn, config):
    status = budget.evaluate(conn, config)
    assert status.action == "allow"
    assert status.total_cost_usd == 0.0
    assert status.five_hour_pct == 0.0
    assert status.soft_usd == 2.0
    assert status.hard_usd == 5.0


def test_cost_above_hard_pauses(conn, config):
    _seed_run(conn, cost_usd=6.0)
    assert budget.policy(conn, config) == "pause"


def test_five_hour_96_pauses(conn, config):
    _seed_run(conn, five_hour_pct=96.0)
    status = budget.evaluate(conn, config)
    assert status.action == "pause"
    assert "rate limit" in status.reason


def test_five_hour_88_no_new_impl(conn, config):
    _seed_run(conn, five_hour_pct=88.0)
    assert budget.policy(conn, config) == "no_new_impl"


def test_five_hour_74_prefer_small(conn, config):
    _seed_run(conn, five_hour_pct=74.0)
    assert budget.policy(conn, config) == "prefer_small"


def test_five_hour_50_allows(conn, config):
    _seed_run(conn, five_hour_pct=50.0)
    assert budget.policy(conn, config) == "allow"


def test_soft_exceeded_rate_low_prefer_small(conn, config):
    _seed_run(conn, cost_usd=3.0, five_hour_pct=10.0)
    status = budget.evaluate(conn, config)
    assert status.action == "prefer_small"
    assert "soft budget" in status.reason


def test_hard_budget_priority_over_lower_rate_signal(conn, config):
    # Cost is over hard budget AND rate limit is only in "prefer_small" range.
    # Hard budget (rule 1) must win over the lower-severity rate signal.
    _seed_run(conn, cost_usd=10.0, five_hour_pct=74.0)
    status = budget.evaluate(conn, config)
    assert status.action == "pause"
    assert "hard budget" in status.reason


def test_policy_matches_evaluate_action(conn, config):
    _seed_run(conn, five_hour_pct=88.0)
    assert budget.policy(conn, config) == budget.evaluate(conn, config).action


def test_describe_non_empty_contains_action(conn, config):
    _seed_run(conn, five_hour_pct=96.0)
    status = budget.evaluate(conn, config)
    text = budget.describe(status)
    assert text
    assert status.action in text


def test_no_budgets_configured_only_rate_limit_drives(conn):
    # With soft/hard unset (defaults), cost never triggers; only rate limit does.
    c = default_config()
    assert c.scheduler.budget.soft_usd is None
    assert c.scheduler.budget.hard_usd is None
    _seed_run(conn, cost_usd=1000.0, five_hour_pct=0.0)
    assert budget.policy(conn, c) == "allow"
