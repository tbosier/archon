"""``archon hook`` handler (build spec §7.11).

Provider hooks (initially Claude Code) invoke ``archon hook <HookName>`` and pipe
a JSON payload on stdin. We record an event (DB + events.jsonl + hooks.log),
classify severity, infer the task-run, and for permission prompts mark the run
blocked and route attention (pane colour/focus is the caller's job; we send the
desktop notification here).

Hard rule: this must NEVER crash the provider CLI. Every path is defensive and
``main`` catches everything, exiting 0.
"""

from __future__ import annotations

import json
import os
import sys

from . import db, notify
from .paths import resolve_paths
from .statusline import infer_task_run_id
from .util import append_event_line, utc_now

# Hooks that represent a human-approval gate: mark the run blocked.
_PERMISSION_HOOKS = {"PermissionRequest", "PermissionPrompt", "PreToolUsePermission"}


def _load_payload(stdin_text: str) -> tuple[dict, bool]:
    """Parse stdin JSON. Returns (payload, ok). Malformed -> ({}, False)."""
    if not stdin_text or not stdin_text.strip():
        return {}, True
    try:
        data = json.loads(stdin_text)
    except (ValueError, TypeError):
        return {}, False
    return (data if isinstance(data, dict) else {}), True


def classify_severity(hook_name: str, payload: dict) -> str:
    """Map a hook to one of info | warn | error | critical."""
    name = (hook_name or "").strip()
    payload = payload if isinstance(payload, dict) else {}

    if name in _PERMISSION_HOOKS:
        return "warn"
    if name in ("StopFailure", "Error", "ProviderError"):
        return "error"
    if name == "Notification":
        # Honour an explicit level if the provider sent one.
        level = str(payload.get("level") or payload.get("severity") or "").lower()
        if level in ("warn", "warning"):
            return "warn"
        if level in ("error", "critical"):
            return "error"
        return "info"
    if name in ("Stop", "SessionEnd", "SessionStart"):
        return "info"
    if name == "ProviderEvent":
        level = str(payload.get("severity") or "").lower()
        if level in ("info", "warn", "error", "critical"):
            return level
        return "info"
    return "info"


def handle_hook(
    hook_name: str,
    stdin_text: str,
    conn=None,
    *,
    env: dict | None = None,
    paths=None,
) -> dict:
    """Record a hook event and update task-run state. Returns a summary dict.

    Summary: ``{"hook", "severity", "task_run_id", "blocked"}``.
    Tolerates malformed JSON (still records an event) and missing DB.
    """
    if env is None:
        env = dict(os.environ)
    if paths is None:
        try:
            paths = resolve_paths()
        except Exception:
            paths = None

    payload, parsed_ok = _load_payload(stdin_text)
    severity = classify_severity(hook_name, payload)
    if not parsed_ok:
        # Malformed input is itself worth flagging, but never fatal.
        severity = "warn" if severity == "info" else severity

    run_id = None
    task_id = env.get("ARCHON_TASK_ID")
    provider_id = env.get("ARCHON_PROVIDER_ID") or (
        payload.get("provider_id") if isinstance(payload, dict) else None
    )

    if conn is not None:
        try:
            run_id = infer_task_run_id(conn, payload, env)
        except Exception:
            run_id = None

    blocked = (hook_name or "").strip() in _PERMISSION_HOOKS

    message = None
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("text")
    if message is None:
        message = f"hook {hook_name}" if parsed_ok else f"hook {hook_name} (malformed payload)"

    raw_json = stdin_text if isinstance(stdin_text, str) else ""

    # 1. DB event.
    if conn is not None:
        try:
            db.insert_event(
                conn,
                event_type=f"hook.{hook_name}",
                severity=severity,
                message=str(message)[:2000] if message is not None else None,
                task_id=task_id,
                task_run_id=run_id,
                provider_id=str(provider_id) if provider_id else None,
                raw_json=raw_json[:20000] if raw_json else None,
            )
        except Exception:
            pass

    # 2. events.jsonl + hooks.log (best-effort).
    record = {
        "ts": utc_now(),
        "hook": hook_name,
        "severity": severity,
        "task_run_id": run_id,
        "task_id": task_id,
        "provider_id": str(provider_id) if provider_id else None,
        "blocked": blocked,
        "message": str(message) if message is not None else None,
    }
    if paths is not None:
        try:
            append_event_line(paths.events_file, record)
        except Exception:
            pass
        try:
            append_event_line(paths.hooks_log, record)
        except Exception:
            pass

    # 3. Permission prompts -> mark run blocked + route attention.
    if blocked:
        if conn is not None and run_id:
            try:
                db.set_task_run_status(conn, run_id, "blocked")
            except Exception:
                pass
        try:
            notify.notify(
                "Archon: permission needed",
                str(message) if message is not None else f"{hook_name}",
                urgency="critical",
            )
        except Exception:
            pass

    return {
        "hook": hook_name,
        "severity": severity,
        "task_run_id": run_id,
        "blocked": blocked,
    }


def main(hook_name: str) -> None:
    """CLI entry: read stdin, real DB, record. Never crashes the provider."""
    try:
        stdin_text = sys.stdin.read()
    except Exception:
        stdin_text = ""

    conn = None
    try:
        conn = db.connect()
        handle_hook(hook_name, stdin_text, conn)
    except Exception:
        try:
            handle_hook(hook_name, stdin_text, None)
        except Exception:
            pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    sys.exit(0)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ProviderEvent")
