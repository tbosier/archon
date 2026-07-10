"""Role-bearing agent helpers for the control center model."""

from __future__ import annotations

import sqlite3

from . import db
from .models import Agent
from .util import new_agent_id


def create_agent(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    role: str,
    provider_id: str | None = None,
    display_name: str | None = None,
    state: str = "idle",
) -> Agent:
    """Create a role agent for a job."""
    agent = Agent(
        id=new_agent_id(role),
        job_id=job_id,
        role=role,
        provider_id=provider_id,
        display_name=display_name or f"{role.title()} Agent",
        state=state,
    )
    db.insert_agent(conn, agent)
    return agent


def agent_for_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    """Return the first agent currently assigned to a task, if any."""
    return conn.execute(
        "SELECT * FROM agents WHERE current_task_id=? ORDER BY updated_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
