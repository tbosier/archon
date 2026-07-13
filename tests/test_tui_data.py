"""View-model builders that feed the Textual dashboard."""

from __future__ import annotations

from archon import attention, dispatcher, planner
from archon.config import default_config
from archon.tui import data


def _cfg():
    c = default_config()
    c.providers["claude"].enabled = True
    c.providers["codex"].enabled = True
    return c


def _ctx(conn, tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    ctx = dispatcher.RepoContext(root=repo, name="demo", session="demo-archon")
    return dispatcher.register_repo(conn, ctx)


def _seed_job(conn, cfg, ctx, message="implement a hello endpoint"):
    plan = planner.heuristic_plan(message, repo_path=ctx.root, config=cfg)
    return planner.persist_plan(conn, cfg, ctx, plan)


def test_build_jobs_nests_tasks(conn, tmp_path):
    cfg = _cfg()
    ctx = _ctx(conn, tmp_path)
    job, tasks = _seed_job(conn, cfg, ctx)

    jobs = data.build_jobs(conn)
    assert len(jobs) == 1
    node = jobs[0]
    assert node.id == job.id
    phases = [t.phase for t in node.tasks]
    assert phases == ["execute", "review", "test"]
    assert all(t.tool in ("claude", "codex") for t in node.tasks)


def test_jobs_with_attention_sort_first(conn, tmp_path):
    cfg = _cfg()
    ctx = _ctx(conn, tmp_path)
    quiet_job, _ = _seed_job(conn, cfg, ctx, "quiet change")
    hot_job, _ = _seed_job(conn, cfg, ctx, "urgent fix")
    attention.open_item(
        conn, kind="plan_approval", severity="warn",
        title="approve plan: urgent fix", job_id=hot_job.id,
        options=["approve", "reject", "edit"], recommended_option="approve",
    )

    jobs = data.build_jobs(conn)
    assert jobs[0].id == hot_job.id
    assert jobs[0].open_attention == 1


def test_build_attention_parses_options(conn, tmp_path):
    cfg = _cfg()
    ctx = _ctx(conn, tmp_path)
    job, _ = _seed_job(conn, cfg, ctx)
    attention.open_item(
        conn, kind="plan_approval", severity="warn",
        title="approve plan: hello", job_id=job.id,
        options=["approve", "reject", "edit"], recommended_option="approve",
    )

    rows = data.build_attention(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row.is_plan_approval
    assert row.options == ["approve", "reject", "edit"]
    assert row.recommended == "approve"


def test_snapshot_reports_budget(conn, tmp_path):
    cfg = _cfg()
    ctx = _ctx(conn, tmp_path)
    _seed_job(conn, cfg, ctx)

    snap = data.build_snapshot(conn, cfg)
    assert snap.jobs
    assert snap.budget_action == "allow"
    assert snap.header_budget.startswith("spend $")
