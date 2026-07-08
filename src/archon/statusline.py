"""``archon statusline`` handler (build spec §7.10).

Provider statusline integrations (initially Claude Code) pipe a JSON blob to
``archon statusline`` on stdin every few seconds. We parse it, best-effort update
the matching task-run's telemetry, and print one short status line back so the
provider can render it.

Hard rule: this must NEVER crash the provider CLI. Every failure path returns a
short neutral string; ``main`` catches everything and exits 0.
"""

from __future__ import annotations

import json
import os
import sys

from . import db

# --- JSON payload helpers ---------------------------------------------------


def _load_payload(stdin_text: str) -> dict:
    """Parse stdin JSON into a dict. Empty/malformed/non-object -> {}."""
    if not stdin_text:
        return {}
    try:
        data = json.loads(stdin_text)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _first(payload: dict, *keys):
    """Return the first non-None value among the given keys."""
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _num(value):
    """Coerce to float; None/garbage -> None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _int(value):
    n = _num(value)
    return int(n) if n is not None else None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


# --- Telemetry extraction ---------------------------------------------------


def extract_telemetry(payload: dict) -> dict:
    """Pull normalised telemetry fields out of a statusline payload.

    Accepts the nested Claude shape (``cost``/``context``/``rate_limits`` objects)
    and flat keys (``cost_usd``, ``total_tokens``, ``context_used_pct`` ...).
    Only fields actually present are returned.
    """
    cost = _dict(payload.get("cost"))
    context = _dict(payload.get("context"))
    limits = _dict(payload.get("rate_limits")) or _dict(payload.get("rate_limit"))

    fields: dict = {}

    cost_usd = _num(_first(cost, "total_cost_usd", "cost_usd", "usd"))
    if cost_usd is None:
        cost_usd = _num(_first(payload, "cost_usd", "total_cost_usd"))
    if cost_usd is not None:
        fields["cost_usd"] = cost_usd

    input_tokens = _int(_first(cost, "input_tokens") or _first(payload, "input_tokens"))
    if input_tokens is not None:
        fields["input_tokens"] = input_tokens

    output_tokens = _int(_first(cost, "output_tokens") or _first(payload, "output_tokens"))
    if output_tokens is not None:
        fields["output_tokens"] = output_tokens

    total_tokens = _int(
        _first(cost, "total_tokens") or _first(payload, "total_tokens", "tokens")
    )
    if total_tokens is not None:
        fields["total_tokens"] = total_tokens

    ctx = _num(_first(context, "used_pct", "context_used_pct", "used_percent"))
    if ctx is None:
        ctx = _num(_first(payload, "context_used_pct"))
    if ctx is not None:
        fields["context_used_pct"] = ctx

    five = _num(
        _first(limits, "five_hour_pct", "5h_pct", "rate_limit_five_hour_pct")
    )
    if five is None:
        five = _num(_first(payload, "rate_limit_five_hour_pct"))
    if five is not None:
        fields["rate_limit_five_hour_pct"] = five

    seven = _num(
        _first(limits, "seven_day_pct", "7d_pct", "rate_limit_seven_day_pct")
    )
    if seven is None:
        seven = _num(_first(payload, "rate_limit_seven_day_pct"))
    if seven is not None:
        fields["rate_limit_seven_day_pct"] = seven

    session_id = _first(payload, "session_id", "provider_session_id")
    if session_id is not None:
        fields["provider_session_id"] = str(session_id)

    transcript = _first(payload, "transcript_path", "transcript")
    if transcript is not None:
        fields["transcript_path"] = str(transcript)

    return fields


# --- Task-run inference (shared with hooks.py) ------------------------------


def infer_task_run_id(conn, payload: dict, env: dict) -> str | None:
    """Best-effort resolution of the task-run this payload belongs to.

    Order of preference:
      1. ``ARCHON_TASK_RUN_ID`` env var (authoritative).
      2. DB lookup by provider_session_id / transcript_path / pane id.
      3. First run of ``ARCHON_TASK_ID`` (if that task has any runs).

    Returns a run id string or None. Never raises.
    """
    run_id = env.get("ARCHON_TASK_RUN_ID")
    if run_id:
        return run_id

    if conn is not None:
        try:
            session_id = _first(payload, "session_id", "provider_session_id")
            transcript = _first(payload, "transcript_path", "transcript")
            pane_id = env.get("ARCHON_PANE_ID") or _first(
                payload, "pane_id", "zellij_pane_id"
            )

            if session_id is not None:
                row = conn.execute(
                    "SELECT id FROM task_runs WHERE provider_session_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (str(session_id),),
                ).fetchone()
                if row:
                    return row["id"]

            if transcript is not None:
                row = conn.execute(
                    "SELECT id FROM task_runs WHERE transcript_path=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (str(transcript),),
                ).fetchone()
                if row:
                    return row["id"]

            if pane_id is not None:
                row = conn.execute(
                    "SELECT id FROM task_runs WHERE zellij_pane_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (str(pane_id),),
                ).fetchone()
                if row:
                    return row["id"]

            task_id = env.get("ARCHON_TASK_ID")
            if task_id:
                row = conn.execute(
                    "SELECT id FROM task_runs WHERE task_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if row:
                    return row["id"]
        except Exception:
            return None

    return None


# --- Status line rendering --------------------------------------------------


def _format_status(provider: str, fields: dict) -> str:
    """Render a compact one-line status such as ``claude · $0.21 · ctx 42% · 5h 12%``."""
    parts = [provider or "provider"]
    if "cost_usd" in fields:
        parts.append(f"${fields['cost_usd']:.2f}")
    elif "total_tokens" in fields:
        parts.append(f"{fields['total_tokens']} tok")
    if "context_used_pct" in fields:
        parts.append(f"ctx {fields['context_used_pct']:.0f}%")
    if "rate_limit_five_hour_pct" in fields:
        parts.append(f"5h {fields['rate_limit_five_hour_pct']:.0f}%")
    return " · ".join(parts)


def handle_statusline(stdin_text: str, conn=None, *, env: dict | None = None) -> str:
    """Parse a statusline payload, update telemetry, and return a status string.

    Tolerant of empty/malformed input and missing/null fields everywhere.
    """
    if env is None:
        env = dict(os.environ)

    payload = _load_payload(stdin_text)
    provider = env.get("ARCHON_PROVIDER_ID") or str(
        _first(payload, "provider_id", "provider") or "claude"
    )

    fields = extract_telemetry(payload)

    if conn is not None:
        try:
            run_id = infer_task_run_id(conn, payload, env)
            if run_id and fields:
                now = _utc_now()
                update = dict(fields)
                update["last_heartbeat_at"] = now
                update["last_output_at"] = now
                db.update_task_run(conn, run_id, **update)
        except Exception:
            # Telemetry updates must never break the status line.
            pass

    return _format_status(provider, fields)


def _utc_now() -> str:
    from .util import utc_now

    return utc_now()


def main() -> None:
    """CLI entry: read stdin, update real DB, print status. Never exits non-zero."""
    try:
        stdin_text = sys.stdin.read()
    except Exception:
        stdin_text = ""

    line = "archon"
    conn = None
    try:
        conn = db.connect()
        line = handle_statusline(stdin_text, conn)
    except Exception:
        try:
            line = handle_statusline(stdin_text, None)
        except Exception:
            line = "archon"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    try:
        print(line)
    except Exception:
        pass
    # Always a clean exit — telemetry failure must not signal the provider.
    sys.exit(0)


if __name__ == "__main__":
    main()
