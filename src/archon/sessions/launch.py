"""Launch Archon-owned provider sessions for the unified agent view."""

from __future__ import annotations

import re
import json
import secrets
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..paths import resolve_paths
from ..util import atomic_write_json, utc_now

KNOWN_PROVIDERS = ("claude", "codex", "copilot")
_PROVIDER_FLAG_RE = re.compile(r"(?:^|\s)--(claude|codex|copilot)\s*$")


@dataclass(frozen=True)
class RoutedPrompt:
    prompt: str
    providers: tuple[str, ...]


def parse_provider_suffix(text: str) -> RoutedPrompt:
    """Return prompt text plus trailing provider flags.

    Provider flags are intentionally accepted only at the end of the input so a
    task can still mention strings like ``--codex`` in the middle.
    """
    remaining = text.strip()
    providers: list[str] = []
    while True:
        match = _PROVIDER_FLAG_RE.search(remaining)
        if match is None:
            break
        provider = match.group(1)
        if provider not in providers:
            providers.insert(0, provider)
        remaining = remaining[: match.start()].rstrip()
    return RoutedPrompt(prompt=remaining, providers=tuple(providers))


def launch_agent(prompt: str, provider: str, *, cwd: Path | None = None) -> str:
    """Start one Archon-owned provider process in a persistent background PTY.

    The PTY host owns the provider for the lifetime of the session. AgentView
    attaches to that host instead of recreating the provider command whenever
    the user opens the session.
    """
    if provider not in KNOWN_PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    paths = resolve_paths().ensure()
    cwd = (cwd or Path.cwd()).resolve()
    stamp = utc_now().replace("-", "").replace(":", "").replace("T", "").replace("Z", "")
    session_id = f"archon-{provider}-{stamp}-{secrets.token_hex(2)}"
    command = foreground_command(provider, prompt, session_id=session_id)
    if shutil.which(command[0]) is None:
        _write_initial_state(
            paths.sessions_dir / f"{session_id}.json",
            session_id=session_id,
            provider=provider,
            cwd=cwd,
            prompt=prompt,
            argv=command,
            status="failed",
            summary=f"{provider} command not found",
            exit_code=127,
        )
        raise FileNotFoundError(f"{provider} command not found")
    _write_initial_state(
        paths.sessions_dir / f"{session_id}.json",
        session_id=session_id,
        provider=provider,
        cwd=cwd,
        prompt=prompt,
        argv=command,
        status="created",
        summary=prompt,
        provider_session_id=(
            _uuid_for(session_id) if provider in {"claude", "copilot"} else None
        ),
    )
    host_argv = [
        sys.executable,
        "-m",
        "archon.sessions.session_host",
        "--session-id",
        session_id,
    ]
    try:
        subprocess.Popen(
            host_argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        _patch_initial_state(
            paths.sessions_dir / f"{session_id}.json",
            status="failed",
            summary=f"could not start session host: {exc}",
            exit_code=1,
        )
        raise
    return session_id


def foreground_command(provider: str, prompt: str, *, session_id: str | None = None) -> list[str]:
    if provider == "claude":
        # Claude accepts an initial prompt as the positional argument.
        return ["claude", "--session-id", _uuid_for(session_id), prompt]
    if provider == "codex":
        # Codex starts its interactive TUI when given a positional prompt.
        return ["codex", prompt]
    if provider == "copilot":
        # Copilot has a first-class interactive-with-initial-prompt flag.
        return ["copilot", "-i", prompt, "--session-id", _uuid_for(session_id)]
    raise ValueError(provider)


def _uuid_for(session_id: str | None) -> str:
    if not session_id:
        return str(uuid.uuid4())
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"archon:{session_id}"))


def _write_initial_state(
    path: Path,
    *,
    session_id: str,
    provider: str,
    cwd: Path,
    prompt: str,
    argv: list[str],
    status: str,
    summary: str,
    exit_code: int | None = None,
    zellij_session: str | None = None,
    zellij_pane_id: str | None = None,
    zellij_tab_id: str | None = None,
    provider_session_id: str | None = None,
) -> None:
    now = utc_now()
    out_path = path.with_suffix(".out.log")
    err_path = path.with_suffix(".err.log")
    state = {
        "session_id": session_id,
        "provider": provider,
        "pid": None,
        "provider_pid": None,
        "provider_session_id": provider_session_id,
        "cwd": str(cwd),
        "title": prompt[:60],
        "summary": summary,
        "prompt": prompt,
        "argv": argv,
        "out_path": str(out_path),
        "err_path": str(err_path),
        "status": status,
        "socket_path": None,
        "attached": False,
        "zellij_session": zellij_session,
        "zellij_pane_id": zellij_pane_id,
        "zellij_tab_id": zellij_tab_id,
        "started_at": now,
        "updated_at": now,
        "exit_code": exit_code,
        "cost_usd": None,
        "total_tokens": None,
    }
    atomic_write_json(path, state)


def _patch_initial_state(path: Path, **updates) -> None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    state.update(updates)
    state["updated_at"] = utc_now()
    atomic_write_json(path, state)
