"""Provider adapter contract.

An ``AgentProvider`` knows how to detect, log into, and launch one AI coding CLI,
and how to normalise that CLI's output into Archon's ``ProviderEvent`` shape. The
Archon core never imports a concrete provider directly — it goes through the
registry and this Protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from ..models import AuthStatus, ProviderMode, TaskRun

LaunchPurpose = Literal["worker", "review", "feature", "login"]


@dataclass
class ProviderInfo:
    id: str
    display_name: str
    command: str
    default_mode: ProviderMode
    login_command: list[str] | None
    installed: bool
    auth_status: AuthStatus
    notes: str | None = None


@dataclass
class ProviderLaunch:
    """A concrete, ready-to-run process description for a Zellij pane."""

    argv: list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    mode: ProviderMode = "interactive"
    # Interactive providers need the prompt pasted into the pane after launch;
    # exec/prompt providers receive it as an argv element instead.
    expects_prompt_paste: bool = False
    captures_jsonl: bool = False
    pane_name: str | None = None
    prompt: str | None = None


@dataclass
class ProviderEvent:
    type: str
    provider_id: str
    task_run_id: str | None = None
    severity: str = "info"
    message: str | None = None
    raw: dict | None = None


@runtime_checkable
class AgentProvider(Protocol):
    id: str
    display_name: str
    command: str
    default_mode: ProviderMode

    def detect_installed(self) -> bool: ...
    def detect_auth(self) -> AuthStatus: ...
    def login_launch(self, repo: Path | None = None) -> ProviderLaunch: ...
    def worker_launch(
        self, task_run: TaskRun, prompt: str, *, purpose: LaunchPurpose = "worker"
    ) -> ProviderLaunch: ...
    def parse_event_line(self, line: str) -> ProviderEvent | None: ...
    def compact_status(self, raw: dict) -> str | None: ...


def archon_env(task_run: TaskRun) -> dict[str, str]:
    """Standard ``ARCHON_*`` environment injected into every provider process."""
    env: dict[str, str] = {
        "ARCHON_TASK_ID": task_run.task_id,
        "ARCHON_TASK_RUN_ID": task_run.id,
        "ARCHON_PROVIDER_ID": task_run.provider_id,
    }
    if task_run.worktree_path:
        env["ARCHON_WORKTREE"] = task_run.worktree_path
    if task_run.zellij_session:
        env["ARCHON_ZELLIJ_SESSION"] = task_run.zellij_session
    if task_run.zellij_pane_name:
        env["ARCHON_PANE_NAME"] = task_run.zellij_pane_name
    env.update(task_run.env_extra)
    return env
