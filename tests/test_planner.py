from __future__ import annotations

import subprocess

import pytest

from archon import db, dispatcher, planner, policy
from archon.config import default_config


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=repo,
        check=True,
    )
    return repo


@pytest.fixture()
def cfg():
    c = default_config()
    c.providers["claude"].enabled = True
    c.providers["codex"].enabled = True
    return c


def test_parse_plan_strips_json_fence():
    raw = """```json
{"title":"Docs","objective":"Update docs","repo":"/r","constraints":[],"acceptance_criteria":[],"tasks":[],"clarifying_question":"Which docs?","overall_risk":"low"}
```"""
    plan = planner.parse_plan(raw)
    assert plan.clarifying_question == "Which docs?"


def test_policy_rejects_execute_without_review(cfg, git_repo):
    plan = planner.PlanProposal(
        title="x",
        objective="x",
        repo=str(git_repo),
        tasks=[
            planner.PlannedTask(
                key="execute",
                title="Do x",
                phase="execute",
                tool="codex",
                model_tier="standard",
                prompt="x",
                risk="low",
            )
        ],
    )
    with pytest.raises(policy.PolicyError):
        policy.validate_plan(plan, cfg)


def test_heuristic_docs_plan_is_docs_only(cfg, git_repo):
    plan = planner.heuristic_plan("update the README docs", repo_path=git_repo, config=cfg)
    policy.validate_plan(plan, cfg)
    assert [t.phase for t in plan.tasks] == ["docs"]


def test_persist_plan_creates_tasks_and_edges(conn, cfg, git_repo):
    ctx = dispatcher.register_repo(conn, dispatcher.resolve_repo_context(git_repo, config=cfg))
    plan = planner.heuristic_plan("add a hello endpoint", repo_path=git_repo, config=cfg)
    policy.validate_plan(plan, cfg)
    job, tasks = planner.persist_plan(conn, cfg, ctx, plan)

    rows = db.list_job_tasks(conn, job.id)
    assert len(rows) == 3
    assert tasks["review"].id in db.dependents_of(conn, tasks["execute"].id)
    assert tasks["test"].id in db.dependents_of(conn, tasks["review"].id)
    execute = db.get_task(conn, tasks["execute"].id)
    assert execute["provider_id"] == cfg.routing.standard.tool
    assert execute["model"] == cfg.routing.standard.model
