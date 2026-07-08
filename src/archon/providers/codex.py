"""OpenAI Codex CLI adapter.

Codex runs in non-interactive *exec* mode and emits JSONL on stdout, so Archon
captures that stream (``captures_jsonl=True``) and hands the prompt as an argv
element rather than pasting it. Sandbox level depends on the task purpose:
reviews are read-only, features/workers get workspace-write.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..models import AuthStatus, TaskRun
from .base import LaunchPurpose, ProviderEvent, ProviderLaunch, archon_env

# Sandbox chosen per purpose. Reviews must never write; features may.
_READ_ONLY = "read-only"
_WORKSPACE_WRITE = "workspace-write"

# Rough severity mapping for known Codex event types.
_ERROR_TYPES = {"error", "stream_error", "turn_failed"}


class CodexProvider:
    id = "codex"
    display_name = "OpenAI Codex CLI"
    command = "codex"
    default_mode = "exec"

    login_command = ["codex", "login"]

    # -- detection ----------------------------------------------------------

    def detect_installed(self) -> bool:
        return shutil.which(self.command) is not None

    def detect_auth(self) -> AuthStatus:
        if not self.detect_installed():
            return "missing"
        # Best-effort only; do not run a paid model call to verify login.
        return "unknown"

    # -- launches -----------------------------------------------------------

    def login_launch(self, repo: Path | None = None) -> ProviderLaunch:
        return ProviderLaunch(
            argv=list(self.login_command),
            cwd=repo or Path.cwd(),
            env={},
            mode="exec",
            expects_prompt_paste=False,
            captures_jsonl=False,
            pane_name=f"{self.id}-login",
        )

    def worker_launch(
        self, task_run: TaskRun, prompt: str, *, purpose: LaunchPurpose = "worker"
    ) -> ProviderLaunch:
        sandbox = _READ_ONLY if purpose == "review" else _WORKSPACE_WRITE
        return ProviderLaunch(
            argv=[self.command, "exec", "--json", "--sandbox", sandbox, prompt],
            cwd=task_run.worktree or Path.cwd(),
            env=archon_env(task_run),
            mode="exec",
            expects_prompt_paste=False,
            captures_jsonl=True,
            pane_name=task_run.zellij_pane_name,
            prompt=prompt,
        )

    # -- telemetry ----------------------------------------------------------

    def parse_event_line(self, line: str) -> ProviderEvent | None:
        line = (line or "").strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            # Malformed / non-JSON output must never crash the reader.
            return None
        if not isinstance(obj, dict):
            return None
        event_type = str(obj.get("type") or obj.get("event") or "event")
        severity = "error" if event_type in _ERROR_TYPES else "info"
        message = obj.get("message") or obj.get("text")
        if message is not None:
            message = str(message)
        return ProviderEvent(
            type=event_type,
            provider_id=self.id,
            task_run_id=None,
            severity=severity,
            message=message,
            raw=obj,
        )

    def compact_status(self, raw: dict) -> str | None:
        if not isinstance(raw, dict):
            return None
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else raw
        total = usage.get("total_tokens") or usage.get("total")
        if total is not None:
            try:
                return f"{int(total)} tok"
            except (TypeError, ValueError):
                return None
        return None
