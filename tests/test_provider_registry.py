"""Tests for the provider registry, health checks, and login helpers."""

from __future__ import annotations

import pytest

from archon.config import CustomProviderConfig, default_config
from archon.provider_health import check_all, check_provider
from archon.provider_login import login_launch_for, login_pane_name
from archon.providers import registry
from archon.providers.custom import CustomProvider


def test_known_provider_ids():
    assert registry.known_provider_ids() == ["claude", "codex", "copilot"]


def test_known_providers_returns_three_instances():
    providers = registry.known_providers()
    assert [p.id for p in providers] == ["claude", "codex", "copilot"]


def test_get_provider_claude():
    provider = registry.get_provider("claude")
    assert provider.id == "claude"
    assert provider.command == "claude"
    assert provider.default_mode == "interactive"


def test_get_provider_codex_and_copilot():
    assert registry.get_provider("codex").default_mode == "exec"
    assert registry.get_provider("copilot").id == "copilot"


def test_get_provider_unknown_raises():
    with pytest.raises(KeyError):
        registry.get_provider("nope")


def test_get_provider_custom_from_config():
    config = default_config()
    config.custom.append(
        CustomProviderConfig(id="aider", display_name="Aider", command="aider")
    )
    provider = registry.get_provider("custom:aider", config)
    assert isinstance(provider, CustomProvider)
    assert provider.id == "custom:aider"
    assert provider.command == "aider"
    # Bare id should also resolve when present in config.
    assert registry.get_provider("aider", config).command == "aider"


def test_install_detection_uses_shutil_which(monkeypatch):
    provider = registry.get_provider("claude")

    monkeypatch.setattr("archon.providers.claude.shutil.which", lambda cmd: "/usr/bin/claude")
    assert provider.detect_installed() is True

    monkeypatch.setattr("archon.providers.claude.shutil.which", lambda cmd: None)
    assert provider.detect_installed() is False


def test_detect_auth_missing_when_not_installed(monkeypatch):
    provider = registry.get_provider("codex")
    monkeypatch.setattr("archon.providers.codex.shutil.which", lambda cmd: None)
    assert provider.detect_auth() == "missing"


def test_detect_auth_unknown_when_installed(monkeypatch):
    provider = registry.get_provider("codex")
    monkeypatch.setattr("archon.providers.codex.shutil.which", lambda cmd: "/usr/bin/codex")
    assert provider.detect_auth() == "unknown"


def test_check_provider_missing(monkeypatch):
    monkeypatch.setattr("archon.providers.claude.shutil.which", lambda cmd: None)
    health = check_provider(registry.get_provider("claude"))
    assert health.installed is False
    assert health.auth_status == "missing"
    assert health.display_name == "Claude Code CLI"


def test_check_all_returns_all_three(monkeypatch):
    for mod in ("claude", "codex", "copilot"):
        monkeypatch.setattr(f"archon.providers.{mod}.shutil.which", lambda cmd: None)
    health = check_all()
    assert set(health.keys()) == {"claude", "codex", "copilot"}
    assert all(h.auth_status == "missing" for h in health.values())


def test_login_pane_name():
    assert login_pane_name("codex") == "codex-login"
    assert login_pane_name("copilot") == "copilot-login"


def test_login_launch_for_codex():
    launch = login_launch_for("codex")
    assert launch.argv == ["codex", "login"]
    assert launch.pane_name == "codex-login"


def test_login_launch_for_claude():
    launch = login_launch_for("claude")
    assert launch.argv == ["claude"]
    assert launch.pane_name == "claude-login"
