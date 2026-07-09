"""Task dispatch: turn a `review-pr`/`feature` request into task runs.

The dispatcher is the seam between Archon's state (DB/config) and the outside
world (Git worktrees, Zellij panes, provider CLIs). It owns the "one task run =
one provider = one worktree = one branch/pane" invariant.
"""

from __future__ import annotations

import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

# Interactive CLIs (Claude/Copilot) need their TUI to finish booting before we
# paste, and a beat between the paste and Enter or the submit is swallowed while
# the paste is still rendering. Tunable via env for slow machines.
PANE_BOOT_DELAY = float(os.environ.get("ARCHON_PANE_BOOT_DELAY", "3.5"))
PANE_ENTER_DELAY = float(os.environ.get("ARCHON_PANE_ENTER_DELAY", "0.8"))

from . import db, github, handoff, prompts
from .config import Config
from .git_worktree import (
    WorktreeInfo,
    create_feature_worktree,
    create_pr_review_worktree,
    default_base_branch,
    repo_root,
)
from .models import Repo, Task, TaskRun
from .providers.base import ProviderLaunch
from .providers.registry import get_provider
from .util import is_dry_run, new_task_id, run_id_for, sanitize_slug
from .zellij import Zellij


class DispatchError(RuntimeError):
    """Raised for user-facing dispatch problems (ambiguous provider, etc.)."""


@dataclass
class RepoContext:
    root: Path
    name: str
    session: str
    repo_id: int | None = None


@dataclass
class DispatchResult:
    task: Task
    runs: list[TaskRun] = field(default_factory=list)
    launches: dict[str, ProviderLaunch] = field(default_factory=dict)
    worktrees: dict[str, WorktreeInfo] = field(default_factory=dict)


def session_name_for(repo_root_path: Path) -> str:
    return f"{sanitize_slug(repo_root_path.name)}-archon"


def cockpit_session(config: Config | None, repo_root_path: Path) -> str:
    """The Zellij session a repo's agents belong to.

    With a shared command center (default), every repo lands in one session so
    you watch all agents on a single screen; otherwise each repo gets its own.
    """
    if config and config.command_center.shared:
        return config.command_center.session
    return session_name_for(repo_root_path)


def resolve_repo_context(
    repo_arg: str | Path | None,
    *,
    session: str | None = None,
    config: Config | None = None,
) -> RepoContext:
    root = repo_root(repo_arg or Path.cwd())
    return RepoContext(
        root=root,
        name=root.name,
        session=session or cockpit_session(config, root),
    )


def register_repo(conn, ctx: RepoContext) -> RepoContext:
    ctx.repo_id = db.upsert_repo(
        conn, Repo(name=ctx.name, root_path=str(ctx.root), zellij_session=ctx.session)
    )
    return ctx


def build_pane_command(launch: ProviderLaunch) -> list[str]:
    """Wrap a provider launch (argv + env) into a single `bash -lc` command so
    the ARCHON_* environment is present inside the Zellij pane."""
    exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in launch.env.items())
    inner = " ".join(shlex.quote(a) for a in launch.argv)
    script = f"{exports} exec {inner}" if exports else f"exec {inner}"
    return ["bash", "-lc", script]


def _launch_run(
    conn,
    zellij: Zellij,
    ctx: RepoContext,
    run: TaskRun,
    launch: ProviderLaunch,
    *,
    dry_run: bool,
) -> None:
    """Open the pane, record the pane id, inject the prompt, mark running."""
    # Ensure the cockpit session exists so dispatching works without a prior
    # `archon up` (idempotent no-op if it already exists).
    zellij.attach_or_create_background(ctx.session)
    pane_command = build_pane_command(launch)
    pane_id = zellij.new_pane(
        session=ctx.session,
        name=run.zellij_pane_name or f"{run.provider_id}-run",
        cwd=str(launch.cwd),
        command=pane_command,
    )
    if pane_id:
        run.zellij_pane_id = pane_id
        db.update_task_run(conn, run.id, zellij_pane_id=pane_id)

    if launch.expects_prompt_paste and launch.prompt and pane_id:
        # Give the provider's TUI time to boot before pasting, then a beat before
        # Enter so the submit isn't swallowed while the paste is still rendering.
        if not dry_run:
            time.sleep(PANE_BOOT_DELAY)
        zellij.paste(ctx.session, pane_id, launch.prompt)
        if not dry_run:
            time.sleep(PANE_ENTER_DELAY)
        zellij.send_enter(ctx.session, pane_id)

    db.set_task_run_status(conn, run.id, "running")
    run.status = "running"
    db.insert_event(
        conn,
        event_type="task_run_launched",
        severity="info",
        message=f"launched {run.provider_id} in pane {pane_id or '(pending)'}",
        task_id=run.task_id,
        task_run_id=run.id,
        provider_id=run.provider_id,
    )


def start_review(
    conn,
    config: Config,
    *,
    ctx: RepoContext,
    pr_number: int,
    provider_ids: list[str],
    base: str | None = None,
    dry_run: bool | None = None,
    zellij: Zellij | None = None,
) -> DispatchResult:
    """Create a PR-review task with one read-only worktree/run per provider."""
    dry = is_dry_run(dry_run)
    zellij = zellij or Zellij(dry_run=dry)
    base = base or default_base_branch(ctx.root)

    task = Task(
        id=new_task_id(),
        repo_id=ctx.repo_id or 0,
        type="pr_review",
        name=f"PR #{pr_number} review",
        status="running",
        prompt=f"Review PR #{pr_number}",
        provider_policy="multi_review" if len(provider_ids) > 1 else "single",
        pr_number=pr_number,
    )
    db.insert_task(conn, task)
    result = DispatchResult(task=task)

    if not dry:
        github.fetch(ctx.root, dry_run=dry)

    for pid in provider_ids:
        provider = get_provider(pid, config)
        wt = create_pr_review_worktree(ctx.root, pr_number, base, pid, dry_run=dry)
        result.worktrees[pid] = wt
        if not dry:
            github.pr_checkout(pr_number, wt.branch, wt.path, dry_run=dry)

        run = TaskRun(
            id=run_id_for(task.id, pid),
            task_id=task.id,
            provider_id=pid,
            status="starting",
            branch=wt.branch,
            base_branch=wt.base_branch,
            worktree_path=str(wt.path),
            zellij_session=ctx.session,
            zellij_pane_name=f"{pid}-pr-{pr_number}-review",
        )
        db.insert_task_run(conn, run)

        prompt = prompts.pr_review_prompt(
            pr_number=pr_number,
            repo_name=ctx.name,
            provider_name=provider.display_name,
            worktree_path=str(wt.path),
            branch=wt.branch,
        )
        launch = provider.worker_launch(run, prompt, purpose="review")
        result.launches[pid] = launch
        result.runs.append(run)
        _launch_run(conn, zellij, ctx, run, launch, dry_run=dry)

    return result


def start_feature(
    conn,
    config: Config,
    *,
    ctx: RepoContext,
    feature_name: str,
    provider_ids: list[str],
    branch: str | None = None,
    base: str | None = None,
    prompt_text: str | None = None,
    variants: bool = False,
    dry_run: bool | None = None,
    zellij: Zellij | None = None,
) -> DispatchResult:
    """Create a feature task. One writer by default; `variants` for multiple."""
    dry = is_dry_run(dry_run)
    zellij = zellij or Zellij(dry_run=dry)
    base = base or default_base_branch(ctx.root)

    if len(provider_ids) > 1 and not variants:
        raise DispatchError(
            "Multiple providers were selected for a feature. Only one provider "
            "should write to a feature branch. Pass --variants to create separate "
            "variant worktrees/branches, or choose a single provider."
        )

    multi = len(provider_ids) > 1
    task = Task(
        id=new_task_id(),
        repo_id=ctx.repo_id or 0,
        type="feature",
        name=feature_name,
        status="running",
        prompt=prompt_text or f"Implement {feature_name}",
        provider_policy="variants" if multi else "single",
    )
    db.insert_task(conn, task)
    result = DispatchResult(task=task)

    if not dry:
        github.fetch(ctx.root, dry_run=dry)

    for pid in provider_ids:
        provider = get_provider(pid, config)
        wt = create_feature_worktree(
            ctx.root,
            feature_name,
            branch if not multi else None,
            base,
            pid if multi else None,
            variants=multi,
            dry_run=dry,
        )
        result.worktrees[pid] = wt

        run = TaskRun(
            id=run_id_for(task.id, pid),
            task_id=task.id,
            provider_id=pid,
            status="starting",
            branch=wt.branch,
            base_branch=wt.base_branch,
            worktree_path=str(wt.path),
            zellij_session=ctx.session,
            zellij_pane_name=f"{pid}-feature-{sanitize_slug(feature_name)}",
        )
        db.insert_task_run(conn, run)

        prompt = prompts.feature_prompt(
            feature_name=feature_name,
            repo_name=ctx.name,
            provider_name=provider.display_name,
            worktree_path=str(wt.path),
            branch=wt.branch,
            feature_description=prompt_text,
        )
        launch = provider.worker_launch(run, prompt, purpose="feature")
        result.launches[pid] = launch
        result.runs.append(run)
        _launch_run(conn, zellij, ctx, run, launch, dry_run=dry)

    return result


# --------------------------------------------------------------------------- #
# Queue-driven engine: enqueue a feature chain, and launch queued tasks.
# `launch_task` is the callback the scheduler drives.
# --------------------------------------------------------------------------- #

# phase -> (codex sandbox purpose, worktree write intent)
_PHASE_PURPOSE = {"plan": "review", "review": "review", "execute": "feature", "test": "feature"}


def repo_context_from_task(conn, task_row) -> RepoContext:
    row = conn.execute("SELECT * FROM repos WHERE id=?", (task_row["repo_id"],)).fetchone()
    if not row:
        raise DispatchError(f"repo {task_row['repo_id']} not found for task {task_row['id']}")
    root = Path(row["root_path"])
    return RepoContext(root=root, name=root.name, session=row["zellij_session"], repo_id=row["id"])


def _base_feature_name(conn, task_row) -> str:
    """The feature name shared by every phase of a chain (drives worktree/branch)."""
    if task_row["parent_task_id"]:
        parent = db.get_task(conn, task_row["parent_task_id"])
        if parent:
            return parent["name"]
    name = task_row["name"]
    for suffix in (" (review)", " (test)", " (plan)"):
        name = name.replace(suffix, "")
    return name


def _phase_prompt(phase, ctx, provider, wt, feature_name, task_prompt) -> str:
    if phase == "plan":
        return prompts.plan_prompt(
            feature_name=feature_name, repo_name=ctx.name, provider_name=provider.display_name,
            worktree_path=str(wt.path), branch=wt.branch, feature_description=task_prompt,
        )
    if phase == "review":
        return prompts.branch_review_prompt(
            branch=wt.branch, repo_name=ctx.name, provider_name=provider.display_name,
            worktree_path=str(wt.path), base_branch=wt.base_branch,
        )
    if phase == "test":
        return prompts.test_prompt(
            feature_name=feature_name, repo_name=ctx.name, provider_name=provider.display_name,
            worktree_path=str(wt.path), branch=wt.branch,
        )
    return prompts.feature_prompt(
        feature_name=feature_name, repo_name=ctx.name, provider_name=provider.display_name,
        worktree_path=str(wt.path), branch=wt.branch, feature_description=task_prompt,
    )


def launch_task(conn, config: Config, task_row, *, zellij: Zellij | None = None,
                dry_run: bool | None = None) -> TaskRun:
    """Create and launch one run for a queued task (the scheduler's launch fn).

    All phases of a feature share one worktree/branch, so plan informs execute and
    review/test see the implemented change. The provider is model-tiered by phase.
    """
    dry = is_dry_run(dry_run)
    zellij = zellij or Zellij(dry_run=dry)
    ctx = repo_context_from_task(conn, task_row)
    provider_id = task_row["provider_id"] or (config.enabled_provider_ids() or ["claude"])[0]
    provider = get_provider(provider_id, config)
    phase = task_row["phase"] or "execute"
    feature_name = _base_feature_name(conn, task_row)
    base = default_base_branch(ctx.root)
    wt = create_feature_worktree(ctx.root, feature_name, None, base, None, variants=False, dry_run=dry)

    run = TaskRun(
        id=run_id_for(task_row["id"], provider_id),
        task_id=task_row["id"],
        provider_id=provider_id,
        status="starting",
        phase=phase,
        branch=wt.branch,
        base_branch=wt.base_branch,
        worktree_path=str(wt.path),
        zellij_session=ctx.session,
        zellij_pane_name=f"{provider_id}-{phase}-{sanitize_slug(feature_name)}",
    )
    db.insert_task_run(conn, run)

    prompt = _phase_prompt(phase, ctx, provider, wt, feature_name, task_row["prompt"])
    launch = provider.worker_launch(run, prompt, purpose=_PHASE_PURPOSE.get(phase, "feature"))
    if run.model:
        db.update_task_run(conn, run.id, model=run.model)
    _launch_run(conn, zellij, ctx, run, launch, dry_run=dry)
    return run


def make_scheduler_launch(zellij: Zellij | None = None, dry_run: bool | None = None):
    """Return a ``launch(conn, config, task_row)`` closure for ``scheduler.tick``."""
    def _launch(conn, config, task_row) -> None:
        launch_task(conn, config, task_row, zellij=zellij, dry_run=dry_run)
    return _launch


def enqueue_feature(conn, config: Config, ctx: RepoContext, *, feature_name: str,
                    provider_id: str, prompt_text: str | None = None) -> dict:
    """Queue a feature as a plan -> execute chain (handoff adds review -> test)."""
    return handoff.enqueue_feature_chain(
        conn, config, repo_id=ctx.repo_id or 0, feature_name=feature_name,
        prompt=prompt_text or f"Implement {feature_name}", provider_id=provider_id,
    )


def complete_task(conn, config: Config, task_id: str) -> dict:
    """Mark a task (and its live runs) done; trigger the reviewer/tester handoff."""
    task = db.get_task(conn, task_id)
    if not task:
        raise DispatchError(f"task {task_id} not found")
    for r in db.list_task_runs(conn):
        if r["task_id"] == task_id and r["status"] in ("running", "starting", "blocked", "stale"):
            db.set_task_run_status(conn, r["id"], "done")
    db.set_task_status(conn, task_id, "done")

    created: dict = {}
    if task["type"] == "feature" and task["phase"] == "execute":
        created = handoff.on_feature_done(conn, config, task)
    return {"task": task_id, "handoff": created}
