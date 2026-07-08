"""Tests for provider launch builders and event parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from archon.config import default_config
from archon.models import TaskRun
from archon.providers.registry import get_provider, known_providers


def make_run(**overrides) -> TaskRun:
    defaults = dict(
        id="RUN-20260707-001-claude",
        task_id="TASK-20260707-001",
        provider_id="claude",
        worktree_path="/fake/worktree",
        zellij_session="demo-archon",
        zellij_pane_name="claude-feature-x",
    )
    defaults.update(overrides)
    return TaskRun(**defaults)


# -- Claude ----------------------------------------------------------------


def test_claude_worker_launch():
    provider = get_provider("claude")
    run = make_run()
    launch = provider.worker_launch(run, "do the thing")
    assert launch.argv == ["claude", "-n", "claude-feature-x"]
    assert launch.expects_prompt_paste is True
    assert launch.captures_jsonl is False
    assert launch.mode == "interactive"
    assert launch.cwd == Path("/fake/worktree")
    assert launch.prompt == "do the thing"


def test_claude_pane_fallback_when_unnamed():
    provider = get_provider("claude")
    run = make_run(zellij_pane_name=None)
    launch = provider.worker_launch(run, "x")
    assert launch.argv[:2] == ["claude", "-n"]
    assert launch.argv[2]  # a non-empty fallback pane name


# -- Codex -----------------------------------------------------------------


def test_codex_review_is_read_only():
    provider = get_provider("codex")
    run = make_run(provider_id="codex", zellij_pane_name="codex-review")
    launch = provider.worker_launch(run, "review this", purpose="review")
    assert launch.argv == [
        "codex", "exec", "--json", "--sandbox", "read-only", "review this",
    ]
    assert launch.captures_jsonl is True
    assert launch.expects_prompt_paste is False
    assert launch.mode == "exec"


def test_codex_feature_is_workspace_write():
    provider = get_provider("codex")
    run = make_run(provider_id="codex")
    launch = provider.worker_launch(run, "build it", purpose="feature")
    assert "--sandbox" in launch.argv
    assert launch.argv[launch.argv.index("--sandbox") + 1] == "workspace-write"


def test_codex_worker_default_is_workspace_write():
    provider = get_provider("codex")
    run = make_run(provider_id="codex")
    launch = provider.worker_launch(run, "work")
    assert "workspace-write" in launch.argv


def test_codex_parse_event_line_valid():
    provider = get_provider("codex")
    event = provider.parse_event_line('{"type": "message", "message": "hi"}')
    assert event is not None
    assert event.type == "message"
    assert event.provider_id == "codex"
    assert event.message == "hi"


def test_codex_parse_event_line_malformed_returns_none():
    provider = get_provider("codex")
    assert provider.parse_event_line("not json at all {{{") is None
    assert provider.parse_event_line("") is None
    assert provider.parse_event_line("   ") is None
    # A JSON scalar (not an object) is also tolerated.
    assert provider.parse_event_line("123") is None


# -- Copilot ---------------------------------------------------------------


def test_copilot_prompt_mode():
    provider = get_provider("copilot")
    run = make_run(provider_id="copilot", zellij_pane_name="copilot-x")
    launch = provider.worker_launch(run, "please help")
    assert launch.argv == ["copilot", "-p", "please help"]
    assert launch.expects_prompt_paste is False
    assert launch.mode == "prompt"


def test_copilot_interactive_without_prompt():
    provider = get_provider("copilot")
    run = make_run(provider_id="copilot")
    launch = provider.worker_launch(run, "")
    assert launch.argv == ["copilot"]
    assert launch.mode == "interactive"


# -- Env injection ---------------------------------------------------------


@pytest.mark.parametrize("provider_id", ["claude", "codex", "copilot"])
def test_launch_env_has_archon_ids(provider_id):
    provider = get_provider(provider_id)
    run = make_run(provider_id=provider_id)
    launch = provider.worker_launch(run, "prompt text")
    assert launch.env["ARCHON_TASK_ID"] == "TASK-20260707-001"
    assert launch.env["ARCHON_TASK_RUN_ID"] == "RUN-20260707-001-claude"
    assert launch.env["ARCHON_PROVIDER_ID"] == provider_id


# -- Custom ----------------------------------------------------------------


def test_custom_paste_launch():
    from archon.config import CustomProviderConfig
    from archon.providers.custom import CustomProvider

    provider = CustomProvider(
        CustomProviderConfig(id="aider", display_name="Aider", command="aider")
    )
    run = make_run(provider_id="custom:aider")
    launch = provider.worker_launch(run, "hello")
    assert launch.argv == ["aider"]
    assert launch.expects_prompt_paste is True


def test_custom_arg_delivery():
    from archon.config import CustomProviderConfig
    from archon.providers.custom import CustomProvider

    provider = CustomProvider(
        CustomProviderConfig(
            id="aider", display_name="Aider", command="aider", prompt_delivery="arg"
        )
    )
    run = make_run(provider_id="custom:aider")
    launch = provider.worker_launch(run, "hello")
    assert launch.argv == ["aider", "hello"]
    assert launch.expects_prompt_paste is False


# -- Per-phase model tiering -----------------------------------------------


def test_claude_plan_phase_uses_strong_model():
    provider = get_provider("claude", default_config())
    run = make_run(phase="plan")
    launch = provider.worker_launch(run, "plan it")
    assert launch.argv == [
        "claude", "-n", "claude-feature-x", "--model", "claude-opus-4-8",
    ]
    assert run.model == "claude-opus-4-8"


def test_claude_execute_phase_uses_cheaper_model():
    provider = get_provider("claude", default_config())
    run = make_run(phase="execute")
    launch = provider.worker_launch(run, "do it")
    assert "--model" in launch.argv
    assert launch.argv[launch.argv.index("--model") + 1] == "claude-sonnet-5"
    assert run.model == "claude-sonnet-5"


def test_codex_review_phase_uses_plan_tier_and_read_only():
    provider = get_provider("codex", default_config())
    run = make_run(
        provider_id="codex", zellij_pane_name="codex-review", phase="review"
    )
    launch = provider.worker_launch(run, "review this", purpose="review")
    # Plan tier: strong reasoning.
    assert "gpt-5.5" in launch.argv
    assert "model_reasoning_effort=high" in launch.argv
    # Sandbox is still read-only for a review purpose.
    assert launch.argv[launch.argv.index("--sandbox") + 1] == "read-only"
    # Prompt remains the final argv element.
    assert launch.argv[-1] == "review this"
    assert run.model == "gpt-5.5"


def test_provider_without_models_keeps_original_argv():
    # Instances from known_providers() have .models = None.
    provider = next(p for p in known_providers() if p.id == "claude")
    assert provider.models is None
    run = make_run(phase="plan")
    launch = provider.worker_launch(run, "do the thing")
    assert launch.argv == ["claude", "-n", "claude-feature-x"]
    assert run.model is None
