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

from . import attention, db, notify, permissions
from .paths import resolve_paths
from .statusline import infer_task_run_id
from .util import append_event_line, utc_now

# Hooks that represent a tool-use / human-approval gate: run the policy engine.
_PERMISSION_HOOKS = {
    "PermissionRequest", "PermissionPrompt", "PreToolUsePermission", "PreToolUse",
}
# Hooks that signal an agent finished its turn / the session ended: mark the run
# done so the reconcile loop advances the plan (execute -> review -> test).
_COMPLETION_HOOKS = {"Stop", "SessionEnd"}


def _extract_command(payload: dict) -> str | None:
    """Best-effort pull of the shell command from a tool-use hook payload.

    Covers the common Claude Code shapes; returns None if no command is found
    (an unparseable request fails safe to ESCALATE downstream).
    """
    if not isinstance(payload, dict):
        return None
    for path in (
        ("tool_input", "command"),
        ("tool", "input", "command"),
        ("input", "command"),
        ("params", "command"),
        ("toolInput", "command"),
    ):
        cur: object = payload
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                cur = None
                break
        if isinstance(cur, str):
            return cur
    for key in ("command", "cmd", "bash_command"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


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

    name = (hook_name or "").strip()
    is_permission = name in _PERMISSION_HOOKS
    is_completion = name in _COMPLETION_HOOKS

    message = None
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("text")
    if message is None:
        message = f"hook {hook_name}" if parsed_ok else f"hook {hook_name} (malformed payload)"

    raw_json = stdin_text if isinstance(stdin_text, str) else ""

    # Team-lead policy verdict (pure) — computed up front so the event log and the
    # return value carry the accurate blocked/decision, side effects applied below.
    verdict = None
    decision: str | None = None
    label = str(message) if message is not None else hook_name
    if is_permission:
        command = _extract_command(payload)
        worktree = None
        if conn is not None and run_id:
            try:
                r = db.find_task_run(conn, run_id)
                worktree = r["worktree_path"] if r is not None else None
            except Exception:
                worktree = None
        verdict = permissions.evaluate_permission(command or "", worktree_path=worktree)
        decision = verdict.decision.value
        label = command or label
    blocked = decision in ("deny", "escalate")

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

    # 3a. Completion signal -> mark the run done so reconcile advances the plan.
    if is_completion and conn is not None and run_id:
        try:
            db.set_task_run_status(conn, run_id, "done")
        except Exception:
            pass

    # 3b. Apply the policy verdict's side effects.
    if verdict is not None:
        if verdict.decision is permissions.Decision.ALLOW:
            # Auto-approve: never touch attention, never block. Record for audit.
            if conn is not None:
                try:
                    db.insert_event(
                        conn,
                        event_type="permission.auto_approved",
                        severity="info",
                        message=label,
                        task_id=task_id,
                        task_run_id=run_id,
                        provider_id=str(provider_id) if provider_id else None,
                        summary=f"auto-approved [{verdict.matched_rule}]: {verdict.reason}",
                    )
                except Exception:
                    pass
        else:
            # DENY or ESCALATE: block the run and route a human decision.
            if conn is not None and run_id:
                try:
                    db.set_task_run_status(conn, run_id, "blocked")
                    if verdict.decision is permissions.Decision.DENY:
                        attention.open_permission_denied(
                            conn, task_run_id=run_id,
                            title=f"BLOCKED: {label}",
                            summary=f"{verdict.reason} [{verdict.matched_rule}]",
                        )
                    else:
                        attention.open_permission_item(
                            conn, task_run_id=run_id,
                            title=label,
                            summary=f"Needs your approval (policy: escalate). via {hook_name}.",
                        )
                except Exception:
                    pass
            try:
                title = "Archon: command blocked" if verdict.decision is permissions.Decision.DENY else "Archon: permission needed"
                notify.notify(title, label, urgency="critical")
            except Exception:
                pass

    return {
        "hook": hook_name,
        "severity": severity,
        "task_run_id": run_id,
        "blocked": blocked,
        "decision": decision,
        "reason": (verdict.reason if verdict is not None else None),
        "matched_rule": (verdict.matched_rule if verdict is not None else None),
    }


# Claude Code PreToolUse honours a JSON decision on stdout. Our three policy
# outcomes map straight onto its vocabulary: allow -> run it, deny -> refuse,
# escalate -> "ask" (show the human the normal prompt). Format is Claude-Code
# specific and version-sensitive; other providers ignore unexpected stdout.
_PRETOOLUSE_HOOKS = {"PreToolUse"}
_DECISION_TO_PROVIDER = {"allow": "allow", "deny": "deny", "escalate": "ask"}


def _emit_provider_decision(hook_name: str, summary: dict) -> int:
    """Return the enforceable decision to the provider. Returns the exit code.

    For Claude Code PreToolUse we both (a) print the modern JSON decision on
    stdout and (b) for a DENY, exit 2 with the reason on stderr — the universal
    "block this tool call" signal older Claude versions key off. Either path
    stops the command from running; together they are version-robust.
    """
    if (hook_name or "").strip() not in _PRETOOLUSE_HOOKS:
        return 0
    permission = _DECISION_TO_PROVIDER.get(summary.get("decision") or "")
    if not permission:
        return 0
    reason = summary.get("reason") or "archon team-lead policy"
    rule = summary.get("matched_rule")
    detail = f"{reason} [{rule}]" if rule else reason
    try:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": permission,
                "permissionDecisionReason": detail,
            }
        }))
    except Exception:
        pass
    if permission == "deny":
        try:
            print(f"Blocked by Archon policy: {detail}", file=sys.stderr)
        except Exception:
            pass
        return 2
    return 0


def main(hook_name: str) -> None:
    """CLI entry: read stdin, real DB, record. Never crashes the provider."""
    try:
        stdin_text = sys.stdin.read()
    except Exception:
        stdin_text = ""

    conn = None
    summary: dict = {}
    try:
        conn = db.connect()
        summary = handle_hook(hook_name, stdin_text, conn)
    except Exception:
        try:
            summary = handle_hook(hook_name, stdin_text, None)
        except Exception:
            summary = {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    code = _emit_provider_decision(hook_name, summary or {})
    sys.exit(code)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ProviderEvent")
