"""Planner/evaluator process boundary with a small JSON IPC protocol."""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import socket
import subprocess
import sys
import threading
import traceback
from typing import Any, Dict, Optional, TextIO


_PROTOCOL_VERSION = 1
_MAX_MESSAGE_BYTES = 64 * 1024 * 1024
_ROLE_METHODS = {
    "planner": {"plan", "self_improve"},
    "evaluator": {"evaluate", "self_improve"},
}
_BROKER_POSITION = {"plan": 6, "evaluate": 3, "self_improve": 1}
_METHOD_SEAT = {"plan": "plan", "evaluate": "evaluate", "self_improve": "self_improve"}
_MISSING = object()


class RoleProcessError(RuntimeError):
    """The role worker failed or violated its IPC contract."""


def _json_bytes(value: Any) -> bytes:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RoleProcessError(f"role IPC value is not strict JSON: {exc}") from exc
    if len(raw) > _MAX_MESSAGE_BYTES:
        raise RoleProcessError(
            f"role IPC message exceeds {_MAX_MESSAGE_BYTES} bytes"
        )
    return raw


def _write_message(stream: TextIO, value: Dict[str, Any]) -> None:
    stream.write(_json_bytes(value).decode("utf-8") + "\n")
    stream.flush()


def _read_message(stream: TextIO) -> Dict[str, Any]:
    line = stream.readline(_MAX_MESSAGE_BYTES + 2)
    if not line:
        raise EOFError("role IPC peer closed the channel")
    if len(line.encode("utf-8")) > _MAX_MESSAGE_BYTES + 1 or not line.endswith("\n"):
        raise RoleProcessError("role IPC message is oversized or unterminated")
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RoleProcessError(f"invalid role IPC JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RoleProcessError("role IPC envelope must be an object")
    return value


def _exact_keys(value: Dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise RoleProcessError(
            f"invalid {label} fields: expected {sorted(expected)}, got {sorted(value)}"
        )


def _dict_list(value: Any, label: str) -> None:
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise RoleProcessError(f"{label} must be a list of objects")


def _validated_result(method: str, value: Any) -> Any:
    if not isinstance(value, dict):
        raise RoleProcessError(f"{method} result must be an object")
    if method == "evaluate":
        fields = {
            "predicted_score", "issues", "fix_verdicts", "penalties",
            "eval_point_results", "weaknesses",
        }
        _exact_keys(value, fields, "evaluate result")
        score = value["predicted_score"]
        if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))):
            raise RoleProcessError("evaluate predicted_score must be numeric or null")
        _dict_list(value["issues"], "evaluate issues")
        _dict_list(value["fix_verdicts"], "evaluate fix_verdicts")
        _dict_list(value["eval_point_results"], "evaluate eval_point_results")
        if not isinstance(value["penalties"], dict):
            raise RoleProcessError("evaluate penalties must be an object")
        if (not isinstance(value["weaknesses"], list)
                or not all(isinstance(v, str) for v in value["weaknesses"])):
            raise RoleProcessError("evaluate weaknesses must be a list of strings")
        from gan.framework.context import EvalContext
        return EvalContext(**value)

    if method == "plan":
        fields = {
            "records", "responses", "config", "patch", "patch_proposed",
            "patch_rejection", "attempts", "truncated", "budget_exhausted",
        }
        _exact_keys(value, fields, "plan result")
        _dict_list(value["records"], "plan records")
        _dict_list(value["responses"], "plan responses")
        if not isinstance(value["config"], dict):
            raise RoleProcessError("plan config must be an object")
    else:
        fields = {
            "records", "self_design", "patch", "patch_proposed",
            "patch_rejection", "attempts", "truncated", "budget_exhausted",
        }
        _exact_keys(value, fields, "self_improve result")
        _dict_list(value["records"], "self_improve records")
        if not isinstance(value["self_design"], str):
            raise RoleProcessError("self_improve self_design must be a string")

    for field in ("patch", "patch_proposed"):
        if not isinstance(value[field], str):
            raise RoleProcessError(f"{method} {field} must be a string")
    if value["patch_rejection"] is not None and not isinstance(value["patch_rejection"], str):
        raise RoleProcessError(f"{method} patch_rejection must be a string or null")
    if type(value["attempts"]) is not int:
        raise RoleProcessError(f"{method} attempts must be an integer")
    for field in ("truncated", "budget_exhausted"):
        if type(value[field]) is not bool:
            raise RoleProcessError(f"{method} {field} must be a boolean")
    return value


def _raise_remote(error: Dict[str, Any]) -> None:
    _exact_keys(error, {"module", "name", "message"}, "remote error")
    module = error["module"]
    name = error["name"]
    message = error["message"]
    if not all(isinstance(v, str) for v in (module, name, message)):
        raise RoleProcessError("remote error fields must be strings")
    if module == "gan.framework.code_repo" and name == "RepoIntegrityError":
        from gan.framework.code_repo import RepoIntegrityError
        raise RepoIntegrityError(message)
    raise RoleProcessError(f"role worker {name}: {message}")


class RoleProcess:
    """API-compatible proxy with one isolated worker per role seat."""

    def __init__(
        self,
        role: str,
        model: str,
        output_dir: str,
        instance: Optional[str] = None,
        code_root: Optional[str] = None,
        attempt_id: Optional[str] = None,
    ):
        if role not in _ROLE_METHODS:
            raise ValueError(f"unsupported role process: {role}")
        self.role = role
        self.model = model
        self.output_dir = os.path.abspath(output_dir)
        self.instance = instance
        self.code_root = code_root
        self.attempt_id = attempt_id
        self.tool_timeout_s: Optional[int] = None
        self.assembly_report: Optional[dict] = None
        self.runtime: Optional[dict] = None
        self._call_id = 0
        self._lock = threading.Lock()
        self._closed = False
        self._process: Optional[subprocess.Popen[str]] = None
        self._socket: Optional[socket.socket] = None
        self._reader: Optional[TextIO] = None
        self._writer: Optional[TextIO] = None
        self._seat: Optional[str] = None
        try:
            self._start_worker("plan" if role == "planner" else "evaluate")
        except Exception:
            self.close()
            raise

    def _prepare_paths(self, seat: str) -> None:
        if not self.instance:
            return
        outer = self.instance.split("_", 1)[1]
        for path in (
            os.path.join(self.output_dir, "workspaces", self.role,
                         f"{self.instance}__{seat}"),
            os.path.join(self.output_dir, "toolsets", self.role, self.instance, seat),
            os.path.join(self.output_dir, "logs", "roles", self.role, self.instance, seat),
            os.path.join(self.output_dir, "trajectory", f"outer_{outer}", "roles",
                         self.role, seat),
            os.path.join(self.output_dir, "ckpt", "design", self.role),
        ):
            os.makedirs(path, exist_ok=True)

    def _start_worker(self, seat: str) -> None:
        self._stop_worker()
        self._prepare_paths(seat)
        broker_socket = os.environ.get("GAN_ROLE_BROKER_UNIX", "").strip()
        if broker_socket:
            token = os.environ.get(f"GAN_ROLE_BROKER_{self.role.upper()}_TOKEN", "")
            if not token or not self.instance:
                raise RoleProcessError("role broker launch capability is missing")
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            channel.connect(broker_socket)
            channel.sendall(_json_bytes({
                "token": token, "role": self.role, "seat": seat,
                "instance": self.instance,
            }) + b"\n")
            auth_reader = channel.makefile("r", encoding="utf-8", errors="strict")
            auth = _read_message(auth_reader)
            if not auth.get("ok"):
                auth_reader.close()
                channel.close()
                raise RoleProcessError(f"role broker denied launch: {auth.get('error', 'invalid reply')}")
            self._socket = channel
            self._reader = auth_reader
            self._writer = channel.makefile("w", encoding="utf-8", errors="strict")
        else:
            env = os.environ.copy()
            env.update({"PYTHONUNBUFFERED": "1", "GAN_ROLE_SEAT": seat})
            process = subprocess.Popen(
                [sys.executable, "-m", "gan.framework.role_process", "--worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True,
                encoding="utf-8", errors="strict", bufsize=1, close_fds=True, env=env,
            )
            if process.stdin is None or process.stdout is None:
                process.terminate()
                raise RoleProcessError("failed to create role IPC pipes")
            self._process = process
            self._reader, self._writer = process.stdout, process.stdin
        self._seat = seat
        _write_message(self._writer, {
            "v": _PROTOCOL_VERSION, "type": "init", "role": self.role,
            "model": self.model, "output_dir": self.output_dir,
            "instance": self.instance, "code_root": self.code_root,
            "attempt_id": self.attempt_id,
        })
        while True:
            reply = _read_message(self._reader)
            if reply.get("type") == "event":
                self._handle_event(reply)
                continue
            if reply.get("type") == "error":
                _exact_keys(reply, {"v", "type", "error"}, "init error")
                _raise_remote(reply["error"])
            _exact_keys(reply, {"v", "type", "assembly_report", "runtime"}, "init reply")
            if reply["v"] != _PROTOCOL_VERSION or reply["type"] != "ready":
                raise RoleProcessError("unexpected role worker init reply")
            report = reply["assembly_report"]
            if report is not None and not isinstance(report, dict):
                raise RoleProcessError("assembly_report must be an object or null")
            runtime = reply["runtime"]
            if not isinstance(runtime, dict):
                raise RoleProcessError("role runtime report must be an object")
            expected_uid = os.environ.get(f"GAN_ROLE_{self.role.upper()}_UID")
            expected_gid = os.environ.get(f"GAN_ROLE_{self.role.upper()}_GID")
            if broker_socket and (
                not expected_uid or not expected_gid
                or runtime.get("euid") != int(expected_uid)
                or runtime.get("egid") != int(expected_gid)
                or runtime.get("no_new_privs") != "1"
                or str(runtime.get("cap_eff", "")).strip("0")
                or runtime.get("seat") != seat
            ):
                raise RoleProcessError(f"role runtime boundary is not active: {runtime}")
            self.assembly_report = report
            self.runtime = runtime
            return

    def _stop_worker(self) -> None:
        writer, process = self._writer, self._process
        if writer is not None:
            try:
                _write_message(writer, {"v": _PROTOCOL_VERSION, "type": "shutdown"})
            except (BrokenPipeError, OSError, RoleProcessError):
                pass
        for stream in (self._writer, self._reader):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=2)
        self._process = None
        self._socket = None
        self._reader = None
        self._writer = None
        self._seat = None

    def plan(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if self.role != "planner":
            raise AttributeError("evaluate role has no plan method")
        return self._invoke("plan", args, kwargs)

    def evaluate(self, *args: Any, **kwargs: Any) -> Any:
        if self.role != "evaluator":
            raise AttributeError("planner role has no evaluate method")
        return self._invoke("evaluate", args, kwargs)

    def self_improve(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self._invoke("self_improve", args, kwargs)

    def _invoke(self, method: str, args: tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
        if method not in _ROLE_METHODS[self.role]:
            raise AttributeError(f"{self.role} role has no {method} method")
        call_args = list(args)
        call_kwargs = dict(kwargs)
        broker = call_kwargs.pop("broker", _MISSING)
        broker_index = _BROKER_POSITION[method]
        if len(call_args) > broker_index:
            if broker is not _MISSING:
                raise TypeError("broker was supplied both positionally and by keyword")
            broker = call_args[broker_index]
            call_args[broker_index] = None
        elif broker is _MISSING:
            broker = None
        _json_bytes(call_args)
        _json_bytes(call_kwargs)
        with self._lock:
            if self._closed:
                raise RoleProcessError("role worker is not running")
            seat = _METHOD_SEAT[method]
            if self._seat != seat:
                self._start_worker(seat)
            if self._reader is None or self._writer is None:
                raise RoleProcessError("role worker channel is not running")
            self._call_id += 1
            call_id = self._call_id
            if broker is not None and hasattr(broker, "set_trajectory_scope"):
                scope_index = {"plan": 8, "evaluate": 7,
                               "self_improve": 3}.get(method)
                scope = None
                if scope_index is not None and len(call_args) > scope_index:
                    scope = call_args[scope_index]
                if scope is None:
                    scope = call_kwargs.get("trajectory_genids")
                broker.set_trajectory_scope(
                    self.role, f"{self.instance}__{seat}",
                    scope if isinstance(scope, list) else [])
            _write_message(self._writer, {
                "v": _PROTOCOL_VERSION,
                "type": "call",
                "id": call_id,
                "method": method,
                "args": call_args,
                "kwargs": call_kwargs,
                "has_broker": broker is not None,
                "broker_info": ({"repo_root": broker.repo_root,
                                 "output_dir": broker.output_dir}
                                if broker is not None else None),
                "role_state": {
                    "attempt_id": self.attempt_id,
                    "tool_timeout_s": self.tool_timeout_s,
                },
            })
            while True:
                try:
                    reply = _read_message(self._reader)
                except EOFError as exc:
                    code = self._process.poll() if self._process is not None else None
                    raise RoleProcessError(f"role worker exited during {method} (exit={code})") from exc
                if reply.get("type") == "event":
                    self._handle_event(reply)
                    continue
                if reply.get("type") == "broker_call":
                    self._handle_broker_call(reply, call_id, method, broker)
                    continue
                if reply.get("type") == "error":
                    _exact_keys(reply, {"v", "type", "id", "error"}, "call error")
                    if reply["id"] != call_id:
                        raise RoleProcessError("role IPC call id mismatch")
                    _raise_remote(reply["error"])
                _exact_keys(
                    reply, {"v", "type", "id", "result", "assembly_report"},
                    "call reply",
                )
                if (reply["v"] != _PROTOCOL_VERSION or reply["type"] != "result"
                        or reply["id"] != call_id):
                    raise RoleProcessError("unexpected role worker call reply")
                report = reply["assembly_report"]
                if report is not None and not isinstance(report, dict):
                    raise RoleProcessError("assembly_report must be an object or null")
                self.assembly_report = report
                return _validated_result(method, reply["result"])

    def _handle_event(self, request: Dict[str, Any]) -> None:
        _exact_keys(request, {"v", "type", "event"}, "role event")
        if request["v"] != _PROTOCOL_VERSION or not isinstance(request["event"], dict):
            raise RoleProcessError("invalid role event")
        from gan.framework import paths
        from utils import trajectory_log
        trajectory_log.append(paths.events_path(self.output_dir), request["event"])

    def _handle_broker_call(
        self, request: Dict[str, Any], call_id: int, role_method: str, broker: Any
    ) -> None:
        _exact_keys(
            request, {"v", "type", "id", "request_id", "method", "args", "kwargs"},
            "broker call",
        )
        if (request["v"] != _PROTOCOL_VERSION or request["id"] != call_id
                or type(request["request_id"]) is not int):
            raise RoleProcessError("invalid broker call identity")
        response = {
            "v": _PROTOCOL_VERSION,
            "type": "broker_result",
            "id": call_id,
            "request_id": request["request_id"],
        }
        try:
            response["result"] = _dispatch_broker(
                broker, request["method"], request["args"], request["kwargs"],
                expected_role=self.role,
                expected_node=self.instance,
                expected_seat=_METHOD_SEAT[role_method],
            )
            response["error"] = None
        except Exception as exc:  # sent back to the synchronous child caller
            response["result"] = None
            response["error"] = {
                "module": type(exc).__module__,
                "name": type(exc).__name__,
                "message": str(exc)[:4000],
            }
        if self._writer is None:
            raise RoleProcessError("role worker channel is not running")
        _write_message(self._writer, response)

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        self._stop_worker()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _dispatch_broker(
    broker: Any,
    method: Any,
    args: Any,
    kwargs: Any,
    *,
    expected_role: str,
    expected_node: Optional[str],
    expected_seat: str,
) -> Any:
    if broker is None:
        raise PermissionError("this role call has no access broker")
    if not isinstance(method, str) or not isinstance(args, list) or not isinstance(kwargs, dict):
        raise ValueError("invalid broker RPC request")
    allowed = {
        "grant", "src_dir", "granted_paths", "covers", "grant_records",
        "log_event", "check_patch", "read_trajectory",
        "read_session_trajectory", "session_trajectory_index", "build_patch",
        "editable_paths",
    }
    if method not in allowed:
        raise PermissionError(f"broker method is not exposed: {method}")
    if method not in {"log_event", "check_patch"}:
        role_arg = args[0] if args else kwargs.get("role")
        node_arg = args[1] if len(args) > 1 else kwargs.get("node_id")
        if role_arg != expected_role:
            raise PermissionError("broker RPC role does not match the role process")
        if expected_node is not None and str(node_arg) != str(expected_node):
            raise PermissionError("broker RPC node does not match the role process instance")
    if "seat" in kwargs and kwargs.get("seat") != expected_seat:
        raise PermissionError("broker RPC seat does not match the active role method")
    if method == "check_patch":
        if len(args) != 1 or set(kwargs) != {"role", "seat"}:
            raise TypeError("check_patch expects patch, role, and seat")
        if kwargs["role"] != expected_role or kwargs["seat"] != expected_seat:
            raise PermissionError("check_patch identity does not match the active role seat")
        from gan.framework import code_repo
        return list(code_repo.check_patch(
            broker.repo_root, str(args[0]), role=expected_role, seat=expected_seat))
    if method == "build_patch":
        if len(args) != 2 or set(kwargs) != {"seat"}:
            raise TypeError("build_patch expects role and node_id")
        if args[0] != expected_role:
            raise PermissionError("build_patch role does not match the role process")
        node = (f"{args[1]}__{expected_seat}" if expected_node is not None
                else args[1])
        return broker.build_patch(str(args[0]), node, seat=expected_seat)
    seat_node = (f"{expected_node}__{expected_seat}"
                 if expected_node is not None else None)
    if method not in {"log_event", "check_patch"} and seat_node is not None:
        args = list(args)
        if len(args) > 1:
            args[1] = seat_node
        else:
            kwargs = dict(kwargs)
            kwargs["node_id"] = seat_node
    if method == "grant_records":
        if len(args) != 2 or kwargs:
            raise TypeError("grant_records expects role and node_id")
        return list(broker.grants.get((str(args[0]), str(args[1])), []))
    target = getattr(broker, method)
    result = target(*args, **kwargs)
    if method == "grant":
        return {"granted": result, "last_result": broker.last_result}
    return result


class _RemoteGrants:
    def __init__(self, broker: "_RemoteBroker"):
        self._broker = broker

    def get(self, key: Any, default: Any = None) -> Any:
        if not isinstance(key, (list, tuple)) or len(key) != 2:
            return default
        records = self._broker._rpc("grant_records", [key[0], key[1]], {})
        return records if records else default


class _RemoteBroker:
    def __init__(self, reader: TextIO, writer: TextIO, call_id: int,
                 repo_root: str, output_dir: str):
        self._reader = reader
        self._writer = writer
        self._call_id = call_id
        self._request_id = 0
        self.repo_root = repo_root
        self.output_dir = output_dir
        self.last_result: Dict[str, Any] = {}
        self.grants = _RemoteGrants(self)

    def _rpc(self, method: str, args: list[Any], kwargs: Dict[str, Any]) -> Any:
        self._request_id += 1
        request_id = self._request_id
        _write_message(self._writer, {
            "v": _PROTOCOL_VERSION,
            "type": "broker_call",
            "id": self._call_id,
            "request_id": request_id,
            "method": method,
            "args": args,
            "kwargs": kwargs,
        })
        reply = _read_message(self._reader)
        _exact_keys(
            reply, {"v", "type", "id", "request_id", "result", "error"},
            "broker result",
        )
        if (reply["v"] != _PROTOCOL_VERSION or reply["type"] != "broker_result"
                or reply["id"] != self._call_id or reply["request_id"] != request_id):
            raise RoleProcessError("broker RPC identity mismatch")
        if reply["error"] is not None:
            error = reply["error"]
            name = error.get("name") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else "broker RPC failed"
            exc_type = {
                "PermissionError": PermissionError,
                "ValueError": ValueError,
                "TypeError": TypeError,
            }.get(name, RuntimeError)
            raise exc_type(message)
        return reply["result"]

    def read_trajectory(self, role: str, node_id: Any, genid: Any,
                        max_chars: int, *, seat: str) -> str:
        return str(self._rpc("read_trajectory",
                             [role, node_id, genid, int(max_chars)], {"seat": seat}))

    def read_session_trajectory(self, role: str, node_id: Any, outer: Any,
                                genid: Any, max_chars: int, *, seat: str) -> str:
        return str(self._rpc("read_session_trajectory",
                             [role, node_id, outer, genid, int(max_chars)],
                             {"seat": seat}))

    def session_trajectory_index(self, role: str, node_id: Any, outer: Any,
                                 *, seat: str) -> list[dict[str, Any]]:
        result = self._rpc("session_trajectory_index", [role, node_id, outer],
                           {"seat": seat})
        return list(result or [])

    def build_patch(self, role: str, node_id: Any, *, seat: str) -> str:
        return str(self._rpc("build_patch", [role, node_id], {"seat": seat}))

    def editable_paths(self, role: str, node_id: Any, seat: str,
                       cap: int = 300) -> dict[str, list[str]]:
        result = self._rpc("editable_paths", [role, node_id, int(cap)], {"seat": seat})
        return dict(result or {})

    def grant(self, *args: Any, **kwargs: Any) -> Any:
        result = self._rpc("grant", list(args), kwargs)
        if not isinstance(result, dict) or set(result) != {"granted", "last_result"}:
            raise RoleProcessError("invalid broker grant result")
        self.last_result = result["last_result"]
        return result["granted"]

    def src_dir(self, *args: Any, **kwargs: Any) -> str:
        return self._rpc("src_dir", list(args), kwargs)

    def granted_paths(self, *args: Any, **kwargs: Any) -> list[str]:
        return self._rpc("granted_paths", list(args), kwargs)

    def covers(self, *args: Any, **kwargs: Any) -> bool:
        return self._rpc("covers", list(args), kwargs)

    def check_patch(self, patch: str, *, role: str, seat: str) -> Any:
        return tuple(self._rpc("check_patch", [patch], {"role": role, "seat": seat}))

    def log_event(self, *args: Any, **kwargs: Any) -> None:
        self._rpc("log_event", list(args), kwargs)


def _error_payload(exc: BaseException) -> Dict[str, str]:
    return {
        "module": type(exc).__module__,
        "name": type(exc).__name__,
        "message": str(exc)[:4000],
    }


def _runtime_report() -> Dict[str, Any]:
    status = {}
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if ":" in line:
                    key, value = line.split(":", 1)
                    status[key] = value.strip()
    except OSError:
        pass
    return {
        "pid": os.getpid(),
        "euid": os.geteuid() if hasattr(os, "geteuid") else None,
        "egid": os.getegid() if hasattr(os, "getegid") else None,
        "seat": os.environ.get("GAN_ROLE_SEAT"),
        "no_new_privs": status.get("NoNewPrivs"),
        "cap_eff": status.get("CapEff"),
    }


def _worker_main() -> None:
    reader = sys.stdin
    protocol_writer = sys.stdout
    try:
        init = _read_message(reader)
        _exact_keys(
            init,
            {"v", "type", "role", "model", "output_dir", "instance", "code_root", "attempt_id"},
            "init request",
        )
        if init["v"] != _PROTOCOL_VERSION or init["type"] != "init":
            raise RoleProcessError("unsupported role IPC version")
        role_name = init["role"]
        if role_name not in _ROLE_METHODS:
            raise ValueError(f"unsupported role process: {role_name}")
        from gan.framework import paths as role_paths
        from utils import trajectory_log as role_trajectory_log
        original_append = role_trajectory_log.append
        event_path = os.path.realpath(role_paths.events_path(init["output_dir"]))

        def append_with_parent_event(path: str, record: Dict[str, Any],
                                     max_bytes: Optional[int] =
                                     role_trajectory_log.DEFAULT_MAX_BYTES) -> None:
            if os.path.realpath(path) == event_path:
                _write_message(protocol_writer, {
                    "v": _PROTOCOL_VERSION, "type": "event", "event": record,
                })
                return
            original_append(path, record, max_bytes=max_bytes)

        role_trajectory_log.append = append_with_parent_event
        with contextlib.redirect_stdout(sys.stderr):
            if role_name == "planner":
                from gan.roles.planner import Planner as RoleClass
            else:
                from gan.roles.evaluator import Evaluator as RoleClass
            role = RoleClass(
                init["model"], init["output_dir"], instance=init["instance"],
                code_root=init["code_root"], attempt_id=init["attempt_id"],
            )
        _write_message(protocol_writer, {
            "v": _PROTOCOL_VERSION,
            "type": "ready",
            "assembly_report": role.assembly_report,
            "runtime": _runtime_report(),
        })
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        _write_message(protocol_writer, {
            "v": _PROTOCOL_VERSION, "type": "error", "error": _error_payload(exc)
        })
        return

    while True:
        request: Dict[str, Any] = {}
        try:
            request = _read_message(reader)
            if request.get("type") == "shutdown":
                _exact_keys(request, {"v", "type"}, "shutdown request")
                return
            _exact_keys(
                request,
                {"v", "type", "id", "method", "args", "kwargs", "has_broker",
                 "broker_info", "role_state"},
                "call request",
            )
            call_id = request["id"]
            method = request["method"]
            if (request["v"] != _PROTOCOL_VERSION or request["type"] != "call"
                    or type(call_id) is not int or method not in _ROLE_METHODS[role_name]
                    or not isinstance(request["args"], list)
                    or not isinstance(request["kwargs"], dict)):
                raise RoleProcessError("invalid role call request")
            state = request["role_state"]
            if not isinstance(state, dict) or set(state) != {"attempt_id", "tool_timeout_s"}:
                raise RoleProcessError("invalid role state")
            role.attempt_id = state["attempt_id"]
            role.tool_timeout_s = state["tool_timeout_s"]
            args = list(request["args"])
            kwargs = dict(request["kwargs"])
            if request["has_broker"]:
                info = request["broker_info"]
                if not isinstance(info, dict) or set(info) != {"repo_root", "output_dir"}:
                    raise RoleProcessError("invalid broker info")
                remote = _RemoteBroker(
                    reader, protocol_writer, call_id, info["repo_root"], info["output_dir"])
                index = _BROKER_POSITION[method]
                if len(args) > index:
                    args[index] = remote
                else:
                    kwargs["broker"] = remote
            with contextlib.redirect_stdout(sys.stderr):
                result = getattr(role, method)(*args, **kwargs)
            if dataclasses.is_dataclass(result):
                result = dataclasses.asdict(result)
            _json_bytes(result)
            _write_message(protocol_writer, {
                "v": _PROTOCOL_VERSION,
                "type": "result",
                "id": call_id,
                "result": result,
                "assembly_report": role.assembly_report,
            })
        except EOFError:
            return
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            _write_message(protocol_writer, {
                "v": _PROTOCOL_VERSION,
                "type": "error",
                "id": request.get("id") if isinstance(request, dict) else None,
                "error": _error_payload(exc),
            })


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if not args.worker:
        parser.error("this module is an internal role worker")
    _worker_main()


if __name__ == "__main__":
    main()
