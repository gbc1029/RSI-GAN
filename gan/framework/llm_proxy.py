"""Parent-side OpenAI-compatible proxy used by GAN agent processes."""
from __future__ import annotations

import http.client
import json
import os
import secrets
import socket
import socketserver
import struct
import subprocess
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


class _UnixHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True
    allow_reuse_address = False
    max_connection_threads = 64
    request_queue_size = 64

    def server_activate(self) -> None:
        self._connection_slots = threading.BoundedSemaphore(
            self.max_connection_threads)
        self._active_lock = threading.Lock()
        self._active_requests = set()
        super().server_activate()

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(10.0)
        return request, address

    def process_request(self, request, client_address) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._active_lock:
            self._active_requests.add(request)
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._active_lock:
                self._active_requests.discard(request)
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_lock:
                self._active_requests.discard(request)
            self._connection_slots.release()

    def close_active_requests(self) -> None:
        with self._active_lock:
            requests = list(self._active_requests)
        for request in requests:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                request.close()
            except OSError:
                pass


class _ProxyHandler(BaseHTTPRequestHandler):
    server_version = "RSI-GAN-LLM-Proxy/1"

    def log_message(self, *_args: Any) -> None:
        return

    def setup(self) -> None:
        super().setup()
        self._deadline = threading.Timer(10.0, self._expire_deadline)
        self._deadline.daemon = True
        self._deadline.start()

    def finish(self) -> None:
        self._clear_deadline()
        super().finish()

    def _expire_deadline(self) -> None:
        try:
            self.request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _clear_deadline(self) -> None:
        deadline = getattr(self, "_deadline", None)
        if deadline is not None:
            deadline.cancel()
            self._deadline = None

    @property
    def proxy(self) -> "ParentLLMProxy":
        return self.server.proxy  # type: ignore[attr-defined]

    @property
    def is_unix(self) -> bool:
        return True

    @property
    def peer_uid(self) -> int:
        if not hasattr(socket, "SO_PEERCRED"):
            raise PermissionError("LLM proxy peer credentials are unavailable")
        credentials = self.request.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _peer_pid, peer_uid, _peer_gid = struct.unpack("3i", credentials)
        return int(peer_uid)

    def _reply(self, status: int, payload: Dict[str, Any]) -> bool:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return False
        return True

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/__gan__/issue-task":
            self._issue_task()
            return
        if self.path == "/__gan__/revoke":
            self._revoke()
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
            raw = self.rfile.read(size)
        except OSError:
            return None
        self._clear_deadline()
        self.request.settimeout(None)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _issue_task(self) -> None:
        if not self.is_unix:
            self._reply(403, {"error": {"message": "framework registration requires Unix transport"}})
            return
        token = self.proxy._bearer(self.headers.get("Authorization"))
        scope = self.proxy.scope_for(token)
        if (not scope or scope.get("role") != "framework"
                or int(scope.get("peer_uid", -1)) != self.peer_uid):
            self.proxy.audit_denied(scope, "task scope registration denied", self.path)
            self._reply(403, {"error": {"message": "registration capability denied"}})
            return
        body = self._read_json()
        if (not body or not body.get("scope_id") or not body.get("model")
                or type(body.get("ttl_s")) not in {int, float}
                or type(body.get("max_calls")) is not int
                or type(body.get("peer_uid")) is not int
                or int(body["peer_uid"]) < 1):
            self._reply(400, {"error": {"message":
                "scope_id, model, ttl_s, max_calls and peer_uid are required"}})
            return
        try:
            issued = self.proxy.issue_scope(
                "task", str(body["scope_id"]), str(body["model"]),
                ttl_s=float(body["ttl_s"]), max_calls=int(body["max_calls"]),
                peer_uid=int(body["peer_uid"]),
            )
        except ValueError as exc:
            self._reply(400, {"error": {"message": str(exc)}})
            return
        self._reply(200, {"token": issued})

    def _revoke(self) -> None:
        if not self.is_unix:
            self._reply(403, {"error": {"message": "framework revoke requires Unix transport"}})
            return
        control = self.proxy._bearer(self.headers.get("Authorization"))
        scope = self.proxy.scope_for(control)
        if (not scope or scope.get("role") != "framework"
                or int(scope.get("peer_uid", -1)) != self.peer_uid):
            self.proxy.audit_denied(scope, "scope revoke denied", self.path)
            self._reply(403, {"error": {"message": "revoke capability denied"}})
            return
        body = self._read_json()
        token = body.get("token") if body else None
        if not isinstance(token, str) or not token:
            self._reply(400, {"error": {"message": "token is required"}})
            return
        self.proxy.revoke_scope(token)
        self._reply(200, {"revoked": True})


class ParentLLMProxy:
    """Per-run proxy serving only an ACL-restricted Unix socket."""

    def __init__(self, output_dir: str, socket_path: str, *,
                 upstream_base: Optional[str] = None,
                 upstream_key: Optional[str] = None,
                 models: Optional[Dict[str, str]] = None,
                 timeout: float = 120.0,
                 socket_uids: Optional[list[int]] = None,
                 setfacl_path: Optional[str] = None) -> None:
        self.output_dir = os.path.abspath(output_dir)
        self.socket_path = os.path.abspath(socket_path)
        self.upstream_base = (upstream_base or os.environ.get("OPENAI_API_BASE") or "").rstrip("/")
        self.upstream_key = upstream_key or os.environ.get("OPENAI_API_KEY") or ""
        self.models = dict(models or {})
        self.timeout = float(timeout)
        self.socket_uids = sorted(set(int(uid) for uid in (socket_uids or [])))
        self.setfacl_path = setfacl_path
        self.max_request_bytes = 8 * 1024 * 1024
        self._tokens: Dict[str, Dict[str, Any]] = {}
        self._token_lock = threading.Lock()
        self._audit_lock = threading.Lock()
        self._server: Optional[_UnixHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._inflight: Dict[str, int] = {}
        self._max_inflight = 32

    @property
    def audit_path(self) -> str:
        return os.path.join(self.output_dir, "logs", "events.jsonl")

    def _audit(self, fields: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.audit_path), exist_ok=True)
        now = time.time()
        readable = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) \
            + f".{int(now * 1000) % 1000:03d}"
        record = {"type": "llm_call", "timestamp": now, "time": readable, **fields}
        with self._audit_lock:
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def audit_denied(self, scope: Optional[Dict[str, Any]], reason: str, path: str) -> None:
        scope = scope or {}
        self._audit({"role": scope.get("role"), "scope_type": scope.get("scope_type"),
                     "scope_id": scope.get("scope_id"), "seat": scope.get("seat"),
                     "model": scope.get("model"),
                     "status": 403, "allow": False, "reason": reason, "path": path})

    def issue_scope(self, role: str, scope_id: str, model: str, *,
                    seat: Optional[str] = None,
                    ttl_s: Optional[float] = None,
                    max_calls: Optional[int] = None,
                    peer_uid: Optional[int] = None) -> str:
        role_seats = {
            "planner": {"plan", "self_improve"},
            "evaluator": {"evaluate", "self_improve"},
        }
        if role in role_seats:
            if seat not in role_seats[role]:
                raise ValueError(f"invalid LLM proxy seat for {role}: {seat!r}")
        elif seat is not None:
            raise ValueError(f"LLM proxy seat is not valid for role {role!r}")
        if role == "task":
            if ttl_s is None or ttl_s <= 0:
                raise ValueError("task LLM scope TTL must be positive")
            if max_calls is None or max_calls < 1:
                raise ValueError("task LLM scope call budget must be positive")
        elif ttl_s is not None or max_calls is not None:
            raise ValueError("TTL and call budget are valid only for task scopes")
        expected_uid = os.geteuid() if peer_uid is None else int(peer_uid)
        if expected_uid < 0:
            raise ValueError("LLM proxy peer uid must be non-negative")
        token = "sk-gan-" + secrets.token_urlsafe(32)
        with self._token_lock:
            self._tokens[token] = {"role": str(role),
                                   "scope_type": "genid" if role == "task" else "outer",
                                   "scope_id": str(scope_id), "seat": seat,
                                   "model": str(model),
                                   "peer_uid": expected_uid,
                                   "expires_at": (time.monotonic() + float(ttl_s)
                                                  if ttl_s is not None else None),
                                   "remaining_calls": max_calls,
                                   "revoked": False}
        return token

    def issue_framework_scope(self, outer: int, *, peer_uid: Optional[int] = None) -> str:
        return self.issue_scope("framework", str(outer), "", peer_uid=peer_uid)

    def scope_for(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        with self._token_lock:
            scope = self._tokens.get(token)
            return dict(scope) if scope else None

    def revoke_scope(self, token: str) -> None:
        with self._token_lock:
            scope = self._tokens.get(token)
            if scope is not None:
                scope["revoked"] = True

    def _authorize_completion(self, token: Optional[str], peer_uid: int
                              ) -> tuple[Optional[Dict[str, Any]], str]:
        if not token:
            return None, "invalid or non-agent capability token"
        with self._token_lock:
            scope = self._tokens.get(token)
            if scope is None or scope.get("role") not in {"task", "planner", "evaluator"}:
                return (dict(scope) if scope else None,
                        "invalid or non-agent capability token")
            if int(scope.get("peer_uid", -1)) != int(peer_uid):
                return dict(scope), "proxy capability peer uid mismatch"
            if scope.get("revoked"):
                return dict(scope), "revoked proxy capability"
            expires_at = scope.get("expires_at")
            if expires_at is not None and time.monotonic() >= float(expires_at):
                scope["revoked"] = True
                return dict(scope), "expired proxy capability"
            remaining = scope.get("remaining_calls")
            if remaining is not None:
                if int(remaining) < 1:
                    scope["revoked"] = True
                    return dict(scope), "proxy call budget exhausted"
                scope["remaining_calls"] = int(remaining) - 1
            return dict(scope), ""

    @staticmethod
    def _bearer(value: Optional[str]) -> Optional[str]:
        if not value or not value.startswith("Bearer "):
            return None
        token = value[7:].strip()
        return token or None

    def bind(self) -> None:
        if self._server is not None:
            raise RuntimeError("LLM proxy socket is already bound")
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)
        if os.path.lexists(self.socket_path):
            raise RuntimeError(f"LLM proxy socket path already exists: {self.socket_path}")
        unix = _UnixHTTPServer(self.socket_path, _ProxyHandler)
        unix.proxy = self
        try:
            os.chmod(self.socket_path, 0o600)
            if self.socket_uids:
                if not self.setfacl_path:
                    raise RuntimeError("LLM proxy socket ACL tool is missing")
                for uid in self.socket_uids:
                    subprocess.run(
                        [self.setfacl_path, "-m", f"u:{uid}:rw", self.socket_path],
                        check=True, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, text=True,
                    )
        except Exception:
            unix.server_close()
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
            raise
        self._server = unix

    def start(self) -> None:
        if self._server is None:
            self.bind()
        if self._started:
            raise RuntimeError("LLM proxy is already running")
        assert self._server is not None
        thread = threading.Thread(target=self._server.serve_forever,
                                  name="rsi-gan-llm-proxy", daemon=True)
        thread.start()
        self._thread = thread
        self._started = True

    def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            if self._started:
                server.shutdown()
            server.close_active_requests()
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)
        self._started = False
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            pass

    def handle_completion(self, handler: _ProxyHandler) -> None:
        token = self._bearer(handler.headers.get("Authorization"))
        scope, denied = self._authorize_completion(token, handler.peer_uid)
        if denied:
            self.audit_denied(scope, denied, handler.path)
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
        delivered = True
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
            delivered = handler._reply(
                status, decoded if isinstance(decoded, dict) else {"data": decoded})
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read(self.max_request_bytes)
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = {"error": {"message": f"upstream HTTP {status}"}}
            delivered = handler._reply(status if status >= 500 else 502, decoded)
        except Exception:
            status = 502
            delivered = handler._reply(
                502, {"error": {"message": "upstream proxy failure"}})
        finally:
            with self._token_lock:
                current = self._inflight.get(token, 1)
                if current <= 1:
                    self._inflight.pop(token, None)
                else:
                    self._inflight[token] = current - 1
            reason = ("client_disconnected" if not delivered else
                      ("allowed" if status < 400 else "upstream failure"))
            self._audit({**scope, "status": status, "allow": status < 400,
                         "reason": reason, "delivered": delivered,
                         "prompt_tokens": usage.get("prompt_tokens"),
                         "completion_tokens": usage.get("completion_tokens"),
                         "total_tokens": usage.get("total_tokens"),
                         # Thinking-intensity knob as requested by the caller
                         # (absent when the client sent none); observability
                         # only -- it is not part of the model policy check.
                         "reasoning_effort": body.get("reasoning_effort"),
                         "latency_ms": round((time.monotonic() - started) * 1000, 2)})

def _control_request(socket_path: str, control_token: str, path: str,
                     body: Dict[str, Any]) -> Dict[str, Any]:
    encoded = json.dumps(body).encode("utf-8")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(10)
        sock.connect(socket_path)
        request = (f"POST {path} HTTP/1.1\r\n".encode("ascii")
                   + b"Host: localhost\r\n"
                   + f"Authorization: Bearer {control_token}\r\n".encode("ascii")
                   + b"Content-Type: application/json\r\n"
                   + f"Content-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode("ascii")
                   + encoded)
        sock.sendall(request)
        response = http.client.HTTPResponse(sock)
        response.begin()
        raw = response.read(1024 * 1024)
        if response.status != 200:
            raise RuntimeError(f"proxy control request denied (HTTP {response.status})")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("proxy control request returned an invalid response")
        return payload
    finally:
        sock.close()


def request_task_token(socket_path: str, control_token: str, scope_id: str,
                       model: str, *, ttl_s: float, max_calls: int,
                       peer_uid: int) -> str:
    """Ask the parent proxy for one generation-bound task capability."""
    payload = _control_request(socket_path, control_token, "/__gan__/issue-task", {
        "scope_id": str(scope_id), "model": str(model),
        "ttl_s": float(ttl_s), "max_calls": int(max_calls),
        "peer_uid": int(peer_uid),
    })
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("proxy task scope registration returned no token")
    return token


def revoke_token(socket_path: str, control_token: str, token: str) -> None:
    """Revoke an issued agent capability through the framework-only channel."""
    _control_request(socket_path, control_token, "/__gan__/revoke", {"token": token})
