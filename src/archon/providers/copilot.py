"""GitHub Copilot CLI adapter.

Copilot supports two shapes: a one-shot programmatic prompt (``copilot -p
<prompt>``) and a fully interactive pane (``copilot``). Archon prefers the ``-p``
prompt form when a prompt is supplied so output can be captured as stdout text;
with no prompt it falls back to the interactive pane.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config import ModelTier, ProviderModels
from ..models import AuthStatus, TaskRun
from .base import LaunchPurpose, ProviderEvent, ProviderLaunch, archon_env


class CopilotProvider:
    id = "copilot"
    display_name = "GitHub Copilot CLI"
    command = "copilot"
    default_mode = "interactive"

    login_command = ["copilot", "login"]
    # If `copilot login` is unavailable in the user's version, they can launch
    # `copilot` and use the interactive /login flow.
    alt_login_command = ["copilot"]

    # Set by the registry when a config is available; None means no tiering.
    models: ProviderModels | None = None

    # -- detection ----------------------------------------------------------

    def detect_installed(self) -> bool:
        return shutil.which(self.command) is not None

    def detect_auth(self) -> AuthStatus:
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
        from .. import phases

        cwd = task_run.worktree or Path.cwd()
        env = archon_env(task_run)
        pane = task_run.zellij_pane_name
        phase = getattr(task_run, "phase", None) or "execute"
        tier = self.models.for_phase(phase) if self.models else ModelTier()
        extra = phases.model_args(self.id, tier)
        if tier.model is not None:
            task_run.model = tier.model
        if prompt:
            # Programmatic one-shot prompt; prompt is passed as an argv element.
            return ProviderLaunch(
                argv=[self.command, "-p", prompt, *extra],
                cwd=cwd,
                env=env,
                mode="prompt",
                expects_prompt_paste=False,
                captures_jsonl=False,
                pane_name=pane,
                prompt=prompt,
            )
        # Interactive pane; nothing to paste since there is no prompt.
        return ProviderLaunch(
            argv=[self.command, *extra],
            cwd=cwd,
            env=env,
            mode="interactive",
            expects_prompt_paste=False,
            captures_jsonl=False,
            pane_name=pane,
            prompt=None,
        )

    # -- telemetry ----------------------------------------------------------

    def parse_event_line(self, line: str) -> ProviderEvent | None:
        # Copilot MVP captures plain stdout text; no structured stream to parse.
        return None

    def compact_status(self, raw: dict) -> str | None:
        return None
