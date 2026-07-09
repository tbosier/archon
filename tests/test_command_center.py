"""The command center: multiple repos, one shared session, one dashboard."""

import subprocess

import pytest

from archon import db, dispatcher
from archon.config import default_config


def _git_repo(tmp_path, name):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=repo, check=True,
    )
    return repo


@pytest.fixture()
def cfg():
    c = default_config()
    c.providers["claude"].enabled = True
    return c


def test_shared_session_spans_repos(tmp_path, cfg):
    a = _git_repo(tmp_path, "alpha")
    b = _git_repo(tmp_path, "beta")
    ctx_a = dispatcher.resolve_repo_context(a, config=cfg)
    ctx_b = dispatcher.resolve_repo_context(b, config=cfg)
    # Two different repos, one shared cockpit session.
    assert ctx_a.session == ctx_b.session == "archon"
    assert ctx_a.root != ctx_b.root


def test_per_repo_session_when_not_shared(tmp_path, cfg):
    cfg.command_center.shared = False
    a = _git_repo(tmp_path, "alpha")
    ctx = dispatcher.resolve_repo_context(a, config=cfg)
    assert ctx.session == "alpha-archon"


def test_dashboard_lists_runs_from_all_repos(conn, tmp_path, cfg):
    a = _git_repo(tmp_path, "alpha")
    b = _git_repo(tmp_path, "beta")
    ctx_a = dispatcher.register_repo(conn, dispatcher.resolve_repo_context(a, config=cfg))
    ctx_b = dispatcher.register_repo(conn, dispatcher.resolve_repo_context(b, config=cfg))

    dispatcher.start_feature(conn, cfg, ctx=ctx_a, feature_name="fa",
                             provider_ids=["claude"], dry_run=True)
    dispatcher.start_feature(conn, cfg, ctx=ctx_b, feature_name="fb",
                             provider_ids=["claude"], dry_run=True)

    rows = db.list_task_runs(conn)
    repos = {r["repo_name"] for r in rows}
    sessions = {r["zellij_session"] for r in rows}
    assert repos == {"alpha", "beta"}          # both projects visible in one dashboard
    assert sessions == {"archon"}              # both in one shared session
