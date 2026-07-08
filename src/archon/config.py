"""Typed Archon configuration: load, save, defaults.

Config lives at ``<config_home>/config.yaml`` (see :mod:`archon.paths`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .paths import Paths, resolve_paths

# Built-in provider knowledge. Mirrors the "Provider defaults" table in the spec.
KNOWN_PROVIDER_DEFAULTS: dict[str, dict] = {
    "claude": {
        "display_name": "Claude Code CLI",
        "command": "claude",
        "default_mode": "interactive",
        "login_command": "claude",
        "notes": "Launching claude surfaces native login/setup if needed.",
        # Strong model for planning/analysis; cheaper model for execution.
        "models": {
            "plan": {"model": "claude-opus-4-8"},
            "execute": {"model": "claude-sonnet-5"},
        },
    },
    "codex": {
        "display_name": "OpenAI Codex CLI",
        "command": "codex",
        "default_mode": "exec",
        "login_command": "codex login",
        "alt_login_command": "codex login --device-auth",
        "notes": "Codex supports explicit login and non-interactive exec mode.",
        "models": {
            "plan": {"model": "gpt-5.5", "reasoning": "high"},
            "execute": {"model": "gpt-5.5", "reasoning": "medium"},
        },
    },
    "copilot": {
        "display_name": "GitHub Copilot CLI",
        "command": "copilot",
        "default_mode": "interactive",
        "login_command": "copilot login",
        "alt_login_command": "copilot",
        "notes": "Copilot authenticates via `copilot login` or interactive /login.",
        "models": {"plan": {}, "execute": {}},
    },
}


class ModelTier(BaseModel):
    """Which model/reasoning a provider uses for one phase of work."""

    model: str | None = None
    reasoning: str | None = None          # codex: low | medium | high
    extra_args: list[str] = Field(default_factory=list)


class ProviderModels(BaseModel):
    """Per-phase model tiers: a strong model to plan, a cheaper one to execute."""

    plan: ModelTier = Field(default_factory=ModelTier)
    execute: ModelTier = Field(default_factory=ModelTier)

    def for_phase(self, phase: str) -> ModelTier:
        # Analytical phases (plan, review) use the strong tier; doing phases
        # (execute, test) use the cheaper tier.
        return self.plan if phase in ("plan", "review") else self.execute


class ProviderConfig(BaseModel):
    enabled: bool = False
    display_name: str
    command: str
    default_mode: Literal["interactive", "exec", "prompt"] = "interactive"
    login_command: str | None = None
    alt_login_command: str | None = None
    notes: str | None = None
    exec_args: list[str] = Field(default_factory=list)
    review_args: list[str] = Field(default_factory=list)
    prompt_args: list[str] = Field(default_factory=list)
    telemetry: str | None = None
    models: ProviderModels = Field(default_factory=ProviderModels)


class CustomProviderConfig(BaseModel):
    id: str
    display_name: str
    command: str
    enabled: bool = False
    default_mode: Literal["interactive", "exec", "prompt"] = "interactive"
    login_command: str | None = None
    prompt_delivery: Literal["paste", "arg"] = "paste"


class StartupConfig(BaseModel):
    show_provider_wizard: Literal["auto", "always", "never"] = "auto"
    provider_panes: Literal["launch_now", "spawn_on_task"] = "launch_now"
    default_task_provider_policy: Literal[
        "ask_if_multiple", "single", "all"
    ] = "ask_if_multiple"


class BudgetConfig(BaseModel):
    """Cost + rate-limit safety rails consumed by the scheduler."""

    soft_usd: float | None = None
    hard_usd: float | None = None
    # Five-hour rate-limit thresholds (percent) → dispatch behaviour, per spec §14.
    prefer_small_at_pct: float = 70.0
    no_new_impl_at_pct: float = 85.0
    pause_at_pct: float = 95.0


class SchedulerConfig(BaseModel):
    """Task queue / idle-worker-pool controls."""

    max_concurrency: int = 3               # global cap on simultaneously running runs
    per_provider_concurrency: int = 1      # cap per provider (one writer per provider)
    auto_handoff: bool = True              # feature → review → test on completion
    plan_before_execute: bool = True       # features get a plan phase before executing
    budget: BudgetConfig = Field(default_factory=BudgetConfig)


class Config(BaseModel):
    version: int = 1
    startup: StartupConfig = Field(default_factory=StartupConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    custom: list[CustomProviderConfig] = Field(default_factory=list)

    # ---- helpers -----------------------------------------------------------

    def enabled_provider_ids(self) -> list[str]:
        ids = [pid for pid, p in self.providers.items() if p.enabled]
        ids += [c.id for c in self.custom if c.enabled]
        return ids

    def is_configured(self) -> bool:
        """True once the user has enabled at least one provider.

        ``default_config()`` pre-seeds the three known providers (all disabled),
        so emptiness of the dict is not a reliable "first run" signal — an
        enabled provider is.
        """
        return bool(self.enabled_provider_ids())

    def provider(self, provider_id: str) -> ProviderConfig | None:
        return self.providers.get(provider_id)


def default_config() -> Config:
    """A config seeded with the three known providers, all disabled."""
    providers = {
        pid: ProviderConfig(enabled=False, **{
            k: v for k, v in defaults.items()
            if k in ProviderConfig.model_fields
        })
        for pid, defaults in KNOWN_PROVIDER_DEFAULTS.items()
    }
    return Config(providers=providers)


def load_config(paths: Paths | None = None) -> Config:
    """Load config from disk, or return an in-memory default if absent."""
    paths = paths or resolve_paths()
    path = paths.config_file
    if not path.exists():
        return default_config()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(raw)


def save_config(config: Config, paths: Paths | None = None) -> Path:
    """Persist config as YAML, creating the config dir if needed."""
    paths = (paths or resolve_paths()).ensure()
    path = paths.config_file
    data = config.model_dump(mode="json", exclude_none=False)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return path
