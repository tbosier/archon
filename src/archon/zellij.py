"""Thin wrapper over ``zellij --session <session> action ...``.

Design goals (see spec §10):

- Every operation shells out to ``zellij`` via :func:`subprocess.run`.
- The wrapper must *never* crash the caller when zellij misbehaves. Failures are
  logged and swallowed; the cockpit keeps running.
- In ``dry_run`` mode nothing is executed. Instead each argv that *would* run is
  appended to :attr:`Zellij.commands` and sensible fake values are returned, so
  tests (and ``--dry-run`` users) can inspect the plan without a live session.
- Pane IDs from ``new-pane`` are unreliable, so :meth:`Zellij.new_pane` infers
  the new pane by diffing ``list-panes --json`` before/after creation.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

logger = logging.getLogger("archon.zellij")


def build_new_pane_argv(
    session: str,
    name: str,
    cwd: str | None,
    command: list[str],
) -> list[str]:
    """Build the argv for ``zellij action new-pane``.

    Shape::

        zellij --session S action new-pane --name NAME [--cwd CWD] -- <command...>

    The ``--`` separator ensures the provider command and its own flags are not
    parsed by zellij.
    """
    argv = ["zellij", "--session", session, "action", "new-pane", "--name", name]
    if cwd:
        argv += ["--cwd", cwd]
    if command:
        argv += ["--"] + list(command)
    return argv


class Zellij:
    """A small, forgiving wrapper around the ``zellij`` CLI."""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        # Every argv we execute (or, in dry-run, would execute) lands here so
        # callers and tests can inspect the plan.
        self.commands: list[list[str]] = []

    # -- internal helpers ---------------------------------------------------

    def _base(self, session: str) -> list[str]:
        return ["zellij", "--session", session, "action"]

    def _run(
        self,
        argv: list[str],
        *,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str] | None:
        """Record and (unless dry-run) execute an argv.

        Returns the completed process on a real run, or ``None`` in dry-run mode
        or when execution failed. Never raises.
        """
        self.commands.append(list(argv))
        if self.dry_run:
            logger.debug("dry-run zellij: %s", " ".join(argv))
            return None
        try:
            return subprocess.run(
                argv,
                check=False,
                capture_output=capture,
                text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - defensive
            logger.warning("zellij command failed (%s): %s", exc, " ".join(argv))
            return None

    # -- session lifecycle --------------------------------------------------

    def attach_or_create_background(self, session: str) -> None:
        """Ensure the session exists without stealing the current terminal.

        ``zellij attach --create-background`` creates the session detached if it
        does not exist and is a no-op otherwise.
        """
        self._run(
            ["zellij", "attach", "--create-background", session],
        )

    def attach(self, session: str) -> None:
        """Attach the current terminal to the session (foreground)."""
        self._run(["zellij", "attach", session])

    # -- pane discovery -----------------------------------------------------

    def list_panes(self, session: str) -> list[dict]:
        """Return the panes reported by ``list-panes --json``.

        Returns ``[]`` on dry-run, failure, or unparseable output. The zellij
        JSON groups panes by tab; we flatten it into a single list of pane dicts,
        annotating each with its ``tab`` name when present.
        """
        argv = self._base(session) + ["list-panes", "--json"]
        proc = self._run(argv, capture=True)
        if proc is None or proc.returncode != 0 or not proc.stdout:
            return []
        return _parse_list_panes(proc.stdout)

    # -- pane creation ------------------------------------------------------

    def new_pane(
        self,
        session: str,
        name: str,
        cwd: str | None,
        command: list[str],
    ) -> str | None:
        """Create a pane and return its zellij pane id (best effort).

        zellij's ``new-pane`` does not reliably print a pane id, so we diff
        ``list-panes`` before and after creation and match the newcomer by
        name/cwd/command. Returns ``None`` if the id cannot be determined
        (including in dry-run mode).
        """
        before = self.list_panes(session)
        before_ids = {_pane_key(p) for p in before}

        argv = build_new_pane_argv(session, name, cwd, command)
        self._run(argv)

        if self.dry_run:
            return None

        after = self.list_panes(session)
        # Prefer a genuinely new pane id (allowing the single-newcomer shortcut).
        new_panes = [p for p in after if _pane_key(p) not in before_ids]
        candidate = _match_pane(new_panes, name, cwd, command, allow_single_fallback=True)
        if candidate is None:
            # Fall back to any pane matching the requested *name*. Do not grab an
            # unrelated single pane here — that would return a wrong id.
            candidate = _match_pane(after, name, cwd, command, allow_single_fallback=False)
        if candidate is None:
            logger.info("could not resolve pane id for new pane %r", name)
            return None
        return _pane_id(candidate)

    # -- pane interaction ---------------------------------------------------

    def paste(self, session: str, pane_id: str, text: str) -> None:
        """Paste text into a pane (does not press Enter)."""
        self._run(
            self._base(session) + ["write-chars", "--pane-id", pane_id, text],
        )

    def send_enter(self, session: str, pane_id: str) -> None:
        """Send a single Enter/newline keypress to a pane."""
        # 13 == carriage return; ``write`` takes raw byte values.
        self._run(
            self._base(session) + ["write", "--pane-id", pane_id, "13"],
        )

    def focus_pane(self, session: str, pane_id: str) -> None:
        self._run(
            self._base(session) + ["focus-pane-with-id", pane_id],
        )

    def rename_pane(self, session: str, pane_id: str, name: str) -> None:
        self._run(
            self._base(session) + ["rename-pane-with-id", pane_id, name],
        )

    def set_pane_color(
        self,
        session: str,
        pane_id: str,
        fg: str | None = None,
        bg: str | None = None,
    ) -> None:
        """Best-effort pane recolour.

        zellij has no stable public "set pane colour by id" action across
        versions, so this is intentionally lenient: it records the intent (useful
        for dry-run/tests and event logs) and attempts a frame-colour action.
        Failure is swallowed like every other zellij call.
        """
        argv = self._base(session) + ["set-pane-color", "--pane-id", pane_id]
        if fg:
            argv += ["--fg", fg]
        if bg:
            argv += ["--bg", bg]
        self._run(argv)

    def close_pane(self, session: str, pane_id: str) -> None:
        self._run(
            self._base(session) + ["close-pane-with-id", pane_id],
        )

    def dump_screen(self, session: str, pane_id: str, path: str) -> None:
        """Dump a pane's screen contents to ``path`` (for stale/hung debugging)."""
        self._run(
            self._base(session)
            + ["dump-screen", "--pane-id", pane_id, path],
        )


# --- module-level parsing helpers ----------------------------------------


def _parse_list_panes(stdout: str) -> list[dict]:
    """Flatten ``list-panes --json`` output into a list of pane dicts.

    zellij versions differ in shape. We handle:

    - a JSON object mapping tab-name -> list-of-panes
    - a top-level JSON list of panes
    - newline-delimited JSON objects (one pane per line)
    """
    stdout = stdout.strip()
    if not stdout:
        return []

    # Try a single JSON document first.
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return _parse_ndjson(stdout)

    panes: list[dict] = []
    if isinstance(data, dict):
        for tab, tab_panes in data.items():
            if isinstance(tab_panes, list):
                for pane in tab_panes:
                    if isinstance(pane, dict):
                        pane.setdefault("tab", tab)
                        panes.append(pane)
    elif isinstance(data, list):
        panes = [p for p in data if isinstance(p, dict)]
    return panes


def _parse_ndjson(stdout: str) -> list[dict]:
    panes: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            panes.append(obj)
    return panes


def _pane_id(pane: dict) -> str | None:
    for key in ("id", "pane_id", "paneId", "terminal_id"):
        value = pane.get(key)
        if value is not None:
            return str(value)
    return None


def _pane_key(pane: dict) -> Any:
    """A hashable identity for a pane, for before/after diffing."""
    pid = _pane_id(pane)
    if pid is not None:
        return pid
    # No id available: fall back to a tuple of identifying fields.
    return (pane.get("title") or pane.get("name"), pane.get("cwd"))


def _match_pane(
    panes: list[dict],
    name: str,
    cwd: str | None,
    command: list[str],
    allow_single_fallback: bool = False,
) -> dict | None:
    """Pick the pane that best matches the identity we just created.

    ``allow_single_fallback`` permits returning the sole candidate even when its
    title doesn't match the requested name — only safe for the "new panes" diff
    set, where a single newcomer is almost certainly ours.
    """
    if not panes:
        return None

    def matches_name(pane: dict) -> bool:
        title = pane.get("title") or pane.get("name") or ""
        return name in str(title)

    named = [p for p in panes if matches_name(p)]
    if len(named) == 1:
        return named[0]
    if named and cwd:
        for pane in named:
            if pane.get("cwd") and str(pane["cwd"]).rstrip("/") == cwd.rstrip("/"):
                return pane
    if named:
        return named[0]
    # Nothing matched by name.
    if allow_single_fallback and len(panes) == 1:
        return panes[0]
    return None
