"""Git worktree management for Archon (see spec §11).

Every task run gets its own isolated worktree and branch, so providers never
fight over ``main`` or over each other's checkouts. This module builds the
worktree/branch names, runs the ``git worktree`` plumbing, and reports state.

Safety (spec §11, §20):

- Never delete or overwrite a dirty worktree.
- If the target worktree path already exists, detect whether it is safe to
  reuse (right branch, clean) and reuse it (``reused=True``) rather than clobber.
- Never force-delete worktrees.
- In ``dry_run`` mode nothing touches git: the argvs that *would* run are
  recorded in :attr:`WorktreeInfo.commands` and ``created=True`` is returned.
"""

from __future__ import annotations

import logging
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("archon.git_worktree")


@dataclass
class WorktreeInfo:
    path: Path
    branch: str
    base_branch: str
    created: bool               # True if a new worktree was made
    reused: bool = False        # True if an existing safe worktree was reused
    commands: list[list[str]] = field(default_factory=list)  # git argvs run/planned


@dataclass
class GitState:
    branch: str | None
    dirty: bool
    ahead: int
    behind: int


# --- low-level git helpers -----------------------------------------------


def _git(
    repo: Path | None,
    args: list[str],
    *,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a git command, capturing output. Returns the completed process.

    ``check=True`` raises :class:`subprocess.CalledProcessError` on non-zero exit.
    """
    argv = ["git"]
    if repo is not None:
        argv += ["-C", str(repo)]
    argv += args
    return subprocess.run(
        argv,
        check=check,
        capture_output=True,
        text=True,
    )


def repo_root(path: str | Path) -> Path:
    """Resolve the repository root via ``git rev-parse --show-toplevel``."""
    proc = _git(Path(path), ["rev-parse", "--show-toplevel"], check=True)
    return Path(proc.stdout.strip())


def default_base_branch(repo: Path) -> str:
    """Best-effort default base branch, e.g. ``origin/main``.

    Tries the remote HEAD symbolic ref; falls back to ``origin/main``.
    """
    # git symbolic-ref refs/remotes/origin/HEAD -> refs/remotes/origin/main
    proc = _git(repo, ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])
    if proc.returncode == 0 and proc.stdout.strip():
        ref = proc.stdout.strip()
        prefix = "refs/remotes/"
        if ref.startswith(prefix):
            return ref[len(prefix):]

    # Fall back: does origin/main or origin/master exist?
    for candidate in ("origin/main", "origin/master"):
        check = _git(repo, ["rev-parse", "--verify", "--quiet", candidate])
        if check.returncode == 0:
            return candidate

    # Local-only repo (no remote): branch from the local default branch.
    for candidate in ("main", "master"):
        check = _git(repo, ["rev-parse", "--verify", "--quiet", candidate])
        if check.returncode == 0:
            return candidate

    # Detached or unusual: use the current branch if we have one.
    head = _git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    if head.returncode == 0 and head.stdout.strip() and head.stdout.strip() != "HEAD":
        return head.stdout.strip()

    return "main"


def sanitize_branch_component(value: str) -> str:
    """Return a git-ref-safe slug for a single path component.

    Handles spaces, slashes, unicode, and leading/trailing dashes. Git ref rules
    forbid (among others): whitespace, ``~^:?*[\\``, ``..``, leading/trailing
    ``/`` or ``.``, trailing ``.lock``. We normalise unicode to ASCII, replace
    every unsafe char with ``-``, and collapse/trim separators.
    """
    # Normalise unicode -> closest ASCII (e.g. "é" -> "e"), drop what won't map.
    normalized = unicodedata.normalize("NFKD", value)
    ascii_str = normalized.encode("ascii", "ignore").decode("ascii")

    out_chars: list[str] = []
    for ch in ascii_str:
        if ch.isalnum() or ch in ("_",):
            out_chars.append(ch)
        else:
            # spaces, slashes, dots, control/special ref chars -> separator
            out_chars.append("-")
    slug = "".join(out_chars)

    # Collapse runs of dashes and trim leading/trailing dashes/dots.
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-.")

    # Avoid the forbidden ``.lock`` suffix.
    if slug.endswith(".lock"):
        slug = slug[: -len(".lock")].rstrip("-.")

    return slug or "item"


# --- worktree creation ----------------------------------------------------


def _add_worktree(
    repo: Path,
    path: Path,
    branch: str,
    base: str,
    dry_run: bool,
) -> WorktreeInfo:
    """Create (or safely reuse) a worktree at ``path`` on new branch ``branch``.

    Command shape::

        git -C <repo> fetch origin
        git -C <repo> worktree add <path> -b <branch> <base>
    """
    commands: list[list[str]] = []
    fetch_argv = ["git", "-C", str(repo), "fetch", "origin"]
    add_argv = [
        "git", "-C", str(repo),
        "worktree", "add", str(path), "-b", branch, base,
    ]

    if dry_run:
        commands.append(fetch_argv)
        commands.append(add_argv)
        return WorktreeInfo(
            path=path,
            branch=branch,
            base_branch=base,
            created=True,
            reused=False,
            commands=commands,
        )

    # Real mode: fetch first (best effort — offline shouldn't be fatal).
    commands.append(fetch_argv)
    fetch = _git(repo, ["fetch", "origin"])
    if fetch.returncode != 0:
        logger.info("git fetch origin failed (continuing): %s", fetch.stderr.strip())

    # If the target path already exists, decide whether it's safe to reuse.
    if path.exists():
        reuse = _try_reuse(path, branch)
        if reuse is not None:
            reuse.base_branch = base
            reuse.commands = commands
            return reuse
        # Path exists but is not safely reusable — refuse to clobber.
        raise RuntimeError(
            f"Worktree path {path} already exists and is not safe to reuse "
            f"(dirty or on a different branch). Refusing to overwrite."
        )

    commands.append(add_argv)
    add = _git(repo, ["worktree", "add", str(path), "-b", branch, base])
    if add.returncode != 0:
        raise RuntimeError(
            f"git worktree add failed for {path}: {add.stderr.strip()}"
        )

    return WorktreeInfo(
        path=path,
        branch=branch,
        base_branch=base,
        created=True,
        reused=False,
        commands=commands,
    )


def _try_reuse(path: Path, branch: str) -> WorktreeInfo | None:
    """Return a reuse WorktreeInfo if ``path`` is a clean worktree on ``branch``.

    Returns ``None`` when it is unsafe to reuse (dirty, or a different branch, or
    not a git worktree at all).
    """
    if not (path / ".git").exists():
        return None
    state = get_git_state(path)
    if state.dirty:
        return None
    if state.branch is not None and state.branch != branch:
        return None
    return WorktreeInfo(
        path=path,
        branch=state.branch or branch,
        base_branch="",  # filled in by caller
        created=False,
        reused=True,
        commands=[],
    )


def create_feature_worktree(
    repo: Path,
    feature_name: str,
    branch: str | None,
    base: str,
    provider_id: str | None,
    variants: bool,
    dry_run: bool = False,
) -> WorktreeInfo:
    """Create an isolated feature worktree.

    Naming (spec §11):

    - single:  ``../<repo>-<feature>``          branch ``feature/<feature>``
    - variant: ``../<repo>-<feature>-<provider>`` branch ``feature/<feature>/<provider>``
      (only when a provider is given *and* ``variants`` is True)
    """
    repo = Path(repo)
    repo_name = repo.name
    feat = sanitize_branch_component(feature_name)

    is_variant = variants and provider_id is not None
    if is_variant:
        provider = sanitize_branch_component(provider_id)  # type: ignore[arg-type]
        dir_name = f"{repo_name}-{feat}-{provider}"
        default_branch = f"feature/{feat}/{provider}"
    else:
        dir_name = f"{repo_name}-{feat}"
        default_branch = f"feature/{feat}"

    target = (repo.parent / dir_name)
    resolved_branch = branch or default_branch

    return _add_worktree(repo, target, resolved_branch, base, dry_run)


def create_pr_review_worktree(
    repo: Path,
    pr_number: int,
    base: str,
    provider_id: str,
    dry_run: bool = False,
) -> WorktreeInfo:
    """Create a provider-specific PR review worktree.

    Naming (spec §11):

    - path:   ``../<repo>-pr-<N>-review-<provider>``
    - branch: ``review/pr-<N>/<provider>``
    """
    repo = Path(repo)
    repo_name = repo.name
    provider = sanitize_branch_component(provider_id)

    dir_name = f"{repo_name}-pr-{pr_number}-review-{provider}"
    target = repo.parent / dir_name
    branch = f"review/pr-{pr_number}/{provider}"

    return _add_worktree(repo, target, branch, base, dry_run)


# --- worktree state -------------------------------------------------------


def get_git_state(worktree: Path) -> GitState:
    """Report branch / dirtiness / ahead-behind for a worktree.

    Robust to fresh worktrees with no upstream: ahead/behind default to 0.
    """
    worktree = Path(worktree)

    # Current branch (or None on detached HEAD).
    branch_proc = _git(worktree, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch: str | None = branch_proc.stdout.strip() if branch_proc.returncode == 0 else None
    if branch == "HEAD":  # detached
        branch = None

    # Dirty? Any staged/unstaged/untracked change shows up in porcelain output.
    # Override status.showUntrackedFiles from the user's global Git config so
    # safety checks never mistake a worktree with untracked files for clean.
    status = _git(worktree, ["status", "--porcelain", "--untracked-files=all"])
    dirty = bool(status.stdout.strip()) if status.returncode == 0 else False

    # Ahead/behind vs upstream, if one is configured.
    ahead = behind = 0
    counts = _git(
        worktree,
        ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
    )
    if counts.returncode == 0 and counts.stdout.strip():
        parts = counts.stdout.split()
        if len(parts) == 2:
            try:
                behind = int(parts[0])
                ahead = int(parts[1])
            except ValueError:
                behind = ahead = 0

    return GitState(branch=branch, dirty=dirty, ahead=ahead, behind=behind)
