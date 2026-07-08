"""Per-phase model tiering.

Archon runs each task in a *phase* (plan/execute/review/test). Analytical phases
(plan, review) deserve a strong model; doing phases (execute, test) use a cheaper
one. This module is the single place that maps a (provider, phase) pair to the
concrete CLI flags that select that model.

It imports :mod:`archon.config` only — no provider adapters — so adapters can
import it without creating a cycle.
"""

from __future__ import annotations

from .config import Config, ModelTier


def resolve_tier(config: Config | None, provider_id: str, phase: str) -> ModelTier:
    """Return the model tier configured for ``provider_id`` in ``phase``.

    Falls back to an empty :class:`ModelTier` (no model selected) when there is
    no config, no such provider, or nothing configured for that phase.
    """
    if config is None:
        return ModelTier()
    provider = config.providers.get(provider_id)
    if provider is None:
        return ModelTier()
    return provider.models.for_phase(phase)


def model_args(provider_id: str, tier: ModelTier) -> list[str]:
    """Provider-specific CLI flags selecting ``tier``'s model + reasoning.

    Returns ``[]`` when the tier has no model. ``tier.extra_args`` is always
    appended last.
    """
    if tier.model is None:
        return []

    if provider_id == "codex":
        args = ["--model", tier.model]
        if tier.reasoning:
            args += ["-c", f"model_reasoning_effort={tier.reasoning}"]
    else:
        # claude, copilot, custom, and any unknown provider share the same shape.
        args = ["--model", tier.model]

    args += list(tier.extra_args)
    return args
