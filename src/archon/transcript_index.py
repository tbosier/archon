"""Transcript indexing + search (build spec §16).

Ingest provider transcripts / JSONL stdout into ``transcript_events`` and the
``transcript_fts`` full-text index, record file touches, and expose ``search``
and ``touched`` queries. Malformed lines are skipped, never fatal.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .util import utc_now

# Map a provider tool name to a file_touches action vocabulary entry.
_ACTION_BY_TOOL = {
    "read": "read",
    "write": "write",
    "create": "write",
    "edit": "edit",
    "multiedit": "edit",
    "notebookedit": "edit",
    "str_replace": "edit",
    "bash": "bash",
    "shell": "bash",
    "run": "bash",
    "test": "test",
    "pytest": "test",
}


def _action_for_tool(tool_name: str | None) -> str:
    if not tool_name:
        return "unknown"
    key = str(tool_name).strip().lower()
    if key in _ACTION_BY_TOOL:
        return _ACTION_BY_TOOL[key]
    # Heuristic fallbacks for compound names like "run_tests".
    if "test" in key:
        return "test"
    if "edit" in key:
        return "edit"
    if "write" in key:
        return "write"
    if "read" in key:
        return "read"
    if "bash" in key or "shell" in key:
        return "bash"
    return "unknown"


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _extract_record(obj: dict) -> dict:
    """Pull normalised (role, tool_name, file_path, text) out of one JSONL object."""
    role = obj.get("role") or obj.get("type") or obj.get("sender")

    tool_name = (
        obj.get("tool")
        or obj.get("tool_name")
        or obj.get("name")
    )

    # Tool input can live under several keys.
    tool_input = _as_dict(obj.get("input")) or _as_dict(obj.get("tool_input")) \
        or _as_dict(obj.get("parameters")) or _as_dict(obj.get("args"))

    file_path = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("filename")
        or obj.get("file_path")
        or obj.get("path")
    )

    # Text can be a plain string or a structured content list.
    text = obj.get("text")
    if text is None:
        content = obj.get("content") or obj.get("message")
        text = _stringify_content(content)
    if text is None and tool_input:
        # Fall back to command / query text so bash lines are searchable.
        text = tool_input.get("command") or tool_input.get("query")

    return {
        "role": str(role) if role is not None else None,
        "tool_name": str(tool_name) if tool_name is not None else None,
        "file_path": str(file_path) if file_path is not None else None,
        "text": str(text) if text is not None else None,
    }


def _stringify_content(content) -> str | None:
    """Flatten a message/content value into searchable text."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if isinstance(content.get("content"), (str, list, dict)):
            return _stringify_content(content["content"])
        return None
    if isinstance(content, list):
        parts = []
        for item in content:
            piece = _stringify_content(item)
            if piece:
                parts.append(piece)
        return "\n".join(parts) if parts else None
    return None


def index_jsonl_text(
    conn,
    text: str,
    *,
    task_id=None,
    task_run_id=None,
    provider_id=None,
    transcript_path=None,
) -> int:
    """Index JSONL text (one JSON object per line). Returns rows indexed.

    Malformed lines are skipped silently.
    """
    if not text:
        return 0

    provider_session_id = None
    count = 0
    now = utc_now()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue

        if provider_session_id is None:
            provider_session_id = obj.get("session_id") or obj.get("provider_session_id")

        rec = _extract_record(obj)

        cur = conn.execute(
            """
            INSERT INTO transcript_events
              (task_id, task_run_id, provider_id, provider_session_id,
               transcript_path, role, tool_name, file_path, text, raw_json,
               created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                task_run_id,
                provider_id,
                str(provider_session_id) if provider_session_id else None,
                transcript_path,
                rec["role"],
                rec["tool_name"],
                rec["file_path"],
                rec["text"],
                line[:20000],
                now,
            ),
        )
        row_id = cur.lastrowid

        # Keep the FTS rowid aligned with transcript_events.id so search can join
        # back for created_at filtering.
        conn.execute(
            """
            INSERT INTO transcript_fts
              (rowid, task_id, task_run_id, provider_id, file_path, text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                task_id or "",
                task_run_id or "",
                provider_id or "",
                rec["file_path"] or "",
                rec["text"] or "",
            ),
        )

        if rec["file_path"]:
            _record_touch(
                conn,
                task_id=task_id,
                task_run_id=task_run_id,
                provider_id=provider_id,
                file_path=rec["file_path"],
                action=_action_for_tool(rec["tool_name"]),
                now=now,
            )

        count += 1

    conn.commit()
    return count


def _record_touch(conn, *, task_id, task_run_id, provider_id, file_path, action, now):
    """Upsert a file_touches row (NOT NULL columns are coalesced to '')."""
    tid = task_id or ""
    rid = task_run_id or ""
    pid = provider_id or ""
    existing = conn.execute(
        """
        SELECT id FROM file_touches
        WHERE task_id=? AND task_run_id=? AND provider_id=?
          AND file_path=? AND action=?
        LIMIT 1
        """,
        (tid, rid, pid, file_path, action),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE file_touches SET last_seen_at=? WHERE id=?",
            (now, existing["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO file_touches
              (task_id, task_run_id, provider_id, file_path, action,
               first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (tid, rid, pid, file_path, action, now, now),
        )


def index_transcript_file(
    conn,
    path,
    *,
    task_id=None,
    task_run_id=None,
    provider_id=None,
) -> int:
    """Read a JSONL transcript file and index it. Missing/unreadable -> 0."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return index_jsonl_text(
        conn,
        text,
        task_id=task_id,
        task_run_id=task_run_id,
        provider_id=provider_id,
        transcript_path=str(p),
    )


# --- Queries ----------------------------------------------------------------

_SINCE_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_SINCE_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def _since_cutoff(since: str | None) -> str | None:
    """Turn ``"7d"`` into an ISO cutoff timestamp, or None if unparseable."""
    if not since:
        return None
    match = _SINCE_RE.match(since)
    if not match:
        return None
    amount = int(match.group(1))
    unit = _SINCE_UNITS[match.group(2).lower()]
    cutoff = datetime.now(timezone.utc) - timedelta(**{unit: amount})
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fts_query(query: str) -> str:
    """Quote a user query as an FTS5 phrase so punctuation can't break MATCH."""
    return '"' + str(query).replace('"', '""') + '"'


def search(conn, query: str, *, since: str | None = None) -> list[dict]:
    """Full-text search transcripts. Returns list of dicts with an excerpt.

    Each dict: ``{task_id, task_run_id, provider_id, file_path, excerpt}``.
    ``since`` (e.g. ``"7d"``) filters by created_at, best-effort.
    """
    if not query or not str(query).strip():
        return []

    cutoff = _since_cutoff(since)
    sql = (
        "SELECT transcript_fts.task_id AS task_id, "
        "transcript_fts.task_run_id AS task_run_id, "
        "transcript_fts.provider_id AS provider_id, "
        "transcript_fts.file_path AS file_path, "
        "transcript_fts.text AS text, e.created_at AS created_at "
        "FROM transcript_fts "
        "JOIN transcript_events e ON e.id = transcript_fts.rowid "
        "WHERE transcript_fts MATCH ?"
    )
    params: list = [_fts_query(query)]
    if cutoff:
        sql += " AND e.created_at >= ?"
        params.append(cutoff)
    sql += " ORDER BY e.created_at DESC, e.id DESC"

    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        return []

    results = []
    for r in rows:
        results.append(
            {
                "task_id": r["task_id"] or None,
                "task_run_id": r["task_run_id"] or None,
                "provider_id": r["provider_id"] or None,
                "file_path": r["file_path"] or None,
                "excerpt": _excerpt(r["text"]),
            }
        )
    return results


def _excerpt(text, limit: int = 200) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def touched(conn, file_path: str) -> list[dict]:
    """Return file_touches rows whose file_path contains ``file_path`` (substring)."""
    if not file_path:
        return []
    pattern = f"%{file_path}%"
    try:
        rows = conn.execute(
            "SELECT * FROM file_touches WHERE file_path LIKE ? "
            "ORDER BY last_seen_at DESC",
            (pattern,),
        ).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]
