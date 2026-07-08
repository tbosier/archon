"""Dependency graph (DAG) helpers: build edges, order, detect cycles, render.

All state lives in the ``task_dependencies`` table; these are pure functions over
:mod:`archon.db`. An edge ``(task_id, depends_on_task_id)`` means *task_id waits
for depends_on_task_id to finish*.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict, deque

from . import db


def add_edge(conn: sqlite3.Connection, task_id: str, depends_on_task_id: str) -> None:
    """Record that ``task_id`` depends on ``depends_on_task_id``."""
    db.add_dependency(conn, task_id, depends_on_task_id)


def chain(conn: sqlite3.Connection, ordered_task_ids: list[str]) -> None:
    """Make a linear chain: each task depends on the one before it."""
    for prev, curr in zip(ordered_task_ids, ordered_task_ids[1:]):
        add_edge(conn, curr, prev)


def edges(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every dependency edge as ``(task_id, depends_on_task_id)`` tuples."""
    return [
        (r["task_id"], r["depends_on_task_id"])
        for r in db.all_dependencies(conn)
    ]


def _all_task_ids(conn: sqlite3.Connection) -> list[str]:
    return [r["id"] for r in db.list_tasks(conn)]


def topological_order(conn: sqlite3.Connection) -> list[str]:
    """Kahn's algorithm over all tasks. Dependencies come before dependents.

    Ties are broken by task id for a stable result. If the graph contains a
    cycle, the tasks stuck in that cycle are omitted from the result.
    """
    node_ids = _all_task_ids(conn)
    nodes = set(node_ids)
    # dependents[x] = tasks that depend on x (edges out of x for ordering).
    dependents: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = {n: 0 for n in node_ids}
    for task_id, dep_id in edges(conn):
        if task_id not in nodes or dep_id not in nodes:
            continue
        dependents[dep_id].append(task_id)
        indegree[task_id] += 1

    queue = deque(sorted(n for n in node_ids if indegree[n] == 0))
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for nxt in sorted(dependents[node]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    return order


def detect_cycle(conn: sqlite3.Connection) -> list[str] | None:
    """Return one cycle as an ordered path of task ids, or ``None`` if acyclic.

    Uses iterative DFS with a recursion stack. The returned path lists the nodes
    forming the cycle in dependency order and repeats the entry node at the end.
    """
    # adjacency: task -> its dependencies (follow "waits for" edges).
    adj: dict[str, list[str]] = defaultdict(list)
    node_ids = _all_task_ids(conn)
    nodes = set(node_ids)
    for task_id, dep_id in edges(conn):
        if task_id in nodes and dep_id in nodes:
            adj[task_id].append(dep_id)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in node_ids}

    for start in node_ids:
        if color[start] != WHITE:
            continue
        # stack holds (node, iterator over its neighbours); path tracks the DFS chain.
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = [start]
        color[start] = GRAY
        while stack:
            node, idx = stack[-1]
            neighbours = adj[node]
            if idx < len(neighbours):
                stack[-1] = (node, idx + 1)
                nxt = neighbours[idx]
                if color[nxt] == GRAY:
                    # Found a back-edge; slice the cycle out of the current path.
                    cut = path.index(nxt)
                    return path[cut:] + [nxt]
                if color[nxt] == WHITE:
                    color[nxt] = GRAY
                    path.append(nxt)
                    stack.append((nxt, 0))
            else:
                color[node] = BLACK
                stack.pop()
                path.pop()
    return None


def ascii_graph(conn: sqlite3.Connection) -> str:
    """Render the DAG as plain-ASCII text for an ``archon graph`` command.

    Roots (tasks nobody in the group depends upon) are printed first, with their
    dependents nested beneath using ``\\-`` connectors. Tasks are grouped by
    ``parent_task_id`` when present. Every line shows ``[status]``.
    """
    tasks = {r["id"]: r for r in db.list_tasks(conn)}
    if not tasks:
        return "(no tasks)"

    # dependents[dep] = tasks that wait on dep — used to nest children.
    # deps_of[task]   = tasks that must finish first.
    dependents: dict[str, list[str]] = defaultdict(list)
    deps_of: dict[str, list[str]] = defaultdict(list)
    for task_id, dep_id in edges(conn):
        if task_id in tasks and dep_id in tasks:
            dependents[dep_id].append(task_id)
            deps_of[task_id].append(dep_id)

    # Group tasks: keyed by parent_task_id, falling back to the task's own id.
    groups: dict[str, list[str]] = defaultdict(list)
    for tid, row in tasks.items():
        group_key = row["parent_task_id"] or tid
        groups[group_key].append(tid)

    def label(tid: str) -> str:
        row = tasks[tid]
        return f"{tid}  [{row['status']}]"

    lines: list[str] = []

    def render(tid: str, depth: int, seen: set[str]) -> None:
        indent = "    " * depth
        connector = "\\- " if depth > 0 else ""
        lines.append(f"{indent}{connector}{label(tid)}")
        if tid in seen:  # guard against cycles
            return
        seen = seen | {tid}
        for child in sorted(dependents[tid]):
            if child in tasks:
                render(child, depth + 1, seen)

    for group_key in sorted(groups):
        members = set(groups[group_key])
        # Roots of this group: members with no dependency inside the group
        # (deps outside the group, or none). These anchor the tree.
        roots = sorted(
            tid for tid in members
            if not any(dep in members for dep in deps_of[tid])
        )
        if not roots:  # pure cycle inside the group — just list members
            roots = sorted(members)
        for root in roots:
            render(root, 0, set())

    return "\n".join(lines)
