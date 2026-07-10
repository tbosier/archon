"""Job lifecycle helpers for the control center."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import agents, db
from .models import Job
from .util import new_job_id, utc_now


def _json_list(value: list[str] | list[dict[str, Any]] | None) -> str:
    return json.dumps(value or [], ensure_ascii=False)


def create_job(
    conn: sqlite3.Connection,
    *,
    repo_id: int,
    title: str,
    objective: str,
    constraints: list[str] | list[dict[str, Any]] | None = None,
    acceptance_criteria: list[str] | list[dict[str, Any]] | None = None,
    status: str = "intake",
    provider_id: str | None = None,
    current_plan: dict[str, Any] | list[Any] | None = None,
) -> Job:
    """Create a durable job plus its lead agent."""
    job = Job(
        id=new_job_id(),
        repo_id=repo_id,
        title=title,
        objective=objective,
        constraints_json=_json_list(constraints),
        acceptance_criteria_json=_json_list(acceptance_criteria),
        status=status,
        current_plan_json=json.dumps(current_plan, ensure_ascii=False) if current_plan is not None else None,
    )
    db.insert_job(conn, job)
    lead = agents.create_agent(
        conn,
        job_id=job.id,
        role="lead",
        provider_id=provider_id,
        display_name="Lead Agent",
        state="planning" if status in ("intake", "planning", "awaiting_plan_approval") else "working",
    )
    db.update_job(conn, job.id, lead_agent_id=lead.id)
    job.lead_agent_id = lead.id
    db.insert_event(
        conn,
        event_type="job.created",
        severity="info",
        message=f"created job {title}",
        job_id=job.id,
        agent_id=lead.id,
        summary=objective,
    )
    return job


def link_task(conn: sqlite3.Connection, *, job_id: str, task_id: str) -> None:
    """Attach an existing task to a job."""
    db.set_task_job(conn, task_id, job_id)


def approve_plan(conn: sqlite3.Connection, job_id: str, *, resolution: str = "approved") -> None:
    """Move a job out of plan approval and record the approval event."""
    db.update_job(conn, job_id, status="running")
    db.insert_event(
        conn,
        event_type="job.plan_approved",
        severity="info",
        message=resolution,
        job_id=job_id,
        summary="Plan approved",
    )


def mark_finished(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    """Mark a job terminal."""
    db.update_job(conn, job_id, status=status, finished_at=utc_now())
