"""End-to-end proof that the permission policy ENFORCES, not just logs.

These tests exercise the real chain a launched worker uses:

  worker -> reads worktree/.claude/settings.json -> runs `archon hook PreToolUse`
         -> gets an enforceable allow/deny -> honours it

against actual subprocesses, so a hard-denied command is provably prevented
from running (not merely classified DENY in isolation — that was the gap the
review caught).
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path


from archon import worker_hooks

SRC = str(Path(__file__).resolve().parents[1] / "src")
HOOK_CLI = [sys.executable, "-m", "archon.cli"]


def _run_hook(event, payload, env_extra, cwd):
    env = {"PYTHONPATH": SRC, "PATH": __import__("os").environ.get("PATH", ""),
           "HOME": __import__("os").environ.get("HOME", ""), **env_extra}
    return subprocess.run(
        [*HOOK_CLI, "hook", event],
        input=json.dumps(payload), text=True, capture_output=True,
        env=env, cwd=str(cwd), timeout=60,
    )


def _decision(proc):
    try:
        return json.loads(proc.stdout or "{}").get("hookSpecificOutput", {}).get("permissionDecision")
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
# The CLI contract: `archon hook PreToolUse` returns an ENFORCEABLE decision.
# --------------------------------------------------------------------------- #

def test_cli_hook_denies_dangerous(tmp_path):
    proc = _run_hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
                     {"ARCHON_HOME": str(tmp_path / "h"), "ARCHON_CONFIG_HOME": str(tmp_path / "c")}, tmp_path)
    assert _decision(proc) == "deny"
    assert proc.returncode == 2  # universal block signal too


def test_cli_hook_allows_safe(tmp_path):
    proc = _run_hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}},
                     {"ARCHON_HOME": str(tmp_path / "h"), "ARCHON_CONFIG_HOME": str(tmp_path / "c")}, tmp_path)
    assert _decision(proc) == "allow"
    assert proc.returncode == 0


def test_cli_hook_escalates_ambiguous(tmp_path):
    proc = _run_hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "python scripts/migrate.py"}},
                     {"ARCHON_HOME": str(tmp_path / "h"), "ARCHON_CONFIG_HOME": str(tmp_path / "c")}, tmp_path)
    assert _decision(proc) == "ask"
    assert proc.returncode == 0


# --------------------------------------------------------------------------- #
# Live worker: a compliant agent reads the installed settings, honours the
# PreToolUse gate, and a hard-denied command is NEVER executed.
# --------------------------------------------------------------------------- #

_FAKE_AGENT = textwrap.dedent(
    """
    import json, os, shlex, subprocess
    wt = os.getcwd()
    settings = json.load(open(os.path.join(wt, ".claude", "settings.json")))
    hook_cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    for cmd in json.loads(os.environ["CANDIDATES"]):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": wt})
        proc = subprocess.run(shlex.split(hook_cmd), input=payload, text=True, capture_output=True)
        decision = None
        try:
            decision = json.loads(proc.stdout or "{}").get("hookSpecificOutput", {}).get("permissionDecision")
        except Exception:
            pass
        allowed = decision == "allow" and proc.returncode == 0
        if allowed:
            subprocess.run(cmd, shell=True)
    """
)


def test_live_worker_blocks_hard_deny_and_runs_allow(tmp_path):
    from archon.backends.local import LocalBackend
    from archon.backends.base import WorkerSpec

    worktree = tmp_path / "wt"
    worktree.mkdir()
    sentinel = worktree / "sentinel"
    sentinel.mkdir()
    (sentinel / "keep.txt").write_text("precious")

    worker_hooks.install_claude_hooks(worktree, archon_cmd=f"{sys.executable} -m archon.cli")
    agent = tmp_path / "fake_agent.py"
    agent.write_text(_FAKE_AGENT)

    spec = WorkerSpec(
        title="w", repo_path=str(worktree), branch="b", tool="claude", model=None, prompt="",
        command=[sys.executable, str(agent)], cwd=str(worktree),
        env={
            "PYTHONPATH": SRC,
            "ARCHON_HOME": str(tmp_path / "h"),
            "ARCHON_CONFIG_HOME": str(tmp_path / "c"),
            "CANDIDATES": json.dumps(["rm -rf sentinel", "echo ran > marker.txt"]),
        },
    )
    backend = LocalBackend()
    handle = backend.launch(spec)
    backend.processes[handle.backend_id].wait(timeout=90)

    # hard-deny was actually prevented from executing
    assert sentinel.exists() and (sentinel / "keep.txt").exists(), "rm -rf was NOT blocked"
    # the allowed command actually ran
    assert (worktree / "marker.txt").exists(), "allowed command did not run"


# --------------------------------------------------------------------------- #
# Completion: a real Stop hook marks the run done -> reconcile advances.
# --------------------------------------------------------------------------- #

def test_stop_hook_completes_run_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHON_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("ARCHON_CONFIG_HOME", str(tmp_path / "c"))
    from archon import db, dispatcher, planner, reconcile
    from archon.config import default_config
    from archon.models import Provider, TaskRun
    from archon.backends.base import WorkerStatus

    cfg = default_config()
    cfg.providers["claude"].enabled = True
    cfg.providers["codex"].enabled = True

    conn = db.connect()  # file DB under ARCHON_HOME (foreign_keys = ON)
    # Satisfy the providers FK that the enforced-FK file DB checks.
    for pid in ("claude", "codex"):
        db.upsert_provider(conn, Provider(id=pid, display_name=pid, command=pid,
                                          enabled=True, installed=True, auth_status="ready"))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = dispatcher.register_repo(conn, dispatcher.RepoContext(root=worktree, name="wt", session="s"))
    plan = planner.heuristic_plan("implement a hello endpoint", repo_path=ctx.root, config=cfg)
    job, tasks = planner.persist_plan(conn, cfg, ctx, plan)
    ex = tasks["execute"]
    db.insert_task_run(conn, TaskRun(
        id="run-ex", task_id=ex.id, provider_id="codex", status="running", phase="execute",
        worktree_path=str(worktree), provider_session_id="s1", provider_session_name="s1"))
    db.set_task_status(conn, ex.id, "running")

    settings = json.loads(worker_hooks.install_claude_hooks(
        worktree, archon_cmd=f"{sys.executable} -m archon.cli").read_text())
    stop_cmd = settings["hooks"]["Stop"][0]["hooks"][0]["command"]

    # Fire the Stop hook exactly as a finished worker would (no ARCHON env: the
    # run must be resolved by cwd/worktree match).
    subprocess.run(
        shlex.split(stop_cmd), input=json.dumps({"cwd": str(worktree)}), text=True,
        capture_output=True, cwd=str(worktree), timeout=60,
        env={"PYTHONPATH": SRC, "PATH": __import__("os").environ.get("PATH", ""),
             "HOME": __import__("os").environ.get("HOME", ""),
             "ARCHON_HOME": str(tmp_path / "h"), "ARCHON_CONFIG_HOME": str(tmp_path / "c")},
    )

    # The run is now done in the shared DB…
    assert db.find_task_run(conn, "run-ex")["status"] == "done"
    # …and reconcile advances the chain (review task appears).
    fake = type("B", (), {"status": lambda self, h: WorkerStatus("running", None, "")})()
    reconcile.reconcile_once(conn, cfg, backend=fake, launch=dispatcher.make_scheduler_launch(dry_run=True))
    assert db.get_task(conn, ex.id)["status"] == "done"
    assert any(t["phase"] == "review" for t in db.list_tasks(conn))
