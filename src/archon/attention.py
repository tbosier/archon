"""Human attention queue helpers."""

from __future__ import annotations

import json
import sqlite3

from . import agents, db
from .models import AttentionItem
from .util import new_attention_id


def _context_for_run(conn: sqlite3.Connection, run_id: str) -> dict[str, str | None]:
    run = db.find_task_run(conn, run_id)
    if run is None:
        return {"task_id": None, "job_id": None, "agent_id": None}
    task = db.get_task(conn, run["task_id"])
    agent = agents.agent_for_task(conn, run["task_id"])
    return {
        "task_id": run["task_id"],
        "job_id": task["job_id"] if task is not None else None,
        "agent_id": agent["id"] if agent is not None else None,
    }


def open_item(
    conn: sqlite3.Connection,
    *,
    kind: str,
    severity: str,
    title: str,
    summary: str | None = None,
    job_id: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    task_run_id: str | None = None,
    options: list[str] | None = None,
    recommended_option: str | None = None,
) -> AttentionItem:
    """Open an attention item, de-duplicating active records for the same scope."""
    existing = db.find_open_attention_item(
        conn,
        kind=kind,
        task_run_id=task_run_id,
        task_id=task_id,
        job_id=job_id,
    )
    if existing is not None:
        return AttentionItem(
            id=existing["id"],
            job_id=existing["job_id"],
            agent_id=existing["agent_id"],
            task_id=existing["task_id"],
            task_run_id=existing["task_run_id"],
            kind=existing["kind"],
            severity=existing["severity"],
            title=existing["title"],
            summary=existing["summary"],
            options_json=existing["options_json"],
            recommended_option=existing["recommended_option"],
            status=existing["status"],
            resolution=existing["resolution"],
            resolved_at=existing["resolved_at"],
        )

    item = AttentionItem(
        id=new_attention_id(),
        job_id=job_id,
        agent_id=agent_id,
        task_id=task_id,
        task_run_id=task_run_id,
        kind=kind,
        severity=severity,
        title=title,
        summary=summary,
        options_json=json.dumps(options or [], ensure_ascii=False),
        recommended_option=recommended_option,
    )
    db.insert_attention_item(conn, item)
    if job_id:
        db.update_job(conn, job_id, status="attention_required")
    if agent_id:
        db.update_agent(conn, agent_id, state="waiting_on_user")
    db.insert_event(
        conn,
        event_type=f"attention.{kind}",
        severity=severity,
        message=title,
        job_id=job_id,
        agent_id=agent_id,
        task_id=task_id,
        task_run_id=task_run_id,
        requires_attention=True,
        summary=summary,
        details_json=json.dumps({"attention_item_id": item.id}, ensure_ascii=False),
    )
    return item


def open_permission_item(
    conn: sqlite3.Connection,
    *,
    task_run_id: str,
    title: str,
    summary: str | None = None,
) -> AttentionItem:
    """Open the standard permission-request decision for a blocked run."""
    ctx = _context_for_run(conn, task_run_id)
    return open_item(
        conn,
        kind="permission_request",
        severity="warn",
        title=title,
        summary=summary,
        job_id=ctx["job_id"],
        agent_id=ctx["agent_id"],
        task_id=ctx["task_id"],
        task_run_id=task_run_id,
        options=["approve", "deny", "inspect"],
        recommended_option="inspect",
    )


def open_permission_denied(
    conn: sqlite3.Connection,
    *,
    task_run_id: str,
    title: str,
    summary: str | None = None,
) -> AttentionItem:
    """Open a hard-deny decision: the policy blocked a dangerous command.

    Distinct from :func:`open_permission_item` (a routine escalation) — this one
    requires an explicit human override and never auto-resolves.
    """
    ctx = _context_for_run(conn, task_run_id)
    return open_item(
        conn,
        kind="permission_denied",
        severity="critical",
        title=title,
        summary=summary,
        job_id=ctx["job_id"],
        agent_id=ctx["agent_id"],
        task_id=ctx["task_id"],
        task_run_id=task_run_id,
        options=["override", "keep_blocked"],
        recommended_option="keep_blocked",
    )


def resolve_item(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    resolution: str,
    status: str = "resolved",
    unblock: bool = True,
) -> None:
    """Resolve an attention item and optionally unblock its run."""
    item = db.get_attention_item(conn, item_id)
    if item is None:
        raise KeyError(item_id)
    db.resolve_attention_item(conn, item_id, resolution=resolution, status=status)
    if unblock and item["task_run_id"] and resolution != "deny":
        db.set_task_run_status(conn, item["task_run_id"], "running")
    if item["task_id"] and resolution != "deny":
        db.set_task_status(conn, item["task_id"], "running")
    if item["agent_id"]:
        db.update_agent(conn, item["agent_id"], state="working")
    if item["job_id"]:
        remaining = db.list_attention_items(conn, status="open", job_id=item["job_id"])
        if not remaining:
            db.update_job(conn, item["job_id"], status="running")
    db.insert_event(
        conn,
        event_type=f"attention.{status}",
        severity="info",
        message=resolution,
        job_id=item["job_id"],
        agent_id=item["agent_id"],
        task_id=item["task_id"],
        task_run_id=item["task_run_id"],
        summary=f"Attention item {status}",
        details_json=json.dumps({"attention_item_id": item_id}, ensure_ascii=False),
    )
