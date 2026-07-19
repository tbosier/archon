"""Small shared helpers: IDs, time, dry-run detection, event logging."""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any

# Monotonic per-process counter keeps IDs unique within a single invocation.
_task_seq = count(1)
_job_seq = count(1)
_agent_seq = count(1)
_attention_seq = count(1)
# A short token minted once per process disambiguates IDs across separate CLI
# invocations that land in the same second (each `archon` run is a new process,
# so the counter alone would collide in a shared database).
_PROC = secrets.token_hex(2)


def utc_now() -> str:
    """ISO-8601 UTC timestamp, e.g. ``2026-07-07T18:03:12Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def new_task_id() -> str:
    """Stable, sortable, process-unique task id: ``TASK-<YYYYMMDD>-<NNN><proc>``."""
    return f"TASK-{_date_stamp()}-{next(_task_seq):03d}{_PROC}"


def new_job_id() -> str:
    """Stable, sortable, process-unique job id: ``JOB-<YYYYMMDD>-<NNN><proc>``."""
    return f"JOB-{_date_stamp()}-{next(_job_seq):03d}{_PROC}"


def new_agent_id(role: str) -> str:
    """Stable agent id with the role in the suffix for easier debugging."""
    return f"AGENT-{_date_stamp()}-{next(_agent_seq):03d}{_PROC}-{sanitize_slug(role)}"


def new_attention_id() -> str:
    """Stable, sortable attention item id."""
    return f"ATTN-{_date_stamp()}-{next(_attention_seq):03d}{_PROC}"


def run_id_for(task_id: str, provider_id: str) -> str:
    """Task-run id derived from its task: ``RUN-<...>-<provider>``.

    Sibling runs of one task share a stem and differ only by provider, mirroring
    the spec's ``RUN-...-claude`` / ``RUN-...-codex`` pairing. Uniqueness follows
    from the task id being unique and one run per provider per task.
    """
    stem = task_id.replace("TASK-", "RUN-", 1)
    return f"{stem}-{sanitize_slug(provider_id)}"


def sanitize_slug(value: str) -> str:
    """Lowercase, filesystem/branch-safe slug. Collapses runs of separators."""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "item"


def is_dry_run(explicit: bool | None = None) -> bool:
    """True when Archon must not touch external tools.

    ``explicit`` (a ``--dry-run`` flag) wins; otherwise honour ``ARCHON_DRY_RUN``.
    """
    if explicit:
        return True
    return os.environ.get("ARCHON_DRY_RUN", "").strip() not in ("", "0", "false", "False")


def append_event_line(path: Path, payload: dict[str, Any]) -> None:
    """Append one JSON object to an ``events.jsonl``-style file. Never raises fatally."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        # Telemetry must never take down the cockpit.
        pass


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace a JSON file atomically so polling readers never see half a write."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """Drop obviously-secret env vars before logging a command."""
    secret = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|AUTH|CREDENTIAL)", re.IGNORECASE)
    return {k: ("***" if secret.search(k) else v) for k, v in env.items()}
