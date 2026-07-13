"""Governance checks for planned and dispatched work."""

from __future__ import annotations

from dataclasses import dataclass


RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass
class PolicyFinding:
    severity: str
    message: str


class PolicyError(RuntimeError):
    def __init__(self, findings: list[PolicyFinding]):
        self.findings = findings
        super().__init__("; ".join(f.message for f in findings))


def docs_only(plan) -> bool:
    return bool(plan.tasks) and all(t.phase == "docs" for t in plan.tasks)


def validate_plan(plan, config) -> None:
    """Apply hard checks to a planner proposal before enqueueing tasks."""
    findings: list[PolicyFinding] = []
    keys = {t.key for t in plan.tasks}
    if len(keys) != len(plan.tasks):
        findings.append(PolicyFinding("error", "planned task keys must be unique"))
    for task in plan.tasks:
        missing = [dep for dep in task.depends_on if dep not in keys]
        if missing:
            findings.append(
                PolicyFinding("error", f"{task.key} depends on unknown task(s): {', '.join(missing)}")
            )

    if not docs_only(plan):
        execute_tasks = [t for t in plan.tasks if t.phase == "execute"]
        for task in execute_tasks:
            reviewers = [
                t for t in plan.tasks
                if t.phase == "review" and task.key in t.depends_on
            ]
            tests = [
                t for t in plan.tasks
                if t.phase == "test" and (
                    task.key in t.depends_on
                    or any(r.key in t.depends_on for r in reviewers)
                )
            ]
            if not reviewers:
                findings.append(PolicyFinding("error", f"execute task {task.key} needs a dependent review task"))
            if not tests:
                findings.append(PolicyFinding("error", f"execute task {task.key} needs a dependent test task"))
            if getattr(config.routing, "review_must_differ_from_execute", True):
                for review in reviewers:
                    if review.tool == task.tool and _risk_rank(review.model_tier) <= _risk_rank(task.model_tier):
                        findings.append(
                            PolicyFinding(
                                "error",
                                f"review task {review.key} must use a different tool or higher tier than {task.key}",
                            )
                        )

    if findings:
        raise PolicyError(findings)


def requires_approval(plan, config, *, yes: bool = False) -> bool:
    if plan.overall_risk == "high":
        return True
    if plan.overall_risk == "low" and config.policy.auto_approve_low_risk:
        return False
    return not yes


def _risk_rank(tier: str) -> int:
    return {"cheap": 0, "standard": 1, "high": 2}.get(tier, 0)
