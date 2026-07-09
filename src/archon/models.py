"""Domain models shared across Archon: providers, tasks, and task runs.

These are deliberately lightweight dataclasses. They mirror the SQLite schema in
:mod:`archon.db` but are provider-agnostic — nothing here is Claude-specific.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# --- Vocabularies (kept as Literal for typing + used as plain strings in the DB) ---

ProviderMode = Literal["interactive", "exec", "prompt"]
AuthStatus = Literal["ready", "needs_login", "unknown", "missing", "error"]
TaskType = Literal["pr_review", "feature", "test", "security", "custom"]
ProviderPolicy = Literal["single", "multi_review", "variants", "ask"]
# A task's phase selects the provider model tier (plan/review = strong,
# execute/test = cheaper) and drives the plan→execute→review→test chain.
Phase = Literal["plan", "execute", "review", "test"]
WorkerState = Literal["idle", "busy", "offline"]

TaskStatus = Literal[
    "queued", "starting", "running", "blocked", "stale",
    "crashed", "done", "failed", "budget_capped", "awaiting_provider",
]
RunStatus = Literal[
    "queued", "starting", "running", "blocked", "stale",
    "crashed", "done", "failed", "budget_capped",
]

# Urgency ordering used by ``archon status`` / the TUI. Lower == more urgent.
RUN_URGENCY: dict[str, int] = {
    "blocked": 0,
    "budget_capped": 1,
    "stale": 2,
    "crashed": 3,
    "failed": 4,
    "running": 5,
    "starting": 6,
    "queued": 7,
    "done": 8,
}

# Status -> colour, consumed by the Rich dashboard.
STATUS_COLORS: dict[str, str] = {
    "blocked": "red",
    "budget_capped": "red",
    "stale": "yellow",
    "crashed": "magenta",
    "failed": "red",
    "running": "blue",
    "queued": "white",
    "starting": "cyan",
    "done": "green",
    "needs_login": "yellow",
    "ready": "green",
    "missing": "red",
    "unknown": "dim",
    "error": "red",
}


def run_urgency(status: str) -> int:
    return RUN_URGENCY.get(status, 99)


# Traffic-light health cue for the dashboard: (glyph, color, label).
# green = working/healthy, yellow = needs a human, red = broken, dim = idle.
_HEALTH_WORKING = {"running", "starting"}
_HEALTH_ATTENTION = {"blocked", "needs_login", "stale", "budget_capped"}
_HEALTH_PROBLEM = {"crashed", "failed", "error", "missing"}
_HEALTH_DONE = {"done", "ready"}


def health_of(status: str) -> tuple[str, str, str]:
    if status in _HEALTH_PROBLEM:
        return ("●", "red", "problem")
    if status in _HEALTH_ATTENTION:
        return ("●", "yellow", "needs help")
    if status in _HEALTH_WORKING:
        return ("●", "green", "working")
    if status in _HEALTH_DONE:
        return ("✓", "green", "done")
    return ("○", "dim", "waiting")


@dataclass
class Repo:
    name: str
    root_path: str
    zellij_session: str
    id: int | None = None


@dataclass
class Provider:
    id: str                       # claude | codex | copilot | custom:<name>
    display_name: str
    command: str
    enabled: bool = False
    installed: bool = False
    auth_status: str = "unknown"
    default_mode: str = "interactive"
    login_command: str | None = None
    last_checked_at: str | None = None
    last_error: str | None = None


@dataclass
class Task:
    id: str
    repo_id: int
    type: str
    name: str
    status: str
    prompt: str
    provider_policy: str = "single"
    priority: int = 0
    pr_number: int | None = None
    phase: str = "execute"            # plan | execute | review | test
    parent_task_id: str | None = None  # groups a feature's plan/execute/review/test
    provider_id: str | None = None     # assigned provider for scheduler auto-dispatch


@dataclass
class Worker:
    """A provider slot in the idle-worker pool."""

    id: str
    provider_id: str
    zellij_session: str | None = None
    zellij_pane_id: str | None = None
    state: str = "idle"               # idle | busy | offline
    current_task_run_id: str | None = None
    max_concurrency: int = 1


@dataclass
class TaskDependency:
    task_id: str
    depends_on_task_id: str


@dataclass
class TaskRun:
    """A single provider's execution of a task, bound to one worktree/branch/pane."""

    id: str
    task_id: str
    provider_id: str
    status: str = "queued"
    phase: str = "execute"
    model: str | None = None
    branch: str | None = None
    base_branch: str | None = None
    worktree_path: str | None = None
    zellij_session: str | None = None
    zellij_pane_id: str | None = None
    zellij_pane_name: str | None = None
    provider_session_name: str | None = None
    provider_session_id: str | None = None
    provider_run_id: str | None = None
    transcript_path: str | None = None
    stdout_log_path: str | None = None
    stderr_log_path: str | None = None
    cost_usd: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    context_used_pct: float | None = None
    rate_limit_five_hour_pct: float | None = None
    rate_limit_seven_day_pct: float | None = None
    last_heartbeat_at: str | None = None
    last_output_at: str | None = None
    soft_budget_usd: float | None = None
    hard_budget_usd: float | None = None

    # Convenience for adapters building environments — not persisted.
    env_extra: dict[str, str] = field(default_factory=dict)

    @property
    def worktree(self) -> Path | None:
        return Path(self.worktree_path) if self.worktree_path else None
