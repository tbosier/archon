"""Provider-agnostic agent-session model, adapters, and registry (the pivot)."""

from __future__ import annotations

from .base import SessionAdapter, pid_alive
from .archon_adapter import ArchonSessionAdapter
from .claude_adapter import ClaudeAdapter
from .codex_adapter import CodexAdapter
from .copilot_adapter import CopilotAdapter
from .launch import KNOWN_PROVIDERS, RoutedPrompt, launch_agent, parse_provider_suffix
from .model import AgentSession, AgentState, summarize, usage_line
from .registry import ArchonDbAdapter, SessionRegistry, default_registry

__all__ = [
    "AgentState",
    "AgentSession",
    "summarize",
    "usage_line",
    "SessionAdapter",
    "pid_alive",
    "ArchonSessionAdapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "CopilotAdapter",
    "KNOWN_PROVIDERS",
    "RoutedPrompt",
    "launch_agent",
    "parse_provider_suffix",
    "ArchonDbAdapter",
    "SessionRegistry",
    "default_registry",
]
