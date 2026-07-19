"""Config and data directory resolution for Archon.

Honours these environment overrides:

- ``ARCHON_CONFIG_HOME`` -> config directory (default ``~/.config/archon``)
- ``ARCHON_HOME``        -> data directory   (default ``~/.local/share/archon``)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def config_home() -> Path:
    return _env_path("ARCHON_CONFIG_HOME") or (Path.home() / ".config" / "archon")


def data_home() -> Path:
    return _env_path("ARCHON_HOME") or (Path.home() / ".local" / "share" / "archon")


@dataclass(frozen=True)
class Paths:
    """Resolved filesystem locations Archon reads and writes."""

    config_dir: Path
    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.yaml"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "archon.db"

    @property
    def events_file(self) -> Path:
        return self.data_dir / "events.jsonl"

    @property
    def hooks_log(self) -> Path:
        return self.data_dir / "hooks.log"

    @property
    def panes_file(self) -> Path:
        return self.data_dir / "panes.json"

    @property
    def queue_file(self) -> Path:
        return self.data_dir / "queue.yaml"

    @property
    def screens_dir(self) -> Path:
        return self.data_dir / "screens"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    def ensure(self) -> "Paths":
        """Create the directory tree if it does not yet exist."""
        for directory in (
            self.config_dir,
            self.data_dir,
            self.screens_dir,
            self.transcripts_dir,
            self.sessions_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self


def resolve_paths() -> Paths:
    """Resolve config/data paths from the environment (without creating them)."""
    return Paths(config_dir=config_home(), data_dir=data_home())
