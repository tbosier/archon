"""Adapter contract for observing agent sessions.

These CLIs do not push events to us — they leave on-disk artifacts (registry
files, JSONL event logs, pid locks). So the honest API is a poll: ``discover()``
returns a snapshot of the sessions the adapter can see right now. A future
refinement can tail the logs and emit an event stream, but discovery is what the
dashboard needs first.
"""

from __future__ import annotations

import os
import time
from typing import Protocol

from .model import AgentSession


class SessionAdapter(Protocol):
    provider_id: str

    def discover(self) -> list[AgentSession]: ...


def pid_alive(pid: int | None) -> bool:
    """Best-effort liveness. Host-accurate; a sandbox with a different PID
    namespace will under-report (treated conservatively by callers)."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        # Exists but owned by another user — it is alive.
        return True
    except OSError:
        return os.path.exists(f"/proc/{pid}")


def now() -> float:
    return time.time()
