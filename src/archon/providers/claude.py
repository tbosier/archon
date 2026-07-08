"""Claude Code CLI adapter.

Claude is an *interactive* provider: Archon launches ``claude -n <pane>`` in a
pane, then pastes the prompt in. Telemetry does not come through this adapter at
all — it arrives out-of-band via ``archon statusline`` and ``archon hook`` — so
:meth:`parse_event_line` intentionally returns ``None``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..models import AuthStatus, TaskRun
from ..util import sanitize_slug
from .base import LaunchPurpose, ProviderEvent, ProviderLaunch, archon_env


class ClaudeProvider:
    id = "claude"
    display_name = "Claude Code CLI"
    command = "claude"
    default_mode = "interactive"

    login_command = ["claude"]

    # -- detection ----------------------------------------------------------

    def detect_installed(self) -> bool:
        return shutil.which(self.command) is not None

    def detect_auth(self) -> AuthStatus:
        # Cheap/best-effort only. We cannot tell whether claude is authenticated
        # without spending a paid model call, so we never claim "ready" here.
        if not self.detect_installed():
            return "missing"
        return "unknown"

    # -- launches -----------------------------------------------------------

    def login_launch(self, repo: Path | None = None) -> ProviderLaunch:
        return ProviderLaunch(
            argv=list(self.login_command),
            cwd=repo or Path.cwd(),
            env={},
            mode="interactive",
            expects_prompt_paste=False,
            captures_jsonl=False,
            pane_name=f"{self.id}-login",
        )

    def worker_launch(
        self, task_run: TaskRun, prompt: str, *, purpose: LaunchPurpose = "worker"
    ) -> ProviderLaunch:
        pane = task_run.zellij_pane_name or f"{self.id}-{sanitize_slug(task_run.id)}"
        return ProviderLaunch(
            argv=[self.command, "-n", pane],
            cwd=task_run.worktree or Path.cwd(),
            env=archon_env(task_run),
            mode="interactive",
            expects_prompt_paste=True,
            captures_jsonl=False,
            pane_name=pane,
            prompt=prompt,
        )

    # -- telemetry ----------------------------------------------------------

    def parse_event_line(self, line: str) -> ProviderEvent | None:
        # Claude telemetry flows through statusline/hooks, not a parsed stream.
        return None

    def compact_status(self, raw: dict) -> str | None:
        if not isinstance(raw, dict):
            return None
        cost = raw.get("cost_usd") or raw.get("cost")
        if cost is not None:
            try:
                return f"${float(cost):.2f}"
            except (TypeError, ValueError):
                return None
        return None
