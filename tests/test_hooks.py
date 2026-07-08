"""Tests for the hook handler."""

from __future__ import annotations

import json

from archon import db, hooks
from archon.models import TaskRun


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


def _events(conn):
    return conn.execute("SELECT * FROM events ORDER BY id").fetchall()


def test_classify_severity_mapping():
    assert hooks.classify_severity("StopFailure", {}) == "error"
    assert hooks.classify_severity("PermissionRequest", {}) == "warn"
    assert hooks.classify_severity("Notification", {}) == "info"
    assert hooks.classify_severity("SessionEnd", {}) == "info"
    assert hooks.classify_severity("Stop", {}) == "info"
    assert hooks.classify_severity("Notification", {"level": "error"}) == "error"


def test_malformed_json_still_records_event():
    conn = db.connect_memory()
    summary = hooks.handle_hook("Notification", "{bad json", conn, env={}, paths=None)
    assert summary["hook"] == "Notification"
    rows = _events(conn)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "hook.Notification"


def test_permission_request_blocks_run():
    conn = db.connect_memory()
    run_id, _ = _seed_run(conn)
    summary = hooks.handle_hook(
        "PermissionRequest",
        json.dumps({"message": "allow edit?"}),
        conn,
        env={"ARCHON_TASK_RUN_ID": run_id},
        paths=None,
    )
    assert summary["blocked"] is True
    assert summary["task_run_id"] == run_id
    row = db.find_task_run(conn, run_id)
    assert row["status"] == "blocked"


def test_non_permission_hook_not_blocked():
    conn = db.connect_memory()
    run_id, _ = _seed_run(conn)
    summary = hooks.handle_hook(
        "Stop",
        json.dumps({"message": "done"}),
        conn,
        env={"ARCHON_TASK_RUN_ID": run_id},
        paths=None,
    )
    assert summary["blocked"] is False
    row = db.find_task_run(conn, run_id)
    assert row["status"] == "running"


def test_handle_hook_without_conn_does_not_crash():
    summary = hooks.handle_hook("Notification", "", None, env={}, paths=None)
    assert summary["hook"] == "Notification"
    assert summary["blocked"] is False


def test_writes_events_and_hooks_log(tmp_path):
    conn = db.connect_memory()

    class FakePaths:
        events_file = tmp_path / "events.jsonl"
        hooks_log = tmp_path / "hooks.log"

    hooks.handle_hook(
        "SessionEnd", json.dumps({"message": "bye"}), conn, env={}, paths=FakePaths
    )
    assert FakePaths.events_file.exists()
    assert FakePaths.hooks_log.exists()
    line = json.loads(FakePaths.events_file.read_text().strip())
    assert line["hook"] == "SessionEnd"
    assert line["severity"] == "info"
