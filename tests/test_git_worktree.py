"""Tests for git worktree management (spec §11)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from archon.git_worktree import (
    create_feature_worktree,
    create_pr_review_worktree,
    default_base_branch,
    get_git_state,
    repo_root,
    sanitize_branch_component,
)


# --- sanitize_branch_component -------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("newButton4User", "newButton4User"),
        ("new button user", "new-button-user"),
        ("feature/with/slashes", "feature-with-slashes"),
        ("--leading-and-trailing--", "leading-and-trailing"),
        ("café déjà", "cafe-deja"),
        ("weird::chars??here", "weird-chars-here"),
        ("dots...and.spaces", "dots-and-spaces"),
        ("", "item"),
        ("///", "item"),
    ],
)
def test_sanitize_branch_component(raw, expected):
    assert sanitize_branch_component(raw) == expected


def test_sanitize_strips_lock_suffix():
    assert not sanitize_branch_component("mybranch.lock").endswith(".lock")


def test_sanitize_result_is_ref_safe():
    # git check-ref-format should accept refs/heads/<component>.
    for raw in ["new button", "café", "a//b", "--x--", "t~ilde^caret"]:
        comp = sanitize_branch_component(raw)
        proc = subprocess.run(
            ["git", "check-ref-format", f"refs/heads/{comp}"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"{comp!r} rejected by git for input {raw!r}"


# --- feature worktree naming (dry-run) -----------------------------------


def test_feature_worktree_single():
    repo = Path("/home/user/ci_amplify_ai")
    info = create_feature_worktree(
        repo, "newButton4User", branch=None, base="origin/main",
        provider_id=None, variants=False, dry_run=True,
    )
    assert info.path == Path("/home/user/ci_amplify_ai-newButton4User")
    assert info.branch == "feature/newButton4User"
    assert info.base_branch == "origin/main"
    assert info.created is True
    assert info.reused is False
    # dry-run records the git argvs it would run.
    add = [c for c in info.commands if "add" in c][0]
    assert add == [
        "git", "-C", "/home/user/ci_amplify_ai",
        "worktree", "add", "/home/user/ci_amplify_ai-newButton4User",
        "-b", "feature/newButton4User", "origin/main",
    ]


def test_feature_worktree_variant():
    repo = Path("/home/user/ci_amplify_ai")
    info = create_feature_worktree(
        repo, "newButton4User", branch=None, base="origin/main",
        provider_id="claude", variants=True, dry_run=True,
    )
    assert info.path == Path("/home/user/ci_amplify_ai-newButton4User-claude")
    assert info.branch == "feature/newButton4User/claude"


def test_feature_worktree_provider_without_variants_is_single():
    # provider given but variants=False -> single (no provider suffix).
    repo = Path("/home/user/ci_amplify_ai")
    info = create_feature_worktree(
        repo, "newButton4User", branch=None, base="origin/main",
        provider_id="claude", variants=False, dry_run=True,
    )
    assert info.path == Path("/home/user/ci_amplify_ai-newButton4User")
    assert info.branch == "feature/newButton4User"


def test_feature_worktree_explicit_branch_wins():
    repo = Path("/home/user/ci_amplify_ai")
    info = create_feature_worktree(
        repo, "newButton4User", branch="feature/custom", base="origin/main",
        provider_id=None, variants=False, dry_run=True,
    )
    assert info.branch == "feature/custom"


# --- PR review worktree naming (dry-run) ---------------------------------


def test_pr_review_worktree_naming():
    repo = Path("/home/user/ci_amplify_ai")
    info = create_pr_review_worktree(
        repo, 552, base="origin/main", provider_id="claude", dry_run=True,
    )
    assert info.path == Path("/home/user/ci_amplify_ai-pr-552-review-claude")
    assert info.branch == "review/pr-552/claude"
    assert info.base_branch == "origin/main"
    assert info.created is True


def test_pr_review_worktree_codex():
    repo = Path("/home/user/ci_amplify_ai")
    info = create_pr_review_worktree(
        repo, 552, base="origin/main", provider_id="codex", dry_run=True,
    )
    assert info.path == Path("/home/user/ci_amplify_ai-pr-552-review-codex")
    assert info.branch == "review/pr-552/codex"


# --- real git for repo_root / get_git_state ------------------------------


def _init_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)
    return path


def test_repo_root(tmp_path):
    repo = _init_repo(tmp_path / "myrepo")
    sub = repo / "src" / "nested"
    sub.mkdir(parents=True)
    assert repo_root(sub).resolve() == repo.resolve()


def test_get_git_state_clean_then_dirty(tmp_path):
    repo = _init_repo(tmp_path / "myrepo")

    clean = get_git_state(repo)
    assert clean.branch == "main"
    assert clean.dirty is False
    assert clean.ahead == 0
    assert clean.behind == 0

    (repo / "README.md").write_text("changed\n")
    dirty = get_git_state(repo)
    assert dirty.dirty is True


def test_get_git_state_untracked_is_dirty(tmp_path):
    repo = _init_repo(tmp_path / "myrepo")
    subprocess.run(
        ["git", "-C", str(repo), "config", "status.showUntrackedFiles", "no"],
        check=True,
    )
    (repo / "newfile.txt").write_text("x\n")
    assert get_git_state(repo).dirty is True


def test_default_base_branch_fallback(tmp_path):
    # A local repo with no remote should branch from its local default branch.
    repo = _init_repo(tmp_path / "myrepo")   # created with `-b main` + a commit
    assert default_base_branch(repo) == "main"


# --- real feature worktree creation end-to-end ---------------------------


def test_create_feature_worktree_real(tmp_path):
    repo = _init_repo(tmp_path / "myrepo")
    info = create_feature_worktree(
        repo, "coolFeature", branch=None, base="main",
        provider_id=None, variants=False, dry_run=False,
    )
    assert info.created is True
    assert info.path.exists()
    assert (info.path / ".git").exists()
    state = get_git_state(info.path)
    assert state.branch == "feature/coolFeature"


def test_reuse_existing_clean_worktree(tmp_path):
    repo = _init_repo(tmp_path / "myrepo")
    first = create_feature_worktree(
        repo, "coolFeature", branch=None, base="main",
        provider_id=None, variants=False, dry_run=False,
    )
    assert first.created is True and first.reused is False

    # Second call, same names, clean worktree already present -> reuse.
    second = create_feature_worktree(
        repo, "coolFeature", branch=None, base="main",
        provider_id=None, variants=False, dry_run=False,
    )
    assert second.reused is True
    assert second.created is False
    assert second.path == first.path


def test_refuse_to_clobber_dirty_worktree(tmp_path):
    repo = _init_repo(tmp_path / "myrepo")
    first = create_feature_worktree(
        repo, "coolFeature", branch=None, base="main",
        provider_id=None, variants=False, dry_run=False,
    )
    # Make the existing worktree dirty.
    (first.path / "README.md").write_text("dirty\n")

    with pytest.raises(RuntimeError):
        create_feature_worktree(
            repo, "coolFeature", branch=None, base="main",
            provider_id=None, variants=False, dry_run=False,
        )
