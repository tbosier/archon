"""Provider-agnostic session model.

The whole pivot rests on this seam: every provider adapter normalises its own
mess (registry files, JSONL event logs, pid locks, or Archon's own DB) into an
:class:`AgentSession`, and the dashboard consumes *only* this — never a
provider-specific shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AgentState(str, Enum):
    STARTING = "starting"
    WORKING = "working"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_INPUT = "waiting_for_input"
    IDLE = "idle"
    COMPLETED = "completed"
    FAILED = "failed"
    DISCONNECTED = "disconnected"


# States where the human is the blocker — the "needs you" bucket.
_ATTENTION_STATES = frozenset({AgentState.WAITING_FOR_APPROVAL, AgentState.WAITING_FOR_INPUT})
# (glyph, color, one-word label) — reuses Archon's traffic-light vocabulary.
_GLYPHS: dict[AgentState, tuple[str, str, str]] = {
    AgentState.STARTING: ("●", "cyan", "starting"),
    AgentState.WORKING: ("●", "green", "working"),
    AgentState.WAITING_FOR_APPROVAL: ("!", "yellow", "needs you"),
    AgentState.WAITING_FOR_INPUT: ("!", "yellow", "needs you"),
    AgentState.IDLE: ("○", "dim", "idle"),
    AgentState.COMPLETED: ("✓", "green", "done"),
    AgentState.FAILED: ("✕", "red", "failed"),
    AgentState.DISCONNECTED: ("⚠", "dim", "disconnected"),
}


@dataclass
class AgentSession:
    """One coding-agent session, normalised across providers."""

    session_id: str
    provider: str                       # claude | codex | copilot | ...
    state: AgentState
    cwd: str | None = None              # working directory / repo root
    repo: str | None = None             # short repo name (basename of cwd)
    branch: str | None = None
    title: str | None = None            # short human name for the session
    summary: str | None = None          # one-line "what it's doing"
    updated_at: str | None = None       # ISO/string timestamp of last activity
    pid: int | None = None
    source: str = "external"            # "archon" (we launched it) | "external"
    provider_session_id: str | None = None   # backend/native id, for attach
    socket_path: str | None = None           # Archon's persistent PTY host
    log_path: str | None = None
    error_log_path: str | None = None
    zellij_session: str | None = None
    zellij_pane_id: str | None = None
    zellij_tab_id: str | None = None
    cost_usd: float | None = None
    ai_credits: float | None = None
    total_tokens: int | None = None

    @property
    def needs_attention(self) -> bool:
        return self.state in _ATTENTION_STATES

    @property
    def glyph(self) -> tuple[str, str]:
        g, color, _ = _GLYPHS.get(self.state, ("○", "dim", "?"))
        return g, color

    @property
    def label(self) -> str:
        return _GLYPHS.get(self.state, ("○", "dim", "?"))[2]


def summarize(sessions: list[AgentSession]) -> dict[str, int]:
    """Header counts for the dashboard: working / need-you / failed / done."""
    working = sum(1 for s in sessions if s.state in (AgentState.WORKING, AgentState.STARTING))
    need_you = sum(1 for s in sessions if s.needs_attention)
    failed = sum(1 for s in sessions if s.state == AgentState.FAILED)
    done = sum(1 for s in sessions if s.state == AgentState.COMPLETED)
    idle = sum(1 for s in sessions if s.state == AgentState.IDLE)
    disconnected = sum(1 for s in sessions if s.state == AgentState.DISCONNECTED)
    return {
        "working": working, "need_you": need_you, "failed": failed,
        "done": done, "idle": idle, "disconnected": disconnected,
    }


def usage_line(session: AgentSession) -> str:
    parts: list[str] = []
    if session.cost_usd is not None:
        parts.append(f"${session.cost_usd:.2f}")
    if session.ai_credits is not None:
        parts.append(f"{session.ai_credits:g} cr")
    if session.total_tokens is not None:
        parts.append(f"{_compact_int(session.total_tokens)} tok")
    return "  ".join(parts)


def _compact_int(value: int) -> str:
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}m".rstrip("0").rstrip(".")
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k".rstrip("0").rstrip(".")
    return str(value)
