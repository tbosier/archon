"""Unified terminal view for Claude, Codex, Copilot, and Archon sessions."""

from __future__ import annotations

import subprocess
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, ListItem, ListView, Static

from .sessions import KNOWN_PROVIDERS, AgentSession, default_registry, launch_agent, parse_provider_suffix
from .sessions.control import (
    focus_session,
    foreground_argv,
    forget_session,
    rerun_session,
    stop_session,
)
from .sessions.model import summarize, usage_line


class ProviderPicker(ModalScreen[str | None]):
    """Pick a provider when the prompt did not include a trailing flag."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("c", "choose('claude')", "claude"),
        Binding("x", "choose('codex')", "codex"),
        Binding("p", "choose('copilot')", "copilot"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="provider-picker"):
            yield Static(Text("Run with provider", style="bold cyan"))
            yield Static(Text("[c] Claude   [x] Codex   [p] Copilot   [esc] cancel", style="dim"))

    def action_choose(self, provider: str) -> None:
        self.dismiss(provider)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AgentView(App[None]):
    """A single TUI for launching and watching all provider sessions."""

    CSS = """
    Screen {
        background: $background;
        color: $text;
    }

    #topbar {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $accent;
        text-style: bold;
    }

    #banner {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $warning 20%;
        color: $warning;
        display: none;
    }

    #banner.visible {
        display: block;
    }

    #sessions {
        width: 100%;
        height: 1fr;
        padding: 0 1;
    }

    #sessions > ListItem {
        height: 1;
    }

    #sessions > ListItem.--highlight {
        background: $accent 25%;
    }

    #command {
        dock: bottom;
        height: 3;
        border: tall $accent;
        background: $panel;
    }

    ProviderPicker {
        align: center middle;
    }

    ProviderPicker > #provider-picker {
        width: 56;
        height: auto;
        border: thick $accent;
        background: $panel;
        padding: 1 2;
    }
    """

    TITLE = "ARCHON"
    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("ctrl+r", "refresh", "refresh", show=False),
        Binding("s", "stop_selected", "stop"),
        Binding("r", "rerun_selected", "rerun"),
        Binding("f", "forget_selected", "forget"),
        Binding("enter", "enter_selected", "enter"),
        Binding("up", "focus_sessions_from_command", "sessions", show=False),
        Binding("left", "focus_command", "command", show=False),
        Binding("escape", "focus_sessions", "sessions", show=False),
    ]

    def __init__(self, *, cwd: Path | None = None, poll_interval: float = 2.0, conn=None, registry=None) -> None:
        super().__init__()
        self.cwd = (cwd or Path.cwd()).resolve()
        self.poll_interval = poll_interval
        self.conn = conn or _open_conn()
        self.registry = registry or default_registry(self.conn)
        self._sessions: list[AgentSession] = []
        self._pending_prompt: str | None = None
        self._selected_session_id: str | None = None
        self._snapshot_signature: tuple | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        yield Static(id="banner")
        yield ListView(id="sessions")
        yield Input(placeholder="Describe a task, then end with --claude, --codex, or --copilot", id="command")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#command", Input).focus()
        self.refresh_sessions()
        self.set_interval(self.poll_interval, self.refresh_sessions)

    def action_refresh(self) -> None:
        self.refresh_sessions()

    def action_focus_sessions(self) -> None:
        self.query_one("#sessions", ListView).focus()

    def action_focus_command(self) -> None:
        self.query_one("#command", Input).focus()

    def action_focus_sessions_from_command(self) -> None:
        if isinstance(self.focused, Input):
            self.query_one("#sessions", ListView).focus()

    def refresh_sessions(self) -> None:
        sessions = self.registry.snapshot()
        signature = _snapshot_signature(sessions)
        if signature == self._snapshot_signature:
            return
        focused = self.focused
        self._sessions = sessions
        self._snapshot_signature = signature
        self._render_topbar()
        view = self.query_one("#sessions", ListView)
        previous = self._selected_session_id
        view.clear()
        if not self._sessions:
            view.append(ListItem(Static(Text("No sessions yet. Type a task below.", style="dim"))))
            self._selected_session_id = None
            return
        for session in self._sessions:
            view.append(ListItem(Static(_session_line(session))))
        ids = [s.session_id for s in self._sessions]
        if previous in ids:
            view.index = ids.index(previous)
            self._selected_session_id = previous
        else:
            view.index = 0
            self._selected_session_id = self._sessions[0].session_id
        if focused is self.query_one("#command", Input):
            self.query_one("#command", Input).focus()

    @on(ListView.Highlighted, "#sessions")
    def _session_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "sessions":
            return
        index = event.list_view.index
        if index is None or index < 0 or index >= len(self._sessions):
            return
        self._selected_session_id = self._sessions[index].session_id

    @on(ListView.Selected, "#sessions")
    def _session_selected(self, event: ListView.Selected) -> None:
        self.action_enter_selected()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        event.input.value = ""
        if not raw:
            return
        routed = parse_provider_suffix(raw)
        if not routed.prompt:
            self._set_banner("Prompt is empty after provider flags.")
            return
        if routed.providers:
            self._launch_many(routed.prompt, routed.providers)
            return
        self._pending_prompt = routed.prompt
        self.push_screen(ProviderPicker(), self._provider_chosen)

    def _provider_chosen(self, provider: str | None) -> None:
        prompt = self._pending_prompt
        self._pending_prompt = None
        if provider is None or not prompt:
            return
        self._launch_many(prompt, (provider,))

    def _launch_many(self, prompt: str, providers: tuple[str, ...]) -> None:
        launched: list[str] = []
        failed: list[str] = []
        for provider in providers:
            if provider not in KNOWN_PROVIDERS:
                failed.append(provider)
                continue
            try:
                launch_agent(prompt, provider, cwd=self.cwd)
                launched.append(provider)
            except Exception:
                failed.append(provider)
        if launched:
            self._set_banner(f"launched: {', '.join(launched)}")
        if failed:
            self._set_banner(f"launch failed: {', '.join(failed)}")
        self.refresh_sessions()

    def action_stop_selected(self) -> None:
        session = self._selected()
        if session is None or session.source != "archon":
            self._set_banner("Only Archon-launched sessions can be stopped here.")
            return
        if stop_session(session.session_id):
            self._set_banner("stopped session")
            self.refresh_sessions()
        else:
            self._set_banner("session was not running")

    def action_rerun_selected(self) -> None:
        session = self._selected()
        if session is None or session.source != "archon":
            self._set_banner("Only Archon-launched sessions can be rerun here.")
            return
        result = rerun_session(session.session_id)
        if result is None:
            self._set_banner("could not rerun session")
            return
        _, provider = result
        self._set_banner(f"reran with {provider}")
        self.refresh_sessions()

    def action_forget_selected(self) -> None:
        session = self._selected()
        if session is None or session.source != "archon":
            self._set_banner("Only Archon-launched sessions can be forgotten here.")
            return
        if forget_session(session.session_id):
            self._set_banner("forgot session")
            self.refresh_sessions()
        else:
            self._set_banner("cannot forget a running session")

    def action_enter_selected(self) -> None:
        if isinstance(self.focused, Input):
            return
        session = self._selected()
        if session is None:
            return
        foreground = foreground_argv(session.session_id)
        if foreground is not None:
            argv, cwd = foreground
            with self.suspend():
                subprocess.run(argv, cwd=str(cwd), check=False)
            self.refresh_sessions()
            self.action_focus_sessions()
            return
        if session.zellij_session and (session.zellij_pane_id or session.zellij_tab_id):
            if focus_session(session.session_id):
                return
        self._set_banner("No foreground view is available for this session.")

    def _selected(self) -> AgentSession | None:
        if self._selected_session_id is None:
            return None
        for session in self._sessions:
            if session.session_id == self._selected_session_id:
                return session
        return None

    def _render_topbar(self) -> None:
        counts = summarize(self._sessions)
        text = Text.assemble(
            ("ARCHON", "bold cyan"),
            ("  ", ""),
            (f"{counts['working']} running", "green"),
            ("  ", "dim"),
            (f"{counts['need_you']} need assistance", "bold yellow"),
            ("  ", "dim"),
            (f"{counts['done']} completed", "green"),
            ("  ", "dim"),
            (f"{counts['failed']} failed", "red"),
            ("  ", "dim"),
            (str(self.cwd), "dim"),
        )
        self.query_one("#topbar", Static).update(text)

    def _set_banner(self, message: str) -> None:
        banner = self.query_one("#banner", Static)
        banner.update(message)
        banner.add_class("visible")
        self.set_timer(4.0, lambda: banner.remove_class("visible"))


def _session_line(session: AgentSession) -> Text:
    glyph, color = session.glyph
    title = session.title or session.repo or session.session_id
    summary = (session.summary or "-").replace("\n", " ")
    if len(summary) > 96:
        summary = summary[:93] + "..."
    usage = usage_line(session)
    return Text.assemble(
        (f"{glyph} ", f"bold {color}"),
        (f"{title:<28.28}", "bold"),
        ("  ", ""),
        (f"{session.provider.upper():<7}", "cyan"),
        ("  ", ""),
        (f"{session.label:<12}", "bold yellow" if session.needs_attention else color),
        ("  ", ""),
        (f"{usage:<14.14} ", "yellow" if usage else "dim"),
        (summary, "dim"),
    )


def _snapshot_signature(sessions: list[AgentSession]) -> tuple:
    return tuple(
        (
            s.session_id,
            s.provider,
            s.state.value,
            s.title,
            s.summary,
            s.updated_at,
            s.pid,
            s.provider_session_id,
            s.socket_path,
            s.source,
            s.cost_usd,
            s.ai_credits,
            s.total_tokens,
        )
        for s in sessions
    )


def run(*, cwd: Path | None = None) -> None:
    AgentView(cwd=cwd).run()


def main() -> None:
    AgentView().run()


if __name__ == "__main__":  # pragma: no cover
    main()


def _open_conn():
    try:
        from . import db
        from .paths import resolve_paths
        return db.connect(resolve_paths().ensure())
    except Exception:
        return None
