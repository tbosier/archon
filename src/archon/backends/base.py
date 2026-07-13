"""Backend contract for launching and controlling worker sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class WorkerSpec:
    title: str
    repo_path: str
    branch: str
    tool: str
    model: str | None
    prompt: str
    use_worktree: bool = True
    parent_id: str | None = None
    command: list[str] | None = None
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    @property
    def working_directory(self) -> Path:
        return Path(self.cwd or self.repo_path)


@dataclass
class WorkerHandle:
    backend_id: str
    title: str


@dataclass
class WorkerStatus:
    state: str
    cost_usd: float | None
    last_output_tail: str


class ExecutionBackend(Protocol):
    def launch(self, spec: WorkerSpec) -> WorkerHandle: ...
    def send(self, handle: WorkerHandle, message: str) -> None: ...
    def status(self, handle: WorkerHandle) -> WorkerStatus: ...
    def output(self, handle: WorkerHandle, lines: int = 200) -> str: ...
    def stop(self, handle: WorkerHandle) -> None: ...
    def attach_command(self, handle: WorkerHandle) -> list[str]: ...
    def list_all(self) -> list[tuple[WorkerHandle, WorkerStatus]]: ...
