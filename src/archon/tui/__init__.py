"""Archon TUI package.

Two surfaces live here:

* the interactive Textual cockpit (:func:`run_app`) — the bare ``archon``
  entrypoint, with jobs tree, live detail tail, attention inbox, and a command
  bar that plans/approves/dispatches work;
* the Rich snapshot tables (:func:`watch`, :func:`show_once`, and the individual
  ``*_table`` builders) still used by ``archon status`` / ``archon up`` /
  ``archon providers``.
"""

from __future__ import annotations

from .tables import (
    attention_table,
    health_legend,
    jobs_table,
    providers_table,
    render,
    show_once,
    task_runs_table,
    watch,
    workers_table,
)

__all__ = [
    # Rich snapshot surface (legacy dashboards)
    "attention_table",
    "health_legend",
    "jobs_table",
    "providers_table",
    "render",
    "show_once",
    "task_runs_table",
    "watch",
    "workers_table",
    # Interactive Textual app
    "ArchonApp",
    "run_app",
]


def __getattr__(name: str):
    # Import Textual lazily so `archon status` (Rich only) never pays for it.
    if name in ("ArchonApp", "run_app"):
        from .app import ArchonApp, run_app
        return {"ArchonApp": ArchonApp, "run_app": run_app}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
