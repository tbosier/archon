"""FastAPI control-center API over the local Archon database."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import attention, budget, db, dispatcher, jobs, queue, scheduler
from .config import load_config
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


class RegisterRepoRequest(BaseModel):
    path: str


class ChatCommandRequest(BaseModel):
    message: str
    repo_id: int | None = None
    repo_path: str | None = None
    provider_id: str | None = None
    dispatch: bool = True
    dry_run: bool = False


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
    asset_dir = Path(__file__).resolve().parents[2] / "docs" / "assets"
    if asset_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(asset_dir)), name="assets")

    def conn_dep() -> Iterator[sqlite3.Connection]:
        conn = db.connect(paths if db_path is None else None, db_path=db_path)
        try:
            yield conn
        finally:
            conn.close()

    def config_dep():
        return load_config(paths)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return CONTROL_CENTER_HTML

    def _brand_file(name: str) -> FileResponse:
        root = Path(__file__).resolve().parents[2]
        path = root / name
        if not path.exists():
            raise HTTPException(status_code=404, detail="brand image not found")
        return FileResponse(path, media_type="image/svg+xml")

    @app.get("/brand/archon-mark.svg")
    def brand_image() -> FileResponse:
        root = Path(__file__).resolve().parents[2]
        candidates = [
            root / "archon-mark.svg",
            root / "docs" / "assets" / "archon-mark.png",
            root / "docs" / "assets" / "archon-logo.png",
            root / "docs" / "assets" / "archon.png",
            root / "archon-mark.png",
            root / "archon-logo.png",
            root / "archon.png",
        ]
        root_image = next((path for path in candidates if path.exists()), None)
        if root_image is None:
            raise HTTPException(status_code=404, detail="brand image not found")
        return FileResponse(root_image)

    @app.get("/brand/archon-mark-mono.svg")
    def brand_mono_image() -> FileResponse:
        return _brand_file("archon-mark-mono.svg")

    @app.get("/favicon.ico")
    def favicon() -> FileResponse:
        favicon_path = Path(__file__).resolve().parents[2] / "archon-favicon.svg"
        if not favicon_path.exists():
            raise HTTPException(status_code=404, detail="favicon not found")
        return FileResponse(favicon_path, media_type="image/svg+xml")

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

    @app.post("/api/repos")
    def register_repo(
        body: RegisterRepoRequest,
        conn: sqlite3.Connection = Depends(conn_dep),
        config=Depends(config_dep),
    ) -> dict[str, Any]:
        try:
            ctx = dispatcher.register_repo(
                conn,
                dispatcher.resolve_repo_context(Path(body.path), config=config),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        row = db.get_repo(conn, ctx.repo_id or 0)
        return dict(row) if row else {"id": ctx.repo_id, "name": ctx.name, "root_path": str(ctx.root)}

    @app.get("/api/providers")
    def providers(config=Depends(config_dep)) -> list[dict[str, Any]]:
        return [
            {
                "id": pid,
                "display_name": provider.display_name,
                "enabled": provider.enabled,
                "default_mode": provider.default_mode,
            }
            for pid, provider in config.providers.items()
        ] + [
            {
                "id": custom.id,
                "display_name": custom.display_name,
                "enabled": custom.enabled,
                "default_mode": custom.default_mode,
            }
            for custom in config.custom
        ]

    @app.post("/api/chat")
    def submit_chat_command(
        body: ChatCommandRequest,
        conn: sqlite3.Connection = Depends(conn_dep),
        config=Depends(config_dep),
    ) -> dict[str, Any]:
        message = body.message.strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        ctx = _repo_context_for_chat(conn, config, repo_id=body.repo_id, repo_path=body.repo_path)
        provider_id = body.provider_id or (config.enabled_provider_ids()[0] if config.enabled_provider_ids() else None)
        if provider_id is None:
            raise HTTPException(status_code=400, detail="enable a provider before submitting work")
        if provider_id not in config.enabled_provider_ids():
            raise HTTPException(status_code=400, detail=f"provider is not enabled: {provider_id}")

        feature_name = _feature_name_from_message(message)
        chain = dispatcher.enqueue_feature(
            conn,
            config,
            ctx,
            feature_name=feature_name,
            provider_id=provider_id,
            prompt_text=message,
        )
        launch = dispatcher.make_scheduler_launch(Zellij(dry_run=body.dry_run), body.dry_run)
        decision = (
            scheduler.tick(conn, config, launch=launch, budget_policy=budget.policy)
            if body.dispatch
            else scheduler.SchedulerDecision(reason="dispatch skipped")
        )
        job_id = chain["execute"].job_id
        return {
            "message": message,
            "repo": {"id": ctx.repo_id, "name": ctx.name, "root_path": str(ctx.root)},
            "provider_id": provider_id,
            "feature_name": feature_name,
            "job": dict(db.get_job(conn, job_id)) if job_id else None,
            "tasks": {k: (vars(v) if v is not None else None) for k, v in chain.items()},
            "scheduler": {
                "dispatched": decision.dispatched,
                "skipped": decision.skipped,
                "paused": decision.paused,
                "reason": decision.reason,
                "budget_action": decision.budget_action,
            },
        }

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

    @app.get("/api/runs")
    def list_runs(conn: sqlite3.Connection = Depends(conn_dep)) -> list[dict[str, Any]]:
        return [_augment_run_for_ui(dict(row)) for row in db.list_task_runs(conn)]

    @app.post("/api/schedule")
    def run_schedule(
        conn: sqlite3.Connection = Depends(conn_dep),
        config=Depends(config_dep),
    ) -> dict[str, Any]:
        launch = dispatcher.make_scheduler_launch(Zellij(), False)
        decision = scheduler.tick(conn, config, launch=launch, budget_policy=budget.policy)
        return {
            "dispatched": decision.dispatched,
            "skipped": decision.skipped,
            "paused": decision.paused,
            "reason": decision.reason,
            "budget_action": decision.budget_action,
        }

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

    @app.post("/api/runs/{run_id}/stop")
    def stop_run(run_id: str, conn: sqlite3.Connection = Depends(conn_dep)) -> dict[str, Any]:
        run = db.find_task_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if run["zellij_session"] and run["zellij_pane_id"]:
            Zellij().close_pane(run["zellij_session"], run["zellij_pane_id"])
        db.set_task_run_status(conn, run_id, "failed")
        task = db.get_task(conn, run["task_id"])
        if task is not None:
            db.set_task_status(conn, task["id"], "failed")
        db.insert_event(
            conn,
            event_type="task_run_stopped",
            severity="warn",
            message=f"stopped {run_id}",
            task_id=run["task_id"],
            task_run_id=run_id,
            provider_id=run["provider_id"],
        )
        return dict(db.find_task_run(conn, run_id))

    @app.post("/api/runs/{run_id}/send-enter")
    def send_enter_to_run(run_id: str, conn: sqlite3.Connection = Depends(conn_dep)) -> dict[str, Any]:
        run = db.find_task_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if not run["zellij_session"] or not run["zellij_pane_id"]:
            raise HTTPException(status_code=409, detail="run has no terminal pane")
        Zellij().send_enter(run["zellij_session"], run["zellij_pane_id"])
        db.insert_event(
            conn,
            event_type="task_run_input_sent",
            severity="info",
            message=f"sent Enter to {run_id}",
            task_id=run["task_id"],
            task_run_id=run_id,
            provider_id=run["provider_id"],
        )
        updated = dict(db.find_task_run(conn, run_id))
        updated.update({"input_sent": True})
        return updated

    @app.post("/api/runs/{run_id}/focus-terminal")
    def focus_terminal(run_id: str, conn: sqlite3.Connection = Depends(conn_dep)) -> dict[str, Any]:
        run = db.find_task_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if not run["zellij_session"] or not run["zellij_pane_id"]:
            raise HTTPException(status_code=409, detail="run has no terminal pane")
        focused = Zellij().focus_pane(run["zellij_session"], run["zellij_pane_id"])
        if not focused:
            raise HTTPException(status_code=502, detail="zellij did not accept the focus command")
        return {
            "focused": True,
            "run_id": run_id,
            "session": run["zellij_session"],
            "pane_id": run["zellij_pane_id"],
            "attach_command": f"zellij attach {run['zellij_session']}",
        }

    return app


app = create_app()


def _repo_context_for_chat(conn, config, *, repo_id: int | None, repo_path: str | None) -> dispatcher.RepoContext:
    if repo_id is not None:
        row = db.get_repo(conn, repo_id)
        if row is None:
            raise HTTPException(status_code=404, detail="repo not found")
        return dispatcher.RepoContext(
            root=Path(row["root_path"]),
            name=row["name"],
            session=row["zellij_session"],
            repo_id=row["id"],
        )
    if repo_path:
        try:
            return dispatcher.register_repo(
                conn,
                dispatcher.resolve_repo_context(Path(repo_path), config=config),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    repos = db.list_repos(conn)
    if len(repos) == 1:
        row = repos[0]
        return dispatcher.RepoContext(
            root=Path(row["root_path"]),
            name=row["name"],
            session=row["zellij_session"],
            repo_id=row["id"],
        )
    raise HTTPException(status_code=400, detail="choose a repo or provide repo_path")


def _feature_name_from_message(message: str) -> str:
    first = message.strip().splitlines()[0]
    for prefix in ("please ", "can you ", "could you ", "make ", "build ", "create ", "add ", "implement "):
        if first.lower().startswith(prefix):
            first = first[len(prefix):]
            break
    first = re.split(r"\b(?:it should|should|does not|doesn't|use good|actually skip)\b", first, flags=re.I)[0]
    first = re.sub(r"\b(?:something dumb like|for something dumb like|regular ol)\b", "", first, flags=re.I)
    first = re.sub(r"^(?:a|an|the)\s+", "", first.strip(), flags=re.I)
    words = first.replace(".", " ").strip().split()
    return " ".join(words[:5]) or "new task"


def _augment_run_for_ui(run: dict[str, Any]) -> dict[str, Any]:
    run["needs_attention"] = False
    run["attention_reason"] = None
    if run.get("status") not in {"running", "starting", "blocked", "stale", "queued"}:
        return run
    session = run.get("zellij_session")
    pane_id = run.get("zellij_pane_id")
    if not session or not pane_id:
        return run

    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(pane_id))
    dump_path = Path("/tmp") / f"archon-pane-{safe_id}.txt"
    try:
        dump_path.unlink(missing_ok=True)
    except OSError:
        pass
    Zellij().dump_screen(str(session), str(pane_id), str(dump_path))
    try:
        screen = dump_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return run

    if "Waiting to run:" in screen and "<ENTER> run" in screen:
        run["needs_attention"] = True
        run["attention_reason"] = "Waiting for command approval in the terminal"
    elif "Please run /login" in screen or "Not logged in" in screen:
        run["needs_attention"] = True
        run["attention_reason"] = "Provider is asking for login"
    return run


CONTROL_CENTER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archon Control Center</title>
  <style>
    :root {
      color-scheme: dark;
      --bg-0: #0a0e14;
      --bg-1: #0f151d;
      --bg-2: #161d27;
      --border: #232c38;
      --text-primary: #e8ecf1;
      --text-muted: #6b7684;
      --accent-active: #b98fff;
      --state-waiting: #e0a458;
      --state-running: #5b8fd6;
      --state-done: #4ade80;
      --state-error: #f87171;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg-0);
      color: var(--text-primary);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    button, input, select, textarea { font: inherit; }
    .shell {
      min-height: 100vh;
      display: grid;
      grid-template-columns: 360px minmax(520px, 1fr) 400px;
      grid-template-rows: 76px minmax(0, 1fr);
    }
    header {
      grid-column: 1 / -1;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 0 20px;
      border-bottom: 1px solid var(--border);
      background: #0c1118;
    }
    .brand { display: flex; align-items: center; gap: 16px; min-width: 0; }
    .brand-mark {
      width: 30px;
      height: 30px;
      flex: 0 0 auto;
      background: linear-gradient(135deg, #d7c1ff 0%, var(--accent-active) 48%, var(--state-running) 100%);
      filter: drop-shadow(0 0 10px rgba(185, 143, 255, .34));
      -webkit-mask: url('/brand/archon-mark-mono.svg') center / contain no-repeat;
      mask: url('/brand/archon-mark-mono.svg') center / contain no-repeat;
    }
    .brand-wordmark { display: flex; align-items: baseline; gap: 18px; min-width: 0; }
    .brand h1 {
      margin: 0;
      font-family: "JetBrains Mono", "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 28px;
      font-weight: 720;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    .breadcrumb {
      min-width: 0;
      color: var(--text-muted);
      font-family: "JetBrains Mono", "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 23px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .health {
      display: flex;
      align-items: baseline;
      gap: 20px;
      flex-wrap: wrap;
      justify-content: flex-end;
      color: var(--text-muted);
      font-size: 19px;
    }
    .stat {
      color: var(--text-muted);
      white-space: nowrap;
    }
    .stat strong {
      color: var(--text-primary);
      font-family: "JetBrains Mono", "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-weight: 700;
    }
    .stat.running strong { color: var(--state-running); }
    .stat-divider {
      width: 1px;
      height: 24px;
      background: var(--border);
    }
    .chip {
      color: var(--text-muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .chip strong { color: var(--text-primary); font-weight: 720; }
    aside, main, section.activity {
      min-height: 0;
      border-right: 1px solid var(--border);
      background: var(--bg-1);
    }
    section.activity { border-right: 0; }
    .pane { padding: 24px 30px; overflow: auto; }
    .pane h2, .panel h2 {
      margin: 0 0 12px;
      font-size: 24px;
      text-transform: uppercase;
      color: var(--text-muted);
      font-weight: 560;
      letter-spacing: 2px;
    }
    label { display: block; margin: 12px 0 6px; color: var(--text-muted); font-size: 12px; }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 7px;
      color: var(--text-primary);
      background: #0a0d12;
      padding: 10px 11px;
      outline: none;
    }
    textarea {
      min-height: 124px;
      resize: vertical;
      line-height: 1.42;
    }
    input:focus, select:focus, textarea:focus { border-color: var(--accent-active); }
    button {
      border: 1px solid #48636b;
      border-radius: 7px;
      background: var(--bg-2);
      color: var(--text-primary);
      padding: 10px 13px;
      cursor: pointer;
      font-weight: 690;
      white-space: nowrap;
    }
    button:hover { background: #352c52; }
    button.primary {
      border-color: #b98fff;
      background: #b98fff;
      color: #150d24;
    }
    button.primary:hover { background: #c9a9ff; }
    button.secondary { background: var(--bg-2); border-color: var(--border); }
    button.danger { background: #3a2024; border-color: #6b3238; color: #ffd2d2; }
    button.danger:hover { background: #4b282d; }
    button:disabled { cursor: wait; opacity: .6; }
    .row { display: flex; gap: 8px; align-items: center; }
    .row > * { min-width: 0; }
    .workbench {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: 28px;
      padding: 32px 38px;
      overflow: hidden;
      background: var(--bg-0);
    }
    .panel {
      border: 1px solid var(--border);
      border-radius: 18px;
      background: var(--bg-1);
      padding: 28px 32px;
    }
    .panel.primary {
      border-color: #3b2d54;
      background: linear-gradient(180deg, #121824 0%, var(--bg-1) 100%);
      box-shadow: 0 0 0 1px rgba(185, 143, 255, .08), 0 12px 34px rgba(0,0,0,.22);
    }
    .panel-title {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 18px;
    }
    .panel-title h2 { margin: 0; }
    .panel-meta {
      min-width: 0;
      color: var(--text-muted);
      font-family: "JetBrains Mono", "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 18px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .command textarea { min-height: 116px; font-size: 28px; }
    .command .row { justify-content: space-between; margin-top: 12px; align-items: center; }
    .hint { color: var(--text-muted); font-size: 13px; overflow-wrap: anywhere; }
    .live {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--text-muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 1px;
    }
    .live::before {
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: var(--state-done);
      box-shadow: 0 0 10px rgba(74, 222, 128, .5);
    }
    .feed-grid {
      min-height: 0;
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(280px, 34%);
      gap: 16px;
    }
    .panel.scroll { min-height: 0; overflow: auto; }
    .messages {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .bubble {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 11px 12px;
      background: var(--bg-2);
      line-height: 1.42;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .bubble.user { background: #1a2130; border-color: #3b2d54; }
    .bubble.system { color: var(--text-muted); }
    .conversation-empty {
      min-height: 230px;
      display: grid;
      place-items: center;
      text-align: center;
      color: var(--text-muted);
      border: 1px dashed var(--border);
      border-radius: 8px;
      background: rgba(10, 14, 20, .38);
      padding: 22px;
    }
    .conversation-empty strong {
      display: block;
      color: var(--text-primary);
      font-size: 14px;
      margin-bottom: 6px;
    }
    .conversation-empty button {
      margin-top: 12px;
      padding: 7px 9px;
      font-size: 12px;
    }
    .list { display: flex; flex-direction: column; gap: 8px; }
    .item {
      padding: 20px 20px;
      border: 1px solid var(--border);
      border-left: 4px solid transparent;
      border-radius: 0;
      background: var(--bg-2);
    }
    .item.waiting { border-left-color: var(--state-waiting); }
    .item.running { border-left-color: var(--state-running); }
    .item.done { border-left-color: var(--state-done); }
    .item.error { border-left-color: var(--state-error); }
    .item.active { border-left-color: var(--accent-active); }
    .item.needs-attention {
      background: #211b12;
      border-color: #594522;
      border-left-color: var(--state-waiting);
    }
    .item strong { display: block; font-size: 24px; font-weight: 560; margin-bottom: 10px; overflow-wrap: anywhere; }
    .item span { display: block; color: var(--text-muted); font-size: 13px; overflow-wrap: anywhere; }
    .attention { border-color: #6d5a28; background: #242016; }
    .error { color: var(--state-error); }
    .empty { color: var(--text-muted); font-size: 13px; padding: 8px 0; }
    .empty-state {
      min-height: 170px;
      display: grid;
      place-items: center;
      text-align: center;
      color: var(--text-muted);
      border: 1px dashed var(--border);
      border-radius: 8px;
      background: rgba(10, 14, 20, .45);
      padding: 18px;
    }
    .empty-state img {
      width: 72px;
      height: 72px;
      object-fit: contain;
      object-position: center;
      opacity: .72;
      margin-bottom: 10px;
    }
    .token, .meta-code, time {
      font-family: "JetBrains Mono", "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .token {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 0;
      font-size: 18px;
      font-weight: 500;
      border: 0;
      background: transparent;
    }
    .token::before {
      content: "";
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: currentColor;
      margin-right: 9px;
    }
    .token.waiting { color: var(--state-waiting); }
    .token.running { color: var(--state-running); }
    .token.done { color: var(--state-done); }
    .token.error { color: var(--state-error); }
    .token.active { color: var(--accent-active); }
    .meta-line {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
      color: var(--text-muted);
      font-size: 16px;
    }
    time { font-size: 15px; color: var(--text-muted); }
    .run-actions { display: flex; gap: 10px; margin-top: 16px; }
    .run-actions button { padding: 8px 14px; font-size: 16px; }
    .attention-reason {
      margin-top: 12px;
      color: var(--state-waiting);
      font-size: 13px;
    }
    .event-list { gap: 0; }
    .event-row {
      padding: 18px 0;
      border-bottom: 1px solid var(--border);
      background: transparent;
    }
    .event-row:first-child { padding-top: 0; }
    .event-row:last-child { border-bottom: 0; }
    .event-row strong {
      display: block;
      font-size: 25px;
      line-height: 1.32;
      font-weight: 480;
      margin-bottom: 10px;
      overflow-wrap: anywhere;
    }
    details.event-row summary {
      cursor: pointer;
      list-style: none;
    }
    details.event-row summary::-webkit-details-marker { display: none; }
    .event-detail {
      margin-top: 9px;
      padding-top: 9px;
      border-top: 1px solid var(--border);
      color: var(--text-muted);
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-line;
    }
    .toast {
      margin-top: 12px;
      border: 1px solid #553f42;
      background: #24191b;
      color: #f0b5b1;
      border-radius: 7px;
      padding: 10px;
      font-size: 13px;
      display: none;
      overflow-wrap: anywhere;
    }
    .sidebar-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 20px;
    }
    .sidebar-head h2 { margin: 0; }
    .workspace-config {
      margin-top: 24px;
      border-top: 1px solid var(--border);
      padding-top: 18px;
    }
    .workspace-config summary {
      cursor: pointer;
      color: var(--text-muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 1.3px;
      list-style: none;
    }
    .workspace-config summary::-webkit-details-marker { display: none; }
    @media (max-width: 1100px) {
      .shell { grid-template-columns: 1fr; grid-template-rows: 76px auto minmax(620px, 1fr) auto; }
      aside, main, section.activity { border-right: 0; border-bottom: 1px solid var(--border); }
      .feed-grid { grid-template-columns: 1fr; }
      .brand h1 { font-size: 22px; }
      .breadcrumb { display: none; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div class="brand">
        <span class="brand-mark" aria-hidden="true"></span>
        <div class="brand-wordmark">
          <h1>ARCHON</h1>
          <span class="breadcrumb" id="headerRepo">/ no repo</span>
        </div>
      </div>
      <div class="health" id="health"></div>
    </header>
    <aside class="pane">
      <div class="sidebar-head">
        <h2>Open Jobs</h2>
      </div>
      <div class="list" id="jobs"></div>
      <details class="workspace-config" id="workspaceConfig">
        <summary>Workspace</summary>
        <label for="repoPath">Repo path</label>
        <div class="row">
          <input id="repoPath" placeholder="/path/to/repo">
          <button class="secondary" id="registerRepo">Add</button>
        </div>
        <div class="toast" id="workspaceError"></div>
        <label for="repoSelect">Repository</label>
        <select id="repoSelect"></select>
        <label for="providerSelect">Provider</label>
        <select id="providerSelect"></select>
      </details>
    </aside>
    <main class="workbench">
      <form class="panel command" id="chatForm">
        <div class="panel-title">
          <h2>Command</h2>
          <span class="panel-meta" id="commandMeta"></span>
        </div>
        <textarea id="message" placeholder="Build a frontend control panel for simulation runs"></textarea>
        <div class="row">
          <span class="live" id="selectionHint">live</span>
          <div class="row">
            <button class="secondary" type="button" id="scheduleNow">Schedule</button>
            <button class="primary" type="submit" id="send">Send</button>
          </div>
        </div>
        <div class="toast" id="commandError"></div>
      </form>
      <div class="feed-grid">
        <section class="panel scroll">
          <h2>Conversation</h2>
          <div class="messages" id="messages">
            <div class="conversation-empty" id="conversationEmpty">
              <div>
                <strong>Agent responses will appear here</strong>
                <div>Submit a task or focus an active pane to follow the workflow.</div>
                <button class="secondary" type="button" id="suggestCommand">Use example command</button>
              </div>
            </div>
          </div>
        </section>
        <section class="panel scroll">
          <h2>Activity</h2>
          <div class="list event-list" id="events"></div>
        </section>
      </div>
    </main>
    <section class="pane activity">
      <h2>Active Runs</h2>
      <div class="list" id="runs"></div>
      <div style="height:20px"></div>
      <h2>Attention</h2>
      <div class="list" id="attention"></div>
    </section>
  </div>
  <script>
    const state = { repos: [], providers: [] };
    const el = (id) => document.getElementById(id);

    function option(value, text) {
      const node = document.createElement('option');
      node.value = value;
      node.textContent = text;
      return node;
    }

    function showError(id, text = '') {
      const node = el(id);
      node.textContent = text;
      node.style.display = text ? 'block' : 'none';
    }

    function bubble(text, kind = 'system') {
      const empty = el('conversationEmpty');
      if (empty) empty.remove();
      const node = document.createElement('div');
      node.className = `bubble ${kind}`;
      node.textContent = text;
      el('messages').prepend(node);
    }

    async function api(path, opts = {}) {
      const res = await fetch(path, {
        headers: { 'content-type': 'application/json' },
        ...opts,
      });
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch {}
        throw new Error(detail);
      }
      return res.json();
    }

    async function safe(path, fallback, label) {
      try {
        return await api(path);
      } catch (err) {
        bubble(`${label}: ${err.message}`);
        return fallback;
      }
    }

    async function refreshAll() {
      showError('workspaceError');
      showError('commandError');
      const [health, repos, providers, jobs, attention, events, runs] = await Promise.all([
        safe('/api/health', { repos: 0, jobs: 0, open_attention: 0 }, 'Health'),
        safe('/api/repos', [], 'Repos'),
        safe('/api/providers', [], 'Providers'),
        safe('/api/jobs', [], 'Jobs'),
        safe('/api/attention?status=open', [], 'Attention'),
        safe('/api/events?limit=40', [], 'Activity'),
        safe('/api/runs', [], 'Runs'),
      ]);
      state.repos = repos;
      state.providers = providers.filter((p) => p.enabled);
      if (!repos.length) el('workspaceConfig').open = true;
      const repoSelect = el('repoSelect');
      const selectedRepo = repoSelect.value;
      repoSelect.replaceChildren();
      repoSelect.appendChild(option('', ''));
      repos.forEach((repo) => repoSelect.appendChild(option(repo.id, `${repo.name}  ${repo.root_path}`)));
      if (selectedRepo && repos.some((repo) => String(repo.id) === selectedRepo)) {
        repoSelect.value = selectedRepo;
      } else if (repos.length) {
        repoSelect.value = String(repos[0].id);
      }

      const providerSelect = el('providerSelect');
      const selectedProvider = providerSelect.value;
      providerSelect.replaceChildren();
      providerSelect.appendChild(option('', ''));
      state.providers.forEach((provider) => providerSelect.appendChild(option(provider.id, provider.display_name)));
      if (selectedProvider && state.providers.some((provider) => provider.id === selectedProvider)) {
        providerSelect.value = selectedProvider;
      } else if (state.providers.length) {
        providerSelect.value = state.providers[0].id;
      }

      const runningCount = runs.filter((run) => isActiveRun(run)).length;
      el('health').innerHTML = `
        <span class="stat"><strong>${health.repos}</strong> ${plural(health.repos, 'repo')}</span>
        <span class="stat"><strong>${health.jobs}</strong> ${plural(health.jobs, 'job')}</span>
        <span class="stat running"><strong>${runningCount}</strong> running</span>
        <span class="stat-divider" aria-hidden="true"></span>
        <span class="stat">decisions <strong>${health.open_attention}</strong></span>
      `;

      updateSelectionHint();
      renderJobs(jobs);
      renderAttention(attention);
      renderRuns(runs);
      renderEvents(events.slice().reverse());
    }

    function updateSelectionHint() {
      const repo = state.repos.find((r) => String(r.id) === el('repoSelect').value);
      const provider = state.providers.find((p) => p.id === el('providerSelect').value);
      el('headerRepo').textContent = repo ? `/ ${repo.name}` : '/ no repo';
      el('commandMeta').textContent = [repo && repo.name, provider && provider.id].filter(Boolean).join(' · ');
      el('selectionHint').textContent = 'live';
    }

    function statusClass(status) {
      if (['running', 'starting', 'working', 'reviewing', 'running_tests'].includes(status)) return 'running';
      if (['done', 'complete', 'ready', 'resolved'].includes(status)) return 'done';
      if (['failed', 'crashed', 'error', 'missing', 'cancelled'].includes(status)) return 'error';
      if (['planning', 'queued', 'blocked', 'stale', 'attention_required', 'budget_capped', 'intake'].includes(status)) return 'waiting';
      return 'active';
    }

    function token(status) {
      const safe = status || 'unknown';
      return `<span class="token ${statusClass(safe)}">${safe}</span>`;
    }

    function plural(count, word) {
      return count === 1 ? word : `${word}s`;
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }

    function relativeTime(iso) {
      if (!iso) return '';
      const then = new Date(iso).getTime();
      const delta = Math.max(0, Date.now() - then);
      const minute = 60 * 1000;
      const hour = 60 * minute;
      const day = 24 * hour;
      if (delta < minute) return 'just now';
      if (delta < hour) return `${Math.floor(delta / minute)}m ago`;
      if (delta < day) return `${Math.floor(delta / hour)}h ago`;
      return `${Math.floor(delta / day)}d ago`;
    }

    function isActiveRun(run) {
      return ['running', 'starting', 'blocked', 'stale', 'queued'].includes(run.status);
    }

    function shortId(id) {
      const value = String(id || '');
      return value.slice(-7) || value;
    }

    function titleCase(text) {
      return String(text || '').split(' ').filter(Boolean).map((word) => {
        if (/^[A-Z0-9]{2,}$/.test(word)) return word;
        if (/^\\d/.test(word)) return word;
        return word.charAt(0).toUpperCase() + word.slice(1);
      }).join(' ');
    }

    function cleanTitle(text, fallback = 'Untitled') {
      let value = String(text || '').replace(/\s+/g, ' ').trim();
      value = value.replace(/^(build|create|make|implement|add)\s+/i, '');
      value = value.replace(/^(a|an|the)\s+/i, '');
      value = value.replace(/\b(for something|using numpy for something|it should.*|should.*)$/i, '');
      value = value.replace(/\b(something dumb like|for something dumb like)\b/i, '');
      const words = value.trim().split(' ').filter(Boolean).slice(0, 5);
      return titleCase(words.join(' ') || fallback);
    }

    function renderList(id, rows, map, extraClass = '') {
      const root = el(id);
      root.replaceChildren();
      if (!rows.length) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = 'None';
        root.appendChild(empty);
        return;
      }
      rows.forEach((row) => {
        const [title, meta] = map(row);
        const node = document.createElement('div');
        node.className = `item ${extraClass}`;
        node.innerHTML = `<strong></strong><span></span>`;
        node.querySelector('strong').textContent = title || '-';
        node.querySelector('span').textContent = meta || '';
        root.appendChild(node);
      });
    }

    function renderJobs(jobs) {
      const root = el('jobs');
      root.replaceChildren();
      if (!jobs.length) return root.appendChild(emptyNode('No active jobs'));
      jobs.forEach((job) => {
        const node = document.createElement('div');
        node.className = `item ${statusClass(job.status)}`;
        node.title = job.objective || job.title || '';
        node.innerHTML = `
          <strong>${escapeHtml(cleanTitle(job.title))}</strong>
          <div class="meta-line">${token(job.status)} <span class="meta-code">${escapeHtml(shortId(job.id))}</span></div>
        `;
        root.appendChild(node);
      });
    }

    function renderAttention(items) {
      const root = el('attention');
      root.replaceChildren();
      if (!items.length) {
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        empty.innerHTML = '<div><img src="/brand/archon-mark.svg" alt=""><div>Nothing needs you right now</div></div>';
        root.appendChild(empty);
        return;
      }
      items.forEach((item) => {
        const node = document.createElement('div');
        node.className = 'item attention';
        node.innerHTML = `
          <strong>${escapeHtml(item.title)}</strong>
          <div class="meta-line">${token(item.severity)} <span>${escapeHtml(item.kind)}</span></div>
          <span>${escapeHtml(item.job_title || item.id)}</span>
        `;
        root.appendChild(node);
      });
    }

    function renderEvents(events) {
      const root = el('events');
      root.replaceChildren();
      if (!events.length) return root.appendChild(emptyNode('No activity yet'));
      groupEvents(events).forEach((event) => root.appendChild(eventNode(event)));
    }

    function eventNode(event) {
      if (event.grouped) {
        const node = document.createElement('details');
        node.className = 'event-row';
        node.innerHTML = `
          <summary>
            <strong>${escapeHtml(event.title)}</strong>
            <div class="meta-line">
              <span class="meta-code">${escapeHtml(event.event_type)}</span>
              <time title="${escapeHtml(event.created_at)}">${relativeTime(event.created_at)}</time>
            </div>
          </summary>
          <div class="event-detail">${escapeHtml(event.detail)}</div>
        `;
        return node;
      }
      const node = document.createElement('div');
      node.className = 'event-row';
      const severity = event.severity || 'info';
      const badge = severity === 'info' ? '' : token(severity);
      node.innerHTML = `
        <strong>${escapeHtml(event.message || event.event_type)}</strong>
        <div class="meta-line">
          ${badge}
          <span class="meta-code">${escapeHtml(event.event_type)}</span>
          <time title="${escapeHtml(event.created_at)}">${relativeTime(event.created_at)}</time>
        </div>
      `;
      return node;
    }

    function groupEvents(events) {
      const grouped = [];
      let launchGroup = [];
      const flushLaunches = () => {
        if (!launchGroup.length) return;
        if (launchGroup.length === 1) {
          grouped.push(launchGroup[0]);
        } else {
          const panes = launchGroup.map((event) => launchPane(event.message)).filter(Boolean);
          const providers = [...new Set(launchGroup.map((event) => event.provider_id).filter(Boolean))];
          grouped.push({
            grouped: true,
            title: `Launched ${launchGroup.length} agents${providers.length ? ` · ${providers.join(', ')}` : ''}`,
            detail: panes.length ? `Panes ${compactPanes(panes)}.` : launchGroup.map((event) => event.message).join('\\n'),
            event_type: 'task_run_launched',
            created_at: launchGroup[0].created_at,
          });
        }
        launchGroup = [];
      };
      events.forEach((event) => {
        if (event.event_type === 'task_run_launched' && /^launched .+ in pane /.test(event.message || '')) {
          launchGroup.push(event);
          return;
        }
        flushLaunches();
        grouped.push(event);
      });
      flushLaunches();
      return grouped;
    }

    function launchPane(message) {
      const match = String(message || '').match(/pane\\s+([^\\s]+)/);
      return match ? match[1] : '';
    }

    function compactPanes(panes) {
      const numeric = panes.map((pane) => Number(pane)).filter((pane) => Number.isInteger(pane)).sort((a, b) => a - b);
      if (numeric.length === panes.length && numeric.length > 1 && numeric[numeric.length - 1] - numeric[0] === numeric.length - 1) {
        return `${numeric[0]}-${numeric[numeric.length - 1]}`;
      }
      return panes.join(', ');
    }

    function renderRuns(runs) {
      const root = el('runs');
      root.replaceChildren();
      const active = runs.filter((run) => isActiveRun(run));
      if (!active.length) return root.appendChild(emptyNode('No active runs'));
      active.slice(0, 8).forEach((run) => {
        const node = document.createElement('div');
        const runClass = run.needs_attention ? 'waiting needs-attention' : statusClass(run.status);
        node.className = `item ${runClass}`;
        node.innerHTML = `
          <strong>${escapeHtml(cleanTitle(run.task_name || run.task_id, 'Run'))}${run.phase ? ` (${escapeHtml(run.phase)})` : ''}</strong>
          <div class="meta-line">${token(run.status)} <span>${escapeHtml(run.provider_id)}</span> <span>${escapeHtml(run.phase || '-')}</span></div>
          <span class="meta-code">${escapeHtml(shortId(run.id))}</span>
          ${run.needs_attention ? `<div class="attention-reason">${escapeHtml(run.attention_reason || 'Waiting for input')}</div>` : ''}
          <div class="run-actions">
            ${run.needs_attention ? `<button class="primary" type="button" data-enter-run="${escapeHtml(run.id)}">Run command</button>` : ''}
            <button class="secondary" type="button" data-focus-run="${escapeHtml(run.id)}">Focus</button>
            <button class="danger" type="button" data-stop-run="${escapeHtml(run.id)}">Stop</button>
          </div>
        `;
        root.appendChild(node);
      });
    }

    function emptyNode(text) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = text;
      return empty;
    }

    async function registerRepo() {
      const path = el('repoPath').value.trim();
      if (!path) return;
      showError('workspaceError');
      el('registerRepo').disabled = true;
      try {
        await api('/api/repos', { method: 'POST', body: JSON.stringify({ path }) });
        el('repoPath').value = '';
        await refreshAll();
      } catch (err) {
        showError('workspaceError', err.message);
      } finally {
        el('registerRepo').disabled = false;
      }
    }

    async function submitTask(event) {
      event.preventDefault();
      const message = el('message').value.trim();
      const repoId = el('repoSelect').value;
      const providerId = el('providerSelect').value;
      showError('commandError');
      if (!message) return;
      if (!repoId) return showError('commandError', 'Choose or add a repository first.');
      if (!providerId) return showError('commandError', 'Enable and select a provider first.');
      bubble(message, 'user');
      el('send').disabled = true;
      try {
        const result = await api('/api/chat', {
          method: 'POST',
          body: JSON.stringify({ message, repo_id: Number(repoId), provider_id: providerId }),
        });
        el('message').value = '';
        const dispatched = result.scheduler.dispatched.length ? result.scheduler.dispatched.join(', ') : 'waiting';
        bubble(`Workflow created: ${result.job.title}\\nProvider: ${result.provider_id}\\nDispatched: ${dispatched}`);
        await refreshAll();
      } catch (err) {
        showError('commandError', err.message);
      } finally {
        el('send').disabled = false;
      }
    }

    async function scheduleNow() {
      showError('commandError');
      el('scheduleNow').disabled = true;
      try {
        const result = await api('/api/schedule', { method: 'POST', body: '{}' });
        const dispatched = result.dispatched.length ? result.dispatched.join(', ') : 'nothing dispatched';
        bubble(`Scheduler tick: ${dispatched}`);
        await refreshAll();
      } catch (err) {
        showError('commandError', err.message);
      } finally {
        el('scheduleNow').disabled = false;
      }
    }

    async function stopRun(runId) {
      if (!runId) return;
      showError('commandError');
      try {
        await api(`/api/runs/${encodeURIComponent(runId)}/stop`, { method: 'POST', body: '{}' });
        bubble(`Stopped run: ${runId}`);
        await refreshAll();
      } catch (err) {
        showError('commandError', err.message);
      }
    }

    async function focusRun(runId) {
      if (!runId) return;
      showError('commandError');
      try {
        const result = await api(`/api/runs/${encodeURIComponent(runId)}/focus-terminal`, { method: 'POST', body: '{}' });
        bubble(`Focused pane ${result.pane_id} in ${result.session}\\n${result.attach_command}`);
      } catch (err) {
        showError('commandError', err.message);
      }
    }

    async function enterRun(runId) {
      if (!runId) return;
      showError('commandError');
      try {
        await api(`/api/runs/${encodeURIComponent(runId)}/send-enter`, { method: 'POST', body: '{}' });
        bubble(`Sent Enter to run: ${runId}`);
        await refreshAll();
      } catch (err) {
        showError('commandError', err.message);
      }
    }

    el('registerRepo').addEventListener('click', registerRepo);
    el('scheduleNow').addEventListener('click', scheduleNow);
    el('suggestCommand').addEventListener('click', () => {
      el('message').value = 'Build a frontend control panel for simulation runs';
      el('message').focus();
    });
    el('runs').addEventListener('click', (event) => {
      const stopId = event.target.dataset && event.target.dataset.stopRun;
      const focusId = event.target.dataset && event.target.dataset.focusRun;
      const enterId = event.target.dataset && event.target.dataset.enterRun;
      if (stopId) stopRun(stopId);
      if (focusId) focusRun(focusId);
      if (enterId) enterRun(enterId);
    });
    el('repoSelect').addEventListener('change', updateSelectionHint);
    el('providerSelect').addEventListener('change', updateSelectionHint);
    el('chatForm').addEventListener('submit', submitTask);
    el('message').addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        el('chatForm').requestSubmit();
      }
    });
    refreshAll();
    const source = new EventSource('/api/events/stream');
    source.onmessage = () => refreshAll();
  </script>
</body>
</html>
"""
