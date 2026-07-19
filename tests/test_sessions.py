"""The cross-provider session model, adapters, and registry (the pivot).

Adapters are exercised against synthetic on-disk fixtures (not the real
~/.claude or ~/.copilot) so attention detection is proven deterministically.
"""

from __future__ import annotations

import json

from archon.sessions import (
    AgentSession,
    AgentState,
    ArchonSessionAdapter,
    ArchonDbAdapter,
    ClaudeAdapter,
    CopilotAdapter,
    CodexAdapter,
    SessionRegistry,
    parse_provider_suffix,
    summarize,
    usage_line,
)


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

def test_needs_attention_and_summary():
    sessions = [
        AgentSession("a", "claude", AgentState.WORKING),
        AgentSession("b", "copilot", AgentState.WAITING_FOR_APPROVAL),
        AgentSession("c", "codex", AgentState.FAILED),
        AgentSession("d", "claude", AgentState.COMPLETED),
    ]
    assert sessions[1].needs_attention and not sessions[0].needs_attention
    counts = summarize(sessions)
    assert counts["working"] == 1 and counts["need_you"] == 1
    assert counts["failed"] == 1 and counts["done"] == 1


def test_usage_line_formats_cost_credits_and_tokens():
    session = AgentSession(
        "u",
        "copilot",
        AgentState.COMPLETED,
        cost_usd=1.25,
        ai_credits=0.33,
        total_tokens=14_807,
    )
    assert usage_line(session) == "$1.25  0.33 cr  14.8k tok"


def test_parse_provider_suffix_accepts_trailing_provider_flags():
    routed = parse_provider_suffix("fix checkout --claude --codex")
    assert routed.prompt == "fix checkout"
    assert routed.providers == ("claude", "codex")


def test_parse_provider_suffix_ignores_middle_provider_words():
    routed = parse_provider_suffix("document the --codex flag behavior")
    assert routed.prompt == "document the --codex flag behavior"
    assert routed.providers == ()


# --------------------------------------------------------------------------- #
# Claude adapter
# --------------------------------------------------------------------------- #

def _claude_fixture(tmp_path, *, pid=4321, sid="sess-abc", cwd="/repos/contract-ai", name="contract-ai"):
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    (sdir / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "sessionId": sid, "cwd": cwd, "name": name}))
    pdir = tmp_path / "projects" / "-repos-contract-ai"
    pdir.mkdir(parents=True)
    transcript = pdir / f"{sid}.jsonl"
    transcript.write_text(json.dumps(
        {"type": "assistant", "message": {"role": "assistant", "content": "Editing the parser"}}) + "\n")
    return sdir, pdir, transcript


def test_claude_working_when_alive_and_fresh(tmp_path):
    sdir, _, transcript = _claude_fixture(tmp_path)
    import os
    os.utime(transcript, (1000.0, 1000.0))
    adapter = ClaudeAdapter(sessions_dir=sdir, projects_dir=tmp_path / "projects",
                            alive_fn=lambda pid: True, now_fn=lambda: 1005.0, idle_after=60)
    (s,) = adapter.discover()
    assert s.state is AgentState.WORKING
    assert s.repo == "contract-ai" and s.title == "contract-ai"
    assert s.summary == "Editing the parser"


def test_claude_idle_when_alive_but_stale(tmp_path):
    sdir, _, transcript = _claude_fixture(tmp_path)
    import os
    os.utime(transcript, (1000.0, 1000.0))
    adapter = ClaudeAdapter(sessions_dir=sdir, projects_dir=tmp_path / "projects",
                            alive_fn=lambda pid: True, now_fn=lambda: 9999.0, idle_after=60)
    (s,) = adapter.discover()
    assert s.state is AgentState.IDLE


def test_claude_disconnected_when_pid_dead(tmp_path):
    sdir, _, _ = _claude_fixture(tmp_path)
    adapter = ClaudeAdapter(sessions_dir=sdir, projects_dir=tmp_path / "projects",
                            alive_fn=lambda pid: False)
    (s,) = adapter.discover()
    assert s.state is AgentState.DISCONNECTED


def test_claude_starting_when_no_transcript(tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    (sdir / "1.json").write_text(json.dumps({"pid": 1, "sessionId": "new", "cwd": "/repos/x"}))
    adapter = ClaudeAdapter(sessions_dir=sdir, projects_dir=tmp_path / "projects",
                            alive_fn=lambda pid: True)
    (s,) = adapter.discover()
    assert s.state is AgentState.STARTING


# --------------------------------------------------------------------------- #
# Copilot adapter — approval detection from the real event log shape
# --------------------------------------------------------------------------- #

def _copilot_fixture(tmp_path, events, *, pid=777, cwd="/repos/coffee-app", name="Coffee App"):
    d = tmp_path / "state" / "uuid-1"
    d.mkdir(parents=True)
    (d / "workspace.yaml").write_text(f"id: uuid-1\ncwd: {cwd}\nname: {name}\n")
    (d / f"inuse.{pid}.lock").write_text(str(pid))
    (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return tmp_path / "state"


def test_copilot_waiting_on_unmatched_permission(tmp_path):
    state_dir = _copilot_fixture(tmp_path, [
        {"type": "session.start", "timestamp": "t0"},
        {"type": "assistant.turn_start", "timestamp": "t1"},
        {"type": "permission.requested", "timestamp": "t2"},
    ])
    adapter = CopilotAdapter(state_dir=state_dir, alive_fn=lambda pid: True)
    (s,) = adapter.discover()
    assert s.state is AgentState.WAITING_FOR_APPROVAL
    assert s.needs_attention and s.repo == "coffee-app" and s.title == "Coffee App"


def test_copilot_not_waiting_when_permission_completed(tmp_path):
    state_dir = _copilot_fixture(tmp_path, [
        {"type": "permission.requested", "timestamp": "t2"},
        {"type": "permission.completed", "timestamp": "t3"},
        {"type": "tool.execution_start", "timestamp": "t4"},
    ])
    adapter = CopilotAdapter(state_dir=state_dir, alive_fn=lambda pid: True)
    (s,) = adapter.discover()
    assert s.state is AgentState.WORKING


def test_copilot_idle_on_turn_end(tmp_path):
    state_dir = _copilot_fixture(tmp_path, [
        {"type": "assistant.turn_start", "timestamp": "t1"},
        {"type": "assistant.turn_end", "timestamp": "t2"},
    ])
    adapter = CopilotAdapter(state_dir=state_dir, alive_fn=lambda pid: True)
    (s,) = adapter.discover()
    assert s.state is AgentState.IDLE


def test_copilot_disconnected_when_lock_dead(tmp_path):
    state_dir = _copilot_fixture(tmp_path, [
        {"type": "tool.execution_start", "timestamp": "t1"},
    ])
    adapter = CopilotAdapter(state_dir=state_dir, alive_fn=lambda pid: False)
    (s,) = adapter.discover()
    assert s.state is AgentState.DISCONNECTED


def test_copilot_reads_shutdown_usage(tmp_path):
    state_dir = _copilot_fixture(tmp_path, [
        {"type": "assistant.turn_start", "timestamp": "t1"},
        {"type": "session.shutdown", "timestamp": "t2", "data": {
            "totalPremiumRequests": 0.33,
            "totalNanoAiu": 2242900000,
            "tokenDetails": {
                "input": {"tokenCount": 9},
                "cache_write": {"tokenCount": 17372},
                "output": {"tokenCount": 141},
            },
        }},
    ])
    adapter = CopilotAdapter(state_dir=state_dir, alive_fn=lambda pid: False)
    (s,) = adapter.discover()
    assert s.ai_credits == 0.33
    assert s.total_tokens == 17522


# --------------------------------------------------------------------------- #
# Codex adapter
# --------------------------------------------------------------------------- #

def test_codex_adapter_reads_native_jsonl_usage(tmp_path):
    root = tmp_path / "codex" / "sessions" / "2026" / "07" / "18"
    root.mkdir(parents=True)
    path = root / "rollout-test.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in [
        {"timestamp": "2026-07-19T02:00:00Z", "type": "session_meta",
         "payload": {"session_id": "native-1", "cwd": "/repos/archon"}},
        {"timestamp": "2026-07-19T02:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "fix cost dashboard"}},
        {"timestamp": "2026-07-19T02:00:02Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": {"total_tokens": 28204},
         }}},
        {"timestamp": "2026-07-19T02:00:03Z", "type": "event_msg",
         "payload": {"type": "task_complete", "last_agent_message": "done"}},
    ]) + "\n")
    adapter = CodexAdapter(sessions_dir=tmp_path / "codex" / "sessions")
    (s,) = adapter.discover()
    assert s.session_id == "codex:native-1"
    assert s.repo == "archon"
    assert s.title == "fix cost dashboard"
    assert s.state is AgentState.COMPLETED
    assert s.total_tokens == 28204


# --------------------------------------------------------------------------- #
# Registry — aggregation, sort, Archon DB overlay
# --------------------------------------------------------------------------- #

def test_archon_session_adapter_reads_runner_state(tmp_path):
    state = tmp_path / "sessions"
    state.mkdir()
    (state / "s1.json").write_text(json.dumps({
        "session_id": "s1",
        "provider": "codex",
        "pid": 123,
        "cwd": "/repos/app",
        "title": "fix bug",
        "summary": "running tests",
        "status": "running",
        "out_path": str(state / "s1.out.log"),
        "err_path": str(state / "s1.err.log"),
        "ai_credits": 0.5,
        "total_tokens": 1234,
        "updated_at": "2026-07-18T00:00:00+00:00",
    }))
    adapter = ArchonSessionAdapter(sessions_dir=state, alive_fn=lambda pid: True)
    (session,) = adapter.discover()
    assert session.session_id == "archon:s1"
    assert session.provider == "codex"
    assert session.state is AgentState.WORKING
    assert session.repo == "app"
    assert session.log_path and session.ai_credits == 0.5 and session.total_tokens == 1234

class _FakeAdapter:
    def __init__(self, sessions):
        self._s = sessions

    def discover(self):
        return self._s


def test_registry_sorts_attention_first_and_dedups():
    working = AgentSession("s1", "claude", AgentState.WORKING)
    needy = AgentSession("s2", "copilot", AgentState.WAITING_FOR_APPROVAL)
    done = AgentSession("s3", "codex", AgentState.COMPLETED)
    dup = AgentSession("s1", "claude", AgentState.WORKING)
    reg = SessionRegistry(adapters=[_FakeAdapter([working, done]), _FakeAdapter([needy, dup])])
    snap = reg.snapshot()
    assert len(snap) == 3  # dup collapsed
    assert snap[0].session_id == "s2"  # needs-you first


def test_registry_dedups_by_provider_session_id_preferring_archon_source():
    external = AgentSession("claude:native-1", "claude", AgentState.WORKING,
                            source="external", provider_session_id="native-1")
    archon = AgentSession("archon:run-1", "claude", AgentState.WAITING_FOR_APPROVAL,
                          source="archon", provider_session_id="native-1")
    reg = SessionRegistry(adapters=[_FakeAdapter([external, archon])])
    snap = reg.snapshot()
    assert len(snap) == 1
    assert snap[0].session_id == "archon:run-1"


def test_registry_overlays_external_attention_onto_archon_live_pane():
    archon = AgentSession(
        "archon:run-1",
        "copilot",
        AgentState.WORKING,
        cwd="/repo",
        source="archon",
        title="make a thing",
        summary="make a thing",
        updated_at="2026-07-19T01:36:54Z",
        zellij_session="sess",
        zellij_tab_id="2",
    )
    external = AgentSession(
        "copilot:native-1",
        "copilot",
        AgentState.WAITING_FOR_APPROVAL,
        cwd="/repo",
        source="external",
        title="Make Thing",
        summary="permission requested",
        updated_at="2026-07-19T01:37:31.784Z",
    )
    reg = SessionRegistry(adapters=[_FakeAdapter([external, archon])])
    snap = reg.snapshot()
    assert len(snap) == 1
    assert snap[0].session_id == "archon:run-1"
    assert snap[0].source == "archon"
    assert snap[0].zellij_tab_id == "2"
    assert snap[0].state is AgentState.WAITING_FOR_APPROVAL
    assert snap[0].summary == "permission requested"


def test_registry_overlays_external_state_onto_persistent_pty_session():
    archon = AgentSession(
        "archon:run-1",
        "codex",
        AgentState.WORKING,
        cwd="/repo",
        source="archon",
        title="make a thing",
        updated_at="2026-07-19T01:36:54Z",
        socket_path="/tmp/archon-test.sock",
    )
    external = AgentSession(
        "codex:native-1",
        "codex",
        AgentState.COMPLETED,
        cwd="/repo",
        source="external",
        summary="finished the task",
        updated_at="2026-07-19T01:37:31Z",
        total_tokens=1234,
    )

    (session,) = SessionRegistry(adapters=[_FakeAdapter([external, archon])]).snapshot()

    assert session.session_id == "archon:run-1"
    assert session.summary == "finished the task"
    assert session.total_tokens == 1234


def test_registry_flaky_adapter_does_not_break_view():
    class Boom:
        def discover(self):
            raise RuntimeError("boom")
    good = AgentSession("ok", "claude", AgentState.WORKING)
    reg = SessionRegistry(adapters=[Boom(), _FakeAdapter([good])])
    assert [s.session_id for s in reg.snapshot()] == ["ok"]


def test_archon_db_adapter_maps_runs(conn):
    import pathlib
    import tempfile
    from archon import dispatcher, planner
    from archon.config import default_config
    from archon.models import TaskRun

    cfg = default_config()
    cfg.providers["claude"].enabled = True
    cfg.providers["codex"].enabled = True
    repo = pathlib.Path(tempfile.mkdtemp()) / "demo"
    repo.mkdir()
    ctx = dispatcher.register_repo(conn, dispatcher.RepoContext(root=repo, name="demo", session="s"))
    plan = planner.heuristic_plan("do a thing", repo_path=ctx.root, config=cfg)
    job, tasks = planner.persist_plan(conn, cfg, ctx, plan)
    ex = tasks["execute"]
    from archon import db
    db.insert_task_run(conn, TaskRun(id="r1", task_id=ex.id, provider_id="codex", status="running",
                                     phase="execute", worktree_path=str(repo),
                                     provider_session_id="sess-x", provider_session_name="sess-x"))

    sessions = ArchonDbAdapter(conn=conn).discover()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.provider == "codex" and s.state is AgentState.WORKING and s.source == "archon"
