"""The team-lead permission policy wired through the hook handler."""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from archon import db, dispatcher, hooks, planner
from archon.config import default_config
from archon.models import TaskRun


def _cfg():
    c = default_config()
    c.providers["claude"].enabled = True
    c.providers["codex"].enabled = True
    return c


def _seed_run(conn, *, worktree, session="sess1"):
    cfg = _cfg()
    repo = pathlib.Path(tempfile.mkdtemp()) / "demo"
    repo.mkdir()
    ctx = dispatcher.register_repo(conn, dispatcher.RepoContext(root=repo, name="demo", session="demo-archon"))
    plan = planner.heuristic_plan("implement a hello endpoint", repo_path=ctx.root, config=cfg)
    job, tasks = planner.persist_plan(conn, cfg, ctx, plan)
    ex = tasks["execute"]
    run = TaskRun(
        id="run-ex", task_id=ex.id, provider_id="codex", status="running",
        phase="execute", worktree_path=str(worktree), provider_session_id=session,
        provider_session_name=session,
    )
    db.insert_task_run(conn, run)
    return run, ex


def _payload(command):
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def _env(run):
    return {"ARCHON_TASK_RUN_ID": run.id}


def test_safe_command_auto_approved(conn, tmp_path):
    run, ex = _seed_run(conn, worktree=tmp_path)
    summary = hooks.handle_hook("PreToolUse", _payload("pytest -q"), conn, env=_env(run), paths=None)
    assert summary["decision"] == "allow"
    assert summary["blocked"] is False
    # run not blocked, no open attention item
    assert db.find_task_run(conn, run.id)["status"] == "running"
    assert db.list_attention_items(conn, status="open") == []


def test_dangerous_command_hard_denied(conn, tmp_path):
    run, ex = _seed_run(conn, worktree=tmp_path)
    summary = hooks.handle_hook("PreToolUse", _payload("rm -rf /"), conn, env=_env(run), paths=None)
    assert summary["decision"] == "deny"
    assert summary["blocked"] is True
    assert db.find_task_run(conn, run.id)["status"] == "blocked"
    items = db.list_attention_items(conn, status="open")
    assert len(items) == 1 and items[0]["kind"] == "permission_denied"


def test_ambiguous_command_escalates(conn, tmp_path):
    run, ex = _seed_run(conn, worktree=tmp_path)
    summary = hooks.handle_hook("PreToolUse", _payload("python scripts/migrate.py"), conn, env=_env(run), paths=None)
    assert summary["decision"] == "escalate"
    assert summary["blocked"] is True
    items = db.list_attention_items(conn, status="open")
    assert len(items) == 1 and items[0]["kind"] == "permission_request"


def test_compound_with_danger_denied(conn, tmp_path):
    run, ex = _seed_run(conn, worktree=tmp_path)
    summary = hooks.handle_hook("PreToolUse", _payload("pytest && sudo rm -rf /"), conn, env=_env(run), paths=None)
    assert summary["decision"] == "deny"
    assert db.find_task_run(conn, run.id)["status"] == "blocked"


def test_stop_hook_marks_run_done(conn, tmp_path):
    run, ex = _seed_run(conn, worktree=tmp_path)
    hooks.handle_hook("Stop", "{}", conn, env=_env(run), paths=None)
    assert db.find_task_run(conn, run.id)["status"] == "done"


def test_missing_command_fails_safe_to_escalate(conn, tmp_path):
    run, ex = _seed_run(conn, worktree=tmp_path)
    # A permission hook with no extractable command must not auto-approve.
    summary = hooks.handle_hook("PreToolUse", "{}", conn, env=_env(run), paths=None)
    assert summary["decision"] in ("escalate", "deny")
    assert summary["blocked"] is True
