"""First-run provider-selection wizard.

Drives which providers Archon enables and how their panes start up. Designed to
be fully non-interactive for automation/tests: pass ``interactive=False`` with
``preselect`` and ``launch_mode`` and no prompt is ever shown. When interactive
and a TTY is present we use ``questionary``; otherwise we fall back to a plain
numbered ``input()`` prompt.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import (
    KNOWN_PROVIDER_DEFAULTS,
    Config,
    ProviderConfig,
    default_config,
)
from .provider_health import ProviderHealth, check_all
from .providers.registry import known_provider_ids, known_providers

_VALID_LAUNCH_MODES = ("launch_now", "spawn_on_task")


def _tty_available() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (ValueError, OSError):  # pragma: no cover - closed streams
        return False


def _normalise_launch_mode(launch_mode: str | None) -> str:
    if launch_mode in _VALID_LAUNCH_MODES:
        return launch_mode
    return "launch_now"


def _ensure_known_providers(config: Config) -> None:
    """Make sure the three built-ins exist in the config (disabled if new)."""
    for pid, defaults in KNOWN_PROVIDER_DEFAULTS.items():
        if pid not in config.providers:
            fields = {
                k: v for k, v in defaults.items() if k in ProviderConfig.model_fields
            }
            config.providers[pid] = ProviderConfig(enabled=False, **fields)


def _default_selection(health: dict[str, ProviderHealth]) -> list[str]:
    """Pre-check providers whose command is installed."""
    return [pid for pid in known_provider_ids() if health.get(pid) and health[pid].installed]


def _ask_checkbox(health: dict[str, ProviderHealth], preselect: list[str] | None) -> list[str]:
    """Interactive multi-select via questionary, with a numbered-prompt fallback."""
    checked = set(preselect) if preselect is not None else set(_default_selection(health))
    ids = known_provider_ids()

    try:
        import questionary

        choices = []
        for pid in ids:
            h = health.get(pid)
            status = f"installed={'yes' if h and h.installed else 'no'} auth={h.auth_status if h else 'unknown'}"
            title = f"{h.display_name if h else pid} ({pid})  {status}"
            choices.append(questionary.Choice(title=title, value=pid, checked=pid in checked))
        answer = questionary.checkbox(
            "Which AI coding CLIs do you want Archon to use?", choices=choices
        ).ask()
        if answer is None:  # user cancelled
            return sorted(checked)
        return answer
    except Exception:
        return _numbered_multiselect(ids, health, checked)


def _numbered_multiselect(
    ids: list[str], health: dict[str, ProviderHealth], checked: set[str]
) -> list[str]:
    """Plain fallback: 'Enter comma-separated numbers to enable'."""
    print("Which AI coding CLIs do you want Archon to use?")
    for i, pid in enumerate(ids, start=1):
        h = health.get(pid)
        mark = "x" if pid in checked else " "
        installed = "installed" if h and h.installed else "missing"
        print(f"  [{mark}] {i}. {pid} ({installed})")
    raw = input("Enter numbers separated by commas (blank = keep defaults): ").strip()
    if not raw:
        return sorted(checked)
    picked: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if token.isdigit() and 1 <= int(token) <= len(ids):
            picked.append(ids[int(token) - 1])
    return picked


def _ask_launch_mode(launch_mode: str | None) -> str:
    default = _normalise_launch_mode(launch_mode)
    try:
        import questionary

        answer = questionary.select(
            "Startup behavior",
            choices=list(_VALID_LAUNCH_MODES),
            default=default,
        ).ask()
        return _normalise_launch_mode(answer)
    except Exception:
        return default


def run_provider_wizard(
    repo,
    existing_config: Config | None = None,
    *,
    interactive: bool = True,
    preselect: list[str] | None = None,
    launch_mode: str | None = None,
) -> Config:
    """Return a :class:`Config` reflecting the user's (or caller's) provider choices.

    Non-interactive mode (``interactive=False``) never prompts: it uses
    ``preselect`` (falling back to installed providers) and ``launch_mode``.
    """
    config = existing_config or default_config()
    _ensure_known_providers(config)

    health = check_all(known_providers())

    if interactive and _tty_available():
        selected = _ask_checkbox(health, preselect)
        mode = _ask_launch_mode(launch_mode)
    else:
        if preselect is not None:
            selected = list(preselect)
        else:
            selected = _default_selection(health)
        mode = _normalise_launch_mode(launch_mode)

    selected_set = set(selected)
    for pid, pconf in config.providers.items():
        pconf.enabled = pid in selected_set

    config.startup.provider_panes = mode
    return config
