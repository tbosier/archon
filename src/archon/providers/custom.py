"""Generic custom provider adapter.

Built from a :class:`archon.config.CustomProviderConfig` so users can wire an
arbitrary CLI (e.g. ``aider``) into Archon without code changes. Defaults to an
interactive paste-delivery provider; set ``prompt_delivery="arg"`` to pass the
prompt as an argv element instead.
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path

from ..config import CustomProviderConfig
from ..models import AuthStatus, TaskRun
from .base import LaunchPurpose, ProviderEvent, ProviderLaunch, archon_env


class CustomProvider:
    def __init__(self, config: CustomProviderConfig) -> None:
        self._config = config
        self.id = config.id if config.id.startswith("custom:") else f"custom:{config.id}"
        self.display_name = config.display_name
        self.command = config.command
        self.default_mode = config.default_mode
        self.login_command = (
            shlex.split(config.login_command) if config.login_command else None
        )
        self._paste = config.prompt_delivery == "paste"

    # -- detection ----------------------------------------------------------

    def detect_installed(self) -> bool:
        return shutil.which(self.command) is not None

    def detect_auth(self) -> AuthStatus:
        if not self.detect_installed():
            return "missing"
        return "unknown"

    # -- launches -----------------------------------------------------------

    def login_launch(self, repo: Path | None = None) -> ProviderLaunch:
        argv = list(self.login_command) if self.login_command else [self.command]
        return ProviderLaunch(
            argv=argv,
            cwd=repo or Path.cwd(),
            env={},
            mode=self.default_mode,
            expects_prompt_paste=False,
            captures_jsonl=False,
            pane_name=f"{self._config.id}-login",
        )

    def worker_launch(
        self, task_run: TaskRun, prompt: str, *, purpose: LaunchPurpose = "worker"
    ) -> ProviderLaunch:
        cwd = task_run.worktree or Path.cwd()
        env = archon_env(task_run)
        pane = task_run.zellij_pane_name
        if self._paste:
            # Interactive: launch bare command and paste the prompt in.
            return ProviderLaunch(
                argv=[self.command],
                cwd=cwd,
                env=env,
                mode=self.default_mode,
                expects_prompt_paste=True,
                captures_jsonl=False,
                pane_name=pane,
                prompt=prompt,
            )
        # Non-interactive: prompt travels as an argv element.
        return ProviderLaunch(
            argv=[self.command, prompt],
            cwd=cwd,
            env=env,
            mode=self.default_mode,
            expects_prompt_paste=False,
            captures_jsonl=False,
            pane_name=pane,
            prompt=prompt,
        )

    # -- telemetry ----------------------------------------------------------

    def parse_event_line(self, line: str) -> ProviderEvent | None:
        return None

    def compact_status(self, raw: dict) -> str | None:
        return None
