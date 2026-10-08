"""Local TCP-to-Unix relay used inside a network-isolated worker."""
from __future__ import annotations

import socket
import threading
from typing import Optional


class LocalLLMRelay:
    def __init__(self, unix_path: str) -> None:
        self.unix_path = unix_path
        self.listener: Optional[socket.socket] = None
        self.port = 0
        self._threads = []

    def start(self) -> int:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(32)
        self.listener = listener
        self.port = int(listener.getsockname()[1])
        thread = threading.Thread(target=self._serve, name="rsi-gan-llm-relay", daemon=True)
        thread.start()
        self._threads.append(thread)
        return self.port

    def close(self) -> None:
        """Stop the listener. In-flight pumps (daemon threads) drain on their own."""
        listener, self.listener = self.listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def _serve(self) -> None:
        assert self.listener is not None
        while self.listener is not None:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            thread = threading.Thread(target=self._bridge, args=(conn,),
                                      name="rsi-gan-llm-relay-conn", daemon=True)
            thread.start()
            self._threads.append(thread)

    def _bridge(self, client: socket.socket) -> None:
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # The timeout is for the CONNECT phase only (fail fast on a dead
            # socket path). It MUST be lifted before pumping: a real gateway
            # call routinely takes >10s, and a socket.timeout in either pump
            # direction silently kills the client's in-flight request while
            # the proxy still completes and audits it (observed: planner
            # responses lost for every call slower than 10s).
            upstream.settimeout(10)
            upstream.connect(self.unix_path)
            upstream.settimeout(None)
            sockets = (client, upstream)
            threads = []
            for source, target in ((client, upstream), (upstream, client)):
                t = threading.Thread(target=self._copy, args=(source, target),
                                     daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

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

