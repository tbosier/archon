"""FastAPI control-center API over the local Archon database."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import attention, db, jobs, queue
from .paths import Paths, resolve_paths
from .zellij import Zellij


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


class CreateJobRequest(BaseModel):
    repo_id: int
    title: str
    objective: str
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    provider_id: str | None = None


class ResolveAttentionRequest(BaseModel):
    resolution: str
    status: str = "resolved"
    unblock: bool = True


class ApprovePlanRequest(BaseModel):
    resolution: str = "approved"


def create_app(*, paths: Paths | None = None, db_path: Path | None = None) -> FastAPI:
    """Build the API app. A fresh SQLite connection is opened per request."""
    app = FastAPI(title="Archon Control Center API", version="0.1.0")
    paths = paths or resolve_paths().ensure()

    def conn_dep() -> Iterator[sqlite3.Connection]:
        conn = db.connect(paths if db_path is None else None, db_path=db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/api/health")
    def health(conn: sqlite3.Connection = Depends(conn_dep)) -> dict[str, Any]:
        return {
            "ok": True,
            "repos": len(db.list_repos(conn)),
            "jobs": len(db.list_jobs(conn)),
            "open_attention": len(db.list_attention_items(conn, status="open")),
        }

    @app.get("/api/repos")
    def repos(conn: sqlite3.Connection = Depends(conn_dep)) -> list[dict[str, Any]]:
        return _rows(db.list_repos(conn))

    @app.get("/api/jobs")
    def list_jobs(conn: sqlite3.Connection = Depends(conn_dep)) -> list[dict[str, Any]]:
        return _rows(db.list_jobs(conn))

    @app.post("/api/jobs")
    def create_job(
        body: CreateJobRequest,
        conn: sqlite3.Connection = Depends(conn_dep),
    ) -> dict[str, Any]:
        if db.get_repo(conn, body.repo_id) is None:
            raise HTTPException(status_code=404, detail="repo not found")
        job = jobs.create_job(
            conn,
            repo_id=body.repo_id,
            title=body.title,
            objective=body.objective,
            constraints=body.constraints,
            acceptance_criteria=body.acceptance_criteria,
            status="intake",
            provider_id=body.provider_id,
        )
        return dict(db.get_job(conn, job.id))

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, conn: sqlite3.Connection = Depends(conn_dep)) -> dict[str, Any]:
        job = db.get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        payload = dict(job)
        payload["tasks"] = _rows(db.list_job_tasks(conn, job_id))
        payload["agents"] = _rows(db.list_agents(conn, job_id=job_id))
        payload["attention"] = _rows(db.list_attention_items(conn, job_id=job_id))
        payload["events"] = _rows(db.list_events(conn, job_id=job_id, limit=100))
        return payload

    @app.post("/api/jobs/{job_id}/approve-plan")
    def approve_plan(
        job_id: str,
        body: ApprovePlanRequest,
        conn: sqlite3.Connection = Depends(conn_dep),
    ) -> dict[str, Any]:
        if db.get_job(conn, job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        jobs.approve_plan(conn, job_id, resolution=body.resolution)
        return dict(db.get_job(conn, job_id))

    @app.get("/api/agents")
    def list_agents(
        job_id: str | None = Query(default=None),
        conn: sqlite3.Connection = Depends(conn_dep),
    ) -> list[dict[str, Any]]:
        return _rows(db.list_agents(conn, job_id=job_id))

    @app.get("/api/attention")
    def list_attention(
        status: str | None = Query(default=None),
        job_id: str | None = Query(default=None),
        conn: sqlite3.Connection = Depends(conn_dep),
    ) -> list[dict[str, Any]]:
        return _rows(db.list_attention_items(conn, status=status, job_id=job_id))

    @app.post("/api/attention/{item_id}/resolve")
    def resolve_attention(
        item_id: str,
        body: ResolveAttentionRequest,
        conn: sqlite3.Connection = Depends(conn_dep),
    ) -> dict[str, Any]:
        try:
            attention.resolve_item(
                conn,
                item_id,
                resolution=body.resolution,
                status=body.status,
                unblock=body.unblock,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="attention item not found") from None
        return dict(db.get_attention_item(conn, item_id))

    @app.get("/api/events")
    def list_events(
        after_id: int | None = Query(default=None),
        job_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        conn: sqlite3.Connection = Depends(conn_dep),
    ) -> list[dict[str, Any]]:
        return list(reversed(_rows(db.list_events(conn, after_id=after_id, job_id=job_id, limit=limit))))

    @app.get("/api/events/stream")
    async def event_stream(
        after_id: int = Query(default=0),
        job_id: str | None = Query(default=None),
    ) -> StreamingResponse:
        async def _events() -> Iterator[str]:
            last_id = after_id
            while True:
                conn = db.connect(paths if db_path is None else None, db_path=db_path)
                try:
                    rows = list(reversed(db.list_events(conn, after_id=last_id, job_id=job_id, limit=100)))
                    for row in rows:
                        last_id = max(last_id, int(row["id"]))
                        yield f"data: {json.dumps(dict(row), default=str)}\n\n"
                finally:
                    conn.close()
                await asyncio.sleep(1.0)

        return StreamingResponse(_events(), media_type="text/event-stream")

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, conn: sqlite3.Connection = Depends(conn_dep)) -> dict[str, Any]:
        if db.get_task(conn, task_id) is None:
            raise HTTPException(status_code=404, detail="task not found")
        queue.cancel_task(conn, task_id)
        return dict(db.get_task(conn, task_id))

    @app.post("/api/runs/{run_id}/focus-terminal")
    def focus_terminal(run_id: str, conn: sqlite3.Connection = Depends(conn_dep)) -> dict[str, Any]:
        run = db.find_task_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if not run["zellij_session"] or not run["zellij_pane_id"]:
            raise HTTPException(status_code=409, detail="run has no terminal pane")
        Zellij().focus_pane(run["zellij_session"], run["zellij_pane_id"])
        return {"focused": True, "run_id": run_id, "pane_id": run["zellij_pane_id"]}

    return app


app = create_app()
