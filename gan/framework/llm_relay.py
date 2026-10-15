"""Local TCP-to-Unix relay used inside a network-isolated worker."""
from __future__ import annotations

import socket
import threading
import time
from typing import Optional


class LocalLLMRelay:
    def __init__(self, unix_path: str, proxy_token: Optional[str] = None, *,
                 max_connections: int = 32, header_timeout_s: float = 10.0) -> None:
        self.unix_path = unix_path
        self.proxy_token = proxy_token
        self.max_connections = int(max_connections)
        self.header_timeout_s = float(header_timeout_s)
        self.listener: Optional[socket.socket] = None
        self.port = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._serve_thread: Optional[threading.Thread] = None
        self._bridge_threads: set[threading.Thread] = set()
        self._connections: set[socket.socket] = set()

    def start(self) -> int:
        if self.listener is not None or self._stop.is_set():
            raise RuntimeError("LLM relay cannot be started twice")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(self.max_connections)
        self.listener = listener
        self.port = int(listener.getsockname()[1])
        thread = threading.Thread(target=self._serve, name="rsi-gan-llm-relay", daemon=True)
        thread.start()
        self._serve_thread = thread
        return self.port

    def close(self) -> None:
        """Stop accepting, close every active bridge and join relay threads."""
        self._stop.set()
        listener, self.listener = self.listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        deadline = time.monotonic() + 5.0
        threads = ([self._serve_thread] if self._serve_thread is not None else [])
        with self._lock:
            threads.extend(self._bridge_threads)
        current = threading.current_thread()
        for thread in threads:
            if thread is current:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def _serve(self) -> None:
        while not self._stop.is_set():
            listener = self.listener
            if listener is None:
                return
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                conn.close()
                continue
            thread = threading.Thread(target=self._bridge, args=(conn,),
                                      name="rsi-gan-llm-relay-conn", daemon=True)
            with self._lock:
                self._connections.add(conn)
                self._bridge_threads.add(thread)
            thread.start()

    def _bridge(self, client: socket.socket) -> None:
        upstream: Optional[socket.socket] = None
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with self._lock:
                self._connections.add(upstream)
            upstream.settimeout(self.header_timeout_s)
            upstream.connect(self.unix_path)

            deadline = time.monotonic() + self.header_timeout_s
            header = b""
            while b"\r\n\r\n" not in header:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("relay request header deadline exceeded")
                client.settimeout(remaining)
                chunk = client.recv(65536)
                if not chunk or len(header) + len(chunk) > 128 * 1024:
                    raise OSError("invalid relay request header")
                header += chunk
            raw_head, pending = header.split(b"\r\n\r\n", 1)
            lines = raw_head.split(b"\r\n")
            if not lines or not lines[0]:
                raise OSError("invalid relay request line")
            forwarded = []
            for line in lines[1:]:
                if not line or line[:1] in b" \t" or b":" not in line:
                    raise OSError("invalid relay request header")
                name, _value = line.split(b":", 1)
                if self.proxy_token and name.strip().lower() == b"authorization":
                    continue
                forwarded.append(line)
            if self.proxy_token:
                forwarded.append(
                    f"Authorization: Bearer {self.proxy_token}".encode("ascii"))
            upstream.sendall(
                b"\r\n".join([lines[0], *forwarded]) + b"\r\n\r\n" + pending)

            # Deadlines protect connect/header setup only. Healthy upstream
            # model calls may legitimately take hundreds of seconds.
            client.settimeout(None)
            upstream.settimeout(None)
            threads = []
            for source, target in ((client, upstream), (upstream, client)):
                t = threading.Thread(target=self._copy, args=(source, target),
                                     daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
        except (OSError, TimeoutError):
            pass
        finally:
            for connection in (client, upstream):
                if connection is None:
                    continue
                try:
                    connection.close()
                except OSError:
                    pass
                with self._lock:
                    self._connections.discard(connection)
            with self._lock:
                self._bridge_threads.discard(threading.current_thread())
            self._slots.release()

    @staticmethod
    def _copy(source: socket.socket, target: socket.socket) -> None:
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                target.sendall(data)
        except OSError:
            return

