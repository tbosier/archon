"""Integration tests for the dispatcher, driven entirely in dry-run mode."""

import subprocess

import pytest

from archon import db, dispatcher
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
    c.providers["codex"].enabled = True
    return c


def test_session_name_derived_from_repo(git_repo):
    c = dispatcher.resolve_repo_context(git_repo)
    assert c.session == "ci_amplify_ai-archon"


def test_review_creates_one_worktree_per_provider(conn, cfg, ctx):
    res = dispatcher.start_review(
        conn, cfg, ctx=ctx, pr_number=552, provider_ids=["claude", "codex"], dry_run=True
    )
    assert len(res.runs) == 2
    assert res.worktrees["claude"].branch == "review/pr-552/claude"
    assert res.worktrees["codex"].branch == "review/pr-552/codex"
    assert res.worktrees["claude"].path.name == "ci_amplify_ai-pr-552-review-claude"


def test_review_codex_uses_read_only_sandbox(conn, cfg, ctx):
    res = dispatcher.start_review(
        conn, cfg, ctx=ctx, pr_number=552, provider_ids=["codex"], dry_run=True
    )
    argv = res.launches["codex"].argv
    assert "--sandbox" in argv and "read-only" in argv


def test_feature_single_writer_default(conn, cfg, ctx):
    res = dispatcher.start_feature(
        conn, cfg, ctx=ctx, feature_name="newButton4User",
        provider_ids=["claude"], dry_run=True,
    )
    assert res.runs[0].branch == "feature/newButton4User"
    assert res.launches["claude"].argv[:2] == ["claude", "-n"]


def test_feature_multi_without_variants_refuses(conn, cfg, ctx):
    with pytest.raises(dispatcher.DispatchError):
        dispatcher.start_feature(
            conn, cfg, ctx=ctx, feature_name="x",
            provider_ids=["claude", "codex"], dry_run=True,
        )


def test_feature_variants_creates_per_provider_branches(conn, cfg, ctx):
    res = dispatcher.start_feature(
        conn, cfg, ctx=ctx, feature_name="btn",
        provider_ids=["claude", "codex"], variants=True, dry_run=True,
    )
    assert res.worktrees["claude"].branch == "feature/btn/claude"
    assert res.worktrees["codex"].branch == "feature/btn/codex"


def test_ids_unique_across_tasks(conn, cfg, ctx):
    r1 = dispatcher.start_review(conn, cfg, ctx=ctx, pr_number=1, provider_ids=["claude"], dry_run=True)
    r2 = dispatcher.start_feature(conn, cfg, ctx=ctx, feature_name="f", provider_ids=["claude"], dry_run=True)
    assert r1.task.id != r2.task.id
    all_run_ids = [run.id for run in (*r1.runs, *r2.runs)]
    assert len(all_run_ids) == len(set(all_run_ids))


def test_runs_persisted_and_marked_running(conn, cfg, ctx):
    dispatcher.start_review(conn, cfg, ctx=ctx, pr_number=7, provider_ids=["claude", "codex"], dry_run=True)
    rows = db.list_task_runs(conn)
    assert {r["status"] for r in rows} == {"running"}


def test_build_pane_command_injects_archon_env(conn, cfg, ctx):
    res = dispatcher.start_feature(
        conn, cfg, ctx=ctx, feature_name="f", provider_ids=["claude"], dry_run=True
    )
    cmd = dispatcher.build_pane_command(res.launches["claude"])
    assert cmd[:2] == ["bash", "-lc"]
    assert "unset CODEX_CI CODEX_SANDBOX_NETWORK_DISABLED" in cmd[2]
    assert "ARCHON_TASK_ID" in cmd[2]
    assert "ARCHON_PROVIDER_ID" in cmd[2]
