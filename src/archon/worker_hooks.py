"""Install Archon's policy hooks into a worker's worktree.

This is what makes the permission policy *enforceable* rather than advisory. A
launched Claude worker reads project-level hooks from ``<cwd>/.claude/settings.json``;
by writing that file into the worktree we wire the ``PreToolUse`` gate (and the
Stop/SessionEnd completion signals) to ``archon hook <Event>``. On ``PreToolUse``
the hook returns a ``permissionDecision`` that Claude Code honours, so a
hard-denied command (e.g. ``rm -rf``) is actually blocked from running — not just
logged after the fact.

Only the ``PreToolUse`` event can *block* a command, so it is the important one;
the others (PermissionRequest/Notification/Stop/StopFailure/SessionEnd) drive the
live dashboard + completion detection.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

# Events we manage, and whether Claude expects a tool "matcher" for them.
_MATCHER_EVENTS = ("PreToolUse", "PermissionRequest", "Notification")
_NO_MATCHER_EVENTS = ("Stop", "StopFailure", "SessionEnd")


def archon_hook_command() -> str:
    """A stable command prefix that invokes ``archon hook`` in a worker env.

    Prefers the installed ``archon`` console script; falls back to running the
    module through the current interpreter (absolute path), which survives a
    worker whose ``PATH`` differs from ours.
    """
    exe = shutil.which("archon")
    if exe:
        return exe
    return f"{sys.executable} -m archon.cli"


def build_claude_settings(archon_cmd: str | None = None) -> dict:
    """The hooks block Archon installs into a Claude worktree."""
    cmd = archon_cmd or archon_hook_command()

    def entry(event: str, *, matcher: bool) -> dict:
        hook = {"type": "command", "command": f"{cmd} hook {event}"}
        return {"matcher": ".*", "hooks": [hook]} if matcher else {"hooks": [hook]}

    hooks: dict[str, list] = {}
    for event in _MATCHER_EVENTS:
        hooks[event] = [entry(event, matcher=True)]
    for event in _NO_MATCHER_EVENTS:
        hooks[event] = [entry(event, matcher=False)]
    return {"hooks": hooks}


def _command_present(entries: list, command: str) -> bool:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []) or []:
            if isinstance(hook, dict) and hook.get("command") == command:
                return True
    return False


def _merge(existing: dict, ours: dict) -> dict:
    """Merge our hooks into any existing settings without clobbering the user's.

    Top-level keys the user set (statusLine, permissions, other events) are kept;
    for each event we manage we append our ``archon hook`` entry only if an
    identical command is not already present (idempotent re-installs).
    """
    merged = dict(existing) if isinstance(existing, dict) else {}
    existing_hooks = merged.get("hooks")
    hooks: dict = dict(existing_hooks) if isinstance(existing_hooks, dict) else {}
    for event, our_entries in ours["hooks"].items():
        our_cmd = our_entries[0]["hooks"][0]["command"]
        current = hooks.get(event)
        current = list(current) if isinstance(current, list) else []
        if not _command_present(current, our_cmd):
            current.extend(our_entries)
        hooks[event] = current
    merged["hooks"] = hooks
    return merged


def install_claude_hooks(worktree_path: str | Path, *, archon_cmd: str | None = None) -> Path:
    """Write/merge ``<worktree>/.claude/settings.json`` with Archon's hooks.

    Returns the settings file path. Idempotent. Never raises on a missing
    worktree parent — it creates the ``.claude`` dir.
    """
    settings_dir = Path(worktree_path) / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_file = settings_dir / "settings.json"

    existing: dict = {}
    if settings_file.exists():
        try:
            existing = json.loads(settings_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    merged = _merge(existing, build_claude_settings(archon_cmd))
    settings_file.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return settings_file


# Providers whose workers Archon can gate via a project settings.json.
_SUPPORTED = {"claude"}


def install_for_provider(provider_id: str, worktree_path: str | Path, *, archon_cmd: str | None = None) -> Path | None:
    """Install hooks if the provider supports project-level gating, else None."""
    base = (provider_id or "").removeprefix("custom:")
    if base not in _SUPPORTED or not worktree_path:
        return None
    return install_claude_hooks(worktree_path, archon_cmd=archon_cmd)
