"""Attach the current terminal to an Archon session's persistent PTY."""

from __future__ import annotations

import argparse
import fcntl
import os
import select
import signal
import struct
import sys
import termios
import tty

from .socket_protocol import FramedSocket


LEFT_ARROW = b"\x1b[D"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    args = parser.parse_args(argv)
    client = FramedSocket()
    try:
        client.connect(args.socket)
    except OSError as exc:
        print(f"archon: session is no longer attachable: {exc}", file=sys.stderr)
        return 1

    _send_winsize(client)
    signal.signal(signal.SIGWINCH, lambda _signum, _frame: _send_winsize(client))

    old_attrs = termios.tcgetattr(sys.stdin.fileno())
    try:
        tty.setraw(sys.stdin.fileno())
        os.write(sys.stdout.fileno(), b"Archon agent view. Press left arrow to return to the command center.\r\n")
        pending = b""
        while True:
            readable, _, _ = select.select([sys.stdin.fileno(), client], [], [])
            if client in readable:
                try:
                    packets, disconnected = client.receive()
                except OSError:
                    break
                if disconnected:
                    break
                for packet in packets:
                    if packet[:1] == b"O":
                        os.write(sys.stdout.fileno(), packet[1:])
            if sys.stdin.fileno() in readable:
                data = os.read(sys.stdin.fileno(), 4096)
                if not data:
                    break
                pending += data
                if pending == LEFT_ARROW:
                    pending = b""
                    return 0
                if LEFT_ARROW.startswith(pending) and len(pending) < len(LEFT_ARROW):
                    continue
                client.send(b"I", pending)
                pending = b""
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attrs)
        client.close()
    return 0


def _send_winsize(client: FramedSocket) -> None:
    for source in (sys.stdin.fileno(), sys.stdout.fileno(), sys.stderr.fileno()):
        try:
            size = fcntl.ioctl(source, termios.TIOCGWINSZ, b"\0" * 8)
            rows, cols, _, _ = struct.unpack("HHHH", size)
            if rows and cols:
                client.send(b"W", struct.pack("!II", rows, cols))
                return
        except (OSError, struct.error):
            continue


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
