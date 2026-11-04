"""Task-child telemetry channel (docs/19 §12.28, CM F5 P1 — task half).

Lives in ``utils/`` because BOTH ends import it from the staged runtime:
the server runs in the trusted harness process, the client in the sandboxed
task child (which does not receive ``gan/framework`` in its allowlist copy).

The task child's audit streams (its per-question trajectory and its CM seam
events) used to be CHILD-WRITTEN files the parent copied back / folded in —
the audited party held write authority over its own audit record. This module
moves the write authority to the parent: the child FORWARDS rows over a
run-scoped unix socket and the parent (the harness, inside the task runner's
trust domain) writes them hash-chained.

Protocol: one JSON object per line, ``{"v": 1, "type": ..., ...}`` —
``traj_row`` (``record``), ``traj_reset``, ``cm_event`` (``event``). Framing
is line-delimited JSON on a dedicated socket: one message = one row, so the
file medium's torn-line tolerance (and its tamper-hiding) disappears by
construction.

The server accepts ONE connection per question run and stops with it; the
harness owns the lifecycle (create before bwrap, stop after the child exits).
A client send failure degrades to the child's LOCAL diagnostics — never a
session failure, never silent (no-silent-fail).
"""
from __future__ import annotations

import json
import os
import socket
import threading
from typing import Any, Callable, Dict, Optional

_PROTOCOL_V = 1


class TelemetryServer:
    """Run-scoped accept-one unix-socket server; sinks are parent closures."""

    def __init__(self, socket_path: str, on_message: Callable[[Dict[str, Any]], None]):
        self._path = socket_path
        self._on_message = on_message
        self._sock: Optional[socket.socket] = None
        self._conn: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.connected = False
        self.rows = 0
        self.cm_events = 0

    def start(self) -> None:
        d = os.path.dirname(self._path)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(self._path):
            os.unlink(self._path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self._path)
        os.chmod(self._path, 0o600)
        srv.listen(1)
        self._sock = srv

        def _serve() -> None:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with self._lock:
                self._conn = conn
                self.connected = True
            f = conn.makefile("r", encoding="utf-8")
            try:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a torn line cannot happen on a framed
                        # socket; if it does, skip instead of sinking
                    if not isinstance(msg, dict):
                        continue
                    mtype = msg.get("type")
                    if mtype == "traj_row":
                        self.rows += 1
                    elif mtype == "cm_event":
                        self.cm_events += 1
                    try:
                        self._on_message(msg)
                    except Exception:
                        continue  # a sink error must not kill the channel
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
                with self._lock:
                    if self._conn is conn:
                        self._conn = None

        self._thread = threading.Thread(target=_serve, name="f5-telemetry",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        try:
            with self._lock:
                if self._conn is not None:
                    self._conn.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            if os.path.exists(self._path):
                os.unlink(self._path)
        except OSError:
            pass


class TelemetryClient:
    """Child-side forwarder; ``send`` is fire-and-forget with a bool result."""

    def __init__(self, socket_path: str, tries: int = 20, delay: float = 0.05):
        self.sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        last: Optional[Exception] = None
        for _ in range(tries):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(socket_path)
                self.sock = s
                break
            except OSError as e:
                last = e
                import time
                time.sleep(delay)
        self.connect_error = None if self.sock is not None else str(last)

    @property
    def active(self) -> bool:
        return self.sock is not None

    def send(self, msg: Dict[str, Any]) -> bool:
        with self._lock:
            if self.sock is None:
                return False
            try:
                self.sock.sendall(
                    (json.dumps(msg, ensure_ascii=False, default=str) + "\n"
                     ).encode("utf-8"))
                return True
            except OSError:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
                return False

    def close(self) -> None:
        with self._lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
