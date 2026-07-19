"""Observe Claude Code sessions — including ones the user launched themselves.

Source of truth on disk (verified 2026-07-13):
- ``~/.claude/sessions/<pid>.json`` — one live registry entry per running Claude
  process: ``{pid, sessionId, cwd, name, kind, entrypoint, version, ...}``.
- ``~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`` — the transcript; its
  freshness + last record tell us working vs idle.

Honest limitation: a permission/approval prompt is not clearly marked in the
transcript, so approval detection for a *user-launched* Claude session is not
available here — that signal comes from Archon's own PreToolUse/PermissionRequest
hooks (DB attention) for sessions Archon launched, and the registry overlays it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .base import now as _now
from .base import pid_alive
from .model import AgentSession, AgentState


def _default_sessions_dir() -> Path:
    return Path.home() / ".claude" / "sessions"


def _default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


@dataclass
class ClaudeAdapter:
    provider_id: str = "claude"
    sessions_dir: Path = field(default_factory=_default_sessions_dir)
    projects_dir: Path = field(default_factory=_default_projects_dir)
    idle_after: float = 60.0  # seconds since last transcript write -> IDLE
    alive_fn: Callable[[int | None], bool] = pid_alive
    now_fn: Callable[[], float] = _now

    def discover(self) -> list[AgentSession]:
        out: list[AgentSession] = []
        if not self.sessions_dir.is_dir():
            return out
        for path in sorted(self.sessions_dir.glob("*.json")):
            try:
                reg = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(reg, dict):
                continue
            out.append(self._session_from_registry(reg))
        return out

    # -- internals ---------------------------------------------------------
    def _session_from_registry(self, reg: dict) -> AgentSession:
        pid = reg.get("pid")
        sid = str(reg.get("sessionId") or reg.get("id") or pid or "")
        cwd = reg.get("cwd")
        alive = self.alive_fn(pid)

        transcript = self._find_transcript(sid)
        state, summary, updated = self._derive_state(alive, transcript)

        return AgentSession(
            session_id=f"claude:{sid}" if sid else f"claude:pid{pid}",
            provider="claude",
            state=state,
            cwd=cwd,
            repo=(Path(cwd).name if cwd else None),
            title=reg.get("name") or (Path(cwd).name if cwd else None),
            summary=summary,
            updated_at=updated,
            pid=pid,
            source="external",
            provider_session_id=sid or None,
        )

    def _find_transcript(self, sid: str) -> Path | None:
        if not sid or not self.projects_dir.is_dir():
            return None
        # Glob by sessionId across project dirs — avoids brittle cwd encoding.
        for match in self.projects_dir.glob(f"*/{sid}.jsonl"):
            return match
        return None

    def _derive_state(self, alive: bool, transcript: Path | None) -> tuple[AgentState, str | None, str | None]:
        if not alive:
            return AgentState.DISCONNECTED, None, None
        if transcript is None or not transcript.exists():
            return AgentState.STARTING, None, None
        try:
            mtime = transcript.stat().st_mtime
        except OSError:
            return AgentState.STARTING, None, None
        summary = _last_text(transcript)
        updated = _iso(mtime)
        if (self.now_fn() - mtime) <= self.idle_after:
            return AgentState.WORKING, summary, updated
        return AgentState.IDLE, summary, updated


def _iso(epoch: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).isoformat(timespec="seconds")


def _last_text(path: Path, *, tail_bytes: int = 8192) -> str | None:
    """Best-effort one-line summary from the transcript's last record."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = _text_of(rec)
        if text:
            return text[:80]
    return None


def _text_of(rec: dict) -> str | None:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else rec.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                return str(block["text"]).strip() or None
    return None
