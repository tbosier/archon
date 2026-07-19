"""Observe native Codex CLI sessions from ``~/.codex/sessions`` JSONL logs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .base import now as _now
from .model import AgentSession, AgentState


def _default_sessions_dir() -> Path:
    return Path.home() / ".codex" / "sessions"


@dataclass
class CodexAdapter:
    provider_id: str = "codex"
    sessions_dir: Path = field(default_factory=_default_sessions_dir)
    idle_after: float = 60.0
    max_sessions: int = 80
    now_fn: Callable[[], float] = _now

    def discover(self) -> list[AgentSession]:
        if not self.sessions_dir.is_dir():
            return []
        paths = sorted(
            self.sessions_dir.glob("**/*.jsonl"),
            key=lambda p: _mtime(p),
            reverse=True,
        )[: self.max_sessions]
        out: list[AgentSession] = []
        for path in paths:
            session = self._session_from_path(path)
            if session is not None:
                out.append(session)
        return out

    def _session_from_path(self, path: Path) -> AgentSession | None:
        meta = _session_meta(path)
        last_user: str | None = None
        last_summary: str | None = None
        total_tokens: int | None = None
        updated_at: str | None = None
        task_complete = False

        for rec in _tail_events(path, tail_bytes=524288):
            updated_at = str(rec.get("timestamp") or updated_at or "") or None
            typ = rec.get("type")
            payload = rec.get("payload")
            if typ == "session_meta" and isinstance(payload, dict):
                meta = payload
            elif typ == "event_msg" and isinstance(payload, dict):
                ptype = payload.get("type")
                if ptype == "user_message" and payload.get("message"):
                    last_user = str(payload["message"])
                elif ptype == "agent_message" and payload.get("message"):
                    last_summary = str(payload["message"])
                elif ptype == "task_complete":
                    task_complete = True
                    if payload.get("last_agent_message"):
                        last_summary = str(payload["last_agent_message"])
                elif ptype == "token_count":
                    total_tokens = _tokens_from_codex_payload(payload) or total_tokens

        sid = str(meta.get("session_id") or meta.get("id") or path.stem)
        cwd = meta.get("cwd")
        mtime = _mtime(path)
        if task_complete:
            state = AgentState.COMPLETED
        elif (self.now_fn() - mtime) <= self.idle_after:
            state = AgentState.WORKING
        else:
            state = AgentState.IDLE

        return AgentSession(
            session_id=f"codex:{sid}",
            provider="codex",
            state=state,
            cwd=str(cwd) if cwd else None,
            repo=(Path(str(cwd)).name if cwd else None),
            title=(last_user[:40] if last_user else (Path(str(cwd)).name if cwd else path.stem)),
            summary=(last_summary or last_user),
            updated_at=updated_at,
            source="external",
            provider_session_id=sid,
            log_path=str(path),
            total_tokens=total_tokens,
        )


def _tokens_from_codex_payload(payload: dict) -> int | None:
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    usage = info.get("total_token_usage")
    if not isinstance(usage, dict):
        return None
    value = usage.get("total_tokens")
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _session_meta(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                if isinstance(rec, dict) and rec.get("type") == "session_meta":
                    payload = rec.get("payload")
                    return payload if isinstance(payload, dict) else {}
                return {}
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def _tail_events(path: Path, *, tail_bytes: int) -> list[dict]:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = chunk.splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]
    events: list[dict] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            events.append(rec)
    return events


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
