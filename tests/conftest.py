"""Shared pytest fixtures.

Ensures ``src/`` is importable and isolates each test's config/data dirs so no
test ever touches a real ``~/.config/archon`` or ``~/.local/share/archon``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    data = tmp_path / "data"
    monkeypatch.setenv("ARCHON_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("ARCHON_HOME", str(data))
    monkeypatch.delenv("ARCHON_DRY_RUN", raising=False)
    from archon.paths import resolve_paths
    return resolve_paths().ensure()


@pytest.fixture()
def conn():
    from archon import db
    c = db.connect_memory()
    yield c
    c.close()
