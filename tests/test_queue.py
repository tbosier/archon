"""Tests for the task queue and dependency-graph helpers."""

from __future__ import annotations

import pytest

from archon import db, queue, taskgraph
from archon.models import Repo


@pytest.fixture()
def conn():
    c = db.connect_memory()
    yield c
    c.close()


@pytest.fixture()
def repo_id(conn):
    return db.upsert_repo(conn, Repo(name="demo", root_path="/tmp/demo", zellij_session="demo"))


def _enqueue(conn, repo_id, name, **kw):
    return queue.enqueue_task(
        conn, repo_id=repo_id, type="feature", name=name,
        prompt=f"do {name}", **kw,
    )


# --- queue -----------------------------------------------------------------

def test_enqueue_inserts_queued_task(conn, repo_id):
    task = _enqueue(conn, repo_id, "alpha")
    row = db.get_task(conn, task.id)
    assert row is not None
    assert row["status"] == "queued"
    assert row["name"] == "alpha"
    assert queue.pending_count(conn) == 1


def test_depends_on_creates_edges(conn, repo_id):
    a = _enqueue(conn, repo_id, "a")
    b = _enqueue(conn, repo_id, "b", depends_on=[a.id])
    assert db.dependencies_of(conn, b.id) == [a.id]
    assert (b.id, a.id) in taskgraph.edges(conn)


def test_ready_tasks_respects_dependencies(conn, repo_id):
    a = _enqueue(conn, repo_id, "a")
    b = _enqueue(conn, repo_id, "b", depends_on=[a.id])

    ready_ids = [r["id"] for r in queue.ready_tasks(conn)]
    assert a.id in ready_ids
    assert b.id not in ready_ids  # dep not done yet

    db.set_task_status(conn, a.id, "done")
    ready_ids = [r["id"] for r in queue.ready_tasks(conn)]
    assert b.id in ready_ids


def test_pending_count_and_cancel(conn, repo_id):
    a = _enqueue(conn, repo_id, "a")
    b = _enqueue(conn, repo_id, "b")
    assert queue.pending_count(conn) == 2

    queue.cancel_task(conn, a.id)
    assert db.get_task(conn, a.id)["status"] == "failed"
    assert queue.pending_count(conn) == 1

    # cancelling a done task is a no-op
    db.set_task_status(conn, b.id, "done")
    queue.cancel_task(conn, b.id)
    assert db.get_task(conn, b.id)["status"] == "done"


# --- taskgraph -------------------------------------------------------------

def test_chain_creates_linear_dependencies(conn, repo_id):
    a = _enqueue(conn, repo_id, "a")
    b = _enqueue(conn, repo_id, "b")
    c = _enqueue(conn, repo_id, "c")
    taskgraph.chain(conn, [a.id, b.id, c.id])
    assert db.dependencies_of(conn, b.id) == [a.id]
    assert db.dependencies_of(conn, c.id) == [b.id]


def test_topological_order_is_valid(conn, repo_id):
    a = _enqueue(conn, repo_id, "a")
    b = _enqueue(conn, repo_id, "b")
    c = _enqueue(conn, repo_id, "c")
    taskgraph.chain(conn, [a.id, b.id, c.id])
    order = taskgraph.topological_order(conn)
    assert set(order) == {a.id, b.id, c.id}
    assert order.index(a.id) < order.index(b.id) < order.index(c.id)


def test_detect_cycle_finds_manual_cycle(conn, repo_id):
    a = _enqueue(conn, repo_id, "a")
    b = _enqueue(conn, repo_id, "b")
    taskgraph.add_edge(conn, a.id, b.id)
    taskgraph.add_edge(conn, b.id, a.id)
    cycle = taskgraph.detect_cycle(conn)
    assert cycle is not None
    assert a.id in cycle and b.id in cycle


def test_detect_cycle_none_when_acyclic(conn, repo_id):
    a = _enqueue(conn, repo_id, "a")
    b = _enqueue(conn, repo_id, "b")
    taskgraph.chain(conn, [a.id, b.id])
    assert taskgraph.detect_cycle(conn) is None


def test_ascii_graph_non_empty_with_ids_and_status(conn, repo_id):
    a = _enqueue(conn, repo_id, "a")
    b = _enqueue(conn, repo_id, "b", depends_on=[a.id])
    db.set_task_status(conn, a.id, "done")
    text = taskgraph.ascii_graph(conn)
    assert a.id in text
    assert b.id in text
    assert "[done]" in text
    assert "[queued]" in text
