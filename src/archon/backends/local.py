"""Local headless backend used for tests and CI-style fallback runs."""

from __future__ import annotations

import subprocess
import os
from dataclasses import dataclass, field

from .base import WorkerHandle, WorkerSpec, WorkerStatus


@dataclass
class LocalBackend:
    dry_run: bool = False
    launches: list[WorkerSpec] = field(default_factory=list)
    processes: dict[str, subprocess.Popen[str]] = field(default_factory=dict)
    sent: list[tuple[str, str]] = field(default_factory=list)

    def launch(self, spec: WorkerSpec) -> WorkerHandle:
        self.launches.append(spec)
        handle = WorkerHandle(backend_id=spec.title, title=spec.title)
        if self.dry_run:
            return handle
        command = spec.command or _default_command(spec)
        self.processes[handle.backend_id] = subprocess.Popen(
            command,
            cwd=str(spec.working_directory),
            env={**os.environ, **spec.env} if spec.env else None,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return handle

    def send(self, handle: WorkerHandle, message: str) -> None:
        """Deliver a message to the live process's stdin.

        Records every send (so callers/tests can verify delivery) and, for a
        real running process, writes the line to its stdin. Raises rather than
        silently succeeding when there is no live process to receive it, so the
        caller can surface the failure instead of assuming it landed.
        """
        self.sent.append((handle.backend_id, message))
        if self.dry_run:
            return
        proc = self.processes.get(handle.backend_id)
        if proc is None or proc.stdin is None:
            raise RuntimeError(f"no live local process for {handle.backend_id!r} to send to")
        if proc.poll() is not None:
            raise RuntimeError(f"local process {handle.backend_id!r} has already exited")
        try:
            proc.stdin.write(message if message.endswith("\n") else message + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError(f"failed to send to local process {handle.backend_id!r}: {exc}") from exc

    def status(self, handle: WorkerHandle) -> WorkerStatus:
        proc = self.processes.get(handle.backend_id)
        if proc is None:
            return WorkerStatus(state="running" if self.dry_run else "missing", cost_usd=None, last_output_tail="")
        state = "running" if proc.poll() is None else ("done" if proc.returncode == 0 else "error")
        return WorkerStatus(state=state, cost_usd=None, last_output_tail="")

    def output(self, handle: WorkerHandle, lines: int = 200) -> str:
        return ""

    def stop(self, handle: WorkerHandle) -> None:
        proc = self.processes.get(handle.backend_id)
        if proc and proc.poll() is None:
            proc.terminate()

    def attach_command(self, handle: WorkerHandle) -> list[str]:
        return []

    def list_all(self) -> list[tuple[WorkerHandle, WorkerStatus]]:
        return [
            (WorkerHandle(backend_id=spec.title, title=spec.title), self.status(WorkerHandle(spec.title, spec.title)))
            for spec in self.launches
        ]


def _default_command(spec: WorkerSpec) -> list[str]:
    if spec.tool == "claude":
        return ["claude", "-p", spec.prompt]
    if spec.tool == "codex":
        return ["codex", "exec", spec.prompt]
    return [spec.tool, spec.prompt]
