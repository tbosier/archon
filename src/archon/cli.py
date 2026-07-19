"""Archon command-line interface.

`archon do` plans and dispatches work; `archon status` shows the dashboard.
Every command that touches the outside world honours `--dry-run` (and the
``ARCHON_DRY_RUN`` env var).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .backends import WorkerHandle
from . import (
    attention,
    budget,
    db,
    dispatcher,
    hooks,
    jobs,
    planner,
    policy,
    queue,
    scheduler,
    statusline,
    taskgraph,
    transcript_index,
)
from .config import Config, load_config, save_config
from .models import Provider, Worker
from .paths import resolve_paths
from .provider_health import check_all
from .provider_login import login_launch_for
from .provider_wizard import run_provider_wizard
from .providers.registry import get_provider, known_provider_ids, known_providers
from .util import is_dry_run

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Archon — one terminal view for Claude, Codex, Copilot, and other coding agents.",
)
providers_app = typer.Typer(no_args_is_help=True, help="Inspect and manage provider CLIs.")
jobs_app = typer.Typer(no_args_is_help=True, help="Create and inspect control-center jobs.")
attention_app = typer.Typer(no_args_is_help=True, help="Inspect and resolve attention items.")
app.add_typer(providers_app, name="providers")
app.add_typer(jobs_app, name="jobs", hidden=True)
app.add_typer(attention_app, name="attention", hidden=True)

console = Console()
err = Console(stderr=True)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Launch the unified agent view when `archon` runs bare."""
    if ctx.invoked_subcommand is not None:
        return
    from .agent_view import run
    run()


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _open(dry: bool = False):
    """Open config + database.

    In dry-run we use a throwaway in-memory database so a preview never writes
    tasks/runs into the real store (which would otherwise skew concurrency and
    leave phantom "running" rows behind).
    """
    paths = resolve_paths().ensure()
    config = load_config(paths)
    conn = db.connect_memory() if dry else db.connect(paths)
    return paths, conn, config


def _sync_providers_to_db(conn, config: Config) -> None:
    """Reflect config + live health into the providers table."""
    health = check_all(known_providers())
    for pid in known_provider_ids():
        pc = config.providers.get(pid)
        h = health.get(pid)
        conn_provider = Provider(
            id=pid,
            display_name=(pc.display_name if pc else pid),
            command=(pc.command if pc else pid),
            enabled=bool(pc.enabled) if pc else False,
            installed=bool(h.installed) if h else False,
            auth_status=(h.auth_status if h else "unknown"),
            default_mode=(pc.default_mode if pc else "interactive"),
            login_command=(pc.login_command if pc else None),
            last_checked_at=None,
        )
        from .util import utc_now
        conn_provider.last_checked_at = utc_now()
        db.upsert_provider(conn, conn_provider)


def _resolve_providers(
    config: Config,
    explicit: list[str],
    *,
    all_providers: bool,
    ask: bool,
    kind: str,
    variants: bool,
) -> list[str]:
    """Decide which providers run a task, prompting only when truly ambiguous."""
    enabled = config.enabled_provider_ids()
    if explicit:
        chosen = explicit
    elif all_providers:
        chosen = enabled
    elif len(enabled) == 1:
        chosen = enabled
    elif len(enabled) == 0:
        raise typer.BadParameter("No providers are enabled. Run `archon setup` first.")
    else:
        if not sys.stdin.isatty() or not ask:
            raise typer.BadParameter(
                f"Multiple providers are enabled ({', '.join(enabled)}). "
                "Specify one or more with --provider, or --all-providers."
            )
        chosen = _prompt_providers(enabled, kind=kind)

    if kind == "feature" and len(chosen) > 1 and not variants:
        raise typer.BadParameter(
            "A feature defaults to a single writer provider. Pass --variants to "
            "run multiple providers as separate variant branches."
        )
    return chosen


def _prompt_providers(enabled: list[str], *, kind: str) -> list[str]:
    try:
        import questionary
        if kind == "feature":
            answer = questionary.select(
                "Which provider should implement this feature?", choices=enabled
            ).ask()
            return [answer] if answer else []
        answers = questionary.checkbox(
            "Which provider(s) should run this task?", choices=enabled
        ).ask()
        return answers or []
    except Exception:  # pragma: no cover - fallback path
        console.print(f"Enabled providers: {', '.join(enabled)}")
        raw = console.input("Enter provider(s), comma-separated: ")
        return [p.strip() for p in raw.split(",") if p.strip()]


# --------------------------------------------------------------------------- #
# Lifecycle commands
# --------------------------------------------------------------------------- #

@app.command()
def init() -> None:
    """Initialise Archon config + database (idempotent)."""
    paths = resolve_paths().ensure()
    conn = db.connect(paths)
    config = load_config(paths)
    if not paths.config_file.exists():
        save_config(config, paths)
    _sync_providers_to_db(conn, config)
    console.print("[green]archon initialised[/green]")
    console.print(f"  config: {paths.config_file}")
    console.print(f"  data:   {paths.db_file}")


@app.command()
def setup(
    repo: Optional[Path] = typer.Option(None, help="Repository root."),
    inside_zellij: bool = typer.Option(False, "--inside-zellij", hidden=True),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Run the provider-selection wizard and save the choice."""
    paths, conn, config = _open()
    config = run_provider_wizard(repo or Path.cwd(), config, interactive=sys.stdin.isatty())
    save_config(config, paths)
    _sync_providers_to_db(conn, config)
    console.print(f"[green]saved providers:[/green] {', '.join(config.enabled_provider_ids()) or '(none)'}")


@app.command(hidden=True)
def server(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8716, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Start the local HTTP/SSE API for the control center."""
    try:
        import uvicorn
    except ModuleNotFoundError:
        raise typer.BadParameter("FastAPI server dependencies are not installed. Reinstall Archon from pyproject.toml.") from None
    console.print(f"[cyan]archon server[/cyan] http://{host}:{port}")
    uvicorn.run("archon.api:app", host=host, port=port, reload=reload)


@app.command(hidden=True)
def web(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8716, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Start the local API used by the browser control center."""
    server(host=host, port=port, reload=reload)


@app.command(hidden=True)
def up(
    repo: Optional[Path] = typer.Option(None, help="Repository root."),
    session: Optional[str] = typer.Option(None, help="Legacy session name stored with the repo."),
    provider: list[str] = typer.Option(None, "--provider", help="Enable a provider (repeatable)."),
    all_providers: bool = typer.Option(False, "--all-providers"),
    ask_providers: bool = typer.Option(False, "--ask-providers", help="Always show the selector."),
    skip_provider_prompt: bool = typer.Option(False, "--skip-provider-prompt"),
    spawn_provider_panes: bool = typer.Option(False, "--spawn-provider-panes"),
    spawn_on_task: bool = typer.Option(False, "--spawn-on-task"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Compatibility alias: initialise repo/provider state and show status."""
    dry = is_dry_run(dry_run)
    paths, conn, config = _open(dry)
    ctx = dispatcher.resolve_repo_context(repo, session=session, config=config)
    dispatcher.register_repo(conn, ctx)

    needs_wizard = ask_providers or (not config.is_configured() and config.startup.show_provider_wizard != "never")
    if needs_wizard and not skip_provider_prompt:
        preselect = provider or None
        config = run_provider_wizard(
            ctx.root, config, interactive=sys.stdin.isatty(), preselect=preselect
        )
        save_config(config, paths)
    elif provider:
        for pid in provider:
            if pid in config.providers:
                config.providers[pid].enabled = True
        save_config(config, paths)

    if not config.is_configured() and skip_provider_prompt:
        raise typer.BadParameter("No providers configured and --skip-provider-prompt was given.")

    _sync_providers_to_db(conn, config)

    launch_now = config.startup.provider_panes == "launch_now"
    if spawn_provider_panes:
        launch_now = True
    if spawn_on_task:
        launch_now = False

    scope = "command center (shared)" if config.command_center.shared else "per-repo"
    console.print(f"[cyan]archon up[/cyan]  repo={ctx.root}  backend={config.backend.kind} [dim]({scope})[/dim]  dry_run={dry}")
    health_all = check_all(known_providers())
    for pid in config.enabled_provider_ids():
        health = health_all.get(pid)
        if health and not health.installed:
            console.print(f"  [yellow]{pid}: not installed[/yellow] — install it, then `archon providers refresh`")
            continue
        # Register an idle worker in the pool for each ready provider.
        per_provider = config.scheduler.per_provider_concurrency
        db.upsert_worker(conn, Worker(id=f"{pid}-w1", provider_id=pid,
                                      zellij_session=ctx.session, state="idle",
                                      max_concurrency=per_provider))
        if health and health.auth_status == "needs_login":
            launch = login_launch_for(pid, config, repo=ctx.root)
            console.print(f"  [yellow]{pid}: login required[/yellow] — run: {' '.join(launch.argv)}")
        elif launch_now:
            console.print(f"  [green]{pid}: ready[/green] (idle worker)")
        else:
            console.print(f"  [dim]{pid}: will spawn on task[/dim]")

    from . import tui
    tui.show_once(conn, console)
    console.print("\n[dim]v2 entrypoint:[/dim] [cyan]archon do \"your outcome\" --repo PATH[/cyan]")


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #

@providers_app.callback(invoke_without_command=True)
def providers_root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _, conn, config = _open()
    _sync_providers_to_db(conn, config)
    from . import tui
    console.print(tui.providers_table(conn))


@providers_app.command("doctor")
def providers_doctor() -> None:
    """Best-effort install + auth check for every known provider."""
    health = check_all(known_providers())
    for pid, h in health.items():
        mark = "green" if h.installed else "red"
        console.print(f"[{mark}]{pid}[/{mark}]  installed={h.installed}  auth={h.auth_status}  cmd={h.command}")
        if h.notes:
            console.print(f"    [dim]{h.notes}[/dim]")


@providers_app.command("enable")
def providers_enable(ids: list[str]) -> None:
    paths, conn, config = _open()
    for pid in ids:
        if pid not in config.providers:
            raise typer.BadParameter(f"Unknown provider: {pid}")
        config.providers[pid].enabled = True
    save_config(config, paths)
    _sync_providers_to_db(conn, config)
    console.print(f"[green]enabled:[/green] {', '.join(ids)}")


@providers_app.command("disable")
def providers_disable(ids: list[str]) -> None:
    paths, conn, config = _open()
    for pid in ids:
        if pid in config.providers:
            config.providers[pid].enabled = False
    save_config(config, paths)
    _sync_providers_to_db(conn, config)
    console.print(f"[yellow]disabled:[/yellow] {', '.join(ids)}")


@providers_app.command("refresh")
def providers_refresh() -> None:
    _, conn, config = _open()
    _sync_providers_to_db(conn, config)
    console.print("[green]provider status refreshed[/green]")


@providers_app.command("login")
def providers_login(
    provider_id: str,
    repo: Optional[Path] = typer.Option(None),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Print the provider's native login command."""
    dry = is_dry_run(dry_run)
    _, conn, config = _open(dry)
    launch = login_launch_for(provider_id, config, repo=repo or Path.cwd())
    console.print(f"[cyan]{provider_id} login command:[/cyan] {' '.join(launch.argv)}")
    console.print("[dim]complete the provider login, then run `archon providers refresh`[/dim]")


# --------------------------------------------------------------------------- #
# Jobs / attention
# --------------------------------------------------------------------------- #

@jobs_app.callback(invoke_without_command=True)
def jobs_root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _, conn, _ = _open()
    rows = db.list_jobs(conn)
    if not rows:
        console.print("[dim]no jobs yet[/dim]")
        return
    for row in rows:
        console.print(
            f"[cyan]{row['id']}[/cyan]  {row['status']:<22} "
            f"{row['repo_name'] or '-':<18} {row['title']}"
        )


@jobs_app.command("create")
def jobs_create(
    title: str,
    repo: Optional[Path] = typer.Option(None, help="Repository root."),
    objective: Optional[str] = typer.Option(None, "--objective", "-o"),
    constraint: list[str] = typer.Option(None, "--constraint", "-c"),
    acceptance: list[str] = typer.Option(None, "--acceptance", "-a"),
    provider_id: Optional[str] = typer.Option(None, "--provider"),
) -> None:
    """Create a durable job from structured input."""
    _, conn, config = _open()
    ctx = dispatcher.register_repo(conn, dispatcher.resolve_repo_context(repo, config=config))
    job = jobs.create_job(
        conn,
        repo_id=ctx.repo_id or 0,
        title=title,
        objective=objective or title,
        constraints=constraint or [],
        acceptance_criteria=acceptance or [],
        status="intake",
        provider_id=provider_id,
    )
    console.print(f"[green]created[/green] {job.id}  {job.title}")


@jobs_app.command("show")
def jobs_show(job_id: str) -> None:
    """Show a job with its tasks, agents, and open decisions."""
    _, conn, _ = _open()
    row = db.get_job(conn, job_id)
    if row is None:
        raise typer.BadParameter(f"No job found for '{job_id}'.")
    console.print(f"[bold]{row['title']}[/bold]  [cyan]{row['id']}[/cyan]  {row['status']}")
    console.print(f"repo: {row['repo_name'] or '-'}")
    console.print(f"objective: {row['objective']}")
    tasks = db.list_job_tasks(conn, job_id)
    if tasks:
        console.print("\n[bold]tasks[/bold]")
        for t in tasks:
            console.print(f"  {t['id']}  {t['phase']:<7} {t['status']:<10} {t['name']}")
    agents_rows = db.list_agents(conn, job_id=job_id)
    if agents_rows:
        console.print("\n[bold]agents[/bold]")
        for a in agents_rows:
            console.print(f"  {a['id']}  {a['state']:<18} {a['display_name']}")
    open_items = db.list_attention_items(conn, status="open", job_id=job_id)
    if open_items:
        console.print("\n[bold yellow]attention[/bold yellow]")
        for item in open_items:
            console.print(f"  {item['id']}  {item['kind']:<20} {item['title']}")


@attention_app.callback(invoke_without_command=True)
def attention_root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _, conn, _ = _open()
    rows = db.list_attention_items(conn, status="open")
    if not rows:
        console.print("[dim]no open attention items[/dim]")
        return
    for item in rows:
        console.print(
            f"[yellow]{item['id']}[/yellow]  {item['kind']:<22} "
            f"{item['severity']:<6} {item['title']}"
        )


@attention_app.command("resolve")
def attention_resolve(
    item_id: str,
    resolution: str = typer.Option("approved", "--resolution", "-r"),
    status: str = typer.Option("resolved", "--status"),
    no_unblock: bool = typer.Option(False, "--no-unblock"),
) -> None:
    """Resolve an attention item and unblock its run by default."""
    _, conn, _ = _open()
    try:
        attention.resolve_item(
            conn,
            item_id,
            resolution=resolution,
            status=status,
            unblock=not no_unblock,
        )
    except KeyError:
        raise typer.BadParameter(f"No attention item found for '{item_id}'.") from None
    console.print(f"[green]{status}[/green] {item_id}")


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #

@app.command("do", hidden=True)
def do_cmd(
    message: str,
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repository root."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Approve and dispatch without prompting."),
    plan_only: bool = typer.Option(False, "--plan-only", help="Only print the plan preview."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Plan a natural-language outcome, optionally approve it, then dispatch."""
    dry = is_dry_run(dry_run)
    _, conn, config = _open(dry)
    ctx = dispatcher.register_repo(conn, dispatcher.resolve_repo_context(repo, config=config))

    try:
        if dry:
            proposal = planner.heuristic_plan(message, repo_path=ctx.root, config=config)
        else:
            proposal = planner.plan_with_llm(
                message,
                repo_path=ctx.root,
                config=config,
                recent_job_titles=planner.recent_job_titles(conn, ctx.repo_id or 0),
            )
        policy.validate_plan(proposal, config)
    except Exception as exc:
        err.print(f"[red]planning failed:[/red] {exc}")
        raise typer.Exit(1)

    console.print(planner.render_plan(proposal))
    if proposal.clarifying_question:
        raise typer.Exit(0)
    if plan_only:
        return

    needs_approval = policy.requires_approval(proposal, config, yes=yes)
    if needs_approval:
        if not sys.stdin.isatty():
            if not dry:
                job = jobs.create_job(
                    conn,
                    repo_id=ctx.repo_id or 0,
                    title=proposal.title,
                    objective=proposal.objective,
                    constraints=proposal.constraints,
                    acceptance_criteria=proposal.acceptance_criteria,
                    status="awaiting_plan_approval",
                    current_plan=proposal.model_dump(mode="json"),
                )
                attention.open_item(
                    conn,
                    kind="plan_approval",
                    severity="warn" if proposal.overall_risk == "high" else "info",
                    title=f"approve plan: {proposal.title}",
                    summary=planner.render_plan(proposal),
                    job_id=job.id,
                    options=["approve", "reject", "edit"],
                    recommended_option="approve" if proposal.overall_risk != "high" else "inspect",
                )
            raise typer.BadParameter("Plan requires approval. Re-run with --yes for low/medium risk or approve from attention.")
        if not typer.confirm("Approve this plan and dispatch?"):
            console.print("[yellow]plan discarded[/yellow]")
            return

    if proposal.overall_risk == "high" and yes and not sys.stdin.isatty():
        raise typer.BadParameter("High-risk plans require interactive approval.")

    job, tasks = planner.persist_plan(conn, config, ctx, proposal)
    launch = dispatcher.make_scheduler_launch(dry_run=dry)
    decision = scheduler.tick(conn, config, launch=launch, budget_policy=budget.policy)
    tag = "[yellow](dry-run)[/yellow] " if dry else ""
    console.print(f"{tag}[green]job[/green] {job.id}  tasks={len(tasks)}")
    console.print(f"dispatched={decision.dispatched or '-'}  skipped={decision.skipped or '-'}")


@app.command("review-pr", hidden=True)
def review_pr(
    pr_number: int,
    repo: Optional[Path] = typer.Option(None),
    provider: list[str] = typer.Option(None, "--provider"),
    all_providers: bool = typer.Option(False, "--all-providers"),
    base: Optional[str] = typer.Option(None, "--base"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Dispatch a PR review, one isolated read-only worktree per provider."""
    dry = is_dry_run(dry_run)
    paths, conn, config = _open(dry)
    ctx = dispatcher.register_repo(conn, dispatcher.resolve_repo_context(repo, config=config))
    provider_ids = _resolve_providers(
        config, provider or [], all_providers=all_providers, ask=True,
        kind="review", variants=True,
    )
    try:
        result = dispatcher.start_review(
            conn, config, ctx=ctx, pr_number=pr_number, provider_ids=provider_ids,
            base=base, dry_run=dry,
        )
    except dispatcher.DispatchError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    _print_dispatch(result, dry)


@app.command(hidden=True)
def feature(
    name: str,
    repo: Optional[Path] = typer.Option(None),
    provider: list[str] = typer.Option(None, "--provider"),
    branch: Optional[str] = typer.Option(None, "--branch"),
    base: Optional[str] = typer.Option(None, "--base"),
    prompt: Optional[str] = typer.Option(None, "--prompt"),
    variants: bool = typer.Option(False, "--variants"),
    now: bool = typer.Option(False, "--now", help="Dispatch immediately, skip the plan→execute queue."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Queue a feature as plan → execute (→ review → test on completion).

    A single provider gets the model-tiered chain; `--variants` runs multiple
    providers as immediate parallel variant branches instead.
    """
    dry = is_dry_run(dry_run)
    paths, conn, config = _open(dry)
    ctx = dispatcher.register_repo(conn, dispatcher.resolve_repo_context(repo, config=config))
    provider_ids = _resolve_providers(
        config, provider or [], all_providers=False, ask=True,
        kind="feature", variants=variants,
    )

    # Variants (or explicit --now) use the immediate multi-writer path.
    if len(provider_ids) > 1 or now:
        try:
            result = dispatcher.start_feature(
                conn, config, ctx=ctx, feature_name=name, provider_ids=provider_ids,
                branch=branch, base=base, prompt_text=prompt, variants=variants, dry_run=dry,
            )
        except dispatcher.DispatchError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        _print_dispatch(result, dry)
        return

    # Single provider: enqueue the model-tiered chain and dispatch what's ready.
    chain = dispatcher.enqueue_feature(
        conn, config, ctx, feature_name=name, provider_id=provider_ids[0], prompt_text=prompt
    )
    launch = dispatcher.make_scheduler_launch(dry_run=dry)
    decision = scheduler.tick(conn, config, launch=launch, budget_policy=budget.policy)

    tag = "[yellow](dry-run)[/yellow] " if dry else ""
    console.print(f"{tag}[bold]{name}[/bold] queued as a plan → execute → review → test chain")
    plan_t, exec_t = chain.get("plan"), chain.get("execute")
    if plan_t:
        console.print(f"  plan     {plan_t.id}  [cyan]{provider_ids[0]}[/cyan] (strong model)")
    console.print(f"  execute  {exec_t.id}  [cyan]{provider_ids[0]}[/cyan] (execute model)")
    console.print(f"  [dim]dispatched now:[/dim] {', '.join(decision.dispatched) or '(waiting)'}")
    console.print("  [dim]run `archon complete <task>` as phases finish, or `archon schedule --watch`.[/dim]")


def _print_dispatch(result: dispatcher.DispatchResult, dry: bool) -> None:
    tag = "[yellow](dry-run)[/yellow] " if dry else ""
    console.print(f"{tag}[bold]{result.task.name}[/bold]  ({result.task.id})")
    for run in result.runs:
        wt = result.worktrees.get(run.provider_id)
        console.print(f"  [cyan]{run.provider_id}[/cyan]  branch={run.branch}  worktree={wt.path if wt else '-'}")
        launch = result.launches.get(run.provider_id)
        if launch:
            console.print(f"    launch: [dim]{' '.join(launch.argv)}[/dim]")


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #

@app.command()
def agents() -> None:
    """Open the unified multi-provider agent view."""
    from .agent_view import run
    run()


@app.command()
def dashboard() -> None:
    """Open the unified agent dashboard."""
    from .agent_view import run
    run()


@app.command()
def sessions(
    watch: bool = typer.Option(False, "--watch", help="Refresh on an interval."),
    interval: float = typer.Option(2.0, "--interval"),
) -> None:
    """Unified view of every agent session — Claude, Copilot, and Archon's own.

    Discovers sessions you launched yourself (from each CLI's on-disk state), not
    just ones Archon dispatched, and shows which need you.
    """
    _, conn, _ = _open()
    from . import tui
    from .sessions import default_registry

    registry = default_registry(conn)
    if not watch:
        tui.show_sessions(registry.snapshot(), console)
        return
    import time as _time
    try:
        while True:
            console.clear()
            tui.show_sessions(registry.snapshot(), console)
            _time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]archon: sessions view closed[/dim]")


@app.command(hidden=True)
def status(watch: bool = typer.Option(False, "--watch")) -> None:
    """Show provider readiness and task-run state (all repos)."""
    _, conn, config = _open()
    _sync_providers_to_db(conn, config)
    from . import tui
    if watch:
        tui.watch(conn)
    else:
        tui.show_once(conn, console)


@app.command(hidden=True)
def tui(inside_zellij: bool = typer.Option(False, "--inside-zellij", hidden=True)) -> None:
    """Live dashboard (Ctrl-C to exit)."""
    _, conn, config = _open()
    _sync_providers_to_db(conn, config)
    from . import tui as _tui
    _tui.watch(conn)


@app.command(hidden=True)
def focus(selector: str) -> None:
    """Print the backend attach command for a task/run selector."""
    _, conn, config = _open()
    row = _find_run(conn, selector)
    if not row or not row["provider_session_id"]:
        raise typer.BadParameter(f"No backend session found for '{selector}'.")
    backend = dispatcher.backend_for_config(config)
    handle = WorkerHandle(
        backend_id=row["provider_session_id"],
        title=row["provider_session_name"] or row["provider_session_id"],
    )
    console.print(" ".join(backend.attach_command(handle)))


@app.command(hidden=True)
def stop(
    selector: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Stop a task run through the configured backend after confirmation."""
    _, conn, config = _open()
    row = _find_run(conn, selector)
    if not row:
        raise typer.BadParameter(f"No task run found for '{selector}'.")
    if not yes and not typer.confirm(f"Stop {row['id']} ({row['provider_session_id'] or 'no backend session'})?"):
        raise typer.Abort()
    if row["provider_session_id"]:
        backend = dispatcher.backend_for_config(config)
        backend.stop(
            WorkerHandle(
                backend_id=row["provider_session_id"],
                title=row["provider_session_name"] or row["provider_session_id"],
            )
        )
    db.set_task_run_status(conn, row["id"], "failed")
    console.print(f"[yellow]stopped[/yellow] {row['id']}")


def _find_run(conn, selector: str):
    for r in db.list_task_runs(conn):
        if selector in (r["id"], r["zellij_pane_name"], r["task_id"], r["task_name"]):
            return r
    return None


@app.command(hidden=True)
def search(query: str, since: Optional[str] = typer.Option(None, "--since")) -> None:
    """Full-text search across transcripts and logs."""
    _, conn, _ = _open()
    hits = transcript_index.search(conn, query, since=since)
    if not hits:
        console.print("[dim]no matches[/dim]")
        return
    for h in hits:
        console.print(f"[cyan]{h.get('task_run_id','-')}[/cyan] | {h.get('provider_id','-')} | {h.get('file_path','-')}")
        if h.get("excerpt"):
            console.print(f"  [dim]{h['excerpt']}[/dim]")


@app.command(hidden=True)
def touched(file_path: str) -> None:
    """Show task runs that touched a file."""
    _, conn, _ = _open()
    rows = transcript_index.touched(conn, file_path)
    if not rows:
        console.print("[dim]no file touches recorded[/dim]")
        return
    for r in rows:
        console.print(f"{r.get('task_run_id','-')} | {r.get('provider_id','-')} | {r.get('action','-')} | {r.get('file_path')}")


# --------------------------------------------------------------------------- #
# Queue / scheduler / budget
# --------------------------------------------------------------------------- #

@app.command("queue", hidden=True)
def queue_cmd() -> None:
    """Show queued and ready tasks."""
    _, conn, _ = _open()
    ready = {r["id"] for r in queue.ready_tasks(conn)}
    rows = [t for t in db.list_tasks(conn) if t["status"] == "queued"]
    if not rows:
        console.print("[dim]queue empty[/dim]")
        return
    console.print(f"[bold]{len(rows)} queued[/bold] ({len(ready)} ready to dispatch)")
    for t in rows:
        state = "[green]ready[/green]" if t["id"] in ready else "[yellow]waiting on deps[/yellow]"
        console.print(f"  {t['id']}  {t['phase']:<7} {t['name']:<28} {t['provider_id'] or '-':<8} {state}")


@app.command(hidden=True)
def graph() -> None:
    """Render the task dependency graph (plan → execute → review → test)."""
    _, conn, _ = _open()
    cycle = taskgraph.detect_cycle(conn)
    if cycle:
        err.print(f"[red]dependency cycle detected:[/red] {' → '.join(cycle)}")
    text = taskgraph.ascii_graph(conn)
    console.print(text if text.strip() else "[dim]no tasks yet[/dim]")


@app.command("budget", hidden=True)
def budget_cmd() -> None:
    """Show the current budget / rate-limit status and dispatch action."""
    _, conn, config = _open()
    status = budget.evaluate(conn, config)
    color = {"allow": "green", "prefer_small": "yellow", "no_new_impl": "yellow", "pause": "red"}
    console.print(f"[{color.get(status.action,'white')}]{budget.describe(status)}[/]")


@app.command(hidden=True)
def schedule(
    watch: bool = typer.Option(False, "--watch", help="Keep dispatching on an interval."),
    interval: float = typer.Option(3.0, "--interval"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Dispatch ready tasks, gated by concurrency and the budget policy."""
    dry = is_dry_run(dry_run)
    _, conn, config = _open(dry)
    launch = dispatcher.make_scheduler_launch(dry_run=dry)

    def _one() -> None:
        d = scheduler.tick(conn, config, launch=launch, budget_policy=budget.policy)
        if d.paused:
            console.print(f"[yellow]paused[/yellow] ({d.reason})")
        else:
            console.print(
                f"budget={d.budget_action}  dispatched={d.dispatched or '-'}  skipped={d.skipped or '-'}"
            )

    if not watch:
        _one()
        return
    import time
    try:
        while True:
            _one()
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]scheduler stopped[/dim]")


@app.command(hidden=True)
def complete(
    selector: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Mark a task done and trigger the reviewer/tester handoff + next dispatch."""
    dry = is_dry_run(dry_run)
    _, conn, config = _open(dry)
    task = _find_task(conn, selector)
    if not task:
        raise typer.BadParameter(f"No task found for '{selector}'.")
    result = dispatcher.complete_task(conn, config, task["id"])
    console.print(f"[green]completed[/green] {task['id']} ({task['phase']})")
    for kind, t in (result.get("handoff") or {}).items():
        if t:
            console.print(f"  [cyan]handoff[/cyan] → {kind}: {t.id}")
    launch = dispatcher.make_scheduler_launch(dry_run=dry)
    d = scheduler.tick(conn, config, launch=launch, budget_policy=budget.policy)
    if d.dispatched:
        console.print(f"  [dim]dispatched next:[/dim] {', '.join(d.dispatched)}")


@app.command(hidden=True)
def pause() -> None:
    """Pause the scheduler (no new dispatches until resumed)."""
    _, conn, _ = _open()
    scheduler.pause(conn)
    console.print("[yellow]scheduler paused[/yellow]")


@app.command(hidden=True)
def resume() -> None:
    """Resume the scheduler."""
    _, conn, _ = _open()
    scheduler.resume(conn)
    console.print("[green]scheduler resumed[/green]")


def _find_task(conn, selector: str):
    for t in db.list_tasks(conn):
        if selector in (t["id"], t["name"]):
            return t
    return None


# --------------------------------------------------------------------------- #
# Provider integration entry points (called by statuslines/hooks)
# --------------------------------------------------------------------------- #

@app.command(name="statusline", hidden=True)
def statusline_cmd() -> None:
    """Read provider statusline JSON from stdin; print a one-line status."""
    statusline.main()


@app.command(hidden=True)
def hook(hook_name: str) -> None:
    """Ingest a provider hook payload from stdin."""
    hooks.main(hook_name)


if __name__ == "__main__":  # pragma: no cover
    app()
