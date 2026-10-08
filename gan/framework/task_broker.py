"""Root-only launcher for TaskAgent harness processes.

The outer worker may request a generation by id, but cannot choose a command,
path, model, interpreter, environment, or host identity.  Those values are
fixed before the evolvable worker starts.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import stat
import threading
from multiprocessing.connection import Connection
from typing import Any, Dict, Tuple


_SCOPE_RE = re.compile(r"(?:0|[1-9][0-9]{0,11})\Z")
_MAX_REQUEST_BYTES = 16 * 1024
_MAX_OUTPUT_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _reply(handler: socketserver.StreamRequestHandler,
           payload: Dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    handler.wfile.write(encoded)


def _set_parent_death_signal() -> None:
    """Ask Linux to terminate this privileged child if its parent exits."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM) != 0:  # PR_SET_PDEATHSIG
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    except (AttributeError, OSError):
        pass


def _validate_run_tree(run_dir: str, owner_uid: int) -> None:
    """Reject path redirection before root changes ownership of the run copy."""
    for root, dirs, files in os.walk(run_dir, topdown=True, followlinks=False):
        root_stat = os.lstat(root)
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise RuntimeError(f"task run tree contains an invalid directory: {root}")
        if root_stat.st_uid != owner_uid:
            raise RuntimeError(f"task run directory has an unexpected owner: {root}")
        for name in dirs + files:
            path = os.path.join(root, name)
            item_stat = os.lstat(path)
            if stat.S_ISLNK(item_stat.st_mode):
                raise RuntimeError(f"task run tree contains a symlink: {path}")
            if item_stat.st_uid != owner_uid:
                raise RuntimeError(f"task run path has an unexpected owner: {path}")
            if stat.S_ISREG(item_stat.st_mode) and item_stat.st_nlink != 1:
                raise RuntimeError(f"task run tree contains a hard-linked file: {path}")


def _restore_directory_ownership(run_dir: str, uid: int, gid: int) -> None:
    """Let the unprivileged framework score and remove the completed run copy."""
    for root, dirs, _files in os.walk(run_dir, topdown=False, followlinks=False):
        for name in dirs:
            path = os.path.join(root, name)
            try:
                if stat.S_ISDIR(os.lstat(path).st_mode):
                    os.chown(path, uid, gid, follow_symlinks=False)
            except FileNotFoundError:
                continue
        try:
            if stat.S_ISDIR(os.lstat(root).st_mode):
                os.chown(root, uid, gid, follow_symlinks=False)
        except FileNotFoundError:
            continue


class _TaskBrokerState:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._launch_tokens: Dict[str, int] = {}
        self._used_scopes = set()

    def register_outer(self, budget: int) -> str:
        if budget < 1:
            raise ValueError("task broker budget must be positive")
        token = "tb-gan-" + secrets.token_urlsafe(32)
        with self._lock:
            self._launch_tokens[token] = int(budget)
        return token

    def _reserve(self, token: str, scope_id: str) -> None:
        if not _SCOPE_RE.fullmatch(scope_id):
            raise PermissionError("invalid task scope id")
        with self._lock:
            remaining = self._launch_tokens.get(token)
            if remaining is None:
                raise PermissionError("invalid task launch capability")
            if remaining < 1:
                raise PermissionError("task launch budget exhausted")
            if scope_id in self._used_scopes:
                raise PermissionError("task scope was already launched")
            self._launch_tokens[token] = remaining - 1
            self._used_scopes.add(scope_id)

    def _run_dir(self, scope_id: str) -> Tuple[str, str]:
        work_root = os.path.realpath(os.path.join(self.config["output_dir"], "work"))
        node_dir = os.path.join(work_root, scope_id)
        run_dir = os.path.join(node_dir, "repo")
        if os.path.realpath(run_dir) != run_dir:
            raise RuntimeError("task run path contains a symlink")
        if os.path.commonpath([work_root, run_dir]) != work_root:
            raise RuntimeError("task run path escapes the output work directory")
        if not os.path.isdir(run_dir):
            raise RuntimeError(f"task run directory is missing: {run_dir}")
        _validate_run_tree(run_dir, int(self.config["owner_uid"]))
        return node_dir, run_dir

    def launch(self, token: str, scope_id: str) -> Tuple[int, str]:
        self._reserve(token, scope_id)
        _node_dir, run_dir = self._run_dir(scope_id)
        from gan.framework import llm_proxy
        from gan.framework import task_execution as tx

        model = str(self.config["model"])
        proxy_token = llm_proxy.request_task_token(
            str(self.config["proxy_socket"]),
            str(self.config["proxy_control_token"]),
            scope_id,
            model,
        )
        env = {
            key: value for key, value in dict(self.config["base_env"]).items()
            if key in {
                "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
                "PYTHONDONTWRITEBYTECODE",
            } or key.startswith("LC_")
        }
        env.update({
            "HOME": "/tmp",
            "PYTHONPATH": run_dir,
            "GAN_TASK_DESIGN": "/workspace/.gan_runtime/design.json",
            "GAN_TASK_TOOLS_DIR": "/workspace/.gan_runtime/tools",
            "GAN_TOOLS_LOAD_REPORT": ".gan_runtime/tools_load_report.json",
            "GAN_TASK_KNOWLEDGE_DIR": "/workspace/.gan_runtime/knowledge",
        })
        task_brief = str(self.config.get("task_brief") or "")
        if task_brief:
            env["GAN_TASK_BRIEF"] = task_brief

        parent_scored = bool(self.config["parent_scored"])
        questions_path = os.path.join(run_dir, "input", "questions.csv")
        if parent_scored and not os.path.isfile(questions_path):
            raise RuntimeError(f"task questions file is missing: {questions_path}")

        try:
            return tx._run_harness_and_report_local(
                str(self.config["python"]),
                run_dir,
                str(self.config["domain"]),
                f"gan_{scope_id}",
                str(self.config["subset"]),
                int(self.config["num_samples"]),
                model,
                env,
                int(self.config["timeout"]),
                questions_path=questions_path if parent_scored else None,
                dataset_root=None if parent_scored else str(self.config["repo_root"]),
                proxy_socket=str(self.config["proxy_socket"]),
                proxy_token=proxy_token,
                log_path=os.path.join(self.config["output_dir"], "logs", "task_runner.log"),
                expected_owner_uid=int(self.config["owner_uid"]),
                # Fixed at root-side registration (same models.yaml entry as
                # ``model``): the outer worker cannot influence it.
                reasoning_effort=self.config.get("reasoning_effort"),
            )
        finally:
            if os.path.isdir(run_dir):
                _restore_directory_ownership(
                    run_dir,
                    int(self.config["owner_uid"]),
                    int(self.config["owner_gid"]),
                )


class _TaskRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
        if not raw or len(raw) > _MAX_REQUEST_BYTES:
            _reply(self, {"ok": False, "error": "invalid task broker request"})
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict) or set(request) != {"token", "scope_id"}:
                raise ValueError("unexpected request fields")
            rc, output = self.server.state.launch(  # type: ignore[attr-defined]
                str(request["token"]), str(request["scope_id"])
            )
            _reply(self, {"ok": True, "rc": int(rc),
                          "output": str(output)[-_MAX_OUTPUT_BYTES:]})
        except PermissionError as exc:
            _reply(self, {"ok": False, "error": str(exc), "denied": True})
        except Exception as exc:  # noqa: BLE001 -- error text is returned, never a traceback
            _reply(self, {"ok": False, "error": str(exc)[:2000]})


_UnixStreamServer = getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)


class _TaskUnixServer(socketserver.ThreadingMixIn, _UnixStreamServer):
    daemon_threads = True


def broker_process_main(control: Connection, socket_path: str,
                        config: Dict[str, Any]) -> None:
    """Entry point for the root child.  Only the private pipe can add budgets."""
    _set_parent_death_signal()
    os.environ["GAN_TASK_SANDBOX_USER"] = str(config["sandbox_username"])
    server = None
    thread = None
    try:
        if os.path.lexists(socket_path):
            os.unlink(socket_path)
        state = _TaskBrokerState(config)
        server = _TaskUnixServer(socket_path, _TaskRequestHandler)
        server.state = state  # type: ignore[attr-defined]
        os.chmod(socket_path, 0o666)
        thread = threading.Thread(target=server.serve_forever,
                                  name="rsi-gan-task-broker", daemon=True)
        thread.start()
        control.send({"ok": True})
        while True:
            message = control.recv()
            operation = message.get("op") if isinstance(message, dict) else None
            if operation == "register":
                try:
                    token = state.register_outer(int(message["budget"]))
                    control.send({"ok": True, "token": token})
                except Exception as exc:  # noqa: BLE001
                    control.send({"ok": False, "error": str(exc)})
            elif operation == "shutdown":
                control.send({"ok": True})
                break
            else:
                control.send({"ok": False, "error": "unknown broker operation"})
    except EOFError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            control.send({"ok": False, "error": str(exc)})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        try:
            if os.path.lexists(socket_path):
                os.unlink(socket_path)
        except OSError:
            pass
        control.close()


def request_task_run(socket_path: str, token: str, scope_id: str,
                     timeout: int) -> Tuple[int, str]:
    """Submit the only outer-controlled field, a generation scope id."""
    body = json.dumps({"token": token, "scope_id": str(scope_id)}).encode("utf-8") + b"\n"
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(max(1, int(timeout) + 60))
        client.connect(socket_path)
        client.sendall(body)
        response = b""
        while len(response) <= _MAX_RESPONSE_BYTES:
            chunk = client.recv(min(65536, _MAX_RESPONSE_BYTES + 1 - len(response)))
            if not chunk:
                break
            response += chunk
            if b"\n" in response:
                break
        if len(response) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("task broker response exceeded the size limit")
        payload = json.loads(response.decode("utf-8"))
        if not isinstance(payload, dict) or not payload.get("ok"):
            reason = payload.get("error") if isinstance(payload, dict) else "invalid response"
            raise RuntimeError(f"task broker launch failed: {reason}")
        return int(payload["rc"]), str(payload.get("output") or "")
    finally:
        client.close()
