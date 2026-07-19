"""Tests for the self-advancing reconcile/tick pass."""

from __future__ import annotations

import pathlib
import tempfile


from archon import db, dispatcher, planner, reconcile
from archon.backends.base import WorkerStatus
from archon.config import default_config
from archon.models import TaskRun


def _cfg():
    c = default_config()
    c.providers["claude"].enabled = True
    c.providers["codex"].enabled = True
    return c


def _seed(conn):
    cfg = _cfg()
    repo = pathlib.Path(tempfile.mkdtemp()) / "demo"
    repo.mkdir()
    ctx = dispatcher.register_repo(conn, dispatcher.RepoContext(root=repo, name="demo", session="demo-archon"))
    plan = planner.heuristic_plan("implement a hello endpoint", repo_path=ctx.root, config=cfg)
    job, tasks = planner.persist_plan(conn, cfg, ctx, plan)
    return cfg, ctx, job, tasks


def _running_run(conn, task, *, session="sess1", cost=0.0):
    run = TaskRun(
        id=f"run-{task.id}",
        task_id=task.id,
        provider_id="codex",
        status="running",
        phase="execute",
        provider_session_id=session,
        provider_session_name=session,
        cost_usd=cost,
    )
    db.insert_task_run(conn, run)
    db.set_task_status(conn, task.id, "running")
    return run


class FakeBackend:
    """Backend whose status() is scripted per session id."""

    def __init__(self, states: dict[str, WorkerStatus] | None = None, raise_on=None):
        self.states = states or {}
        self.raise_on = raise_on or set()

    def status(self, handle):
        if handle.backend_id in self.raise_on:
            raise RuntimeError("backend boom")
        return self.states.get(handle.backend_id, WorkerStatus(state="running", cost_usd=None, last_output_tail=""))

    # unused protocol methods
    def launch(self, spec): ...
    def send(self, handle, message): ...
    def output(self, handle, lines=200): return ""
    def stop(self, handle): ...
    def attach_command(self, handle): return []
    def list_all(self): return []


def _dry_launch():
    return dispatcher.make_scheduler_launch(dry_run=True)


def test_backend_done_advances_chain(conn):
    cfg, ctx, job, tasks = _seed(conn)
    ex = tasks["execute"]
    _running_run(conn, ex, session="sx")
    backend = FakeBackend({"sx": WorkerStatus(state="done", cost_usd=None, last_output_tail="")})

    result = reconcile.reconcile_once(conn, cfg, backend=backend, launch=_dry_launch())

    assert ex.id in result.completed
    assert db.get_task(conn, ex.id)["status"] == "done"
    # handoff created a review task
    review = [t for t in db.list_tasks(conn) if t["phase"] == "review"]
    assert review, "handoff did not create a review task"


def test_hook_driven_done_run_advances(conn):
    cfg, ctx, job, tasks = _seed(conn)
    ex = tasks["execute"]
    run = _running_run(conn, ex, session="sy")
    # Simulate a Stop hook: run flipped to done, but backend still says running.
    db.set_task_run_status(conn, run.id, "done")
    backend = FakeBackend({"sy": WorkerStatus(state="running", cost_usd=None, last_output_tail="")})

    result = reconcile.reconcile_once(conn, cfg, backend=backend, launch=_dry_launch())

    assert ex.id in result.completed
    assert db.get_task(conn, ex.id)["status"] == "done"


def test_error_state_marks_failed(conn):
    cfg, ctx, job, tasks = _seed(conn)
    ex = tasks["execute"]
    run = _running_run(conn, ex, session="sz")
    backend = FakeBackend({"sz": WorkerStatus(state="error", cost_usd=None, last_output_tail="")})

    result = reconcile.reconcile_once(conn, cfg, backend=backend, launch=_dry_launch())

    assert run.id in result.failed
    assert db.find_task_run(conn, run.id)["status"] == "failed"
    assert db.get_task(conn, ex.id)["status"] != "done"


def test_cost_is_synced(conn):
    cfg, ctx, job, tasks = _seed(conn)
    ex = tasks["execute"]
    run = _running_run(conn, ex, session="sc", cost=0.0)
    backend = FakeBackend({"sc": WorkerStatus(state="running", cost_usd=1.23, last_output_tail="")})

    result = reconcile.reconcile_once(conn, cfg, backend=backend, launch=_dry_launch())

    assert run.id in result.reconciled
    assert abs(float(db.find_task_run(conn, run.id)["cost_usd"]) - 1.23) < 1e-6


def test_idempotent_no_double_handoff(conn):
    cfg, ctx, job, tasks = _seed(conn)
    ex = tasks["execute"]
    _running_run(conn, ex, session="sd")
    backend = FakeBackend({"sd": WorkerStatus(state="done", cost_usd=None, last_output_tail="")})

    reconcile.reconcile_once(conn, cfg, backend=backend, launch=_dry_launch())
    reconcile.reconcile_once(conn, cfg, backend=backend, launch=_dry_launch())

    review = [t for t in db.list_tasks(conn) if t["phase"] == "review"]
    assert len(review) == 1, f"handoff ran twice: {len(review)} review tasks"


def test_missing_session_surfaced_not_assumed_done(conn):
    from archon import db as _db
    cfg, ctx, job, tasks = _seed(conn)
    ex = tasks["execute"]
    run = _running_run(conn, ex, session="sg")
    backend = FakeBackend({"sg": WorkerStatus(state="missing", cost_usd=None, last_output_tail="")})

    result = reconcile.reconcile_once(conn, cfg, backend=backend, launch=_dry_launch())

    # NOT completed — surfaced as ambiguous instead.
    assert ex.id not in result.completed
    assert run.id in result.stale
    assert _db.find_task_run(conn, run.id)["status"] == "stale"
    items = _db.list_attention_items(conn, status="open")
    assert any(i["kind"] == "worker_gone" for i in items)


def test_idle_is_not_treated_as_completion(conn):
    cfg, ctx, job, tasks = _seed(conn)
    ex = tasks["execute"]
    _running_run(conn, ex, session="si")
    backend = FakeBackend({"si": WorkerStatus(state="idle", cost_usd=None, last_output_tail="")})

    result = reconcile.reconcile_once(conn, cfg, backend=backend, launch=_dry_launch())

    assert ex.id not in result.completed
    assert db.get_task(conn, ex.id)["status"] != "done"
    assert db.find_task_run(conn, "run-" + ex.id)["status"] == "running"


def test_stale_run_recovers_when_session_returns(conn):
    cfg, ctx, job, tasks = _seed(conn)
    ex = tasks["execute"]
    run = _running_run(conn, ex, session="sr")
    db.set_task_run_status(conn, run.id, "stale")
    backend = FakeBackend({"sr": WorkerStatus(state="running", cost_usd=None, last_output_tail="")})

    reconcile.reconcile_once(conn, cfg, backend=backend, launch=_dry_launch())

    assert db.find_task_run(conn, run.id)["status"] == "running"


def test_backend_exception_is_safe(conn):
    cfg, ctx, job, tasks = _seed(conn)
    ex = tasks["execute"]
    run = _running_run(conn, ex, session="se")
    backend = FakeBackend(raise_on={"se"})

    # Must not raise.
    result = reconcile.reconcile_once(conn, cfg, backend=backend, launch=_dry_launch())
    assert db.find_task_run(conn, run.id)["status"] == "running"
    assert ex.id not in result.completed
