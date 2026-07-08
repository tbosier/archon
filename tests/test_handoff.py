"""Tests for the reviewer/tester handoff chain builder."""

from __future__ import annotations

import pytest

from archon import db, handoff
from archon.config import Config, SchedulerConfig, default_config
from archon.models import Repo


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


def test_feature_chain_plan_then_execute(conn, repo_id):
    cfg = _cfg(plan_before_execute=True)
    result = handoff.enqueue_feature_chain(
        conn, cfg, repo_id=repo_id, feature_name="widget",
        prompt="build a widget", provider_id="claude",
    )
    plan, execute = result["plan"], result["execute"]
    assert plan is not None
    assert plan.phase == "plan"
    assert execute.phase == "execute"
    # execute depends on plan
    assert db.dependencies_of(conn, execute.id) == [plan.id]
    # both grouped under the plan task
    assert db.get_task(conn, execute.id)["parent_task_id"] == plan.id
    assert plan.type == "feature" and execute.type == "feature"


def test_feature_chain_execute_only_when_planning_disabled(conn, repo_id):
    cfg = _cfg(plan_before_execute=False)
    result = handoff.enqueue_feature_chain(
        conn, cfg, repo_id=repo_id, feature_name="widget",
        prompt="build a widget", provider_id="claude",
    )
    assert result["plan"] is None
    assert result["execute"].phase == "execute"
    assert db.dependencies_of(conn, result["execute"].id) == []


def test_on_feature_done_creates_review_and_test(conn, repo_id):
    cfg = _cfg(plan_before_execute=False, auto_handoff=True)
    chain = handoff.enqueue_feature_chain(
        conn, cfg, repo_id=repo_id, feature_name="widget",
        prompt="build a widget", provider_id="claude",
    )
    execute_row = db.get_task(conn, chain["execute"].id)

    result = handoff.on_feature_done(conn, cfg, execute_row)
    review, test = result["review"], result["test"]

    assert review.phase == "review"
    assert test.phase == "test"
    # review depends on execute; test depends on review
    assert db.dependencies_of(conn, review.id) == [execute_row["id"]]
    assert db.dependencies_of(conn, test.id) == [review.id]
    # provider reused, grouped under the feature's parent (its own id here)
    assert review.provider_id == "claude"
    assert db.get_task(conn, review.id)["parent_task_id"] == execute_row["id"]


def test_on_feature_done_noop_when_auto_handoff_off(conn, repo_id):
    cfg = _cfg(plan_before_execute=False, auto_handoff=False)
    chain = handoff.enqueue_feature_chain(
        conn, cfg, repo_id=repo_id, feature_name="widget",
        prompt="build a widget", provider_id="claude",
    )
    execute_row = db.get_task(conn, chain["execute"].id)
    before = len(db.list_tasks(conn))

    result = handoff.on_feature_done(conn, cfg, execute_row)
    assert result == {}
    assert len(db.list_tasks(conn)) == before


def test_on_feature_done_provider_fallback(conn, repo_id):
    # No provider on the execute task -> fall back to first enabled provider.
    cfg = default_config()
    cfg.scheduler = SchedulerConfig(plan_before_execute=False, auto_handoff=True)
    cfg.providers["claude"].enabled = True
    chain = handoff.enqueue_feature_chain(
        conn, cfg, repo_id=repo_id, feature_name="widget",
        prompt="build a widget", provider_id=None,
    )
    execute_row = db.get_task(conn, chain["execute"].id)
    result = handoff.on_feature_done(conn, cfg, execute_row)
    assert result["review"].provider_id == "claude"
