"""Tests for the non-interactive provider-selection wizard."""

from __future__ import annotations

from pathlib import Path

from archon.provider_wizard import run_provider_wizard


def test_preselect_enables_exactly_those(monkeypatch):
    # Avoid touching the real filesystem for install detection.
    for mod in ("claude", "codex", "copilot"):
        monkeypatch.setattr(f"archon.providers.{mod}.shutil.which", lambda cmd: None)

    config = run_provider_wizard(
        Path("/fake/repo"),
        interactive=False,
        preselect=["claude", "codex"],
    )

    assert config.enabled_provider_ids() == ["claude", "codex"]
    assert config.is_configured() is True


def test_launch_mode_sets_provider_panes(monkeypatch):
    for mod in ("claude", "codex", "copilot"):
        monkeypatch.setattr(f"archon.providers.{mod}.shutil.which", lambda cmd: None)

    config = run_provider_wizard(
        Path("/fake/repo"),
        interactive=False,
        preselect=["claude"],
        launch_mode="spawn_on_task",
    )
    assert config.startup.provider_panes == "spawn_on_task"


def test_default_launch_mode_is_launch_now(monkeypatch):
    for mod in ("claude", "codex", "copilot"):
        monkeypatch.setattr(f"archon.providers.{mod}.shutil.which", lambda cmd: None)

    config = run_provider_wizard(Path("/fake/repo"), interactive=False, preselect=["claude"])
    assert config.startup.provider_panes == "launch_now"


def test_invalid_launch_mode_falls_back(monkeypatch):
    for mod in ("claude", "codex", "copilot"):
        monkeypatch.setattr(f"archon.providers.{mod}.shutil.which", lambda cmd: None)

    config = run_provider_wizard(
        Path("/fake/repo"),
        interactive=False,
        preselect=["claude"],
        launch_mode="bogus",
    )
    assert config.startup.provider_panes == "launch_now"


def test_no_preselect_uses_installed(monkeypatch):
    # claude + codex installed, copilot missing. All adapters share the real
    # `shutil` module, so patch a single command-aware `which`.
    installed = {"claude", "codex"}
    monkeypatch.setattr(
        "shutil.which", lambda cmd: f"/usr/bin/{cmd}" if cmd in installed else None
    )

    config = run_provider_wizard(Path("/fake/repo"), interactive=False)
    assert config.enabled_provider_ids() == ["claude", "codex"]


def test_empty_preselect_enables_none(monkeypatch):
    for mod in ("claude", "codex", "copilot"):
        monkeypatch.setattr(f"archon.providers.{mod}.shutil.which", lambda cmd: None)

    config = run_provider_wizard(Path("/fake/repo"), interactive=False, preselect=[])
    assert config.enabled_provider_ids() == []
    # Known providers still present in config, just disabled.
    assert set(config.providers.keys()) == {"claude", "codex", "copilot"}
