"""Observe sessions launched directly from the Archon agent view."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..paths import resolve_paths
from .base import pid_alive
from .model import AgentSession, AgentState


def _default_sessions_dir() -> Path:
    return resolve_paths().sessions_dir


_STATUS_TO_STATE = {
    "starting": AgentState.STARTING,
    "created": AgentState.IDLE,
    "running": AgentState.WORKING,
    "interactive": AgentState.WORKING,
    "completed": AgentState.COMPLETED,
    "failed": AgentState.FAILED,
}


@dataclass
class ArchonSessionAdapter:
    provider_id: str = "archon"
    sessions_dir: Path = field(default_factory=_default_sessions_dir)
    alive_fn: Callable[[int | None], bool] = pid_alive

    def discover(self) -> list[AgentSession]:
        if not self.sessions_dir.is_dir():
            return []
        out: list[AgentSession] = []
        for path in sorted(self.sessions_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            out.append(self._from_state(data))
        return out

    def _from_state(self, data: dict) -> AgentSession:
        status = str(data.get("status") or "starting")
        pid = data.get("pid")
        state = _STATUS_TO_STATE.get(status, AgentState.STARTING)
        if status == "running" and not self.alive_fn(pid):
            state = AgentState.DISCONNECTED
        cwd = data.get("cwd")
        return AgentSession(
            session_id=f"archon:{data.get('session_id') or pid}",
            provider=str(data.get("provider") or "archon"),
            state=state,
            cwd=cwd,
            repo=(Path(cwd).name if cwd else None),
            title=data.get("title"),
            summary=data.get("summary") or data.get("prompt"),
            updated_at=data.get("updated_at"),
            pid=pid,
            source="archon",
            provider_session_id=data.get("provider_session_id") or data.get("session_id"),
            socket_path=data.get("socket_path"),
            log_path=data.get("out_path"),
            error_log_path=data.get("err_path"),
            zellij_session=data.get("zellij_session"),
            zellij_pane_id=data.get("zellij_pane_id"),
            zellij_tab_id=data.get("zellij_tab_id"),
            cost_usd=data.get("cost_usd"),
            ai_credits=data.get("ai_credits"),
            total_tokens=data.get("total_tokens"),
        )
