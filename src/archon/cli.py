"""Archon command-line interface.

`archon up` launches the cockpit; `archon review-pr` / `archon feature` dispatch
work; `archon status` shows the dashboard. Every command that touches the outside
world honours `--dry-run` (and the ``ARCHON_DRY_RUN`` env var).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from . import db, dispatcher, hooks, statusline, transcript_index
from .config import Config, load_config, save_config
from .models import Provider
from .paths import resolve_paths
from .provider_health import check_all
from .provider_login import login_launch_for, login_pane_name
from .provider_wizard import run_provider_wizard
from .providers.registry import get_provider, known_provider_ids, known_providers
from .util import is_dry_run
from .zellij import Zellij

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Archon — a Zellij-native command center for parallel AI coding agents.",
)
providers_app = typer.Typer(no_args_is_help=True, help="Inspect and manage provider CLIs.")
app.add_typer(providers_app, name="providers")

console = Console()
err = Console(stderr=True)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _open():
    paths = resolve_paths().ensure()
    conn = db.connect(paths)
    config = load_config(paths)
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


@app.command()
def up(
    repo: Optional[Path] = typer.Option(None, help="Repository root."),
    session: Optional[str] = typer.Option(None, help="Zellij session name."),
    provider: list[str] = typer.Option(None, "--provider", help="Enable a provider (repeatable)."),
    all_providers: bool = typer.Option(False, "--all-providers"),
    ask_providers: bool = typer.Option(False, "--ask-providers", help="Always show the selector."),
    skip_provider_prompt: bool = typer.Option(False, "--skip-provider-prompt"),
    spawn_provider_panes: bool = typer.Option(False, "--spawn-provider-panes"),
    spawn_on_task: bool = typer.Option(False, "--spawn-on-task"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Start (or attach to) the cockpit for a repository."""
    paths, conn, config = _open()
    dry = is_dry_run(dry_run)
    ctx = dispatcher.resolve_repo_context(repo, session=session)
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

    zellij = Zellij(dry_run=dry)
    zellij.attach_or_create_background(ctx.session)

    launch_now = config.startup.provider_panes == "launch_now"
    if spawn_provider_panes:
        launch_now = True
    if spawn_on_task:
        launch_now = False

    console.print(f"[cyan]archon up[/cyan]  repo={ctx.root}  session={ctx.session}  dry_run={dry}")
    for pid in config.enabled_provider_ids():
        health = check_all(known_providers()).get(pid)
        if health and not health.installed:
            console.print(f"  [yellow]{pid}: not installed[/yellow] — install it, then `archon providers refresh`")
            continue
        if health and health.auth_status == "needs_login":
            launch = login_launch_for(pid, config, repo=ctx.root)
            zellij.new_pane(ctx.session, login_pane_name(pid), str(ctx.root),
                            ["bash", "-lc", " ".join(launch.argv)])
            console.print(f"  [yellow]{pid}: opened login pane[/yellow] ({login_pane_name(pid)})")
        elif launch_now:
            console.print(f"  [green]{pid}: ready[/green] (worker pane)")
        else:
            console.print(f"  [dim]{pid}: will spawn on task[/dim]")

    from . import tui
    tui.show_once(conn, console)


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
    """Open a Zellij pane running the provider's native login command."""
    _, conn, config = _open()
    dry = is_dry_run(dry_run)
    launch = login_launch_for(provider_id, config, repo=repo or Path.cwd())
    ctx = dispatcher.resolve_repo_context(repo)
    zellij = Zellij(dry_run=dry)
    zellij.attach_or_create_background(ctx.session)
    zellij.new_pane(ctx.session, login_pane_name(provider_id), str(ctx.root),
                    ["bash", "-lc", " ".join(launch.argv)])
    console.print(f"[cyan]login pane opened for {provider_id}[/cyan] — complete the flow, then `archon providers refresh`")


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #

@app.command("review-pr")
def review_pr(
    pr_number: int,
    repo: Optional[Path] = typer.Option(None),
    provider: list[str] = typer.Option(None, "--provider"),
    all_providers: bool = typer.Option(False, "--all-providers"),
    base: Optional[str] = typer.Option(None, "--base"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Dispatch a PR review, one isolated read-only worktree per provider."""
    paths, conn, config = _open()
    dry = is_dry_run(dry_run)
    ctx = dispatcher.register_repo(conn, dispatcher.resolve_repo_context(repo))
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


@app.command()
def feature(
    name: str,
    repo: Optional[Path] = typer.Option(None),
    provider: list[str] = typer.Option(None, "--provider"),
    branch: Optional[str] = typer.Option(None, "--branch"),
    base: Optional[str] = typer.Option(None, "--base"),
    prompt: Optional[str] = typer.Option(None, "--prompt"),
    variants: bool = typer.Option(False, "--variants"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Dispatch a feature implementation (single writer unless --variants)."""
    paths, conn, config = _open()
    dry = is_dry_run(dry_run)
    ctx = dispatcher.register_repo(conn, dispatcher.resolve_repo_context(repo))
    provider_ids = _resolve_providers(
        config, provider or [], all_providers=False, ask=True,
        kind="feature", variants=variants,
    )
    try:
        result = dispatcher.start_feature(
            conn, config, ctx=ctx, feature_name=name, provider_ids=provider_ids,
            branch=branch, base=base, prompt_text=prompt, variants=variants, dry_run=dry,
        )
    except dispatcher.DispatchError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    _print_dispatch(result, dry)


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
def status(watch: bool = typer.Option(False, "--watch")) -> None:
    """Show provider readiness and task-run state."""
    _, conn, config = _open()
    _sync_providers_to_db(conn, config)
    from . import tui
    if watch:
        tui.watch(conn)
    else:
        tui.show_once(conn, console)


@app.command()
def tui(inside_zellij: bool = typer.Option(False, "--inside-zellij", hidden=True)) -> None:
    """Live dashboard (Ctrl-C to exit)."""
    _, conn, config = _open()
    _sync_providers_to_db(conn, config)
    from . import tui as _tui
    _tui.watch(conn)


@app.command()
def focus(selector: str) -> None:
    """Focus the Zellij pane for a task/run selector."""
    _, conn, _ = _open()
    row = _find_run(conn, selector)
    if not row or not row["zellij_pane_id"]:
        raise typer.BadParameter(f"No pane found for '{selector}'.")
    Zellij().focus_pane(row["zellij_session"], row["zellij_pane_id"])
    console.print(f"[cyan]focused[/cyan] {row['id']} → pane {row['zellij_pane_id']}")


@app.command()
def stop(
    selector: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Stop a task run (Ctrl-C its pane) after confirmation."""
    _, conn, _ = _open()
    row = _find_run(conn, selector)
    if not row:
        raise typer.BadParameter(f"No task run found for '{selector}'.")
    if not yes and not typer.confirm(f"Stop {row['id']} (pane {row['zellij_pane_id']})?"):
        raise typer.Abort()
    z = Zellij()
    if row["zellij_pane_id"]:
        z.close_pane(row["zellij_session"], row["zellij_pane_id"])
    db.set_task_run_status(conn, row["id"], "failed")
    console.print(f"[yellow]stopped[/yellow] {row['id']}")


def _find_run(conn, selector: str):
    for r in db.list_task_runs(conn):
        if selector in (r["id"], r["zellij_pane_name"], r["task_id"], r["task_name"]):
            return r
    return None


@app.command()
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


@app.command()
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
# Provider integration entry points (called by statuslines/hooks)
# --------------------------------------------------------------------------- #

@app.command(name="statusline")
def statusline_cmd() -> None:
    """Read provider statusline JSON from stdin; print a one-line status."""
    statusline.main()


@app.command()
def hook(hook_name: str) -> None:
    """Ingest a provider hook payload from stdin."""
    hooks.main(hook_name)


if __name__ == "__main__":  # pragma: no cover
    app()
