"""Best-effort desktop notifications.

Tries platform-native notifiers in turn (``notify-send`` on Linux, ``osascript``
on macOS, ``terminal-notifier`` as a fallback). This is telemetry glue: it must
NEVER raise, even if no notifier exists or a subprocess blows up.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

# Map an Archon urgency to the ``notify-send`` urgency vocabulary.
_NOTIFY_SEND_URGENCY = {
    "low": "low",
    "normal": "normal",
    "warn": "normal",
    "warning": "normal",
    "critical": "critical",
    "error": "critical",
}


def notify(title: str, message: str, *, urgency: str = "normal") -> bool:
    """Fire a desktop notification. Returns True if a notifier ran, else False.

    Never raises. If every notifier is missing or fails, returns False so the
    caller can fall back to (e.g.) colouring a pane.
    """
    title = "" if title is None else str(title)
    message = "" if message is None else str(message)
    urgency = (urgency or "normal").lower()

    for runner in (_notify_send, _osascript, _terminal_notifier):
        try:
            if runner(title, message, urgency):
                return True
        except Exception:
            # A broken notifier must never take down the cockpit.
            continue
    return False


def _run(argv: list[str]) -> bool:
    """Run a notifier command, swallowing all errors. True on exit code 0."""
    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _notify_send(title: str, message: str, urgency: str) -> bool:
    if sys.platform == "darwin":
        return False
    if not shutil.which("notify-send"):
        return False
    level = _NOTIFY_SEND_URGENCY.get(urgency, "normal")
    return _run(["notify-send", "-u", level, title, message])


def _osascript(title: str, message: str, urgency: str) -> bool:
    if sys.platform != "darwin":
        return False
    if not shutil.which("osascript"):
        return False
    # Escape double quotes for the AppleScript string literals.
    safe_title = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    return _run(["osascript", "-e", script])


def _terminal_notifier(title: str, message: str, urgency: str) -> bool:
    if not shutil.which("terminal-notifier"):
        return False
    return _run(["terminal-notifier", "-title", title, "-message", message])
