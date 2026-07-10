"""Control-center job, agent, and attention behavior."""

from __future__ import annotations

import json
import subprocess

import pytest

from archon import attention, db, dispatcher, hooks, jobs
from archon.config import default_config
from archon.models import Repo, Task, TaskRun


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "alpha"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=repo, check=True,
    )
    return repo


def test_enqueue_feature_creates_job_and_links_tasks(conn, git_repo):
    cfg = default_config()
    cfg.providers["claude"].enabled = True
    ctx = dispatcher.register_repo(conn, dispatcher.resolve_repo_context(git_repo, config=cfg))

    chain = dispatcher.enqueue_feature(
        conn,
        cfg,
        ctx,
        feature_name="forecast rebates",
        provider_id="claude",
        prompt_text="Add rebate tier forecasting.",
    )

    rows = db.list_jobs(conn)
    assert len(rows) == 1
    assert rows[0]["title"] == "forecast rebates"
    job_id = rows[0]["id"]
    assert chain["execute"].job_id == job_id
    if chain["plan"] is not None:
        assert chain["plan"].job_id == job_id
    assert {t["job_id"] for t in db.list_job_tasks(conn, job_id)} == {job_id}
    assert db.list_agents(conn, job_id=job_id)[0]["role"] == "lead"


def test_permission_hook_opens_attention_and_resolve_unblocks(conn):
    repo_id = db.upsert_repo(conn, Repo(name="demo", root_path="/tmp/demo", zellij_session="archon"))
    job = jobs.create_job(
        conn,
        repo_id=repo_id,
        title="demo job",
        objective="Demo objective",
        status="running",
    )
    task = Task(
        id="TASK-1",
        repo_id=repo_id,
        type="feature",
        name="demo",
        status="running",
        prompt="do it",
        job_id=job.id,
    )
    db.insert_task(conn, task)
    run = TaskRun(id="RUN-1", task_id=task.id, provider_id="claude", status="running")
    db.insert_task_run(conn, run)

    summary = hooks.handle_hook(
        "PermissionRequest",
        json.dumps({"message": "allow edit?"}),
        conn,
        env={"ARCHON_TASK_RUN_ID": run.id},
        paths=None,
    )

    assert summary["blocked"] is True
    item = db.list_attention_items(conn, status="open")[0]
    assert item["kind"] == "permission_request"
    assert item["job_id"] == job.id
    assert db.find_task_run(conn, run.id)["status"] == "blocked"
    assert db.get_job(conn, job.id)["status"] == "attention_required"

    attention.resolve_item(conn, item["id"], resolution="approved")

    assert db.get_attention_item(conn, item["id"])["status"] == "resolved"
    assert db.find_task_run(conn, run.id)["status"] == "running"
    assert db.get_job(conn, job.id)["status"] == "running"
