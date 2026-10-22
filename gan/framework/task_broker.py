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
import signal
import socket
import socketserver
import stat
import struct
import threading
import time
from multiprocessing.connection import Connection
from typing import Any, Dict, Tuple


_SCOPE_RE = re.compile(r"(?:0|[1-9][0-9]{0,11})\Z")
_MAX_REQUEST_BYTES = 16 * 1024
_MAX_OUTPUT_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class _OuterNotActive(RuntimeError):
    pass


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


def _proc_parent_starttime(pid: int) -> Tuple[int, int]:
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="ascii") as handle:
            raw = handle.read()
    except (OSError, ValueError) as exc:
        raise PermissionError("task broker peer process is unavailable") from exc
    end = raw.rfind(")")
    fields = raw[end + 2:].split() if end >= 0 else []
    if len(fields) < 20:
        raise PermissionError("task broker peer process identity is malformed")
    try:
        return int(fields[1]), int(fields[19])
    except ValueError as exc:
        raise PermissionError("task broker peer process identity is malformed") from exc


def _proc_real_ids(pid: int) -> Tuple[int, int]:
    try:
        with open(f"/proc/{int(pid)}/status", "r", encoding="ascii") as handle:
            fields = {}
            for line in handle:
                if line.startswith(("Uid:", "Gid:")):
                    key, value = line.split(":", 1)
                    fields[key] = value.split()
    except (OSError, ValueError) as exc:
        raise PermissionError("task broker peer process is unavailable") from exc
    try:
        return int(fields["Uid"][0]), int(fields["Gid"][0])
    except (KeyError, IndexError, ValueError) as exc:
        raise PermissionError("task broker peer credentials are malformed") from exc


def _is_descendant(pid: int, ancestor_pid: int, ancestor_starttime: int) -> bool:
    try:
        _parent, current_starttime = _proc_parent_starttime(ancestor_pid)
    except PermissionError:
        return False
    if current_starttime != ancestor_starttime:
        return False
    current = int(pid)
    seen = set()
    for _ in range(256):
        if current == ancestor_pid:
            return True
        if current <= 1 or current in seen:
            return False
        seen.add(current)
        try:
            current, _starttime = _proc_parent_starttime(current)
        except PermissionError:
            return False
    return False


def _sealed_secret_fd(value: str) -> int:
    if not hasattr(os, "memfd_create"):
        raise RuntimeError("secure task token injection requires memfd_create")
    import fcntl

    data = str(value).encode("utf-8")
    if not data or len(data) > 4096 or b"\x00" in data:
        raise RuntimeError("invalid task proxy capability")
    flags = int(getattr(os, "MFD_CLOEXEC", 0x0001)) | int(
        getattr(os, "MFD_ALLOW_SEALING", 0x0002))
    fd = os.memfd_create("rsi-gan-task-proxy", flags)
    try:
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError("short write while sealing task proxy capability")
            offset += written
        os.lseek(fd, 0, os.SEEK_SET)
        seals = (fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW |
                 fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
        return fd
    except Exception:
        os.close(fd)
        raise


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


class _TaskBrokerState:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._outers: Dict[int, Dict[str, int]] = {}
        self._used_scopes = set()
        self._acl_reconciled = False

    def register_outer(self, outer: int, budget: int) -> None:
        if outer < 1 or budget < 1:
            raise ValueError("task broker budget must be positive")
        with self._lock:
            if not self._acl_reconciled:
                from gan.framework import task_execution as tx
                tx.reconcile_stale_task_acl(
                    str(self.config["setfacl"]), str(self.config["output_dir"]),
                    int(self.config["owner_uid"]), int(self.config["owner_gid"]),
                    int(self.config["sandbox_uid"]),
                )
                self._acl_reconciled = True
            if outer in self._outers:
                raise ValueError("task broker outer was already registered")
            self._outers[outer] = {"remaining": int(budget)}

    def activate_outer(self, outer: int, supervisor_pid: int) -> None:
        if outer < 1 or supervisor_pid <= 1:
            raise ValueError("invalid task broker outer activation")
        _parent, starttime = _proc_parent_starttime(supervisor_pid)
        real_uid, real_gid = _proc_real_ids(supervisor_pid)
        if (real_uid != int(self.config["owner_uid"])
                or real_gid != int(self.config["owner_gid"])):
            raise PermissionError("outer supervisor has an unexpected owner")
        with self._lock:
            scope = self._outers.get(outer)
            if scope is None:
                raise ValueError("task broker outer was not registered")
            if "pid" in scope:
                raise ValueError("task broker outer was already activated")
            scope.update({"pid": int(supervisor_pid), "starttime": int(starttime)})

    def release_outer(self, outer: int) -> None:
        with self._lock:
            self._outers.pop(int(outer), None)
            self._used_scopes = {
                key for key in self._used_scopes if key[0] != int(outer)
            }

    def _reserve(self, outer: int, scope_id: str, *, peer_pid: int,
                 peer_uid: int, peer_gid: int) -> None:
        if not _SCOPE_RE.fullmatch(scope_id):
            raise PermissionError("invalid task scope id")
        if (peer_uid != int(self.config["owner_uid"])
                or peer_gid != int(self.config["owner_gid"])):
            raise PermissionError("task broker peer has an unexpected identity")
        with self._lock:
            active = self._outers.get(int(outer))
            if active is None or "pid" not in active:
                raise _OuterNotActive("task broker outer is not active")
            if not _is_descendant(peer_pid, int(active["pid"]), int(active["starttime"])):
                raise PermissionError("task broker peer is outside the active outer process tree")
            if int(active["remaining"]) < 1:
                raise PermissionError("task launch budget exhausted")
            key = (int(outer), scope_id)
            if key in self._used_scopes:
                raise PermissionError("task scope was already launched")
            active["remaining"] = int(active["remaining"]) - 1
            self._used_scopes.add(key)

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

    def launch(self, outer: int, scope_id: str, *, peer_pid: int,
               peer_uid: int, peer_gid: int) -> Tuple[int, str]:
        self._reserve(outer, scope_id, peer_pid=peer_pid,
                      peer_uid=peer_uid, peer_gid=peer_gid)
        _node_dir, run_dir = self._run_dir(scope_id)
        from gan.framework import llm_proxy
        from gan.framework import task_execution as tx

        domain = str(self.config["domain"])
        model = str(self.config["model"])

        parent_scored = bool(self.config["parent_scored"])
        questions_path = os.path.join(run_dir, "input", "questions.csv")
        if parent_scored and not os.path.isfile(questions_path):
            raise RuntimeError(f"task questions file is missing: {questions_path}")

        total_timeout = int(self.config["timeout"])
        stage_timeout = max(1, total_timeout // 2) if domain == "imo_proof" else total_timeout

        proxy_token = llm_proxy.request_task_token(
            str(self.config["proxy_socket"]),
            str(self.config["proxy_control_token"]),
            scope_id,
            model,
            ttl_s=float(self.config["timeout"]) + 60.0,
            max_calls=int(self.config["task_llm_max_calls"]),
            peer_uid=int(self.config["sandbox_uid"]),
        )
        try:
            proxy_token_fd = _sealed_secret_fd(proxy_token)
        except Exception:
            llm_proxy.revoke_token(
                str(self.config["proxy_socket"]),
                str(self.config["proxy_control_token"]), proxy_token,
            )
            raise
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
            "GAN_SECURE_MODE": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "GAN_TASK_DESIGN": "/workspace/.gan_runtime/design.json",
            "GAN_TASK_TOOLS_DIR": "/workspace/.gan_runtime/tools",
            "GAN_TOOLS_LOAD_REPORT": ".gan_runtime/tools_load_report.json",
            "GAN_TASK_KNOWLEDGE_DIR": "/workspace/.gan_runtime/knowledge",
            # Context-management snapshot + the child's audit sidecar
            # (docs/10 §11 D2 / §11.5). The broker builds the child env
            # ITSELF, so these must be pinned HERE: the runner's env dict
            # (assembled by ``assemble_task_env``) never crosses the broker
            # boundary -- ``request_task_run`` passes only scope/model/token/
            # timeout (observed: every driver-mode round ran the task child
            # with NO seam at all; the worker's cwd fallback pointed at the
            # scratch tmpfs, where ``.gan_runtime`` does not exist). Paths
            # mirror assemble_task_env; the sidecar is pre-created
            # parent-side BEFORE this launch, so the rw-bind source exists.
            "GAN_CM_CORE": "/workspace/.gan_runtime/cm",
            "GAN_CM_EVENTS": "/workspace/cm_events.jsonl",
            # Agent-owned CM modules are delivered under FIXED names, so the
            # env pins are static and pinnable here (the runner's env never
            # crosses this boundary). A generation whose design names no
            # module leaves the file absent; the child degrades loudly on
            # that claim-vs-snapshot gap instead of guessing at candidates.
            "GAN_CM_POLICY": "/workspace/.gan_runtime/cm/cm_policy.py",
            "GAN_CM_ASSEMBLY": "/workspace/.gan_runtime/cm/cm_assembly.py",
            # Per-question wall + per-call client budget, pinned at root-side
            # registration (the outer worker cannot influence them). Without
            # these the harness silently used its 300s default regardless of
            # the domain's question_timeout_s (observed: search_arena 600s ran
            # at 300s in driver mode; paper_review masked it, 300 == 300).
            "GAN_QUESTION_TIMEOUT_S": str(int(self.config["question_timeout_s"])),
            "GAN_LLM_TIMEOUT_S": str(float(self.config["llm_client_timeout_s"])),
        })
        task_brief = str(self.config.get("task_brief") or "")
        if task_brief:
            env["GAN_TASK_BRIEF"] = task_brief

        try:
            # Harness-side sampling (only for domains that score in the harness
            # and read the dataset themselves): derive this generation's seed
            # from the run-persistent seed file written by the driver.
            sample_seed = None
            anchor_ids = None
            if (not parent_scored
                    and str(self.config.get("sampling_mode", "")) == "seeded"):
                seed_path = os.path.join(str(self.config["output_dir"]),
                                         "ckpt", "sample_seed.json")
                try:
                    with open(seed_path, "r", encoding="utf-8") as f:
                        seed_info = json.load(f)
                    sample_seed = tx.derive_sample_seed(
                        int(seed_info["base_seed"]), scope_id)
                    anchor_ids = list(seed_info.get("anchor_ids") or [])
                except (OSError, ValueError, KeyError):
                    sample_seed = None  # fall back to the legacy prefix slice
            rc, output = tx._run_harness_and_report_local(
                str(self.config["python"]),
                run_dir,
                domain,
                f"gan_{scope_id}",
                str(self.config["subset"]),
                int(self.config["num_samples"]),
                model,
                env,
                stage_timeout,
                questions_path=questions_path if parent_scored else None,
                dataset_root=None if parent_scored else str(self.config["repo_root"]),
                proxy_socket=str(self.config["proxy_socket"]),
                proxy_token_fd=proxy_token_fd,
                expected_owner_uid=int(self.config["owner_uid"]),
                # Fixed at root-side registration (same models.yaml entry as
                # ``model``): the outer worker cannot influence it.
                reasoning_effort=self.config.get("reasoning_effort"),
                sample_seed=sample_seed,
                anchor_ids=anchor_ids,
                expected_owner_gid=int(self.config["owner_gid"]),
                setpriv_path=str(self.config["setpriv"]),
                setfacl_path=str(self.config["setfacl"]),
                bwrap_path=str(self.config["bwrap"]),
                python_prefix=str(self.config["python_prefix"]),
                sandbox_etc_dir=str(self.config["sandbox_etc_dir"]),
                sandbox_profile=dict(self.config["sandbox_profile"]),
            )
            if domain == "imo_proof" and rc == 0:
                grading_rc, grading_output = tx.run_imo_proof_grading(
                    str(self.config["python"]),
                    run_dir,
                    f"gan_{scope_id}",
                    int(self.config["num_samples"]),
                    model,
                    env,
                    stage_timeout,
                    str(self.config["proxy_socket"]),
                    proxy_token_fd=proxy_token_fd,
                    reasoning_effort=self.config.get("reasoning_effort"),
                    expected_owner_uid=int(self.config["owner_uid"]),
                    expected_owner_gid=int(self.config["owner_gid"]),
                    setpriv_path=str(self.config["setpriv"]),
                    setfacl_path=str(self.config["setfacl"]),
                    bwrap_path=str(self.config["bwrap"]),
                    python_prefix=str(self.config["python_prefix"]),
                    sandbox_etc_dir=str(self.config["sandbox_etc_dir"]),
                    sandbox_profile=dict(self.config["sandbox_profile"]),
                )
                output = f"{output}\n[imo_proof_grading]\n{grading_output}"
                rc = grading_rc
            elif domain == "polyglot" and rc == 0:
                # Docker stays in this root-only broker; neither the outer
                # worker nor the TaskAgent receives the host socket.
                output_path = os.path.join(run_dir, "outputs", f"gan_{scope_id}")
                tx.run_polyglot_eval(
                    os.path.join(output_path, "predictions.csv"),
                    str(self.config["repo_root"]),
                    output_path,
                )
            return rc, output
        finally:
            os.close(proxy_token_fd)
            llm_proxy.revoke_token(
                str(self.config["proxy_socket"]),
                str(self.config["proxy_control_token"]), proxy_token,
            )


class _TaskRequestHandler(socketserver.StreamRequestHandler):
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

    def handle(self) -> None:
        raw = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
        if not raw or len(raw) > _MAX_REQUEST_BYTES:
            _reply(self, {"ok": False, "error": "invalid task broker request"})
            return
        self._clear_deadline()
        self.request.settimeout(None)
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict) or set(request) != {"outer", "scope_id"}:
                raise ValueError("unexpected request fields")
            if not hasattr(socket, "SO_PEERCRED"):
                raise PermissionError("task broker peer credentials are unavailable")
            credentials = self.request.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            peer_pid, peer_uid, peer_gid = struct.unpack("3i", credentials)
            rc, output = self.server.state.launch(  # type: ignore[attr-defined]
                int(request["outer"]), str(request["scope_id"]),
                peer_pid=peer_pid, peer_uid=peer_uid, peer_gid=peer_gid,
            )
            _reply(self, {"ok": True, "rc": int(rc),
                          "output": str(output)[-_MAX_OUTPUT_BYTES:]})
        except _OuterNotActive as exc:
            _reply(self, {"ok": False, "error": str(exc), "retry": True})
        except PermissionError as exc:
            _reply(self, {"ok": False, "error": str(exc), "denied": True})
        except Exception as exc:  # noqa: BLE001 -- error text is returned, never a traceback
            _reply(self, {"ok": False, "error": str(exc)[:2000]})


_UnixStreamServer = getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)


class _TaskUnixServer(socketserver.ThreadingMixIn, _UnixStreamServer):
    daemon_threads = True
    request_queue_size = 8

    def server_activate(self) -> None:
        self._connection_slots = threading.BoundedSemaphore(8)
        super().server_activate()

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(10.0)
        return request, address

    def process_request(self, request, client_address) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


def broker_process_main(control: Connection, socket_path: str,
                        config: Dict[str, Any]) -> None:
    """Entry point for the root child.  Only the private pipe can add budgets."""
    _set_parent_death_signal()
    os.environ["GAN_TASK_SANDBOX_USER"] = str(config["sandbox_username"])
    server = None
    thread = None
    try:
        if os.path.lexists(socket_path):
            raise RuntimeError(f"task broker socket path already exists: {socket_path}")
        state = _TaskBrokerState(config)
        server = _TaskUnixServer(socket_path, _TaskRequestHandler)
        server.state = state  # type: ignore[attr-defined]
        os.chown(socket_path, int(config["owner_uid"]), int(config["owner_gid"]))
        os.chmod(socket_path, 0o600)
        thread = threading.Thread(target=server.serve_forever,
                                  name="rsi-gan-task-broker", daemon=True)
        thread.start()
        control.send({"ok": True})
        while True:
            message = control.recv()
            operation = message.get("op") if isinstance(message, dict) else None
            if operation == "register":
                try:
                    state.register_outer(int(message["outer"]), int(message["budget"]))
                    control.send({"ok": True})
                except Exception as exc:  # noqa: BLE001
                    control.send({"ok": False, "error": str(exc)})
            elif operation == "activate":
                try:
                    state.activate_outer(
                        int(message["outer"]), int(message["supervisor_pid"]))
                    control.send({"ok": True})
                except Exception as exc:  # noqa: BLE001
                    control.send({"ok": False, "error": str(exc)})
            elif operation == "release":
                try:
                    state.release_outer(int(message["outer"]))
                    control.send({"ok": True})
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


def request_task_run(socket_path: str, outer: int, scope_id: str,
                     timeout: int) -> Tuple[int, str]:
    """Submit the only outer-controlled field, a generation scope id."""
    body = json.dumps({"outer": int(outer), "scope_id": str(scope_id)}).encode("utf-8") + b"\n"
    for attempt in range(51):
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
            if isinstance(payload, dict) and payload.get("ok"):
                return int(payload["rc"]), str(payload.get("output") or "")
            if (isinstance(payload, dict) and payload.get("retry") is True
                    and attempt < 50):
                time.sleep(0.1)
                continue
            reason = payload.get("error") if isinstance(payload, dict) else "invalid response"
            raise RuntimeError(f"task broker launch failed: {reason}")
        finally:
            client.close()
    raise RuntimeError("task broker outer did not become active")
