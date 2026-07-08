"""Thin wrapper over the ``gh`` (GitHub CLI) and a little ``git fetch`` helper.

Used by the PR-review workflow (spec §1.4). Everything here is best-effort and
honours dry-run: no external command runs when ``dry_run`` is set. Read helpers
return ``""`` in dry-run; ``pr_checkout`` and ``fetch`` return the argv they
would have run so callers can log / display the plan.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("archon.github")


def gh_available() -> bool:
    """True if the ``gh`` CLI is on PATH."""
    return shutil.which("gh") is not None


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )


def pr_view(pr_number: int, repo: Path | None = None, dry_run: bool = False) -> str:
    """Return ``gh pr view <N>`` output (empty string on dry-run/failure)."""
    argv = ["gh", "pr", "view", str(pr_number)]
    if dry_run:
        logger.debug("dry-run: %s", " ".join(argv))
        return ""
    proc = _run(argv, cwd=repo)
    if proc.returncode != 0:
        logger.info("gh pr view failed: %s", proc.stderr.strip())
        return ""
    return proc.stdout


def pr_diff(pr_number: int, repo: Path | None = None, dry_run: bool = False) -> str:
    """Return ``gh pr diff <N>`` output (empty string on dry-run/failure)."""
    argv = ["gh", "pr", "diff", str(pr_number)]
    if dry_run:
        logger.debug("dry-run: %s", " ".join(argv))
        return ""
    proc = _run(argv, cwd=repo)
    if proc.returncode != 0:
        logger.info("gh pr diff failed: %s", proc.stderr.strip())
        return ""
    return proc.stdout


def pr_checkout(
    pr_number: int,
    branch: str,
    worktree: Path,
    dry_run: bool = False,
) -> list[str]:
    """Check out a PR into ``worktree`` on ``branch``; return the argv used.

    Runs ``gh pr checkout <N> -b <branch> --force`` inside the worktree. The
    ``--force`` matches the spec's example flow; it overwrites the *local review
    branch* checkout, never a dirty user worktree (callers own that safety).
    """
    argv = ["gh", "pr", "checkout", str(pr_number), "-b", branch, "--force"]
    if dry_run:
        logger.debug("dry-run (%s): %s", worktree, " ".join(argv))
        return argv
    proc = _run(argv, cwd=worktree)
    if proc.returncode != 0:
        logger.info("gh pr checkout failed: %s", proc.stderr.strip())
    return argv


def fetch(repo: Path, dry_run: bool = False) -> None:
    """Run ``git fetch origin`` in ``repo`` (best effort; no-op on dry-run)."""
    argv = ["git", "-C", str(repo), "fetch", "origin"]
    if dry_run:
        logger.debug("dry-run: %s", " ".join(argv))
        return
    proc = subprocess.run(argv, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        logger.info("git fetch origin failed: %s", proc.stderr.strip())
