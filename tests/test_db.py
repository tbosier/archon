from archon import db
from archon.models import Provider, Repo, Task, TaskRun


def test_schema_initializes_cleanly(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    )}
    for expected in ("repos", "providers", "jobs", "agents", "attention_items",
                     "tasks", "task_runs", "events",
                     "transcript_events", "transcript_fts", "file_touches",
                     "provider_panes"):
        assert expected in names


def test_repo_upsert_is_idempotent(conn):
    repo = Repo(name="demo", root_path="/tmp/demo", zellij_session="demo-archon")
    first = db.upsert_repo(conn, repo)
    second = db.upsert_repo(conn, repo)
    assert first == second
    assert conn.execute("SELECT COUNT(*) c FROM repos").fetchone()["c"] == 1


def test_provider_upsert_and_list(conn):
    db.upsert_provider(conn, Provider(id="claude", display_name="Claude", command="claude",
                                      enabled=True, installed=True, auth_status="ready"))
    providers = db.list_providers(conn)
    assert len(providers) == 1
    assert providers[0].enabled is True
    assert providers[0].auth_status == "ready"


def _seed_task_run(conn, run_status="running"):
    repo_id = db.upsert_repo(conn, Repo(name="d", root_path="/tmp/d", zellij_session="d"))
    task = Task(id="TASK-1", repo_id=repo_id, type="feature", name="thing",
                status="running", prompt="do it")
    db.insert_task(conn, task)
    run = TaskRun(id="RUN-1", task_id="TASK-1", provider_id="claude", status=run_status)
    db.insert_task_run(conn, run)
    return run


def test_task_run_status_update(conn):
    _seed_task_run(conn, "running")
    db.set_task_run_status(conn, "RUN-1", "blocked")
    assert db.find_task_run(conn, "RUN-1")["status"] == "blocked"


def test_task_run_telemetry_update(conn):
    _seed_task_run(conn)
    db.update_task_run(conn, "RUN-1", cost_usd=0.42, total_tokens=14000, context_used_pct=63.0)
    row = db.find_task_run(conn, "RUN-1")
    assert row["cost_usd"] == 0.42
    assert row["total_tokens"] == 14000


def test_list_task_runs_joins_task_name(conn):
    _seed_task_run(conn)
    rows = db.list_task_runs(conn)
    assert rows[0]["task_name"] == "thing"
