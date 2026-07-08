"""Task dispatch: turn a `review-pr`/`feature` request into task runs.

The dispatcher is the seam between Archon's state (DB/config) and the outside
world (Git worktrees, Zellij panes, provider CLIs). It owns the "one task run =
one provider = one worktree = one branch/pane" invariant.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from . import db, github, prompts
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


def resolve_repo_context(repo_arg: str | Path | None, *, session: str | None = None) -> RepoContext:
    root = repo_root(repo_arg or Path.cwd())
    return RepoContext(
        root=root,
        name=root.name,
        session=session or session_name_for(root),
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
        zellij.paste(ctx.session, pane_id, launch.prompt)
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
