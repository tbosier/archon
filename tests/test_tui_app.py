"""Smoke tests for the interactive Textual cockpit via the Pilot harness."""

from __future__ import annotations

from textual.widgets import Input, Tree

from archon import attention, dispatcher, planner
from archon.config import default_config
from archon.tui import planning
from archon.tui.app import ArchonApp, ConfirmModal, PlanModal, WelcomeScreen
from archon.tui.planning import DispatchOutcome


def _cfg():
    c = default_config()
    c.providers["claude"].enabled = True
    c.providers["codex"].enabled = True
    c.backend.kind = "local"
    return c


def _ctx(conn, tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    ctx = dispatcher.RepoContext(root=repo, name="demo", session="demo-archon")
    return dispatcher.register_repo(conn, ctx)


def _seed_job(conn, cfg, ctx, message="implement a hello endpoint"):
    plan = planner.heuristic_plan(message, repo_path=ctx.root, config=cfg)
    return planner.persist_plan(conn, cfg, ctx, plan)


async def test_app_mounts_and_lists_jobs(conn, tmp_path):
    cfg = _cfg()
    ctx = _ctx(conn, tmp_path)
    _seed_job(conn, cfg, ctx)

    app = ArchonApp(conn, cfg, ctx, poll_interval=1000, reconcile_interval=1000, auto_reconcile=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#jobs", Tree)
        assert len(tree.root.children) == 1
        assert len(tree.root.children[0].children) == 3  # execute/review/test


async def test_command_bar_previews_and_approves(conn, tmp_path, monkeypatch):
    cfg = _cfg()
    ctx = _ctx(conn, tmp_path)
    proposal = planner.heuristic_plan("add a hello endpoint", repo_path=ctx.root, config=cfg)

    monkeypatch.setattr(planning, "propose", lambda *a, **k: proposal)
    captured = {}

    def fake_dispatch(conn_, config_, ctx_, prop):
        captured["proposal"] = prop
        return DispatchOutcome(job_id="job_x", task_count=3, dispatched=["t1"], skipped=[])

    monkeypatch.setattr(planning, "dispatch", fake_dispatch)

    app = ArchonApp(conn, cfg, ctx, poll_interval=1000, reconcile_interval=1000, auto_reconcile=False)
    async with app.run_test() as pilot:
        command = app.query_one("#command", Input)
        command.focus()
        command.value = "add a hello endpoint"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PlanModal)

        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()
        assert captured.get("proposal") is proposal


async def test_command_bar_discard_leaves_no_job(conn, tmp_path, monkeypatch):
    cfg = _cfg()
    ctx = _ctx(conn, tmp_path)
    proposal = planner.heuristic_plan("add a hello endpoint", repo_path=ctx.root, config=cfg)
    monkeypatch.setattr(planning, "propose", lambda *a, **k: proposal)

    called = {"dispatch": False}
    monkeypatch.setattr(planning, "dispatch", lambda *a, **k: called.__setitem__("dispatch", True))

    app = ArchonApp(conn, cfg, ctx, poll_interval=1000, reconcile_interval=1000, auto_reconcile=False)
    async with app.run_test() as pilot:
        command = app.query_one("#command", Input)
        command.focus()
        command.value = "add a hello endpoint"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")  # discard
        await pilot.pause()
        assert called["dispatch"] is False


async def test_no_repo_disables_planning(conn, tmp_path):
    cfg = _cfg()
    app = ArchonApp(conn, cfg, None, poll_interval=1000, reconcile_interval=1000, auto_reconcile=False)
    async with app.run_test() as pilot:
        command = app.query_one("#command", Input)
        command.focus()
        command.value = "do something"
        await pilot.press("enter")
        await pilot.pause()
        # No modal, no crash — planning is a no-op without a repo context.
        assert not isinstance(app.screen, PlanModal)


async def test_welcome_screen_offers_resume(conn, tmp_path):
    cfg = _cfg()
    ctx = _ctx(conn, tmp_path)
    _seed_job(conn, cfg, ctx)  # prior-session work exists
    app = ArchonApp(conn, cfg, ctx, poll_interval=1000, reconcile_interval=1000, auto_reconcile=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, WelcomeScreen)
        await pilot.press("enter")  # keep supervising
        await pilot.pause()
        assert not isinstance(app.screen, WelcomeScreen)


async def test_pr_review_routes_to_start_review(conn, tmp_path, monkeypatch):
    cfg = _cfg()
    ctx = _ctx(conn, tmp_path)
    calls = {}

    def fake_start_review(conn_, config_, *, ctx, pr_number, provider_ids, **kw):
        calls["pr"] = pr_number
        calls["providers"] = provider_ids

    monkeypatch.setattr(dispatcher, "start_review", fake_start_review)

    app = ArchonApp(conn, cfg, ctx, poll_interval=1000, reconcile_interval=1000, auto_reconcile=False)
    async with app.run_test() as pilot:
        command = app.query_one("#command", Input)
        command.focus()
        command.value = "review PR #42"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()
        assert calls.get("pr") == 42


async def test_pr_review_cancel_does_nothing(conn, tmp_path, monkeypatch):
    cfg = _cfg()
    ctx = _ctx(conn, tmp_path)
    called = {"start": False}
    monkeypatch.setattr(dispatcher, "start_review",
                        lambda *a, **k: called.__setitem__("start", True))
    app = ArchonApp(conn, cfg, ctx, poll_interval=1000, reconcile_interval=1000, auto_reconcile=False)
    async with app.run_test() as pilot:
        command = app.query_one("#command", Input)
        command.focus()
        command.value = "review pr 9"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("n")  # cancel
        await pilot.pause()
        assert called["start"] is False


async def test_new_project_creates_repo_then_plans(conn, tmp_path, monkeypatch):
    cfg = _cfg()
    ctx = _ctx(conn, tmp_path)  # base dir = ctx.root.parent = tmp_path
    plan = planner.heuristic_plan("a cli todo app", repo_path=ctx.root, config=cfg)
    monkeypatch.setattr(planning, "propose", lambda *a, **k: plan)

    app = ArchonApp(conn, cfg, ctx, poll_interval=1000, reconcile_interval=1000, auto_reconcile=False)
    async with app.run_test() as pilot:
        command = app.query_one("#command", Input)
        command.focus()
        command.value = "start a new project called todo-cli: a cli todo app"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("y")           # confirm creation
        await pilot.pause()
        await pilot.pause()
        # repo scaffolded on disk and the app re-pointed at it
        created = list(tmp_path.glob("todo-cli*"))
        assert created and (created[0] / ".git").exists()
        assert app.ctx is not None and app.ctx.root == created[0]
        # then the feature planner previews the described work
        assert isinstance(app.screen, PlanModal)


async def test_reconcile_loop_is_wired(conn, tmp_path, monkeypatch):
    from archon import reconcile
    from archon.reconcile import ReconcileResult

    cfg = _cfg()
    ctx = _ctx(conn, tmp_path)
    calls = {"n": 0}

    def fake_reconcile(conn_, config_, *, backend, launch=None):
        calls["n"] += 1
        return ReconcileResult()

    monkeypatch.setattr(reconcile, "reconcile_once", fake_reconcile)

    app = ArchonApp(conn, cfg, ctx, poll_interval=1000, reconcile_interval=1000, auto_reconcile=False)
    async with app.run_test() as pilot:
        app._reconcile_tick()  # invoke the worker body directly
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert calls["n"] >= 1
