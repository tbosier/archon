"""SQLite persistence for Archon: schema creation and small typed accessors.

The schema is provider-agnostic and matches section 6 of the build spec. Callers
get a plain ``sqlite3.Connection`` with ``row_factory`` set to ``sqlite3.Row``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import Provider, Repo, Task, TaskRun, Worker
from .paths import Paths, resolve_paths
from .util import utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  root_path TEXT NOT NULL UNIQUE,
  zellij_session TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS providers (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  command TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  installed INTEGER NOT NULL DEFAULT 0,
  auth_status TEXT NOT NULL,
  default_mode TEXT NOT NULL,
  login_command TEXT,
  last_checked_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_panes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider_id TEXT NOT NULL,
  repo_id INTEGER,
  zellij_session TEXT NOT NULL,
  zellij_pane_id TEXT,
  zellij_pane_name TEXT NOT NULL,
  purpose TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(provider_id) REFERENCES providers(id),
  FOREIGN KEY(repo_id) REFERENCES repos(id)
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  repo_id INTEGER NOT NULL,
  type TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  pr_number INTEGER,
  prompt TEXT NOT NULL,
  provider_policy TEXT NOT NULL,
  phase TEXT NOT NULL DEFAULT 'execute',
  parent_task_id TEXT,
  provider_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT,
  FOREIGN KEY(repo_id) REFERENCES repos(id)
);

CREATE TABLE IF NOT EXISTS task_dependencies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  depends_on_task_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(task_id, depends_on_task_id),
  FOREIGN KEY(task_id) REFERENCES tasks(id),
  FOREIGN KEY(depends_on_task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS workers (
  id TEXT PRIMARY KEY,
  provider_id TEXT NOT NULL,
  zellij_session TEXT,
  zellij_pane_id TEXT,
  state TEXT NOT NULL DEFAULT 'idle',
  current_task_run_id TEXT,
  max_concurrency INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(provider_id) REFERENCES providers(id)
);

CREATE TABLE IF NOT EXISTS scheduler_state (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  status TEXT NOT NULL,
  phase TEXT NOT NULL DEFAULT 'execute',
  model TEXT,
  branch TEXT,
  base_branch TEXT,
  worktree_path TEXT,
  zellij_session TEXT,
  zellij_pane_id TEXT,
  zellij_pane_name TEXT,
  provider_session_name TEXT,
  provider_session_id TEXT,
  provider_run_id TEXT,
  transcript_path TEXT,
  stdout_log_path TEXT,
  stderr_log_path TEXT,
  cost_usd REAL DEFAULT 0,
  input_tokens INTEGER,
  output_tokens INTEGER,
  total_tokens INTEGER,
  context_used_pct REAL,
  rate_limit_five_hour_pct REAL,
  rate_limit_seven_day_pct REAL,
  last_heartbeat_at TEXT,
  last_output_at TEXT,
  soft_budget_usd REAL,
  hard_budget_usd REAL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT,
  FOREIGN KEY(task_id) REFERENCES tasks(id),
  FOREIGN KEY(provider_id) REFERENCES providers(id)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT,
  task_run_id TEXT,
  provider_id TEXT,
  event_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  message TEXT,
  raw_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id),
  FOREIGN KEY(task_run_id) REFERENCES task_runs(id),
  FOREIGN KEY(provider_id) REFERENCES providers(id)
);

CREATE TABLE IF NOT EXISTS transcript_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT,
  task_run_id TEXT,
  provider_id TEXT,
  provider_session_id TEXT,
  transcript_path TEXT,
  role TEXT,
  tool_name TEXT,
  file_path TEXT,
  text TEXT,
  raw_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id),
  FOREIGN KEY(task_run_id) REFERENCES task_runs(id),
  FOREIGN KEY(provider_id) REFERENCES providers(id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
  task_id, task_run_id, provider_id, file_path, text
);

CREATE TABLE IF NOT EXISTS file_touches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  task_run_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  file_path TEXT NOT NULL,
  action TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id),
  FOREIGN KEY(task_run_id) REFERENCES task_runs(id),
  FOREIGN KEY(provider_id) REFERENCES providers(id)
);
"""


def connect(paths: Paths | None = None, *, db_path: Path | None = None) -> sqlite3.Connection:
    """Open (and initialise) the Archon database."""
    if db_path is None:
        paths = (paths or resolve_paths()).ensure()
        db_path = paths.db_file
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    return conn


def connect_memory() -> sqlite3.Connection:
    """In-memory database, handy for tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


# Columns added after 0.1.0; applied to pre-existing databases on connect.
_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "tasks": [("phase", "TEXT NOT NULL DEFAULT 'execute'"), ("parent_task_id", "TEXT"),
              ("provider_id", "TEXT")],
    "task_runs": [("phase", "TEXT NOT NULL DEFAULT 'execute'"), ("model", "TEXT")],
}


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    for table, columns in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# --- Repos ------------------------------------------------------------------

def upsert_repo(conn: sqlite3.Connection, repo: Repo) -> int:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO repos (name, root_path, zellij_session, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(root_path) DO UPDATE SET
          name=excluded.name,
          zellij_session=excluded.zellij_session,
          updated_at=excluded.updated_at
        """,
        (repo.name, repo.root_path, repo.zellij_session, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM repos WHERE root_path=?", (repo.root_path,)).fetchone()
    return int(row["id"])


# --- Providers --------------------------------------------------------------

def upsert_provider(conn: sqlite3.Connection, p: Provider) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO providers
          (id, display_name, command, enabled, installed, auth_status,
           default_mode, login_command, last_checked_at, last_error,
           created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          display_name=excluded.display_name,
          command=excluded.command,
          enabled=excluded.enabled,
          installed=excluded.installed,
          auth_status=excluded.auth_status,
          default_mode=excluded.default_mode,
          login_command=excluded.login_command,
          last_checked_at=excluded.last_checked_at,
          last_error=excluded.last_error,
          updated_at=excluded.updated_at
        """,
        (
            p.id, p.display_name, p.command, int(p.enabled), int(p.installed),
            p.auth_status, p.default_mode, p.login_command, p.last_checked_at,
            p.last_error, now, now,
        ),
    )
    conn.commit()


def list_providers(conn: sqlite3.Connection) -> list[Provider]:
    rows = conn.execute("SELECT * FROM providers ORDER BY id").fetchall()
    return [
        Provider(
            id=r["id"], display_name=r["display_name"], command=r["command"],
            enabled=bool(r["enabled"]), installed=bool(r["installed"]),
            auth_status=r["auth_status"], default_mode=r["default_mode"],
            login_command=r["login_command"], last_checked_at=r["last_checked_at"],
            last_error=r["last_error"],
        )
        for r in rows
    ]


# --- Tasks ------------------------------------------------------------------

def insert_task(conn: sqlite3.Connection, task: Task) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO tasks
          (id, repo_id, type, name, status, priority, pr_number, prompt,
           provider_policy, phase, parent_task_id, provider_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.id, task.repo_id, task.type, task.name, task.status,
            task.priority, task.pr_number, task.prompt, task.provider_policy,
            task.phase, task.parent_task_id, task.provider_id, now, now,
        ),
    )
    conn.commit()


def get_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def list_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()


def set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    conn.execute(
        "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
        (status, utc_now(), task_id),
    )
    conn.commit()


# --- Task runs --------------------------------------------------------------

_RUN_COLUMNS = [
    "id", "task_id", "provider_id", "status", "phase", "model", "branch", "base_branch",
    "worktree_path", "zellij_session", "zellij_pane_id", "zellij_pane_name",
    "provider_session_name", "provider_session_id", "provider_run_id",
    "transcript_path", "stdout_log_path", "stderr_log_path", "cost_usd",
    "input_tokens", "output_tokens", "total_tokens", "context_used_pct",
    "rate_limit_five_hour_pct", "rate_limit_seven_day_pct", "last_heartbeat_at",
    "last_output_at", "soft_budget_usd", "hard_budget_usd",
]


def insert_task_run(conn: sqlite3.Connection, run: TaskRun) -> None:
    now = utc_now()
    values = [getattr(run, c) for c in _RUN_COLUMNS]
    placeholders = ", ".join("?" for _ in _RUN_COLUMNS)
    conn.execute(
        f"INSERT INTO task_runs ({', '.join(_RUN_COLUMNS)}, created_at, updated_at) "
        f"VALUES ({placeholders}, ?, ?)",
        (*values, now, now),
    )
    conn.commit()


def update_task_run(conn: sqlite3.Connection, run_id: str, **fields) -> None:
    if not fields:
        return
    fields = {k: v for k, v in fields.items() if k in _RUN_COLUMNS}
    if not fields:
        return
    assignments = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE task_runs SET {assignments}, updated_at=? WHERE id=?",
        (*fields.values(), utc_now(), run_id),
    )
    conn.commit()


def set_task_run_status(conn: sqlite3.Connection, run_id: str, status: str) -> None:
    update_task_run(conn, run_id, status=status)


def find_task_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()


def list_task_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT tr.*, t.name AS task_name, t.type AS task_type "
        "FROM task_runs tr JOIN tasks t ON t.id = tr.task_id "
        "ORDER BY tr.created_at DESC"
    ).fetchall()


# --- Events -----------------------------------------------------------------

def insert_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    severity: str = "info",
    message: str | None = None,
    task_id: str | None = None,
    task_run_id: str | None = None,
    provider_id: str | None = None,
    raw_json: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO events
          (task_id, task_run_id, provider_id, event_type, severity, message,
           raw_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (task_id, task_run_id, provider_id, event_type, severity, message,
         raw_json, utc_now()),
    )
    conn.commit()


# --- Task dependency graph (DAG) --------------------------------------------

def add_dependency(conn: sqlite3.Connection, task_id: str, depends_on_task_id: str) -> None:
    """Record that ``task_id`` must wait for ``depends_on_task_id`` to finish."""
    conn.execute(
        "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_task_id, created_at) "
        "VALUES (?, ?, ?)",
        (task_id, depends_on_task_id, utc_now()),
    )
    conn.commit()


def dependencies_of(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?", (task_id,)
    ).fetchall()
    return [r["depends_on_task_id"] for r in rows]


def dependents_of(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT task_id FROM task_dependencies WHERE depends_on_task_id=?", (task_id,)
    ).fetchall()
    return [r["task_id"] for r in rows]


def all_dependencies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT task_id, depends_on_task_id FROM task_dependencies").fetchall()


def ready_task_ids(conn: sqlite3.Connection) -> list[str]:
    """Queued tasks whose every dependency task is ``done`` (or has no deps).

    Returned highest-priority first, then oldest first — the scheduler's order.
    """
    rows = conn.execute(
        """
        SELECT t.id
        FROM tasks t
        WHERE t.status = 'queued'
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks dep ON dep.id = d.depends_on_task_id
            WHERE d.task_id = t.id AND dep.status != 'done'
          )
        ORDER BY t.priority DESC, t.created_at ASC
        """
    ).fetchall()
    return [r["id"] for r in rows]


# --- Worker pool ------------------------------------------------------------

def upsert_worker(conn: sqlite3.Connection, w: "Worker") -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO workers
          (id, provider_id, zellij_session, zellij_pane_id, state,
           current_task_run_id, max_concurrency, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          provider_id=excluded.provider_id,
          zellij_session=excluded.zellij_session,
          zellij_pane_id=excluded.zellij_pane_id,
          state=excluded.state,
          current_task_run_id=excluded.current_task_run_id,
          max_concurrency=excluded.max_concurrency,
          updated_at=excluded.updated_at
        """,
        (w.id, w.provider_id, w.zellij_session, w.zellij_pane_id, w.state,
         w.current_task_run_id, w.max_concurrency, now, now),
    )
    conn.commit()


def list_workers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM workers ORDER BY provider_id, id").fetchall()


def set_worker_state(conn: sqlite3.Connection, worker_id: str, state: str,
                     current_task_run_id: str | None = None) -> None:
    conn.execute(
        "UPDATE workers SET state=?, current_task_run_id=?, updated_at=? WHERE id=?",
        (state, current_task_run_id, utc_now(), worker_id),
    )
    conn.commit()


def count_running_runs(conn: sqlite3.Connection, provider_id: str | None = None) -> int:
    if provider_id:
        row = conn.execute(
            "SELECT COUNT(*) c FROM task_runs WHERE status IN ('running','starting') "
            "AND provider_id=?", (provider_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) c FROM task_runs WHERE status IN ('running','starting')"
        ).fetchone()
    return int(row["c"])


def total_cost_usd(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) s FROM task_runs").fetchone()
    return float(row["s"] or 0.0)


def max_rate_limit_pct(conn: sqlite3.Connection, column: str = "rate_limit_five_hour_pct") -> float:
    if column not in ("rate_limit_five_hour_pct", "rate_limit_seven_day_pct"):
        raise ValueError(f"unknown rate-limit column: {column}")
    row = conn.execute(f"SELECT MAX({column}) m FROM task_runs").fetchone()
    return float(row["m"] or 0.0)


# --- Scheduler key/value state ---------------------------------------------

def set_scheduler_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO scheduler_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, utc_now()),
    )
    conn.commit()


def get_scheduler_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM scheduler_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
