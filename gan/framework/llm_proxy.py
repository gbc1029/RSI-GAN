"""Parent-side OpenAI-compatible proxy used by GAN agent processes."""
from __future__ import annotations

import http.client
import json
import os
import secrets
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional


def _normalize_model_name(model: Any) -> Optional[str]:
    if not isinstance(model, str):
        return None
    prefix = "openai/"
    return model[len(prefix):] if model.startswith(prefix) else model


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _UnixHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True
    allow_reuse_address = True


class _ProxyHandler(BaseHTTPRequestHandler):
    server_version = "RSI-GAN-LLM-Proxy/1"

    def log_message(self, *_args: Any) -> None:
        return

    @property
    def proxy(self) -> "ParentLLMProxy":
        return self.server.proxy  # type: ignore[attr-defined]

    @property
    def is_unix(self) -> bool:
        return bool(getattr(self.server, "is_unix", False))

    def _reply(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/__gan__/issue-task":
            self._issue_task()
            return
        if self.path != "/v1/chat/completions":
            self.proxy.audit_denied(None, "unknown endpoint", self.path)
            self._reply(404, {"error": {"message": "unknown endpoint"}})
            return
        self.proxy.handle_completion(self)

    def _read_json(self) -> Optional[Dict[str, Any]]:
        try:
            size = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            size = -1
        if size < 0 or size > self.proxy.max_request_bytes:
            return None
        try:
            value = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            return None
        return value if isinstance(value, dict) else None

    def _issue_task(self) -> None:
        if not self.is_unix:
            self._reply(403, {"error": {"message": "framework registration requires Unix transport"}})
            return
        token = self.proxy._bearer(self.headers.get("Authorization"))
        scope = self.proxy.scope_for(token)
        if not scope or scope.get("role") != "framework":
            self.proxy.audit_denied(scope, "task scope registration denied", self.path)
            self._reply(403, {"error": {"message": "registration capability denied"}})
            return
        body = self._read_json()
        if not body or not body.get("scope_id") or not body.get("model"):
            self._reply(400, {"error": {"message": "scope_id and model are required"}})
            return
        self._reply(200, {"token": self.proxy.issue_scope(
            "task", str(body["scope_id"]), str(body["model"]))})


class ParentLLMProxy:
    """Per-run proxy serving TCP and Unix-socket transports."""

    def __init__(self, output_dir: str, socket_path: str, *,
                 upstream_base: Optional[str] = None,
                 upstream_key: Optional[str] = None,
                 models: Optional[Dict[str, str]] = None,
                 timeout: float = 120.0) -> None:
        self.output_dir = os.path.abspath(output_dir)
        self.socket_path = os.path.abspath(socket_path)
        self.upstream_base = (upstream_base or os.environ.get("OPENAI_API_BASE") or "").rstrip("/")
        self.upstream_key = upstream_key or os.environ.get("OPENAI_API_KEY") or ""
        self.models = dict(models or {})
        self.timeout = float(timeout)
        self.max_request_bytes = 8 * 1024 * 1024
        self._tokens: Dict[str, Dict[str, Any]] = {}
        self._token_lock = threading.Lock()
        self._audit_lock = threading.Lock()
        self._servers = []
        self._threads = []
        self._inflight: Dict[str, int] = {}
        self._max_inflight = 32
        self.tcp_port = 0

    @property
    def audit_path(self) -> str:
        return os.path.join(self.output_dir, "logs", "events.jsonl")

    def _audit(self, fields: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.audit_path), exist_ok=True)
        record = {"type": "llm_call", "timestamp": time.time(), **fields}
        with self._audit_lock:
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def audit_denied(self, scope: Optional[Dict[str, Any]], reason: str, path: str) -> None:
        scope = scope or {}
        self._audit({"role": scope.get("role"), "scope_type": scope.get("scope_type"),
                     "scope_id": scope.get("scope_id"), "model": scope.get("model"),
                     "status": 403, "allow": False, "reason": reason, "path": path})

    def issue_scope(self, role: str, scope_id: str, model: str) -> str:
        token = "sk-gan-" + secrets.token_urlsafe(32)
        with self._token_lock:
            self._tokens[token] = {"role": str(role),
                                   "scope_type": "genid" if role == "task" else "outer",
                                   "scope_id": str(scope_id), "model": str(model)}
        return token

    def issue_framework_scope(self, outer: int) -> str:
        return self.issue_scope("framework", str(outer), "")

    def scope_for(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        with self._token_lock:
            scope = self._tokens.get(token)
            return dict(scope) if scope else None

    @staticmethod
    def _bearer(value: Optional[str]) -> Optional[str]:
        if not value or not value.startswith("Bearer "):
            return None
        token = value[7:].strip()
        return token or None

    def start(self) -> None:
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)
        if os.path.exists(self.socket_path):
            if not stat_is_socket(self.socket_path):
                raise RuntimeError(f"LLM proxy socket path is not a socket: {self.socket_path}")
            os.unlink(self.socket_path)
        tcp = _ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
        tcp.proxy = self
        tcp.is_unix = False
        unix = _UnixHTTPServer(self.socket_path, _ProxyHandler)
        unix.proxy = self
        unix.is_unix = True
        os.chmod(self.socket_path, 0o666)
        self._servers = [tcp, unix]
        self.tcp_port = int(tcp.server_address[1])
        for server in self._servers:
            thread = threading.Thread(target=server.serve_forever,
                                      name="rsi-gan-llm-proxy", daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._servers = []
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            pass

    def handle_completion(self, handler: _ProxyHandler) -> None:
        token = self._bearer(handler.headers.get("Authorization"))
        scope = self.scope_for(token)
        if not scope or scope.get("role") not in {"task", "planner", "evaluator"}:
            self.audit_denied(scope, "invalid or non-agent capability token", handler.path)
            handler._reply(403, {"error": {"message": "invalid proxy capability"}})
            return
        body = handler._read_json()
        if not body or not isinstance(body.get("model"), str) or not isinstance(body.get("messages"), list):
            self.audit_denied(scope, "malformed chat completion request", handler.path)
            handler._reply(400, {"error": {"message": "model and messages are required"}})
            return
        requested_model = _normalize_model_name(body["model"])
        scoped_model = _normalize_model_name(scope.get("model"))
        allowed_model = _normalize_model_name(self.models.get(scope["role"]))
        if requested_model != scoped_model or requested_model != allowed_model:
            self.audit_denied(scope, "model is not allowed for token scope", handler.path)
            handler._reply(403, {"error": {"message": "model policy denied"}})
            return
        body["model"] = requested_model
        with self._token_lock:
            count = self._inflight.get(token, 0)
            if count >= self._max_inflight:
                self._audit({**scope, "status": 429, "allow": False,
                             "reason": "proxy concurrency limit"})
                handler._reply(429, {"error": {"message": "proxy concurrency limit"}})
                return
            self._inflight[token] = count + 1
        started = time.monotonic()
        status = 502
        usage: Dict[str, Any] = {}
        try:
            if not self.upstream_base or not self.upstream_key:
                raise RuntimeError("trusted upstream base/key is not configured")
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            url = self.upstream_base
            if not url.endswith("/v1"):
                url += "/v1"
            url += "/chat/completions"
            request = urllib.request.Request(
                url, data=payload, method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.upstream_key}"},
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(self.max_request_bytes)
                status = int(response.status)
            decoded = json.loads(raw.decode("utf-8"))
            usage = (decoded.get("usage") or {}) if isinstance(decoded, dict) else {}
            handler._reply(status, decoded if isinstance(decoded, dict) else {"data": decoded})
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read(self.max_request_bytes)
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = {"error": {"message": f"upstream HTTP {status}"}}
            handler._reply(status if status >= 500 else 502, decoded)
        except Exception:
            handler._reply(502, {"error": {"message": "upstream proxy failure"}})
            status = 502
        finally:
            with self._token_lock:
                current = self._inflight.get(token, 1)
                if current <= 1:
                    self._inflight.pop(token, None)
                else:
                    self._inflight[token] = current - 1
            self._audit({**scope, "status": status, "allow": status < 400,
                         "reason": "allowed" if status < 400 else "upstream failure",
                         "prompt_tokens": usage.get("prompt_tokens"),
                         "completion_tokens": usage.get("completion_tokens"),
                         "total_tokens": usage.get("total_tokens"),
                         "latency_ms": round((time.monotonic() - started) * 1000, 2)})





def stat_is_socket(path: str) -> bool:
    try:
        return (os.stat(path).st_mode & 0o170000) == 0o140000
    except OSError:
        return False


def request_task_token(socket_path: str, control_token: str, scope_id: str, model: str) -> str:
    """Ask the parent proxy for one generation-bound task capability."""
    body = json.dumps({"scope_id": str(scope_id), "model": str(model)}).encode("utf-8")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(10)
        sock.connect(socket_path)
        request = (b"POST /__gan__/issue-task HTTP/1.1\r\n"
                   b"Host: localhost\r\n"
                   + f"Authorization: Bearer {control_token}\r\n".encode("ascii")
                   + b"Content-Type: application/json\r\n"
                   + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                   + body)
        sock.sendall(request)
        response = http.client.HTTPResponse(sock)
        response.begin()
        raw = response.read(1024 * 1024)
        if response.status != 200:
            raise RuntimeError(f"proxy task scope registration denied (HTTP {response.status})")
        payload = json.loads(raw.decode("utf-8"))
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise RuntimeError("proxy task scope registration returned no token")
        return token
    finally:
        sock.close()
