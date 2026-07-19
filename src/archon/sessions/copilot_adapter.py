"""Observe GitHub Copilot CLI sessions.

Source of truth on disk (verified 2026-07-13): ``~/.copilot/session-state/<uuid>/``
with ``workspace.yaml`` (cwd, name), ``events.jsonl`` (a real lifecycle stream:
``permission.requested`` / ``permission.completed``, ``assistant.turn_start`` /
``assistant.turn_end``, ``tool.execution_*``, ``session.start``), and an
``inuse.<pid>.lock`` for liveness.

Unlike Claude, Copilot's event log records permission prompts explicitly, so
approval detection works for sessions Archon never launched — the pivot's
strongest attention signal.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .base import pid_alive
from .model import AgentSession, AgentState


def _default_state_dir() -> Path:
    return Path.home() / ".copilot" / "session-state"


@dataclass
class CopilotAdapter:
    provider_id: str = "copilot"
    state_dir: Path = field(default_factory=_default_state_dir)
    alive_fn: Callable[[int | None], bool] = pid_alive

    def discover(self) -> list[AgentSession]:
        out: list[AgentSession] = []
        if not self.state_dir.is_dir():
            return out
        for d in sorted(self.state_dir.glob("*/")):
            if not d.is_dir():
                continue
            out.append(self._session_from_dir(d))
        return out

    def _session_from_dir(self, d: Path) -> AgentSession:
        ws = _parse_workspace(d / "workspace.yaml")
        alive = self._lock_alive(d)
        events_path = d / "events.jsonl"
        state, updated = _state_from_events(events_path, alive)
        usage = _usage_from_events(events_path)
        name = ws.get("name")
        cwd = ws.get("cwd")
        return AgentSession(
            session_id=f"copilot:{ws.get('id') or d.name}",
            provider="copilot",
            state=state,
            cwd=cwd,
            repo=(Path(cwd).name if cwd else None),
            title=(name[:40] if name else (Path(cwd).name if cwd else d.name)),
            summary=(name[:80] if name else None),
            updated_at=updated or ws.get("updated_at"),
            source="external",
            provider_session_id=ws.get("id") or d.name,
            cost_usd=usage.cost_usd,
            ai_credits=usage.ai_credits,
            total_tokens=usage.total_tokens,
        )

    def _lock_alive(self, d: Path) -> bool:
        for lock in d.glob("inuse.*.lock"):
            parts = lock.name.split(".")
            if len(parts) >= 2 and parts[1].isdigit() and self.alive_fn(int(parts[1])):
                return True
        return False


def _parse_workspace(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v is not None}


def _state_from_events(path: Path, alive: bool) -> tuple[AgentState, str | None]:
    if not path.exists():
        return (AgentState.WORKING if alive else AgentState.DISCONNECTED), None
    events = _tail_events(path)
    if not events:
        return (AgentState.WORKING if alive else AgentState.DISCONNECTED), None

    updated = str(events[-1].get("timestamp") or "") or None

    # Pending approval: the most recent permission.requested has no later
    # permission.completed. This is the "needs you" signal.
    last_req = last_comp = -1
    for i, ev in enumerate(events):
        t = ev.get("type")
        if t == "permission.requested":
            last_req = i
        elif t == "permission.completed":
            last_comp = i
    if last_req > last_comp:
        return AgentState.WAITING_FOR_APPROVAL, updated

    last_type = events[-1].get("type")
    if not alive:
        # Session ended. If it wrapped a turn cleanly, call it done; else gone.
        return (AgentState.COMPLETED if last_type in ("assistant.turn_end", "session.end") else AgentState.DISCONNECTED), updated
    if last_type in ("assistant.turn_end", "hook.end"):
        return AgentState.IDLE, updated
    if last_type in ("session.end",):
        return AgentState.COMPLETED, updated
    # Mid-turn / tool running.
    return AgentState.WORKING, updated


def _tail_events(path: Path, *, tail_bytes: int = 65536) -> list[dict]:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    events: list[dict] = []
    lines = chunk.splitlines()
    # Drop a possibly-partial first line when we seeked into the middle.
    if size > tail_bytes and lines:
        lines = lines[1:]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            events.append(rec)
    return events


@dataclass
class _Usage:
    cost_usd: float | None = None
    ai_credits: float | None = None
    total_tokens: int | None = None


def _usage_from_events(path: Path) -> _Usage:
    usage = _Usage()
    for ev in _tail_events(path, tail_bytes=262144):
        data = ev.get("data")
        if not isinstance(data, dict):
            data = {}

        if "totalPremiumRequests" in data:
            usage.ai_credits = _as_float(data.get("totalPremiumRequests"))
        if "totalNanoAiu" in data and usage.ai_credits is None:
            nano_aiu = _as_float(data.get("totalNanoAiu"))
            usage.ai_credits = None if nano_aiu is None else nano_aiu / 1_000_000_000

        tokens = _token_count_from_copilot(data)
        if tokens is not None:
            usage.total_tokens = tokens

        _walk_usage(ev, usage)
    return usage


def _token_count_from_copilot(data: dict) -> int | None:
    details = data.get("tokenDetails")
    if isinstance(details, dict):
        total = 0
        found = False
        for value in details.values():
            if isinstance(value, dict) and "tokenCount" in value:
                token_count = _as_int(value.get("tokenCount"))
                if token_count is not None:
                    total += token_count
                    found = True
        if found:
            return total
    return _as_int(data.get("currentTokens"))


def _walk_usage(value: Any, usage: _Usage) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"cost_usd", "total_cost_usd", "usd"}:
                usage.cost_usd = _as_float(item)
            elif lowered in {"ai_credits", "credits", "premiumrequests", "totalpremiumrequests"}:
                usage.ai_credits = _as_float(item)
            elif lowered in {"total_tokens", "totaltokens", "currenttokens"}:
                usage.total_tokens = _as_int(item)
            _walk_usage(item, usage)
    elif isinstance(value, list):
        for item in value:
            _walk_usage(item, usage)
    elif isinstance(value, str):
        match = re.search(r"AI Credits\s*([0-9]+(?:\.[0-9]+)?)", value, re.IGNORECASE)
        if match:
            usage.ai_credits = _as_float(match.group(1))


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
