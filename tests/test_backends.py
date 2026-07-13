from __future__ import annotations

import subprocess

import pytest

from archon.backends.agentdeck import (
    AgentDeckBackend,
    AgentDeckError,
    build_launch_argv,
)
from archon.backends.base import WorkerSpec
from archon.backends.local import LocalBackend


def test_agentdeck_launch_argv_without_worktree():
    spec = WorkerSpec(
        title="codex-execute-x",
        repo_path="/repo/wt",
        branch="feature/x",
        tool="codex",
        model="gpt-5-codex",
        prompt="do it",
        use_worktree=False,
    )
    argv = build_launch_argv(spec)
    assert argv == [
        "agent-deck",
        "launch",
        "/repo/wt",
        "--title",
        "codex-execute-x",
        "--title-lock",
        "--no-parent",
        "--json",
        "-c",
        "codex",
        "--message",
        "do it",
        "--model",
        "gpt-5-codex",
    ]


def test_agentdeck_launch_argv_with_worktree():
    spec = WorkerSpec(
        title="claude-plan-x",
        repo_path="/repo",
        branch="archon/x",
        tool="claude",
        model=None,
        prompt="plan it",
    )
    argv = build_launch_argv(spec)
    assert "--worktree" in argv
    assert argv[argv.index("--worktree") + 1] == "archon/x"
    assert "--new-branch" in argv


def test_agentdeck_launch_parses_json_handle(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"id":"abc123","title":"worker"}',
            stderr="",
        )

    monkeypatch.setattr("archon.backends.agentdeck.shutil.which", lambda _: "/bin/agent-deck")
    monkeypatch.setattr("archon.backends.agentdeck.subprocess.run", fake_run)

    backend = AgentDeckBackend()
    handle = backend.launch(
        WorkerSpec(
            title="worker",
            repo_path="/repo",
            branch="feature/x",
            tool="claude",
            model=None,
            prompt="hello",
            use_worktree=False,
        )
    )
    assert handle.backend_id == "abc123"
    assert handle.title == "worker"


def test_agentdeck_non_json_output_raises(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="not json", stderr="")

    monkeypatch.setattr("archon.backends.agentdeck.shutil.which", lambda _: "/bin/agent-deck")
    monkeypatch.setattr("archon.backends.agentdeck.subprocess.run", fake_run)

    with pytest.raises(AgentDeckError):
        AgentDeckBackend().list_all()


def test_local_backend_dry_run_records_launch():
    backend = LocalBackend(dry_run=True)
    spec = WorkerSpec(
        title="dry-worker",
        repo_path="/repo",
        branch="feature/x",
        tool="codex",
        model=None,
        prompt="hello",
    )
    handle = backend.launch(spec)
    assert handle.backend_id == "dry-worker"
    assert backend.launches == [spec]
    assert backend.status(handle).state == "running"
