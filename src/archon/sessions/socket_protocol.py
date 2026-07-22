"""Portable framed messages over local Unix stream sockets."""

from __future__ import annotations

import socket
import struct


_HEADER = struct.Struct("!I")
_MAX_MESSAGE_SIZE = 1024 * 1024


class FramedSocket:
    """A Unix stream socket that preserves application message boundaries.

    ``SOCK_SEQPACKET`` would provide these boundaries itself, but macOS does
    not support that socket type for ``AF_UNIX``. A short length prefix gives
    ``SOCK_STREAM`` the same semantics on every supported platform.
    """

    def __init__(self, sock: socket.socket | None = None) -> None:
        self.socket = sock or socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._buffer = bytearray()

    def fileno(self) -> int:
        return self.socket.fileno()

    def close(self) -> None:
        self.socket.close()

    def connect(self, path: str) -> None:
        self.socket.connect(path)

    def settimeout(self, timeout: float | None) -> None:
        self.socket.settimeout(timeout)

    def send(self, kind: bytes, data: bytes = b"") -> None:
        if len(kind) != 1:
            raise ValueError("message kind must be exactly one byte")
        message = kind + data
        if len(message) > _MAX_MESSAGE_SIZE:
            raise ValueError("message is too large")
        self.socket.sendall(_HEADER.pack(len(message)) + message)

    def receive(self) -> tuple[list[bytes], bool]:
        """Read available bytes and return complete messages plus EOF state."""
        data = self.socket.recv(65536)
        if not data:
            return [], True
        self._buffer.extend(data)
        messages: list[bytes] = []
        while len(self._buffer) >= _HEADER.size:
            (size,) = _HEADER.unpack_from(self._buffer)
            if size < 1 or size > _MAX_MESSAGE_SIZE:
                raise OSError("invalid session socket message size")
            frame_size = _HEADER.size + size
            if len(self._buffer) < frame_size:
                break
            messages.append(bytes(self._buffer[_HEADER.size:frame_size]))
            del self._buffer[:frame_size]
        return messages, False
