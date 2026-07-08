"""Tests for per-phase model tiering (``archon.phases``)."""

from __future__ import annotations

from archon.config import ModelTier, default_config
from archon.phases import model_args, resolve_tier


# -- model_args ------------------------------------------------------------


def test_model_args_claude():
    assert model_args("claude", ModelTier(model="claude-opus-4-8")) == [
        "--model",
        "claude-opus-4-8",
    ]


def test_model_args_codex_includes_reasoning():
    args = model_args("codex", ModelTier(model="gpt-5.5", reasoning="high"))
    assert "--model" in args
    assert "gpt-5.5" in args
    assert "model_reasoning_effort=high" in args


def test_model_args_codex_without_reasoning():
    args = model_args("codex", ModelTier(model="gpt-5.5"))
    assert args == ["--model", "gpt-5.5"]


def test_model_args_empty_tier_is_empty():
    assert model_args("codex", ModelTier()) == []
    assert model_args("claude", ModelTier()) == []


def test_model_args_copilot_and_unknown():
    assert model_args("copilot", ModelTier(model="gpt-x")) == ["--model", "gpt-x"]
    assert model_args("mystery", ModelTier(model="gpt-x")) == ["--model", "gpt-x"]


def test_model_args_appends_extra_args():
    tier = ModelTier(model="claude-opus-4-8", extra_args=["--foo", "bar"])
    assert model_args("claude", tier) == [
        "--model",
        "claude-opus-4-8",
        "--foo",
        "bar",
    ]


# -- resolve_tier ----------------------------------------------------------


def test_resolve_tier_claude_phases():
    config = default_config()
    assert resolve_tier(config, "claude", "plan").model == "claude-opus-4-8"
    assert resolve_tier(config, "claude", "execute").model == "claude-sonnet-5"
    # review -> plan tier; test -> execute tier.
    assert resolve_tier(config, "claude", "review").model == "claude-opus-4-8"
    assert resolve_tier(config, "claude", "test").model == "claude-sonnet-5"


def test_resolve_tier_codex_reasoning():
    config = default_config()
    assert resolve_tier(config, "codex", "plan").reasoning == "high"
    assert resolve_tier(config, "codex", "execute").reasoning == "medium"


def test_resolve_tier_missing_returns_empty():
    assert resolve_tier(None, "claude", "plan").model is None
    assert resolve_tier(default_config(), "nope", "plan").model is None
