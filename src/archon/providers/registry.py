"""Provider registry: the one place that knows the concrete adapters.

The Archon core resolves providers through :func:`get_provider` / :func:`known_providers`
so nothing else needs to import a concrete adapter (keeping the core provider-agnostic).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import AgentProvider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .copilot import CopilotProvider
from .custom import CustomProvider

if TYPE_CHECKING:
    from ..config import Config

# Ordered so callers get a stable, spec-matching sequence.
_BUILTINS: dict[str, type] = {
    "claude": ClaudeProvider,
    "codex": CodexProvider,
    "copilot": CopilotProvider,
}


def known_provider_ids() -> list[str]:
    """The three built-in provider ids, in display order."""
    return list(_BUILTINS.keys())


def known_providers() -> list[AgentProvider]:
    """Fresh instances of the three built-in adapters."""
    return [cls() for cls in _BUILTINS.values()]


def get_provider(provider_id: str, config: "Config | None" = None) -> AgentProvider:
    """Resolve a provider id to a concrete adapter instance.

    Built-in ids (``claude``/``codex``/``copilot``) return the matching adapter.
    A ``custom:<name>`` id (or a bare custom id present in ``config.custom``)
    builds a :class:`CustomProvider` from that config entry.
    """
    if provider_id in _BUILTINS:
        adapter = _BUILTINS[provider_id]()
        if config is not None:
            provider_config = config.providers.get(provider_id)
            if provider_config is not None:
                adapter.models = provider_config.models
        return adapter

    if config is not None:
        wanted = provider_id[len("custom:"):] if provider_id.startswith("custom:") else provider_id
        for entry in config.custom:
            if entry.id == wanted or f"custom:{entry.id}" == provider_id:
                return CustomProvider(entry)

    raise KeyError(f"Unknown provider id: {provider_id!r}")
