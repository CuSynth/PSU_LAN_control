"""Minimal raw-socket SCPI transport (Rigol instruments listen on TCP 5555)."""
from __future__ import annotations

import socket
import threading


class ScpiError(Exception):
    """Communication failure (timeout, closed socket, ...)."""


class ScpiConnection:
    def __init__(self, host: str, port: int = 5555, timeout: float = 2.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def open(self) -> None:
        self.close()
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise ScpiError(f"cannot connect to {self.host}:{self.port}: {e}") from e
        s.settimeout(self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = s
        self._buf = b""

    def close(self) -> None:
        s, self._sock = self._sock, None
        self._buf = b""
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def write(self, cmd: str) -> None:
        with self._lock:
            self._send(cmd)

    def query(self, cmd: str) -> str:
        with self._lock:
            self._send(cmd)
            return self._readline()

    # -- internals --------------------------------------------------------
    def _send(self, cmd: str) -> None:
        if self._sock is None:
            raise ScpiError("not connected")
        try:
            self._sock.sendall(cmd.encode("ascii") + b"\n")
        except OSError as e:
            self.close()
            raise ScpiError(f"send failed: {e}") from e

    def _readline(self) -> str:
        assert self._sock is not None
        try:
            while b"\n" not in self._buf:
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise ScpiError("connection closed by instrument")
                self._buf += chunk
        except (OSError, ScpiError) as e:
            # after a timeout the reply stream is out of sync -> drop the link
            self.close()
            if isinstance(e, ScpiError):
                raise
            raise ScpiError(f"receive failed: {e}") from e
        line, _, self._buf = self._buf.partition(b"\n")
        return line.decode("ascii", "replace").strip()
