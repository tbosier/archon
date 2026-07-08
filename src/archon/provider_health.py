"""Best-effort provider health checks.

Health is intentionally cheap: an installed-command probe (``shutil.which`` via
the adapter) plus a best-effort auth status. We never spend a paid model call to
determine auth — uncertain means ``unknown``, and a missing command short-circuits
to ``missing``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .providers.base import AgentProvider
from .providers.registry import known_providers


@dataclass
class ProviderHealth:
    id: str
    display_name: str
    command: str
    installed: bool
    auth_status: str  # ready | needs_login | unknown | missing | error
    default_mode: str
    notes: str | None = None


def check_provider(provider: AgentProvider) -> ProviderHealth:
    """Probe a single provider. Never raises — errors become ``auth_status="error"``."""
    notes: str | None = None
    try:
        installed = provider.detect_installed()
    except Exception as exc:  # pragma: no cover - defensive
        return ProviderHealth(
            id=provider.id,
            display_name=getattr(provider, "display_name", provider.id),
            command=getattr(provider, "command", ""),
            installed=False,
            auth_status="error",
            default_mode=getattr(provider, "default_mode", "interactive"),
            notes=str(exc),
        )

    if not installed:
        auth_status = "missing"
    else:
        try:
            auth_status = provider.detect_auth()
        except Exception as exc:  # pragma: no cover - defensive
            auth_status = "error"
            notes = str(exc)

    return ProviderHealth(
        id=provider.id,
        display_name=getattr(provider, "display_name", provider.id),
        command=getattr(provider, "command", ""),
        installed=installed,
        auth_status=auth_status,
        default_mode=getattr(provider, "default_mode", "interactive"),
        notes=notes,
    )


def check_all(
    providers: list[AgentProvider] | None = None,
) -> dict[str, ProviderHealth]:
    """Check every provider, keyed by id. Defaults to the three built-ins."""
    providers = providers if providers is not None else known_providers()
    return {p.id: check_provider(p) for p in providers}
