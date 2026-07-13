"""Planner intake: user intent -> validated task graph proposal."""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import db, jobs, queue
from .dispatcher import RepoContext
from .models import Task
from .util import sanitize_slug


class PlannedTask(BaseModel):
    key: str
    title: str
    phase: Literal["plan", "execute", "review", "test", "docs"]
    tool: str
    model_tier: Literal["cheap", "standard", "high"]
    prompt: str
    depends_on: list[str] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"]
    est_cost_usd: float | None = None


class PlanProposal(BaseModel):
    title: str
    objective: str
    repo: str
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    tasks: list[PlannedTask] = Field(default_factory=list)
    clarifying_question: str | None = None
    overall_risk: Literal["low", "medium", "high"] = "low"


def plan_with_llm(
    message: str,
    *,
    repo_path: Path,
    config,
    recent_job_titles: list[str] | None = None,
    planner_command: list[str] | None = None,
) -> PlanProposal:
    """Call the planner LLM and validate the JSON response, retrying once."""
    command = planner_command or ["claude", "-p", "--output-format", "json"]
    prompt = planner_prompt(
        message,
        repo_path=repo_path,
        config=config,
        recent_job_titles=recent_job_titles or [],
    )
    raw = _run_planner(command, prompt, cwd=repo_path)
    try:
        return parse_plan(raw)
    except Exception as exc:
        retry_prompt = f"{prompt}\n\nYour previous response failed validation:\n{exc}\nReturn corrected JSON only."
        return parse_plan(_run_planner(command, retry_prompt, cwd=repo_path))


def heuristic_plan(message: str, *, repo_path: Path, config) -> PlanProposal:
    """Small deterministic planner for dry-runs and tests."""
    title = _title_from_message(message)
    repo = repo_path.name
    docs = _looks_docs_only(message)
    if docs:
        route = config.routing.for_tier("cheap")
        return PlanProposal(
            title=title,
            objective=message,
            repo=str(repo_path),
            constraints=["Do not push, merge, or open a PR."],
            acceptance_criteria=["Documentation reflects the requested change."],
            tasks=[
                PlannedTask(
                    key="docs",
                    title=f"Update docs for {title}",
                    phase="docs",
                    tool=route.tool,
                    model_tier="cheap",
                    prompt=f"Update documentation in {repo}: {message}",
                    risk="low",
                    est_cost_usd=0.10,
                )
            ],
            overall_risk="low",
        )

    execute_route = config.routing.for_tier("standard")
    review_route = config.routing.for_tier("high")
    test_route = config.routing.for_tier("cheap")
    return PlanProposal(
        title=title,
        objective=message,
        repo=str(repo_path),
        constraints=["Work in an Archon-managed branch/worktree.", "Do not push, merge, or open a PR."],
        acceptance_criteria=["Implementation is complete.", "Review and verification tasks complete."],
        tasks=[
            PlannedTask(
                key="execute",
                title=f"Implement {title}",
                phase="execute",
                tool=execute_route.tool,
                model_tier="standard",
                prompt=f"Implement this request in {repo}: {message}",
                risk="medium",
                est_cost_usd=0.75,
            ),
            PlannedTask(
                key="review",
                title=f"Review {title}",
                phase="review",
                tool=review_route.tool,
                model_tier="high",
                prompt=f"Review the implementation for: {message}",
                depends_on=["execute"],
                risk="medium",
                est_cost_usd=0.40,
            ),
            PlannedTask(
                key="test",
                title=f"Verify {title}",
                phase="test",
                tool=test_route.tool,
                model_tier="cheap",
                prompt=f"Run focused verification for: {message}",
                depends_on=["review"],
                risk="low",
                est_cost_usd=0.20,
            ),
        ],
        overall_risk="medium",
    )


def parse_plan(raw: str) -> PlanProposal:
    return PlanProposal.model_validate_json(_strip_fences(raw))


def persist_plan(conn: sqlite3.Connection, config, ctx: RepoContext, plan: PlanProposal) -> tuple[object, dict[str, Task]]:
    """Persist an approved plan as a job, tasks, and dependency edges."""
    first_provider = _route(config, plan.tasks[0]).tool if plan.tasks else None
    job = jobs.create_job(
        conn,
        repo_id=ctx.repo_id or 0,
        title=plan.title,
        objective=plan.objective,
        constraints=plan.constraints,
        acceptance_criteria=plan.acceptance_criteria,
        status="running",
        provider_id=first_provider,
        current_plan=plan.model_dump(mode="json"),
    )
    created: dict[str, Task] = {}
    parent_id: str | None = None
    for planned in plan.tasks:
        route = _route(config, planned)
        task = queue.enqueue_task(
            conn,
            repo_id=ctx.repo_id or 0,
            type="feature" if planned.phase in ("plan", "execute", "docs") else planned.phase,
            name=planned.title,
            prompt=planned.prompt,
            phase=planned.phase,
            parent_task_id=parent_id,
            provider_id=route.tool,
            job_id=job.id,
            model_tier=planned.model_tier,
            model=route.model,
            depends_on=[created[k].id for k in planned.depends_on],
        )
        if parent_id is None:
            parent_id = task.id
        created[planned.key] = task
    return job, created


def planner_prompt(message: str, *, repo_path: Path, config, recent_job_titles: list[str]) -> str:
    enabled = config.enabled_provider_ids()
    routing = config.routing.model_dump(mode="json")
    payload = {
        "user_message": message,
        "repo_path": str(repo_path),
        "repo_name": repo_path.name,
        "enabled_tools": enabled,
        "routing": routing,
        "recent_job_titles": recent_job_titles,
    }
    return (
        "You are Archon's planner. Respond ONLY with JSON matching this schema: "
        f"{json.dumps(PlanProposal.model_json_schema(), ensure_ascii=False)}\n\n"
        "Rules: prefer the smallest plan; 1-task docs plans are fine; every execute task needs "
        "a dependent review task assigned to a different tool or higher tier and a test task; "
        "never plan push, merge, PR creation, or PR submission; if ambiguity changes the plan "
        "shape, set clarifying_question and return no tasks.\n\n"
        f"Input:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def recent_job_titles(conn: sqlite3.Connection, repo_id: int, limit: int = 5) -> list[str]:
    rows = db.list_jobs(conn)
    return [r["title"] for r in rows if r["repo_id"] == repo_id][:limit]


def render_plan(plan: PlanProposal) -> str:
    if plan.clarifying_question:
        return f"Clarifying question: {plan.clarifying_question}"
    lines = [f"{plan.title}  risk={plan.overall_risk}", f"Objective: {plan.objective}"]
    if plan.acceptance_criteria:
        lines.append("Acceptance: " + "; ".join(plan.acceptance_criteria))
    lines.append("Tasks:")
    for task in plan.tasks:
        deps = f" after {', '.join(task.depends_on)}" if task.depends_on else ""
        cost = f" ~${task.est_cost_usd:.2f}" if task.est_cost_usd is not None else ""
        lines.append(
            f"  - {task.key}: {task.phase} · {task.tool}/{task.model_tier}{deps}{cost} · {task.title}"
        )
    return "\n".join(lines)


def _route(config, task: PlannedTask):
    route = config.routing.for_tier(task.model_tier)
    # Planner may choose a concrete tool; config still owns the model for that tier.
    if task.tool and task.tool != route.tool:
        return type(route)(tool=task.tool, model=route.model)
    return route


def _run_planner(command: list[str], prompt: str, *, cwd: Path) -> str:
    try:
        proc = subprocess.run(
            [*command, prompt],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        quoted = " ".join(shlex.quote(part) for part in command)
        raise RuntimeError(f"planner command not found: {quoted}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError((exc.stderr or exc.stdout or str(exc)).strip()) from exc
    return proc.stdout


def _strip_fences(raw: str) -> str:
    text = (raw or "").strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if match:
        return match.group(1).strip()
    return text


def _title_from_message(message: str) -> str:
    words = sanitize_slug(message).replace("-", " ").split()
    return " ".join(words[:7]).title() or "Archon Task"


def _looks_docs_only(message: str) -> bool:
    lowered = message.lower()
    return any(word in lowered for word in ("docs", "documentation", "readme")) and not any(
        word in lowered for word in ("implement", "fix", "bug", "endpoint", "api")
    )
