"""End-to-end CLI tests via Typer's CliRunner (all dry-run / isolated home)."""

import subprocess

import pytest
from typer.testing import CliRunner

from archon.cli import app

runner = CliRunner()


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


@pytest.fixture(autouse=True)
def _isolated(isolated_home):
    # isolated_home sets ARCHON_CONFIG_HOME / ARCHON_HOME for every CLI test.
    yield


def test_init_reports_paths():
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert "archon initialised" in result.stdout


def test_providers_doctor_lists_known():
    result = runner.invoke(app, ["providers", "doctor"])
    assert result.exit_code == 0
    for pid in ("claude", "codex", "copilot"):
        assert pid in result.stdout


def test_enable_then_status_shows_provider():
    runner.invoke(app, ["init"])
    assert runner.invoke(app, ["providers", "enable", "claude"]).exit_code == 0
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "claude" in result.stdout


def test_review_pr_dry_run(git_repo):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["providers", "enable", "claude", "codex"])
    result = runner.invoke(app, [
        "review-pr", "552", "--repo", str(git_repo),
        "--provider", "claude", "--provider", "codex", "--dry-run",
    ])
    assert result.exit_code == 0
    assert "review/pr-552/claude" in result.stdout or "PR #552" in result.stdout


def test_feature_multi_without_variants_fails(git_repo):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["providers", "enable", "claude", "codex"])
    result = runner.invoke(app, [
        "feature", "btn", "--repo", str(git_repo),
        "--provider", "claude", "--provider", "codex", "--dry-run",
    ])
    assert result.exit_code != 0


def test_feature_single_dry_run(git_repo):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["providers", "enable", "claude"])
    result = runner.invoke(app, [
        "feature", "newButton4User", "--repo", str(git_repo),
        "--provider", "claude", "--dry-run",
    ])
    assert result.exit_code == 0
    assert "newButton4User" in result.stdout


def test_do_dry_run_yes_dispatches_plan(git_repo):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, [
        "do", "add a hello endpoint", "--repo", str(git_repo), "--dry-run", "--yes",
    ])
    assert result.exit_code == 0
    assert "Add A Hello Endpoint" in result.stdout
    assert "dispatched=" in result.stdout


def test_review_pr_ambiguous_provider_noninteractive_fails(git_repo):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["providers", "enable", "claude", "codex"])
    # No --provider, not a TTY -> should refuse rather than hang.
    result = runner.invoke(app, ["review-pr", "1", "--repo", str(git_repo), "--dry-run"])
    assert result.exit_code != 0


def test_statusline_tolerates_garbage():
    result = runner.invoke(app, ["statusline"], input="not json{{{")
    assert result.exit_code == 0


def test_hook_tolerates_garbage():
    result = runner.invoke(app, ["hook", "Notification"], input="{bad")
    assert result.exit_code == 0


def test_bare_archon_launches_textual_app(monkeypatch):
    """Bare `archon` (no subcommand) boots the interactive Textual cockpit."""
    calls = {}

    def fake_run_app(conn, config, ctx=None, **kwargs):
        calls["conn"] = conn
        calls["config"] = config
        calls["ctx"] = ctx

    monkeypatch.setattr("archon.tui.run_app", fake_run_app)
    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.stdout
    assert "conn" in calls and calls["config"] is not None
