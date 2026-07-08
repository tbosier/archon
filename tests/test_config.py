from archon.config import Config, default_config, load_config, save_config
from archon.paths import resolve_paths


def test_default_config_has_known_providers():
    cfg = default_config()
    assert set(cfg.providers) == {"claude", "codex", "copilot"}
    assert all(not p.enabled for p in cfg.providers.values())
    assert not cfg.is_configured()


def test_save_and_load_roundtrip(isolated_home):
    cfg = default_config()
    cfg.providers["claude"].enabled = True
    cfg.providers["codex"].enabled = True
    save_config(cfg)

    loaded = load_config()
    assert loaded.is_configured()
    assert set(loaded.enabled_provider_ids()) == {"claude", "codex"}


def test_load_missing_returns_default(isolated_home):
    # No file written yet.
    assert not resolve_paths().config_file.exists()
    cfg = load_config()
    assert isinstance(cfg, Config)
    assert not cfg.is_configured()


def test_enabled_provider_ids_includes_custom():
    from archon.config import CustomProviderConfig

    cfg = default_config()
    cfg.custom.append(CustomProviderConfig(id="aider", display_name="Aider", command="aider", enabled=True))
    assert "aider" in cfg.enabled_provider_ids()
