"""Persistent PTY host for an Archon-owned provider session."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import signal
import socket
import struct
import sys
import termios
from pathlib import Path

from ..paths import resolve_paths
from ..util import atomic_write_json, utc_now
from .launch import KNOWN_PROVIDERS, foreground_command

_HISTORY_LIMIT = 8 * 1024 * 1024
_PACKET_SIZE = 32 * 1024
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args(argv)

    state_path = resolve_paths().ensure().sessions_dir / f"{args.session_id}.json"
    state = _read_state(state_path)
    if state is None:
        return 2
    provider = str(state.get("provider") or "")
    prompt = state.get("prompt")
    if provider not in KNOWN_PROVIDERS or not isinstance(prompt, str) or not prompt:
        _patch_state(state_path, status="failed", summary="invalid provider session state", exit_code=2)
        return 2
    command = foreground_command(provider, prompt, session_id=args.session_id)

    socket_path = _socket_path(args.session_id)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        socket_path.unlink(missing_ok=True)
        server.bind(str(socket_path))
        socket_path.chmod(0o600)
        server.listen(1)
    except OSError as exc:
        server.close()
        _patch_state(state_path, status="failed", summary=f"could not create attach socket: {exc}", exit_code=1)
        return 1

    pid, master_fd = pty.fork()
    if pid == 0:
        cwd = state.get("cwd")
        if cwd:
            os.chdir(str(cwd))
        # The executable is selected from KNOWN_PROVIDERS and argv is never shell-evaluated.
        os.execvp(command[0], command)  # nosemgrep

    _set_winsize(master_fd, struct.pack("!II", 24, 80))
    out_path = Path(state.get("out_path") or state_path.with_suffix(".out.log"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    history = bytearray()
    client: socket.socket | None = None
    _patch_state(
        state_path,
        pid=os.getpid(),
        provider_pid=pid,
        socket_path=str(socket_path),
        status="running",
        attached=False,
    )

    exit_code = 1
    try:
        with out_path.open("ab", buffering=0) as output:
            while True:
                watched: list[object] = [server, master_fd]
                if client is not None:
                    watched.append(client)
                readable, _, _ = select.select(watched, [], [], 0.5)

                if server in readable:
                    incoming, _ = server.accept()
                    if client is not None:
                        client.close()
                    client = incoming
                    _send_output(client, b"\x1b[2J\x1b[H")
                    for offset in range(0, len(history), _PACKET_SIZE):
                        _send_output(client, bytes(history[offset:offset + _PACKET_SIZE]))
                    _patch_state(state_path, attached=True)

                if master_fd in readable:
                    try:
                        data = os.read(master_fd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        waited, status = os.waitpid(pid, 0)
                        if waited == pid:
                            exit_code = os.waitstatus_to_exitcode(status)
                        break
                    output.write(data)
                    history.extend(data)
                    if len(history) > _HISTORY_LIMIT:
                        del history[:-_HISTORY_LIMIT]
                    if client is not None and not _send_output(client, data):
                        client.close()
                        client = None
                        _patch_state(state_path, attached=False)

                if client is not None and client in readable:
                    try:
                        packet = client.recv(65536)
                    except OSError:
                        packet = b""
                    if not packet:
                        client.close()
                        client = None
                        _patch_state(state_path, attached=False)
                    elif packet[:1] == b"I":
                        try:
                            os.write(master_fd, packet[1:])
                        except OSError:
                            break
                    elif packet[:1] == b"W" and len(packet) == 9:
                        _set_winsize(master_fd, packet[1:])
                        try:
                            os.kill(pid, signal.SIGWINCH)
                        except OSError:
                            pass

                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    exit_code = os.waitstatus_to_exitcode(status)
                    break
    finally:
        if client is not None:
            client.close()
        server.close()
        try:
            os.close(master_fd)
        except OSError:
            pass
        socket_path.unlink(missing_ok=True)

    summary = _tail_summary(out_path) or str(state.get("prompt") or "session ended")
    _patch_state(
        state_path,
        status="completed" if exit_code == 0 else "failed",
        exit_code=exit_code,
        summary=summary,
        attached=False,
        socket_path=None,
    )
    return exit_code


def _socket_path(session_id: str) -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    fallback = Path("/tmp") / f"archon-{os.getuid()}"
    root = Path(runtime) / "archon" if runtime else fallback
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        root = fallback
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return root / f"{digest}.sock"


def _set_winsize(master_fd: int, packed_size: bytes) -> None:
    try:
        rows, cols = struct.unpack("!II", packed_size)
        if rows and cols:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except (OSError, struct.error):
        pass


def _send_output(client: socket.socket, data: bytes) -> bool:
    for offset in range(0, len(data), _PACKET_SIZE):
        try:
            client.sendall(b"O" + data[offset:offset + _PACKET_SIZE])
        except OSError:
            return False
    return True


def _read_state(path: Path) -> dict | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def _patch_state(path: Path, **updates) -> None:
    state = _read_state(path) or {}
    state.update(updates)
    state["updated_at"] = utc_now()
    atomic_write_json(path, state)


def _tail_summary(path: Path, *, max_chars: int = 160) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        return None
    text = _ANSI_RE.sub("", text).replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][-max_chars:] if lines else None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
