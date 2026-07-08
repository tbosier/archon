"""Provider login-pane helpers.

Turns a provider id into a ready-to-run :class:`ProviderLaunch` for its native
login command, plus the pane name Archon uses for that login pane.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .providers.base import ProviderLaunch
from .providers.registry import get_provider
from .util import sanitize_slug

if TYPE_CHECKING:
    from .config import Config


def login_pane_name(provider_id: str) -> str:
    """Deterministic pane name for a provider login pane, e.g. ``codex-login``."""
    return f"{sanitize_slug(provider_id)}-login"


def login_launch_for(
    provider_id: str, config: "Config | None" = None, repo=None
) -> ProviderLaunch:
    """Build the :class:`ProviderLaunch` that runs a provider's login command."""
    provider = get_provider(provider_id, config)
    launch = provider.login_launch(repo=repo)
    launch.pane_name = login_pane_name(provider_id)
    return launch
