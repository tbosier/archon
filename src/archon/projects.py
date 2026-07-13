"""Create a brand-new project from scratch so the command bar can start work
with no pre-existing repo.

`git init` + an initial commit is the minimum an execution worktree needs: the
dispatcher's ``create_feature_worktree`` branches off an existing repo, so a
"start a new project" intent has to first materialise that repo.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .util import sanitize_slug


class ProjectError(RuntimeError):
    """Raised when a new project cannot be created safely."""


@dataclass
class ProjectInfo:
    path: Path
    slug: str
    default_branch: str
    created: bool
    commands: list[list[str]] = field(default_factory=list)


def create_project(
    base_dir: str | Path,
    name: str,
    *,
    description: str | None = None,
    default_branch: str = "main",
    dry_run: bool = False,
) -> ProjectInfo:
    """Scaffold ``<base_dir>/<slug>`` as a fresh git repo with one commit.

    Refuses to touch a directory that already exists and is non-empty (never
    clobbers). In ``dry_run`` the git argvs that *would* run are recorded and
    nothing touches disk.
    """
    slug = sanitize_slug(name) or "new-project"
    path = Path(base_dir).expanduser() / slug

    if path.exists() and any(path.iterdir()):
        raise ProjectError(f"{path} already exists and is not empty")

    commands = [
        ["git", "init", "-b", default_branch],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=archon@local", "-c", "user.name=Archon",
         "commit", "-m", "Initial commit (archon)"],
    ]
    if dry_run:
        return ProjectInfo(path=path, slug=slug, default_branch=default_branch,
                           created=False, commands=commands)

    path.mkdir(parents=True, exist_ok=True)
    title = name.strip() or slug
    readme = f"# {title}\n"
    if description:
        readme += f"\n{description.strip()}\n"
    (path / "README.md").write_text(readme, encoding="utf-8")

    try:
        _git(path, ["init", "-b", default_branch])
        _git(path, ["add", "-A"])
        _git(path, ["-c", "user.email=archon@local", "-c", "user.name=Archon",
                    "commit", "-m", "Initial commit (archon)"])
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise ProjectError(f"git init failed for {path}: {detail}") from exc

    return ProjectInfo(path=path, slug=slug, default_branch=default_branch,
                       created=True, commands=commands)


def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )
