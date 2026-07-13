"""Agent Deck backend implementation.

All ``agent-deck`` invocations live in this module so CLI drift is isolated to a
single contract surface.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .base import WorkerHandle, WorkerSpec, WorkerStatus


class AgentDeckError(RuntimeError):
    """Raised when the agent-deck CLI is missing or returns an error."""


@dataclass
class AgentDeckBackend:
    binary: str = "agent-deck"
    timeout: float = 60.0

    def launch(self, spec: WorkerSpec) -> WorkerHandle:
        # Forward the worker env (CLAUDE_CONFIG_DIR, ARCHON_TASK_RUN_ID, ARCHON_HOME…)
        # so the tmux session agent-deck spawns inherits it. Enforcement works
        # without this (project settings.json is env-independent), but this is
        # what lets the hook attribute events to the right run + DB.
        env = {**os.environ, **spec.env} if spec.env else None
        data = self._run_json(build_launch_argv(spec, binary=self.binary), env=env)
        return _handle_from_payload(data, fallback_title=spec.title)

    def send(self, handle: WorkerHandle, message: str) -> None:
        self._run(["session", "send", handle.backend_id, message])

    def status(self, handle: WorkerHandle) -> WorkerStatus:
        data = self._run_json(["session", "show", handle.backend_id, "--json"])
        return _status_from_payload(data)

    def output(self, handle: WorkerHandle, lines: int = 200) -> str:
        proc = self._run(["session", "output", handle.backend_id, "--json"])
        text = _output_from_payload(_loads(proc.stdout))
        if lines <= 0:
            return text
        return "\n".join(text.splitlines()[-lines:])

    def stop(self, handle: WorkerHandle) -> None:
        self._run(["session", "stop", handle.backend_id])

    def attach_command(self, handle: WorkerHandle) -> list[str]:
        return [self.binary, "session", "attach", handle.backend_id]

    def list_all(self) -> list[tuple[WorkerHandle, WorkerStatus]]:
        data = self._run_json(["list", "--json"])
        sessions = data if isinstance(data, list) else data.get("sessions", [])
        result: list[tuple[WorkerHandle, WorkerStatus]] = []
        if not isinstance(sessions, list):
            return result
        for item in sessions:
            if not isinstance(item, dict):
                continue
            handle = _handle_from_payload(item, fallback_title=str(item.get("title") or "worker"))
            result.append((handle, _status_from_payload(item)))
        return result

    def version(self) -> str | None:
        if shutil.which(self.binary) is None:
            return None
        proc = self._run(["--version"])
        return proc.stdout.strip() or None

    def _run_json(self, args: Sequence[str], *, env: dict | None = None) -> Any:
        proc = self._run(args, env=env)
        return _loads(proc.stdout)

    def _run(self, args: Sequence[str], *, env: dict | None = None) -> subprocess.CompletedProcess[str]:
        if shutil.which(self.binary) is None:
            raise AgentDeckError(f"{self.binary!r} is not installed")
        argv = [self.binary, *args]
        try:
            return subprocess.run(
                argv,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
        except subprocess.CalledProcessError as exc:
            msg = (exc.stderr or exc.stdout or str(exc)).strip()
            raise AgentDeckError(f"agent-deck failed: {msg}") from exc


def build_launch_argv(spec: WorkerSpec, *, binary: str = "agent-deck") -> list[str]:
    argv = [
        binary,
        "launch",
        spec.repo_path,
        "--title",
        spec.title,
        "--title-lock",
        "--no-parent",
        "--json",
        "-c",
        spec.tool,
        "--message",
        spec.prompt,
    ]
    if spec.model:
        argv += ["--model", spec.model]
    if spec.use_worktree:
        argv += ["--worktree", spec.branch, "--new-branch"]
    if spec.parent_id:
        argv += ["--parent", spec.parent_id]
        try:
            argv.remove("--no-parent")
        except ValueError:
            pass
    return argv


def _loads(raw: str) -> Any:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentDeckError(f"agent-deck returned non-JSON output: {text[:200]}") from exc


def _handle_from_payload(payload: Any, *, fallback_title: str) -> WorkerHandle:
    data = _first_dict(payload)
    backend_id = _first_str(data, "id", "session_id", "sessionId", "name", "title")
    if not backend_id:
        raise AgentDeckError(f"agent-deck response did not include a session id: {payload!r}")
    title = _first_str(data, "title", "name") or fallback_title
    return WorkerHandle(backend_id=backend_id, title=title)


def _status_from_payload(payload: Any) -> WorkerStatus:
    data = _first_dict(payload)
    state = _first_str(data, "status", "state") or "missing"
    cost = data.get("cost_usd") or data.get("costUSD") or data.get("cost")
    try:
        cost_usd = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_usd = None
    output = _first_str(data, "last_output_tail", "last_output", "output", "response") or ""
    return WorkerStatus(state=state, cost_usd=cost_usd, last_output_tail=output)


def _output_from_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    data = _first_dict(payload)
    return _first_str(data, "output", "response", "text", "content", "last_response") or ""


def _first_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        for key in ("session", "data", "result"):
            value = payload.get(key)
            if isinstance(value, dict):
                return value
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return {}


def _first_str(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return str(value)
    return None
