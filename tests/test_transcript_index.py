"""Tests for the transcript indexer + search."""

from __future__ import annotations

import json

from archon import db, transcript_index as ti


SAMPLE_LINES = [
    {"role": "user", "text": "please fix the auth flow"},
    {"role": "assistant", "text": "reading files now"},
    {"tool": "Read", "input": {"file_path": "app/auth.py"}, "text": "opened auth.py"},
    {"tool": "Edit", "input": {"file_path": "app/auth.py"}, "text": "patched login bug"},
    {"tool": "Bash", "input": {"command": "pytest tests/test_auth.py"}},
]


def _jsonl(objs):
    return "\n".join(json.dumps(o) for o in objs)


def test_index_and_search_finds_keyword():
    conn = db.connect_memory()
    count = ti.index_jsonl_text(
        conn,
        _jsonl(SAMPLE_LINES),
        task_id="TASK-1",
        task_run_id="RUN-1",
        provider_id="claude",
    )
    assert count == len(SAMPLE_LINES)

    hits = ti.search(conn, "login")
    assert hits
    assert any("login" in (h["excerpt"] or "") for h in hits)
    assert hits[0]["task_id"] == "TASK-1"
    assert hits[0]["task_run_id"] == "RUN-1"


def test_touched_finds_edited_file():
    conn = db.connect_memory()
    ti.index_jsonl_text(
        conn, _jsonl(SAMPLE_LINES), task_id="TASK-1", task_run_id="RUN-1", provider_id="claude"
    )
    rows = ti.touched(conn, "auth.py")
    assert rows
    actions = {r["action"] for r in rows}
    assert "read" in actions
    assert "edit" in actions
    assert all(r["file_path"] == "app/auth.py" for r in rows)


def test_malformed_lines_skipped():
    conn = db.connect_memory()
    text = "\n".join(
        [
            json.dumps({"role": "user", "text": "hello world"}),
            "{ this is not valid json",
            "",
            "12345",  # valid json but not an object -> skipped
            json.dumps({"role": "assistant", "text": "goodbye world"}),
        ]
    )
    count = ti.index_jsonl_text(conn, text, task_id="T", task_run_id="R", provider_id="p")
    assert count == 2
    assert ti.search(conn, "hello")


def test_search_since_filter_best_effort():
    conn = db.connect_memory()
    ti.index_jsonl_text(
        conn, _jsonl(SAMPLE_LINES), task_id="TASK-1", task_run_id="RUN-1", provider_id="claude"
    )
    # A wide window includes everything.
    assert ti.search(conn, "auth", since="7d")
    # Age the rows well into the past, then a tight window excludes them.
    conn.execute("UPDATE transcript_events SET created_at = '2000-01-01T00:00:00Z'")
    conn.commit()
    assert ti.search(conn, "auth", since="1h") == []
    # ...but no filter still finds them.
    assert ti.search(conn, "auth")


def test_search_empty_query_returns_empty():
    conn = db.connect_memory()
    assert ti.search(conn, "") == []


def test_index_transcript_file(tmp_path):
    conn = db.connect_memory()
    p = tmp_path / "transcript.jsonl"
    p.write_text(_jsonl(SAMPLE_LINES), encoding="utf-8")
    count = ti.index_transcript_file(conn, p, task_id="T", task_run_id="R", provider_id="claude")
    assert count == len(SAMPLE_LINES)
    row = conn.execute(
        "SELECT transcript_path FROM transcript_events LIMIT 1"
    ).fetchone()
    assert row["transcript_path"] == str(p)


def test_missing_file_returns_zero():
    conn = db.connect_memory()
    assert ti.index_transcript_file(conn, "/no/such/file.jsonl") == 0


def test_punctuation_query_does_not_crash():
    conn = db.connect_memory()
    ti.index_jsonl_text(conn, _jsonl(SAMPLE_LINES), task_id="T", task_run_id="R", provider_id="p")
    # A query with FTS-special characters must not raise.
    assert isinstance(ti.search(conn, 'app/auth.py "quoted"'), list)
