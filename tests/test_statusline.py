"""Tests for the statusline telemetry handler."""

from __future__ import annotations

import json

from archon import db, statusline
from archon.models import Task, TaskRun


def _seed_run(conn, run_id="RUN-20260707-001-claude", task_id="TASK-20260707-001"):
    conn.execute(
        "INSERT INTO tasks (id, repo_id, type, name, status, priority, prompt, "
        "provider_policy, created_at, updated_at) "
        "VALUES (?, 1, 'feature', 'demo', 'running', 0, 'p', 'single', 't', 't')",
        (task_id,),
    )
    db.insert_task_run(
        conn, TaskRun(id=run_id, task_id=task_id, provider_id="claude", status="running")
    )
    conn.commit()
    return run_id, task_id


def test_empty_string_returns_string_no_crash():
    out = statusline.handle_statusline("", None, env={})
    assert isinstance(out, str)
    assert out


def test_malformed_json_returns_string():
    out = statusline.handle_statusline("{not json", None, env={})
    assert isinstance(out, str)


def test_missing_and_null_fields_tolerated():
    payload = json.dumps({"cost": None, "context": None, "rate_limits": None})
    out = statusline.handle_statusline(payload, None, env={"ARCHON_PROVIDER_ID": "codex"})
    assert isinstance(out, str)
    assert "codex" in out


def test_nested_claude_payload_formats_line():
    payload = json.dumps(
        {
            "cost": {"total_cost_usd": 0.21, "total_tokens": 1234},
            "context": {"used_pct": 42},
            "rate_limits": {"five_hour_pct": 12},
        }
    )
    out = statusline.handle_statusline(payload, None, env={"ARCHON_PROVIDER_ID": "claude"})
    assert "claude" in out
    assert "$0.21" in out
    assert "42%" in out
    assert "5h 12%" in out


def test_updates_task_run_when_run_id_matches():
    conn = db.connect_memory()
    run_id, task_id = _seed_run(conn)
    payload = json.dumps(
        {
            "session_id": "sess-abc",
            "transcript_path": "/tmp/t.jsonl",
            "cost": {"total_cost_usd": 1.5, "total_tokens": 999},
            "context": {"used_pct": 60},
        }
    )
    statusline.handle_statusline(payload, conn, env={"ARCHON_TASK_RUN_ID": run_id})

    row = db.find_task_run(conn, run_id)
    assert abs(row["cost_usd"] - 1.5) < 1e-9
    assert row["total_tokens"] == 999
    assert abs(row["context_used_pct"] - 60) < 1e-9
    assert row["provider_session_id"] == "sess-abc"
    assert row["transcript_path"] == "/tmp/t.jsonl"
    assert row["last_heartbeat_at"]


def test_flat_keys_accepted():
    fields = statusline.extract_telemetry(
        {"cost_usd": 0.5, "total_tokens": 10, "context_used_pct": 5}
    )
    assert fields["cost_usd"] == 0.5
    assert fields["total_tokens"] == 10
    assert fields["context_used_pct"] == 5


def test_infer_by_session_id():
    conn = db.connect_memory()
    run_id, _ = _seed_run(conn)
    db.update_task_run(conn, run_id, provider_session_id="sess-xyz")
    inferred = statusline.infer_task_run_id(conn, {"session_id": "sess-xyz"}, {})
    assert inferred == run_id
