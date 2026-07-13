"""Tests for from-scratch project creation."""

from __future__ import annotations

import subprocess

import pytest

from archon import projects


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def test_create_project_initialises_repo(tmp_path):
    info = projects.create_project(tmp_path, "Invoice Parser", description="parse invoices")
    assert info.created
    assert info.path.exists()
    assert (info.path / "README.md").exists()
    # a real git repo with one commit
    assert (info.path / ".git").exists()
    log = _git(info.path, "log", "--oneline")
    assert log.returncode == 0 and log.stdout.strip()
    assert "invoice" in info.slug


def test_create_project_dry_run_touches_nothing(tmp_path):
    info = projects.create_project(tmp_path, "demo", dry_run=True)
    assert not info.created
    assert not info.path.exists()
    assert info.commands  # the git argvs that would have run


def test_create_project_refuses_nonempty_dir(tmp_path):
    slug_dir = tmp_path / "taken"
    slug_dir.mkdir()
    (slug_dir / "keep.txt").write_text("x")
    with pytest.raises(projects.ProjectError):
        projects.create_project(tmp_path, "taken")
