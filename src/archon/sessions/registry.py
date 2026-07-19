"""Unified session registry — one list across every provider.

Aggregates the on-disk adapters (external sessions the user launched) with
Archon's own launched runs (from the DB), so the dashboard shows everything in
one place. This is milestone 1 (discover) feeding milestone 2 (attention).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .base import SessionAdapter
from .archon_adapter import ArchonSessionAdapter
from .claude_adapter import ClaudeAdapter
from .codex_adapter import CodexAdapter
from .copilot_adapter import CopilotAdapter
from .model import AgentSession, AgentState

# Sort key: needs-you first, then active, then quiet, then terminal.
_STATE_ORDER = {
    AgentState.WAITING_FOR_APPROVAL: 0,
    AgentState.WAITING_FOR_INPUT: 0,
    AgentState.STARTING: 1,
    AgentState.WORKING: 1,
    AgentState.IDLE: 2,
    AgentState.FAILED: 3,
    AgentState.DISCONNECTED: 4,
    AgentState.COMPLETED: 5,
}

# Archon run status -> normalized state.
_RUN_STATE = {
    "running": AgentState.WORKING,
    "starting": AgentState.STARTING,
    "blocked": AgentState.WAITING_FOR_APPROVAL,
    "stale": AgentState.DISCONNECTED,
    "done": AgentState.COMPLETED,
    "failed": AgentState.FAILED,
    "crashed": AgentState.FAILED,
    "queued": AgentState.STARTING,
}


@dataclass
class ArchonDbAdapter:
    """Archon-launched runs as normalized sessions (source='archon')."""

    provider_id: str = "archon"
    conn: sqlite3.Connection | None = None

    def discover(self) -> list[AgentSession]:
        if self.conn is None:
            return []
        from .. import db

        blocked: set[str] = {
            item["task_run_id"]
            for item in db.list_attention_items(self.conn, status="open")
            if item["task_run_id"]
        }
        out: list[AgentSession] = []
        for run in db.list_task_runs(self.conn):
            if not run["provider_session_id"]:
                continue
            state = _RUN_STATE.get(run["status"], AgentState.WORKING)
            if run["id"] in blocked:
                state = AgentState.WAITING_FOR_APPROVAL
            cwd = run["worktree_path"]
            out.append(AgentSession(
                session_id=f"archon:{run['id']}",
                provider=run["provider_id"],
                state=state,
                cwd=cwd,
                repo=run["repo_name"] if "repo_name" in run.keys() else None,
                branch=run["branch"],
                title=run["task_name"] if "task_name" in run.keys() else None,
                summary=f"{run['phase'] or 'task'} · {run['task_name'] if 'task_name' in run.keys() else ''}".strip(" ·"),
                updated_at=run["updated_at"] if "updated_at" in run.keys() else None,
                source="archon",
                provider_session_id=run["provider_session_id"],
            ))
        return out


@dataclass
class SessionRegistry:
    adapters: list[SessionAdapter] = field(default_factory=list)

    def snapshot(self) -> list[AgentSession]:
        collected: list[AgentSession] = []
        for adapter in self.adapters:
            try:
                collected.extend(adapter.discover())
            except Exception:
                # A flaky adapter must never take down the whole view.
                continue
        return _dedup_and_sort(collected)


def default_registry(conn: sqlite3.Connection | None = None) -> SessionRegistry:
    adapters: list[SessionAdapter] = [ArchonSessionAdapter(), ClaudeAdapter(), CopilotAdapter(), CodexAdapter()]
    if conn is not None:
        adapters.append(ArchonDbAdapter(conn=conn))
    return SessionRegistry(adapters=adapters)


def _dedup_and_sort(sessions: list[AgentSession]) -> list[AgentSession]:
    sessions = _overlay_external_state_on_live_archon_sessions(sessions)
    seen: dict[str, AgentSession] = {}
    for s in sessions:
        key = (
            f"native:{s.provider}:{s.provider_session_id}"
            if s.provider_session_id
            else f"session:{s.session_id}"
        )
        # Prefer the Archon (DB) record when a native session id collides.
        prev = seen.get(key)
        if prev is None or (s.source == "archon" and prev.source != "archon"):
            seen[key] = s
    return sorted(
        seen.values(),
        key=lambda s: (
            0 if (s.socket_path or s.zellij_pane_id or s.zellij_tab_id) else 1,
            _STATE_ORDER.get(s.state, 9),
            0 if s.source == "archon" else 1,
            s.provider,
            s.repo or "",
            s.session_id,
        ),
    )


def _overlay_external_state_on_live_archon_sessions(sessions: list[AgentSession]) -> list[AgentSession]:
    """Collapse provider-native rows that mirror an Archon-owned live pane.

    Copilot/Claude may create their own session-state entries after Archon opens
    an interactive pane. For the user, those are the same run. Keep the Archon
    row because it has the pane id needed for Enter/left-arrow navigation, but
    copy the provider-native attention state onto it.
    """
    live_archon = [
        s for s in sessions
        if s.source == "archon"
        and (s.socket_path or (s.zellij_session and (s.zellij_pane_id or s.zellij_tab_id)))
    ]
    if not live_archon:
        return sessions
    skip: set[int] = set()
    for idx, external in enumerate(sessions):
        if external.source == "archon":
            continue
        match = _matching_live_archon(external, live_archon)
        if match is None:
            continue
        if external.needs_attention:
            match.state = external.state
        if external.summary:
            match.summary = external.summary
        if external.updated_at:
            match.updated_at = external.updated_at
        if external.cost_usd is not None:
            match.cost_usd = external.cost_usd
        if external.ai_credits is not None:
            match.ai_credits = external.ai_credits
        if external.total_tokens is not None:
            match.total_tokens = external.total_tokens
        skip.add(idx)
    return [s for idx, s in enumerate(sessions) if idx not in skip]


def _matching_live_archon(external: AgentSession, candidates: list[AgentSession]) -> AgentSession | None:
    ext_time = _parse_time(external.updated_at)
    best: tuple[float, AgentSession] | None = None
    for candidate in candidates:
        if candidate.provider != external.provider:
            continue
        if candidate.cwd and external.cwd and candidate.cwd != external.cwd:
            continue
        cand_time = _parse_time(candidate.updated_at)
        if ext_time is not None and cand_time is not None:
            delta = abs((ext_time - cand_time).total_seconds())
            if delta > 900:
                continue
        else:
            delta = 0.0
        if best is None or delta < best[0]:
            best = (delta, candidate)
    return best[1] if best else None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
