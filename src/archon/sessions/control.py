"""Small control helpers for Archon-owned agent sessions."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

from ..paths import resolve_paths
from ..util import atomic_write_json
from .base import pid_alive


def state_path(session_id: str) -> Path | None:
    native = _native_id(session_id)
    if not native:
        return None
    path = resolve_paths().sessions_dir / f"{native}.json"
    return path if path.exists() else None


def read_state(session_id: str) -> dict | None:
    path = state_path(session_id)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def tail_logs(session_id: str, *, max_chars: int = 4000) -> str:
    data = read_state(session_id)
    if data is None:
        return ""
    chunks: list[str] = []
    for label, key in (("stdout", "out_path"), ("stderr", "err_path")):
        value = data.get(key)
        if not value:
            continue
        text = _tail(Path(value), max_chars=max_chars // 2)
        if text:
            chunks.append(f"[{label}]\n{text}")
    return "\n\n".join(chunks)


def stop_session(session_id: str) -> bool:
    data = read_state(session_id)
    if data is None:
        return False
    stopped = False
    # The host owns cleanup and final state. Stop the provider first and let the
    # host observe its exit instead of terminating both processes at once.
    for key in ("provider_pid", "pid"):
        pid = data.get(key)
        if not pid_alive(pid):
            continue
        try:
            os.kill(int(pid), signal.SIGTERM)
            stopped = True
            break
        except OSError:
            pass
    if stopped:
        _patch_state(session_id, summary="stopping by user")
    return stopped


def focus_session(session_id: str) -> bool:
    data = read_state(session_id)
    if data is None:
        return False
    zellij_session = data.get("zellij_session")
    tab_id = data.get("zellij_tab_id")
    pane_id = data.get("zellij_pane_id")
    if zellij_session and tab_id:
        from ..zellij import Zellij

        return Zellij().focus_tab(str(zellij_session), str(tab_id))
    if not zellij_session or not pane_id:
        return False
    from ..zellij import Zellij

    return Zellij().focus_pane(str(zellij_session), str(pane_id))


def foreground_argv(session_id: str) -> tuple[list[str], Path] | None:
    data = read_state(session_id)
    if data is None:
        return None
    socket_path = data.get("socket_path")
    cwd = data.get("cwd")
    if not socket_path or not Path(str(socket_path)).exists():
        return None
    wrapped = [
        sys.executable,
        "-m",
        "archon.sessions.interactive_pane",
        "--socket",
        str(socket_path),
    ]
    return wrapped, Path(cwd) if cwd else Path.cwd()


def forget_session(session_id: str) -> bool:
    data = read_state(session_id)
    path = state_path(session_id)
    if data is None or path is None:
        return False
    if data.get("status") == "running" and pid_alive(data.get("pid")):
        return False
    for key in ("out_path", "err_path"):
        value = data.get(key)
        if value:
            try:
                Path(value).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    try:
        path.unlink()
        return True
    except OSError:
        return False


def rerun_session(session_id: str) -> tuple[str, str] | None:
    from .launch import launch_agent

    data = read_state(session_id)
    if data is None:
        return None
    prompt = data.get("prompt")
    provider = data.get("provider")
    cwd = data.get("cwd")
    if not prompt or not provider:
        return None
    new_id = launch_agent(str(prompt), str(provider), cwd=Path(cwd) if cwd else None)
    return new_id, str(provider)


def _patch_state(session_id: str, **updates) -> None:
    path = state_path(session_id)
    data = read_state(session_id)
    if path is None or data is None:
        return
    from ..util import utc_now

    data.update(updates)
    data["updated_at"] = utc_now()
    atomic_write_json(path, data)


def _native_id(session_id: str) -> str | None:
    if session_id.startswith("archon:"):
        return session_id.split(":", 1)[1]
    if session_id.startswith("archon-"):
        return session_id
    return None


def _tail(path: Path, *, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:].strip()
