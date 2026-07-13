"""The per-worktree hook installer that makes the policy enforceable."""

from __future__ import annotations

import json

from archon import worker_hooks


def _read(worktree):
    return json.loads((worktree / ".claude" / "settings.json").read_text())


def test_install_wires_pretooluse(tmp_path):
    worker_hooks.install_claude_hooks(tmp_path, archon_cmd="archon")
    hooks = _read(tmp_path)["hooks"]
    # PreToolUse is the only event that can BLOCK a command — it must be wired.
    assert "PreToolUse" in hooks
    cmd = hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert cmd == "archon hook PreToolUse"
    # completion + escalation events too
    for event in ("PermissionRequest", "Notification", "Stop", "SessionEnd"):
        assert event in hooks


def test_install_is_idempotent(tmp_path):
    worker_hooks.install_claude_hooks(tmp_path, archon_cmd="archon")
    worker_hooks.install_claude_hooks(tmp_path, archon_cmd="archon")
    hooks = _read(tmp_path)["hooks"]
    assert len(hooks["PreToolUse"]) == 1  # not duplicated


def test_install_preserves_existing_settings(tmp_path):
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(json.dumps({
        "statusLine": {"type": "command", "command": "mystatus"},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-own-hook"}]}]},
    }))
    worker_hooks.install_claude_hooks(tmp_path, archon_cmd="archon")
    data = _read(tmp_path)
    # user's own settings survive
    assert data["statusLine"]["command"] == "mystatus"
    cmds = [h["command"] for e in data["hooks"]["PreToolUse"] for h in e["hooks"]]
    assert "my-own-hook" in cmds and "archon hook PreToolUse" in cmds


def test_install_for_provider_only_supported(tmp_path):
    assert worker_hooks.install_for_provider("codex", tmp_path) is None
    assert worker_hooks.install_for_provider("claude", tmp_path) is not None


def test_dispatch_chain_installs_hooks(tmp_path):
    # The launch chokepoint (_launch_run) must write settings.json before the
    # worker boots — this is the wiring that was missing.
    from archon import db, dispatcher
    from archon.backends.base import WorkerHandle
    from archon.models import TaskRun
    from archon.providers.base import ProviderLaunch

    conn = db.connect_memory()
    wt = tmp_path / "wt"
    wt.mkdir()
    run = TaskRun(id="r1", task_id="t1", provider_id="claude", status="starting",
                  worktree_path=str(wt))
    db.insert_task_run(conn, run)

    class FakeBackend:
        def launch(self, spec):
            return WorkerHandle(backend_id="s1", title="s1")

    launch = ProviderLaunch(argv=["claude"], cwd=wt, env={}, mode="interactive",
                            expects_prompt_paste=False, captures_jsonl=False,
                            pane_name="p", prompt="hi")
    dispatcher._launch_run(conn, FakeBackend(), dispatcher.RepoContext(root=wt, name="wt", session="s"),
                           run, launch, dry_run=False)

    settings = json.loads((wt / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"].endswith("hook PreToolUse")
