"""Focused tests for the new unified AgentView surface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from textual.widgets import Input, ListView

from archon.agent_view import AgentView, ProviderPicker
from archon.sessions import ArchonSessionAdapter, SessionRegistry
from archon.sessions.control import tail_logs
from archon.sessions.launch import launch_agent
from archon.sessions.socket_protocol import FramedSocket


@pytest.fixture(autouse=True)
def _isolated(isolated_home):
    yield


async def test_agent_view_routes_trailing_provider_suffix(monkeypatch, isolated_home, conn, tmp_path):
    calls = []

    def fake_launch(prompt, provider, *, cwd=None):
        calls.append((prompt, provider, cwd))
        return f"s-{provider}"

    monkeypatch.setattr("archon.agent_view.launch_agent", fake_launch)
    registry = SessionRegistry(adapters=[ArchonSessionAdapter(
        sessions_dir=isolated_home.sessions_dir,
        alive_fn=lambda pid: True,
    )])
    app = AgentView(cwd=tmp_path, poll_interval=1000, conn=conn, registry=registry)
    async with app.run_test() as pilot:
        command = app.query_one("#command", Input)
        command.value = "fix auth --claude --codex"
        await pilot.press("enter")
        await pilot.pause()

    assert [(p, provider) for p, provider, _ in calls] == [
        ("fix auth", "claude"),
        ("fix auth", "codex"),
    ]


async def test_agent_view_prompts_for_provider_when_suffix_missing(monkeypatch, conn, tmp_path):
    calls = []
    monkeypatch.setattr(
        "archon.agent_view.launch_agent",
        lambda prompt, provider, *, cwd=None: calls.append((prompt, provider)) or "s1",
    )
    app = AgentView(cwd=tmp_path, poll_interval=1000, conn=conn)
    async with app.run_test() as pilot:
        command = app.query_one("#command", Input)
        command.value = "fix auth"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ProviderPicker)
        await pilot.press("x")
        await pilot.pause()

    assert calls == [("fix auth", "codex")]


async def test_agent_view_lists_archon_session_and_reads_logs(isolated_home, conn, tmp_path):
    out = isolated_home.sessions_dir / "s1.out.log"
    err = isolated_home.sessions_dir / "s1.err.log"
    out.write_text("hello from stdout\n", encoding="utf-8")
    err.write_text("", encoding="utf-8")
    (isolated_home.sessions_dir / "s1.json").write_text(json.dumps({
        "session_id": "s1",
        "provider": "codex",
        "pid": 999,
        "cwd": str(tmp_path),
        "title": "fix auth",
        "summary": "done",
        "status": "completed",
        "out_path": str(out),
        "err_path": str(err),
        "updated_at": "2026-07-18T00:00:00+00:00",
    }), encoding="utf-8")

    registry = SessionRegistry(adapters=[ArchonSessionAdapter(
        sessions_dir=isolated_home.sessions_dir,
        alive_fn=lambda pid: True,
    )])
    app = AgentView(cwd=tmp_path, poll_interval=1000, conn=conn, registry=registry)
    async with app.run_test() as pilot:
        await pilot.pause()
        view = app.query_one("#sessions", ListView)
        assert len(view.children) == 1
        assert app._selected().title == "fix auth"
        assert "hello from stdout" in tail_logs("archon:s1")


async def test_agent_view_does_not_rebuild_unchanged_session_list(isolated_home, conn, tmp_path, monkeypatch):
    (isolated_home.sessions_dir / "s1.json").write_text(json.dumps({
        "session_id": "s1",
        "provider": "codex",
        "pid": 999,
        "cwd": str(tmp_path),
        "title": "fix auth",
        "summary": "done",
        "status": "completed",
        "updated_at": "2026-07-18T00:00:00+00:00",
    }), encoding="utf-8")
    registry = SessionRegistry(adapters=[ArchonSessionAdapter(
        sessions_dir=isolated_home.sessions_dir,
        alive_fn=lambda pid: True,
    )])
    app = AgentView(cwd=tmp_path, poll_interval=1000, conn=conn, registry=registry)
    async with app.run_test() as pilot:
        await pilot.pause()
        view = app.query_one("#sessions", ListView)
        calls = {"clear": 0}
        original_clear = view.clear

        def counted_clear(*args, **kwargs):
            calls["clear"] += 1
            return original_clear(*args, **kwargs)

        monkeypatch.setattr(view, "clear", counted_clear)
        app.refresh_sessions()
        await pilot.pause()

    assert calls["clear"] == 0


async def test_up_from_command_focuses_session_list(isolated_home, conn, tmp_path):
    (isolated_home.sessions_dir / "s1.json").write_text(json.dumps({
        "session_id": "s1",
        "provider": "codex",
        "pid": 999,
        "cwd": str(tmp_path),
        "title": "fix auth",
        "summary": "done",
        "status": "completed",
        "updated_at": "2026-07-18T00:00:00+00:00",
    }), encoding="utf-8")
    registry = SessionRegistry(adapters=[ArchonSessionAdapter(
        sessions_dir=isolated_home.sessions_dir,
        alive_fn=lambda pid: True,
    )])
    app = AgentView(cwd=tmp_path, poll_interval=1000, conn=conn, registry=registry)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.focused, Input)
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is app.query_one("#sessions", ListView)


async def test_enter_on_selected_session_focuses_zellij_pane(isolated_home, conn, tmp_path, monkeypatch):
    (isolated_home.sessions_dir / "s1.json").write_text(json.dumps({
        "session_id": "s1",
        "provider": "codex",
        "pid": 999,
        "cwd": str(tmp_path),
        "title": "fix auth",
        "summary": "running",
        "status": "interactive",
        "zellij_session": "sess",
        "zellij_tab_id": "tab-2",
        "updated_at": "2026-07-18T00:00:00+00:00",
    }), encoding="utf-8")
    calls = []
    monkeypatch.setattr("archon.agent_view.focus_session", lambda sid: calls.append(sid) or True)
    registry = SessionRegistry(adapters=[ArchonSessionAdapter(
        sessions_dir=isolated_home.sessions_dir,
        alive_fn=lambda pid: True,
    )])
    app = AgentView(cwd=tmp_path, poll_interval=1000, conn=conn, registry=registry)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("up")
        await pilot.press("enter")
        await pilot.pause()

    assert calls == ["archon:s1"]


def test_launch_agent_records_failed_state_when_provider_missing(isolated_home, tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda command: None)
    with pytest.raises(FileNotFoundError):
        launch_agent("fix auth", "codex", cwd=tmp_path)

    states = list(isolated_home.sessions_dir.glob("*.json"))
    assert len(states) == 1
    data = json.loads(states[0].read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert data["summary"] == "codex command not found"


def test_launch_agent_inside_zellij_records_foreground_session(isolated_home, tmp_path, monkeypatch):
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "sess")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "pane-1")
    monkeypatch.setattr("shutil.which", lambda command: f"/usr/bin/{command}")
    launches = []

    class FakeHost:
        pid = 4321

    monkeypatch.setattr(
        "archon.sessions.launch.subprocess.Popen",
        lambda argv, **kwargs: launches.append((argv, kwargs)) or FakeHost(),
    )

    sid = launch_agent("fix auth", "codex", cwd=tmp_path)

    state = json.loads((isolated_home.sessions_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert state["status"] == "created"
    assert state["pid"] is None
    assert state["zellij_session"] is None
    assert state["zellij_tab_id"] is None
    assert state["argv"] == ["codex", "fix auth"]
    assert launches[0][0][-2:] == ["--session-id", sid]
    assert launches[0][1]["start_new_session"] is True


def test_foreground_argv_attaches_to_existing_socket_without_original_prompt(
    isolated_home, tmp_path
):
    from archon.sessions.control import foreground_argv

    socket_path = tmp_path / "session.sock"
    socket_path.touch()
    (isolated_home.sessions_dir / "s1.json").write_text(json.dumps({
        "session_id": "s1",
        "provider": "codex",
        "cwd": str(tmp_path),
        "prompt": "do not run this twice",
        "argv": ["codex", "do not run this twice"],
        "socket_path": str(socket_path),
    }), encoding="utf-8")

    result = foreground_argv("archon:s1")

    assert result is not None
    argv, cwd = result
    assert argv[-2:] == ["--socket", str(socket_path)]
    assert "do not run this twice" not in argv
    assert cwd == tmp_path


def test_session_host_survives_detach_and_accepts_reconnect(isolated_home, tmp_path, monkeypatch):
    sid = "persistent-test"
    state_path = isolated_home.sessions_dir / f"{sid}.json"
    out_path = isolated_home.sessions_dir / f"{sid}.out.log"
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        '#!/bin/sh\nprintf "ready:%s" "$1"\nIFS= read -r line\nprintf "\\ngot:%s\\n" "$line"\n',
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    state_path.write_text(json.dumps({
        "session_id": sid,
        "provider": "codex",
        "cwd": str(tmp_path),
        "prompt": "test persistence",
        "argv": ["codex", "test persistence"],
        "out_path": str(out_path),
        "err_path": str(isolated_home.sessions_dir / f"{sid}.err.log"),
        "status": "created",
    }), encoding="utf-8")
    host = subprocess.Popen([
        sys.executable,
        "-m",
        "archon.sessions.session_host",
        "--session-id",
        sid,
    ])
    first = None
    second = None
    try:
        for _ in range(200):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("socket_path") and Path(state["socket_path"]).exists():
                break
            time.sleep(0.01)
        else:
            pytest.fail("session host did not create its attach socket")

        first = FramedSocket()
        first.settimeout(2)
        first.connect(state["socket_path"])
        assert b"ready" in _recv_agent_output(first, expected=b"ready")
        first.close()
        first = None
        assert host.poll() is None

        second = FramedSocket()
        second.settimeout(2)
        second.connect(state["socket_path"])
        second.send(b"I", b"hello\n")
        assert b"got:hello" in _recv_agent_output(second, expected=b"got:hello")
        assert host.wait(timeout=2) == 0
        final = json.loads(state_path.read_text(encoding="utf-8"))
        assert final["status"] == "completed"
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        if host.poll() is None:
            host.terminate()
            host.wait(timeout=2)


def _recv_agent_output(client: FramedSocket, *, expected: bytes) -> bytes:
    output = bytearray()
    while expected not in output:
        packets, disconnected = client.receive()
        if disconnected:
            break
        for packet in packets:
            if packet[:1] == b"O":
                output.extend(packet[1:])
    return bytes(output)
