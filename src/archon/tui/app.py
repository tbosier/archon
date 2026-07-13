"""Interactive Textual dashboard — the primary `archon` (no-args) surface.

A command-first cockpit (lazygit/k9s style): an attention inbox pinned on top, a
jobs tree with live detail tail, and a command bar that runs the same
planner → policy → dispatch pipeline as ``archon do`` and previews the plan for
approval before anything launches.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, ListItem, ListView, RichLog, Static, Tree

from .. import budget, db, dispatcher, intent, projects, reconcile, scheduler
from ..backends import WorkerHandle
from ..intent import Intent
from ..models import health_of
from ..planner import PlanProposal
from . import data, planning

_ACCENT = "cyan"


class PlanModal(ModalScreen[str]):
    """Plan preview — the signature moment. Returns approve | edit | discard."""

    BINDINGS = [
        Binding("y,enter", "decide('approve')", "approve"),
        Binding("e", "decide('edit')", "edit"),
        Binding("n,escape", "decide('discard')", "discard"),
    ]

    def __init__(self, proposal: PlanProposal) -> None:
        super().__init__()
        self._proposal = proposal

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-box"):
            yield Static(_plan_renderable(self._proposal), id="plan-body")
            yield Static(
                Text("y approve   e edit   n/esc discard", style="dim"),
                id="plan-hint",
            )

    def action_decide(self, choice: str) -> None:
        self.dismiss(choice)


class AnswerModal(ModalScreen[str | None]):
    """Type a reply relayed to a worker via ``backend.send``."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="answer-box"):
            yield Static(Text(self._prompt, style="bold"))
            yield Input(placeholder="reply to the worker… (enter to send, esc to cancel)", id="answer-input")

    def on_mount(self) -> None:
        self.query_one("#answer-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    """A one-key confirm for actions that launch workers (PR review, new project)."""

    BINDINGS = [
        Binding("y,enter", "confirm(True)", "yes"),
        Binding("n,escape", "confirm(False)", "no"),
    ]

    def __init__(self, title: str, detail: str = "") -> None:
        super().__init__()
        self._title = title
        self._detail = detail

    def compose(self) -> ComposeResult:
        body = Text()
        body.append(self._title + "\n", style=f"bold {_ACCENT}")
        if self._detail:
            body.append(self._detail + "\n", style="")
        body.append("\n[y] go   [n/esc] cancel", style="dim")
        with Vertical(id="answer-box"):
            yield Static(body)

    def action_confirm(self, ok: bool) -> None:
        self.dismiss(ok)


class WelcomeScreen(ModalScreen[str]):
    """Resume-or-start launch screen. Returns 'supervise' or 'new'."""

    BINDINGS = [
        Binding("enter", "choose('supervise')", "supervise"),
        Binding("n", "choose('new')", "new task"),
        Binding("escape", "choose('supervise')", "supervise"),
    ]

    def __init__(self, summary: Text) -> None:
        super().__init__()
        self._summary = summary

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-box"):
            yield Static(self._summary, id="plan-body")
            yield Static(
                Text("[enter] keep supervising these    [n] start something new", style="dim"),
                id="plan-hint",
            )

    def action_choose(self, choice: str) -> None:
        self.dismiss(choice)


def _plan_renderable(proposal: PlanProposal) -> Text:
    if proposal.clarifying_question:
        return Text.assemble(
            ("Planner needs a clarification:\n\n", "bold yellow"),
            (proposal.clarifying_question, ""),
        )
    body = Text()
    body.append(f"{proposal.title}", style=f"bold {_ACCENT}")
    body.append(f"   risk={proposal.overall_risk}\n", style="dim")
    body.append(f"{proposal.objective}\n\n", style="")
    if proposal.acceptance_criteria:
        body.append("Acceptance: ", style="bold")
        body.append("; ".join(proposal.acceptance_criteria) + "\n\n", style="")
    body.append("Tasks\n", style="bold")
    total = 0.0
    for t in proposal.tasks:
        glyph, color, _ = health_of("queued")
        deps = f"  after {', '.join(t.depends_on)}" if t.depends_on else ""
        cost = f"  ~${t.est_cost_usd:.2f} est" if t.est_cost_usd is not None else ""
        if t.est_cost_usd is not None:
            total += t.est_cost_usd
        body.append(f"  {glyph} ", style=color)
        body.append(f"{t.phase:<7} ", style="bold")
        body.append(f"{t.tool}/{t.model_tier}", style=_ACCENT)
        body.append(f"{deps}{cost}\n", style="dim")
        body.append(f"      {t.title}\n", style="")
    if total:
        body.append(f"\n~${total:.2f} rough estimate", style="yellow")
        body.append(" — heuristic, not billed cost; actual spend updates live as workers run.\n", style="dim")
    return body


class ArchonApp(App[None]):
    """The Archon orchestration cockpit."""

    CSS_PATH = "styles.tcss"
    TITLE = "ARCHON"

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("colon,greater_than_sign", "focus_command", "command", show=True, key_display=":"),
        Binding("j,down", "nav_down", "down", show=False),
        Binding("k,up", "nav_up", "up", show=False),
        Binding("y", "attention('approve')", "approve", show=True),
        Binding("n", "attention('reject')", "reject", show=True),
        Binding("a", "attention('answer')", "answer", show=True),
        Binding("e", "attention('edit')", "edit", show=False),
        Binding("s", "stop_run", "stop", show=True),
        Binding("r", "retry_task", "retry", show=True),
        Binding("d", "show_diff", "diff", show=True),
        Binding("ctrl+r", "refresh_now", "refresh", show=False),
        Binding("escape", "focus_jobs", "jobs", show=False),
    ]

    def __init__(
        self,
        conn,
        config,
        ctx: dispatcher.RepoContext | None = None,
        *,
        poll_interval: float = 2.0,
        reconcile_interval: float = 4.0,
        planner_command: list[str] | None = None,
        auto_reconcile: bool = True,
    ) -> None:
        super().__init__()
        self.conn = conn
        self.config = config
        self.ctx = ctx
        self.poll_interval = poll_interval
        self.reconcile_interval = reconcile_interval
        self.planner_command = planner_command
        self.auto_reconcile = auto_reconcile
        self._snapshot: data.Snapshot | None = None
        self._signature: str | None = None
        self._attention_rows: list[data.AttentionRow] = []
        self._selected_task_id: str | None = None
        self._task_nodes: dict[str, data.TaskNode] = {}
        try:
            self.backend = dispatcher.backend_for_config(config)
            self._backend_error: str | None = None
        except Exception as exc:  # pragma: no cover - env dependent
            self.backend = None
            self._backend_error = str(exc)

    # -- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        yield Static(id="banner")
        yield Static(Text("ATTENTION", style="bold"), classes="section-title")
        yield ListView(id="attention")
        with Horizontal(id="body"):
            yield Tree(Text("JOBS", style="bold"), id="jobs")
            yield RichLog(id="detail", wrap=True, markup=False, highlight=False)
        yield Input(placeholder="> describe an outcome for this repo…", id="command")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#jobs", Tree).focus()
        if self._backend_error:
            self._set_banner(f"backend unavailable: {self._backend_error} — read-only view")
        elif self.ctx is None:
            self._set_banner("not inside a git repo — 'start a new project: ...' still works from the command bar")
        self.refresh_snapshot(force=True)
        self.set_interval(self.poll_interval, self.refresh_snapshot)
        # Self-advancing loop: reconcile live state → complete → dispatch next.
        if self.auto_reconcile and self.backend is not None:
            self.set_interval(self.reconcile_interval, self._reconcile_tick)
        self._maybe_welcome()

    def _maybe_welcome(self) -> None:
        """On startup, offer to resume prior work instead of a blank slate."""
        snap = self._snapshot
        if snap is None or not snap.jobs:
            self.action_focus_command()
            return
        running = sum(1 for j in snap.jobs for t in j.tasks if t.status in ("running", "starting"))
        needs = len(snap.attention)
        summary = Text()
        summary.append("Welcome back to ARCHON\n\n", style=f"bold {_ACCENT}")
        summary.append(f"{len(snap.jobs)} job(s) from previous sessions", style="")
        summary.append(f"   ·   {running} running   ·   {needs} need you\n\n", style="dim")
        for job in snap.jobs[:8]:
            mark = "●" if any(t.status in ("running", "starting") for t in job.tasks) else "○"
            summary.append(f"  {mark} {job.title}", style="")
            summary.append(f"   {job.repo_name}  [{job.status}]\n", style="dim")
        self.push_screen(WelcomeScreen(summary), self._after_welcome)

    def _after_welcome(self, choice: str | None) -> None:
        if choice == "new":
            self.action_focus_command()
        else:
            self.query_one("#jobs", Tree).focus()

    @work(thread=True, exclusive=True, group="reconcile")
    def _reconcile_tick(self) -> None:
        """Off-thread: sync backend state, advance completed work, dispatch next."""
        if self.backend is None:
            return
        try:
            result = reconcile.reconcile_once(self.conn, self.config, backend=self.backend)
        except Exception:
            return
        if result.completed or result.dispatched or result.failed or result.reconciled:
            self.call_from_thread(lambda: self.refresh_snapshot(force=True))

    # -- snapshot / rendering ---------------------------------------------
    def refresh_snapshot(self, force: bool = False) -> None:
        try:
            snap = data.build_snapshot(self.conn, self.config)
        except Exception as exc:  # pragma: no cover - defensive
            self._set_banner(f"db read failed: {exc}")
            return
        self._snapshot = snap
        self._render_topbar(snap)
        signature = _signature(snap)
        if force or signature != self._signature:
            self._signature = signature
            self._render_attention(snap)
            self._render_jobs(snap)
        self._refresh_detail_tail()

    def _render_topbar(self, snap: data.Snapshot) -> None:
        repo = self.ctx.name if self.ctx else "no-repo"
        bar = Text()
        bar.append("ARCHON", style=f"bold {_ACCENT}")
        bar.append(f"  ·  {repo}", style="dim")
        bar.append(f"   {snap.header_budget}", style="")
        color = {"allow": "green", "prefer_small": "yellow", "no_new_impl": "yellow", "pause": "red"}
        bar.append(f"  [{snap.budget_action}]", style=color.get(snap.budget_action, "white"))
        self.query_one("#topbar", Static).update(bar)

    def _render_attention(self, snap: data.Snapshot) -> None:
        view = self.query_one("#attention", ListView)
        prev = view.index
        view.clear()
        self._attention_rows = snap.attention
        for row in snap.attention:
            glyph, color, _ = health_of("blocked" if row.severity in ("warn", "critical") else "queued")
            line = Text()
            line.append(f"{glyph} ", style=color)
            line.append(f"{row.title}", style="bold")
            if row.job_title:
                line.append(f"   {row.job_title}", style="dim")
            if row.is_plan_approval:
                hint = "y approve · n reject · e edit"
            elif row.kind == "permission_denied":
                hint = "y override · n keep-blocked"
            elif row.kind == "permission_request":
                hint = "y approve · n deny · a answer"
            else:
                hint = "a answer · y · n"
            line.append(f"   [{hint}]", style=_ACCENT)
            view.append(ListItem(Label(line)))
        if not snap.attention:
            view.append(ListItem(Label(Text("(nothing needs you right now)", style="dim"))))
        if prev is not None and snap.attention:
            view.index = min(prev, len(snap.attention) - 1)

    def _render_jobs(self, snap: data.Snapshot) -> None:
        tree = self.query_one("#jobs", Tree)
        tree.clear()
        tree.root.expand()
        self._task_nodes = {}
        for job in snap.jobs:
            label = Text()
            label.append(f"{job.title}", style="bold")
            label.append(f"  {job.repo_name}", style="dim")
            if job.open_attention:
                label.append(f"  ({job.open_attention})", style="yellow")
            job_node = tree.root.add(label, expand=True)
            for task in job.tasks:
                glyph, color = task.glyph
                tlabel = Text()
                tlabel.append(f"{glyph} ", style=color)
                tlabel.append(f"{task.phase:<7} ", style="")
                tlabel.append(f"{task.tool}/{task.tier}", style="dim")
                if task.cost_usd:
                    tlabel.append(f"  ${task.cost_usd:.2f}", style="green")
                leaf = job_node.add_leaf(tlabel, data=task.id)
                self._task_nodes[task.id] = task
                if task.id == self._selected_task_id:
                    tree.select_node(leaf)
        if not snap.jobs:
            tree.root.add_leaf(Text("(no jobs yet — type an outcome below)", style="dim"))

    # -- detail pane -------------------------------------------------------
    def _current_task(self) -> data.TaskNode | None:
        if self._selected_task_id:
            return self._task_nodes.get(self._selected_task_id)
        return None

    def _render_detail(self, task: data.TaskNode) -> None:
        log = self.query_one("#detail", RichLog)
        log.clear()
        head = Text()
        head.append(f"{task.name}\n", style=f"bold {_ACCENT}")
        head.append(f"tool {task.tool} · {task.tier}", style="")
        if task.model:
            head.append(f" · {task.model}", style="dim")
        head.append(f" · ${task.cost_usd:.2f}\n", style="green")
        head.append(f"phase {task.phase} · status {task.status}\n", style="")
        if task.branch:
            head.append(f"branch {task.branch}\n", style="dim")
        head.append("─" * 40, style="dim")
        log.write(head)
        if task.session_id and self.backend is not None:
            log.write(Text("fetching worker output…", style="dim"))
            self._fetch_output(task)
        else:
            log.write(Text("(no live worker session for this task)", style="dim"))

    @work(thread=True, exclusive=True, group="detail")
    def _fetch_output(self, task: data.TaskNode) -> None:
        handle = WorkerHandle(backend_id=task.session_id, title=task.session_name or task.session_id)
        try:
            out = self.backend.output(handle, lines=200)
        except Exception as exc:  # pragma: no cover - backend dependent
            out = f"[output unavailable: {exc}]"
        self.call_from_thread(self._write_output, task.id, out)

    def _write_output(self, task_id: str, out: str) -> None:
        if task_id != self._selected_task_id:
            return
        log = self.query_one("#detail", RichLog)
        log.write(Text(out or "(no output yet)"))

    def _refresh_detail_tail(self) -> None:
        task = self._current_task()
        if task and task.session_id and self.backend is not None and task.status in ("running", "starting"):
            self._fetch_output(task)

    @on(Tree.NodeHighlighted, "#jobs")
    def _on_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        task_id = event.node.data
        if isinstance(task_id, str) and task_id in self._task_nodes:
            self._selected_task_id = task_id
            self._render_detail(self._task_nodes[task_id])

    @on(Tree.NodeSelected, "#jobs")
    def _on_node_selected(self, event: Tree.NodeSelected) -> None:
        # Enter on a task leaf attaches to its worker (Enter on a job toggles it).
        if isinstance(event.node.data, str):
            self.action_attach()

    # -- command bar / intent routing -------------------------------------
    @on(Input.Submitted, "#command")
    def _on_command(self, event: Input.Submitted) -> None:
        message = event.value.strip()
        if not message:
            return
        event.input.value = ""
        self._handle_command(message)

    @work(exclusive=True, group="command")
    async def _handle_command(self, text: str) -> None:
        known = []
        if self._snapshot:
            known = [j.title for j in self._snapshot.jobs] + [j.id for j in self._snapshot.jobs]
        routed = intent.classify(text, known_job_titles=known)
        if routed.intent is Intent.PR_REVIEW:
            await self._flow_pr_review(routed)
        elif routed.intent is Intent.NEW_PROJECT:
            await self._flow_new_project(routed)
        elif routed.intent is Intent.MESSAGE_TO_JOB:
            await self._flow_message_to_job(routed)
        else:
            await self._flow_feature(routed.message or text)

    async def _flow_feature(self, message: str) -> None:
        if self.ctx is None:
            self.notify("no repo here — try: start a new project: <description>", severity="warning", timeout=8)
            return
        self.query_one("#command", Input).disabled = True
        self._set_banner(f"planning: {message}")
        try:
            proposal = await asyncio.to_thread(
                planning.propose, self.conn, self.config, self.ctx, message,
                planner_command=self.planner_command,
            )
        except Exception as exc:
            self._set_banner("")
            self.query_one("#command", Input).disabled = False
            self.notify(f"planning failed: {exc}", severity="error", timeout=8)
            return
        self._set_banner("")
        self.query_one("#command", Input).disabled = False

        if proposal.clarifying_question:
            await self.push_screen_wait(PlanModal(proposal))
            self.query_one("#command", Input).value = message
            self.action_focus_command()
            return

        choice = await self.push_screen_wait(PlanModal(proposal))
        if choice == "approve":
            await self._dispatch_proposal(proposal)
        elif choice == "edit":
            self.query_one("#command", Input).value = f"{message} — "
            self.action_focus_command()
        else:
            self.notify("plan discarded", severity="warning")

    async def _flow_pr_review(self, routed) -> None:
        if self.ctx is None:
            self.notify("open archon inside the repo to review its PRs", severity="warning", timeout=8)
            return
        if routed.pr_number is None:
            self.notify("couldn't find a PR number in that", severity="warning")
            return
        ok = await self.push_screen_wait(ConfirmModal(
            f"Review PR #{routed.pr_number} in {self.ctx.name}?",
            "Creates an isolated worktree, checks out the PR, and dispatches a review.",
        ))
        if not ok:
            return
        providers = self.config.enabled_provider_ids()[:1] or ["claude"]
        try:
            await asyncio.to_thread(
                dispatcher.start_review, self.conn, self.config,
                ctx=self.ctx, pr_number=routed.pr_number, provider_ids=providers,
            )
        except Exception as exc:
            self.notify(f"PR review failed: {exc}", severity="error", timeout=8)
            return
        self.notify(f"dispatched review of PR #{routed.pr_number}")
        self.refresh_snapshot(force=True)

    async def _flow_new_project(self, routed) -> None:
        name = routed.project_name or "new-project"
        base = self.ctx.root.parent if self.ctx else Path.cwd()
        ok = await self.push_screen_wait(ConfirmModal(
            f"Start a new project '{name}'?",
            f"Creates {base}/{name} (git init + first commit), then plans the work.",
        ))
        if not ok:
            return
        try:
            info = await asyncio.to_thread(
                projects.create_project, base, name, description=routed.message,
            )
        except Exception as exc:
            self.notify(f"could not create project: {exc}", severity="error", timeout=8)
            return
        # Point the app at the new repo, then plan the described work in it.
        new_ctx = dispatcher.register_repo(
            self.conn, dispatcher.resolve_repo_context(info.path, config=self.config)
        )
        self.ctx = new_ctx
        self.notify(f"created {info.path}")
        self.refresh_snapshot(force=True)
        await self._flow_feature(routed.message or f"scaffold {name}")

    async def _flow_message_to_job(self, routed) -> None:
        target = self._find_job(routed.job_ref)
        if target is None:
            self.notify(f"no open job matching {routed.job_ref!r}", severity="warning")
            return
        run = self._live_run_for_job(target)
        if run is None or not run.session_id or self.backend is None:
            self.notify("that job has no live worker to message", severity="warning")
            return
        handle = WorkerHandle(backend_id=run.session_id, title=run.session_name or run.session_id)
        try:
            await asyncio.to_thread(self.backend.send, handle, routed.message)
        except Exception as exc:
            self.notify(f"could not deliver: {exc}", severity="error", timeout=8)
            return
        self.notify(f"sent to {target.title}")
        self.refresh_snapshot(force=True)

    def _find_job(self, ref: str | None):
        if not ref or not self._snapshot:
            return None
        ref_l = ref.lower()
        for job in self._snapshot.jobs:
            if ref_l in job.id.lower() or ref_l in job.title.lower():
                return job
        return None

    def _live_run_for_job(self, job) -> data.TaskNode | None:
        for task in job.tasks:
            if task.session_id and task.status in ("running", "starting", "blocked"):
                return task
        return None

    async def _dispatch_proposal(self, proposal: PlanProposal) -> None:
        try:
            outcome = await asyncio.to_thread(
                planning.dispatch, self.conn, self.config, self.ctx, proposal
            )
        except Exception as exc:
            self.notify(f"dispatch failed: {exc}", severity="error", timeout=8)
            return
        dispatched = ", ".join(outcome.dispatched) or "queued"
        self.notify(f"job {outcome.job_id} · {outcome.task_count} tasks · dispatched {dispatched}")
        self.refresh_snapshot(force=True)

    # -- attention actions -------------------------------------------------
    def _current_attention(self) -> data.AttentionRow | None:
        view = self.query_one("#attention", ListView)
        idx = view.index
        if idx is None or not self._attention_rows or idx >= len(self._attention_rows):
            return None
        return self._attention_rows[idx]

    def action_attention(self, verb: str) -> None:
        if isinstance(self.focused, Input):
            return
        row = self._current_attention()
        if row is None:
            self.notify("no attention item selected", severity="warning")
            return
        if verb == "answer":
            self._answer_worker(row)
        elif verb == "approve":
            self._resolve_attention(row, approve=True)
        elif verb == "reject":
            self._resolve_attention(row, approve=False)
        elif verb == "edit":
            self._edit_attention_plan(row)

    @work(exclusive=True, group="attention")
    async def _resolve_attention(self, row: data.AttentionRow, *, approve: bool) -> None:
        from .. import attention
        is_perm = row.kind in ("permission_request", "permission_denied")
        try:
            if row.is_plan_approval:
                if approve:
                    if row.job_id:
                        await asyncio.to_thread(self._dispatch_stored_plan, row.job_id)
                    await asyncio.to_thread(attention.resolve_item, self.conn, row.id,
                                            resolution="approve", status="resolved", unblock=True)
                    self.notify(f"approved: {row.title}")
                else:
                    await asyncio.to_thread(attention.resolve_item, self.conn, row.id,
                                            resolution="reject", status="resolved", unblock=False)
                    if row.job_id:
                        await asyncio.to_thread(db.update_job, self.conn, row.job_id, status="cancelled")
                    self.notify(f"rejected: {row.title}", severity="warning")
            elif is_perm and approve:
                # Unblock the run, and for a routine escalation actually tell the
                # live agent to proceed (real delivery, not just a DB flip).
                await asyncio.to_thread(attention.resolve_item, self.conn, row.id,
                                        resolution="approve", status="resolved", unblock=True)
                if row.kind == "permission_denied":
                    self.notify("overridden & unblocked — press Enter on the run to attach and re-issue", timeout=8)
                elif row.session_id and self.backend is not None:
                    handle = WorkerHandle(backend_id=row.session_id, title=row.session_name or row.session_id)
                    try:
                        await asyncio.to_thread(self.backend.send, handle, "approved: go ahead")
                        self.notify(f"approved & sent: {row.title}")
                    except Exception as exc:
                        self.notify(f"unblocked, but couldn't notify worker: {exc}", severity="warning", timeout=8)
                else:
                    self.notify(f"approved: {row.title}")
            elif is_perm and not approve:
                await asyncio.to_thread(attention.resolve_item, self.conn, row.id,
                                        resolution="deny", status="resolved", unblock=False)
                self.notify(f"kept blocked: {row.title}", severity="warning")
            else:
                resolution = "approve" if approve else "deny"
                await asyncio.to_thread(attention.resolve_item, self.conn, row.id,
                                        resolution=resolution, status="resolved", unblock=approve)
                self.notify(f"{resolution}d: {row.title}")
        except Exception as exc:
            self.notify(f"could not resolve: {exc}", severity="error", timeout=8)
            return
        self.refresh_snapshot(force=True)

    def _dispatch_stored_plan(self, job_id: str) -> None:
        """Approve a plan_approval item created headlessly by ``archon do``."""
        job = db.get_job(self.conn, job_id)
        if job is None or not job["current_plan_json"]:
            return
        proposal = PlanProposal.model_validate_json(job["current_plan_json"])
        planning.dispatch(self.conn, self.config, self.ctx or _ctx_from_job(job), proposal)
        db.update_job(self.conn, job_id, status="running")

    @work(exclusive=True, group="answer")
    async def _answer_worker(self, row: data.AttentionRow) -> None:
        reply = await self.push_screen_wait(AnswerModal(row.title))
        if not reply:
            return
        from .. import attention
        if row.session_id and self.backend is not None:
            handle = WorkerHandle(backend_id=row.session_id, title=row.session_name or row.session_id)
            try:
                await asyncio.to_thread(self.backend.send, handle, reply)
            except Exception as exc:
                self.notify(f"could not send: {exc}", severity="error", timeout=8)
                return
        try:
            await asyncio.to_thread(
                attention.resolve_item, self.conn, row.id,
                resolution="answered", status="resolved", unblock=True,
            )
        except Exception as exc:
            self.notify(f"resolve failed: {exc}", severity="error", timeout=8)
            return
        self.notify(f"answered: {row.title}")
        self.refresh_snapshot(force=True)

    def _edit_attention_plan(self, row: data.AttentionRow) -> None:
        if not row.is_plan_approval or not row.job_id:
            self.notify("nothing to edit here", severity="warning")
            return
        job = db.get_job(self.conn, row.job_id)
        if job is not None:
            self.query_one("#command", Input).value = job["objective"] or job["title"]
            self.action_focus_command()

    # -- run actions -------------------------------------------------------
    def action_attach(self) -> None:
        if isinstance(self.focused, Input):
            return
        task = self._current_task()
        if not task or not task.session_id or self.backend is None:
            self.notify("no worker session to attach to", severity="warning")
            return
        handle = WorkerHandle(backend_id=task.session_id, title=task.session_name or task.session_id)
        try:
            cmd = self.backend.attach_command(handle)
        except Exception as exc:
            self.notify(f"attach unavailable: {exc}", severity="error")
            return
        with self.suspend():
            subprocess.run(cmd)
        self.refresh_snapshot(force=True)

    def action_show_diff(self) -> None:
        if isinstance(self.focused, Input):
            return
        task = self._current_task()
        if not task or not task.worktree_path:
            self.notify("no worktree for this task", severity="warning")
            return
        with self.suspend():
            subprocess.run(["git", "-C", task.worktree_path, "diff"])

    @work(thread=True, exclusive=True, group="stop")
    def action_stop_run(self) -> None:
        if isinstance(self.focused, Input):
            return
        task = self._current_task()
        if not task or not task.run_id:
            self.call_from_thread(self.notify, "no run to stop", "", "warning")
            return
        if task.session_id and self.backend is not None:
            handle = WorkerHandle(backend_id=task.session_id, title=task.session_name or task.session_id)
            try:
                self.backend.stop(handle)
            except Exception:
                pass
        db.set_task_run_status(self.conn, task.run_id, "failed")
        db.set_task_status(self.conn, task.id, "failed")
        self.call_from_thread(self.notify, f"stopped {task.phase}", "", "warning")
        self.call_from_thread(lambda: self.refresh_snapshot(force=True))

    @work(thread=True, exclusive=True, group="retry")
    def action_retry_task(self) -> None:
        if isinstance(self.focused, Input):
            return
        task = self._current_task()
        if not task:
            return
        db.set_task_status(self.conn, task.id, "queued")
        launch = dispatcher.make_scheduler_launch(dry_run=False)
        scheduler.tick(self.conn, self.config, launch=launch, budget_policy=budget.policy)
        self.call_from_thread(self.notify, f"retrying {task.phase}", "", "")
        self.call_from_thread(lambda: self.refresh_snapshot(force=True))

    # -- navigation / focus ------------------------------------------------
    def action_nav_down(self) -> None:
        self._cursor("action_cursor_down")

    def action_nav_up(self) -> None:
        self._cursor("action_cursor_up")

    def _cursor(self, name: str) -> None:
        widget = self.focused
        method = getattr(widget, name, None)
        if callable(method):
            method()

    def action_focus_command(self) -> None:
        self.query_one("#command", Input).focus()

    def action_focus_jobs(self) -> None:
        self.query_one("#jobs", Tree).focus()

    def action_refresh_now(self) -> None:
        self.refresh_snapshot(force=True)

    # -- helpers -----------------------------------------------------------
    def _set_banner(self, message: str) -> None:
        banner = self.query_one("#banner", Static)
        banner.update(Text(message))
        banner.set_class(bool(message), "visible")


def _signature(snap: data.Snapshot) -> str:
    jobs = ";".join(
        f"{j.id}:{j.status}:{j.open_attention}:" + ",".join(
            f"{t.id}:{t.status}:{t.cost_usd:.2f}:{t.session_id or ''}" for t in j.tasks
        )
        for j in snap.jobs
    )
    att = ";".join(f"{a.id}:{a.kind}" for a in snap.attention)
    return f"{jobs}||{att}"


def _ctx_from_job(job) -> dispatcher.RepoContext:
    root = Path(job["repo_root_path"] or ".")
    return dispatcher.RepoContext(
        root=root,
        name=job["repo_name"] or root.name,
        session=job["zellij_session"] or f"{root.name}-archon",
        repo_id=job["repo_id"],
    )


def run_app(
    conn,
    config,
    ctx: dispatcher.RepoContext | None = None,
    *,
    poll_interval: float = 2.0,
    planner_command: list[str] | None = None,
) -> None:
    """Launch the interactive Textual dashboard (blocks until quit)."""
    ArchonApp(
        conn, config, ctx,
        poll_interval=poll_interval,
        planner_command=planner_command,
    ).run()
