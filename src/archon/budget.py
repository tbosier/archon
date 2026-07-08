"""Global budget + rate-limit policy the scheduler consults before dispatch.

This module is pure: it reads two aggregates from the database (total cost and
the maximum five-hour rate-limit percentage across task runs) and compares them
against the configured :class:`~archon.config.BudgetConfig` thresholds. It never
mutates state.

The scheduler calls :func:`policy` before dispatching work and honours the
returned action:

- ``"allow"``:        dispatch normally.
- ``"prefer_small"``: only small/analytical tasks (reviews/tests) should
  dispatch; no new implementation work.
- ``"no_new_impl"``:  no new implementation agents at all (stricter than
  ``prefer_small`` — even small implementation is held back).
- ``"pause"``:        stop dispatch entirely until the limit resets.

Decision rules (spec §14) are evaluated in priority order, first match wins.
Let ``cost = db.total_cost_usd(conn)``, ``five = db.max_rate_limit_pct(conn)``
and ``b = config.scheduler.budget``:

1. ``b.hard_usd is not None and cost >= b.hard_usd``      -> ``"pause"`` (hard budget)
2. ``five >= b.pause_at_pct`` (default 95)                -> ``"pause"`` (rate limit)
3. ``five >= b.no_new_impl_at_pct`` (default 85)          -> ``"no_new_impl"``
4. ``(b.soft_usd is not None and cost >= b.soft_usd)`` or
   ``five >= b.prefer_small_at_pct`` (default 70)         -> ``"prefer_small"``
5. otherwise                                              -> ``"allow"``
"""

from __future__ import annotations

from dataclasses import dataclass

from . import db


@dataclass
class BudgetStatus:
    """The outcome of evaluating budget + rate-limit policy at a point in time."""

    action: str            # "allow" | "prefer_small" | "no_new_impl" | "pause"
    reason: str            # short human explanation
    total_cost_usd: float
    five_hour_pct: float
    soft_usd: float | None
    hard_usd: float | None


def evaluate(conn, config) -> BudgetStatus:
    """Read cost/rate-limit aggregates and decide the dispatch action.

    Rules are checked in priority order; the first match wins (see module
    docstring). The returned :class:`BudgetStatus` always carries the observed
    cost and five-hour percentage plus the configured soft/hard budgets.
    """
    b = config.scheduler.budget
    cost = db.total_cost_usd(conn)
    five = db.max_rate_limit_pct(conn)

    if b.hard_usd is not None and cost >= b.hard_usd:
        action = "pause"
        reason = f"hard budget reached: ${cost:.2f} >= ${b.hard_usd:.2f}"
    elif five >= b.pause_at_pct:
        action = "pause"
        reason = f"rate limit critical: five-hour {five:.0f}% >= {b.pause_at_pct:.0f}%"
    elif five >= b.no_new_impl_at_pct:
        action = "no_new_impl"
        reason = f"rate limit high: five-hour {five:.0f}% >= {b.no_new_impl_at_pct:.0f}%"
    elif (b.soft_usd is not None and cost >= b.soft_usd) or five >= b.prefer_small_at_pct:
        action = "prefer_small"
        if b.soft_usd is not None and cost >= b.soft_usd:
            reason = f"soft budget reached: ${cost:.2f} >= ${b.soft_usd:.2f}"
        else:
            reason = f"rate limit elevated: five-hour {five:.0f}% >= {b.prefer_small_at_pct:.0f}%"
    else:
        action = "allow"
        reason = "within budget and rate limits"

    return BudgetStatus(
        action=action,
        reason=reason,
        total_cost_usd=cost,
        five_hour_pct=five,
        soft_usd=b.soft_usd,
        hard_usd=b.hard_usd,
    )


def policy(conn, config) -> str:
    """Return just the action string — what the scheduler calls per dispatch."""
    return evaluate(conn, config).action


def describe(status: BudgetStatus) -> str:
    """One-line summary for the dashboard/CLI, e.g. for a status footer."""
    soft = f"${status.soft_usd:.2f}" if status.soft_usd is not None else "-"
    hard = f"${status.hard_usd:.2f}" if status.hard_usd is not None else "-"
    return (
        f"[{status.action}] {status.reason} "
        f"(cost ${status.total_cost_usd:.2f}, five-hour {status.five_hour_pct:.0f}%, "
        f"soft {soft}, hard {hard})"
    )
