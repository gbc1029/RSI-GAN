"""Root-only launcher for planner/evaluator seat processes."""
from __future__ import annotations

import ctypes
import json
import os
import re
import selectors
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import threading
import shutil
import tempfile
from multiprocessing.connection import Connection
from typing import Any, Dict, List, Tuple

from gan.framework.sandbox_etc import SANDBOX_HOSTNAME, validate_sandbox_etc

_INSTANCE_RE = re.compile(r"outer_(0|[1-9][0-9]{0,11})(_eval)?\Z")
_MAX_AUTH_BYTES = 16 * 1024
_ROLE_SEATS = {
    "planner": {"plan", "self_improve"},
    "evaluator": {"evaluate", "self_improve"},
}


class _OuterNotActive(RuntimeError):
    pass


class _RoleCleanupPending(RuntimeError):
    pass


def _set_parent_death_signal() -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    except (AttributeError, OSError):
        pass


def _setpriv_command(setpriv: str, uid: int, gid: int, command: List[str]) -> List[str]:
    return [
        setpriv, "--reuid", str(uid), "--regid", str(gid), "--clear-groups",
        "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
        "--no-new-privs", "--", *command,
    ]


def _add_empty_parents(command: List[str], path: str, created: set[str]) -> None:
    parent = os.path.dirname(path)
    pending = []
    while parent and parent != "/" and parent not in created:
        pending.append(parent)
        next_parent = os.path.dirname(parent)
        if next_parent == parent:
            break
        parent = next_parent
    for item in reversed(pending):
        command.extend(["--dir", item])
        created.add(item)


def _open_dir(path: str) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise RuntimeError(f"role sandbox source is not a directory: {path}")
    return fd


def _proc_parent_starttime(pid: int) -> Tuple[int, int]:
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="ascii") as handle:
            raw = handle.read()
    except (OSError, ValueError) as exc:
        raise PermissionError("role broker peer process is unavailable") from exc
    end = raw.rfind(")")
    fields = raw[end + 2:].split() if end >= 0 else []
    if len(fields) < 20:
        raise PermissionError("role broker peer process identity is malformed")
    try:
        return int(fields[1]), int(fields[19])
    except ValueError as exc:
        raise PermissionError("role broker peer process identity is malformed") from exc


def _proc_real_ids(pid: int) -> Tuple[int, int]:
    try:
        with open(f"/proc/{int(pid)}/status", "r", encoding="ascii") as handle:
            fields = {}
            for line in handle:
                if line.startswith(("Uid:", "Gid:")):
                    key, value = line.split(":", 1)
                    fields[key] = value.split()
    except (OSError, ValueError) as exc:
        raise PermissionError("role broker peer process is unavailable") from exc
    try:
        return int(fields["Uid"][0]), int(fields["Gid"][0])
    except (KeyError, IndexError, ValueError) as exc:
        raise PermissionError("role broker peer credentials are malformed") from exc


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
        raise RuntimeError("secure role token injection requires memfd_create")
    import fcntl

    data = str(value).encode("utf-8")
    if not data or len(data) > 4096 or b"\x00" in data:
        raise RuntimeError("invalid role proxy capability")
    flags = int(getattr(os, "MFD_CLOEXEC", 0x0001)) | int(
        getattr(os, "MFD_ALLOW_SEALING", 0x0002))
    fd = os.memfd_create("rsi-gan-role-proxy", flags)
    try:
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError("short write while sealing role proxy capability")
            offset += written
        os.lseek(fd, 0, os.SEEK_SET)
        seals = (fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW |
                 fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
        return fd
    except Exception:
        os.close(fd)
        raise


def _copy_runtime_projection(code_root: str, role: str) -> str:
    """Create the read-only code view needed by one role worker.

    The authoritative per-run tree remains parent-only.  The projection keeps
    frozen runtime plumbing plus the catalogs needed by the active role, while
    deliberately omitting domains, git metadata, credentials, and run data.
    """
    root = tempfile.mkdtemp(prefix=f"rsi-role-{role}-")

    def copy_tree(rel: str, *, names: set[str] | None = None) -> None:
        src = os.path.join(code_root, rel)
        if not os.path.isdir(src):
            return
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            if any(os.path.islink(os.path.join(dirpath, name))
                   for name in [*dirnames, *filenames]):
                raise RuntimeError(f"role runtime input contains a symlink: {rel}")
        dst = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)

        def ignore(_dir: str, entries: list[str]) -> list[str]:
            ignored = {".git", "__pycache__", ".env", "outputs", "logs",
                       "runs", "workspaces", "trajectory", "scores",
                       "ckpt", "dataset", "datasets"}
            if names is not None:
                return [n for n in entries if n not in names]
            return [n for n in entries if n in ignored or n.endswith((".pyc", ".pyo"))]

        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore,
                        symlinks=False)

    # Runtime packages.  These are frozen plumbing or the active role's code;
    # no benchmark/domain tree is needed by planner/evaluator workers.
    for rel in ("agent", "utils", "gan/framework", "gan/design",
                "gan/tools/work/common", "gan/tools/design", "gan/tools/deep",
                f"gan/tools/work/{role}"):
        copy_tree(rel)

    for rel in ("gan/__init__.py", "gan/patch.py", "gan/summary.py",
                "gan/tools/__init__.py", "gan/tools/assembly.py",
                "gan/roles/base_role.py",
                f"gan/roles/{role}.py"):
        src = os.path.join(code_root, rel)
        if os.path.isfile(src):
            if os.path.islink(src):
                raise RuntimeError(f"role runtime input contains a symlink: {rel}")
            dst = os.path.join(root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

    # Catalog metadata is needed by the role's normal tool assembly.  Keep only
    # the active role and task catalogs; other role registries/components are
    # unrelated to this worker and never enter its namespace.
    for rel in ("gan/registries/__init__.py", "gan/registries/loader.py",
                f"gan/registries/{role}.json", "gan/registries/task.json"):
        src = os.path.join(code_root, rel)
        if os.path.isfile(src):
            dst = os.path.join(root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    for component_role in (role, "task"):
        copy_tree(f"gan/components/{component_role}")

    # A projection must never contain a symlink that points back into the
    # authoritative tree.  copytree normally dereferences links; fail closed if
    # one appeared in a runtime input instead of silently widening the view.
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            if os.path.islink(os.path.join(dirpath, name)):
                shutil.rmtree(root, ignore_errors=True)
                raise RuntimeError("role runtime projection contains a symlink")
        os.chmod(dirpath, 0o555)
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o444)
    # bwrap resolves the bind source after setpriv has dropped privileges.  The
    # projection root itself must therefore be traversable without relying on
    # host ACL inheritance from the temporary-directory implementation.
    os.chmod(root, 0o755)
    return root


def _open_beneath(base_fd: int, relative: str, allowed_uids: set[int]) -> int:
    parts = [part for part in str(relative).replace("\\", "/").split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise RuntimeError(f"invalid role sandbox relative path: {relative}")
    current = os.dup(base_fd)
    try:
        for part in parts:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
            os.close(current)
            current = child
            info = os.fstat(current)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in allowed_uids:
                raise RuntimeError(
                    f"role sandbox path has an unexpected owner or type: {relative}"
                )
        return current
    except Exception:
        os.close(current)
        raise


def _open_parent_dirs(base_fd: int, relative: str,
                      allowed_uids: set[int]) -> List[int]:
    """Open every directory before the bind source's final component."""
    parts = [part for part in str(relative).replace("\\", "/").split("/") if part]
    if len(parts) < 2 or any(part in (".", "..") for part in parts):
        return []
    base = os.dup(base_fd)
    current = base
    parents: List[int] = []
    base_open = True
    try:
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
            try:
                info = os.fstat(child)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid not in allowed_uids:
                    raise RuntimeError(
                        f"role sandbox parent has an unexpected owner or type: {relative}"
                    )
            except Exception:
                os.close(child)
                raise
            if base_open:
                os.close(base)
                base_open = False
            parents.append(child)
            current = child
        return parents
    except Exception:
        if base_open:
            os.close(base)
        for fd in parents:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _open_absolute_parent_dirs(path: str, allowed_uids: set[int]) -> List[int]:
    """Open every directory before an absolute path's final component."""
    absolute = os.path.abspath(path)
    parts = [part for part in absolute.split(os.sep) if part]
    if not absolute.startswith(os.sep) or len(parts) < 2:
        return []
    root_fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    current = root_fd
    parents: List[int] = []
    root_open = True
    try:
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
            try:
                info = os.fstat(child)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid not in allowed_uids:
                    raise RuntimeError(
                        f"role sandbox ancestor has an unexpected owner or type: {path}"
                    )
            except Exception:
                os.close(child)
                raise
            if root_open:
                os.close(root_fd)
                root_open = False
            parents.append(child)
            current = child
        return parents
    except Exception:
        if root_open:
            os.close(root_fd)
        for fd in parents:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _setfacl(setfacl: str, fd: int, operation: str, acl: str) -> None:
    result = subprocess.run(
        [setfacl, operation, acl, f"/proc/self/fd/{fd}"], pass_fds=(fd,),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "setfacl failed").strip()
        raise RuntimeError(detail[-2000:])


def _walk_tree(root_fd: int, visit: Any) -> None:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC

    def walk(directory_fd: int) -> None:
        for name in os.listdir(directory_fd):
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError("role writable tree contains a symlink")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise RuntimeError("role writable tree contains a non-regular entry")
            flags = directory_flags if stat.S_ISDIR(info.st_mode) else file_flags
            child = os.open(name, flags, dir_fd=directory_fd)
            try:
                child_info = os.fstat(child)
                if stat.S_ISREG(child_info.st_mode) and child_info.st_nlink != 1:
                    raise RuntimeError("role writable tree contains a hard-linked file")
                if stat.S_ISDIR(child_info.st_mode):
                    walk(child)
                visit(child, child_info)
            finally:
                os.close(child)
    walk(root_fd)
    visit(root_fd, os.fstat(root_fd))


def _grant_tree(root_fd: int, setfacl: str, owner_uid: int, role_uid: int,
                allowed_uids: set[int]) -> None:
    def grant(fd: int, info: os.stat_result) -> None:
        if info.st_uid not in allowed_uids:
            raise RuntimeError("role writable tree contains an unexpected owner")
        if stat.S_ISDIR(info.st_mode):
            _setfacl(
                setfacl, fd, "-m",
                f"u:{role_uid}:rwx,u:{owner_uid}:rwx,d:u:{role_uid}:rwx,d:u:{owner_uid}:rwx",
            )
        else:
            _setfacl(setfacl, fd, "-m", f"u:{role_uid}:rw-,u:{owner_uid}:rw-")
    _walk_tree(root_fd, grant)


def _reclaim_tree(root_fd: int, setfacl: str, owner_uid: int, owner_gid: int,
                  role_uid: int) -> None:
    def reclaim(fd: int, info: os.stat_result) -> None:
        if info.st_uid not in {owner_uid, role_uid}:
            raise RuntimeError("role writable tree owner changed unexpectedly")
        if stat.S_ISDIR(info.st_mode):
            _setfacl(setfacl, fd, "-x", f"d:u:{role_uid}")
        _setfacl(setfacl, fd, "-x", f"u:{role_uid}")
        os.fchown(fd, owner_uid, owner_gid)

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC

    def walk(directory_fd: int) -> None:
        for name in os.listdir(directory_fd):
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not (
                    stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                os.unlink(name, dir_fd=directory_fd)
                continue
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                os.unlink(name, dir_fd=directory_fd)
                continue
            flags = directory_flags if stat.S_ISDIR(info.st_mode) else file_flags
            child = os.open(name, flags, dir_fd=directory_fd)
            try:
                child_info = os.fstat(child)
                if stat.S_ISDIR(child_info.st_mode):
                    walk(child)
                reclaim(child, child_info)
            finally:
                os.close(child)
    walk(root_fd)
    reclaim(root_fd, os.fstat(root_fd))


def _relay(client: socket.socket, process: subprocess.Popen[bytes]) -> None:
    if process.stdin is None or process.stdout is None:
        raise RuntimeError("role worker pipes were not created")
    os.set_blocking(process.stdout.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, "client")
    selector.register(process.stdout, selectors.EVENT_READ, "worker")
    try:
        while process.poll() is None:
            for key, _mask in selector.select(timeout=1):
                if key.data == "client":
                    data = client.recv(65536)
                    if not data:
                        return
                    process.stdin.write(data)
                    process.stdin.flush()
                else:
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        return
                    client.sendall(data)
    finally:
        selector.close()


class _RoleBrokerState:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._acl_lock = threading.Lock()
        self._active: Dict[str, int] = {}
        self._scopes: Dict[Tuple[int, str, str, str], Dict[str, Any]] = {}
        self._outers: Dict[int, Dict[str, int]] = {}
        self._runtime_dirs: Dict[int, str] = {}
        self._proxy_tokens_by_pid: Dict[int, str] = {}
        self._scope_by_pid: Dict[int, Tuple[int, str, str, str]] = {}
        self._traverse_refs: Dict[Tuple[int, int, int], int] = {}
        self._traverse_by_pid: Dict[int, List[Tuple[int, Tuple[int, int, int]]]] = {}

    def _acquire_traverse(self, fds: List[int], setfacl: str,
                          role_uid: int) -> List[Tuple[int, Tuple[int, int, int]]]:
        entries: List[Tuple[int, Tuple[int, int, int]]] = []
        try:
            with self._acl_lock:
                for fd in fds:
                    info = os.fstat(fd)
                    key = (int(info.st_dev), int(info.st_ino), role_uid)
                    if self._traverse_refs.get(key, 0) == 0:
                        _setfacl(setfacl, fd, "-m", f"u:{role_uid}:x")
                    self._traverse_refs[key] = self._traverse_refs.get(key, 0) + 1
                    entries.append((fd, key))
            return entries
        except Exception:
            for fd, key in entries:
                try:
                    with self._acl_lock:
                        remaining = self._traverse_refs.get(key, 0) - 1
                        if remaining <= 0:
                            self._traverse_refs.pop(key, None)
                            _setfacl(setfacl, fd, "-x", f"u:{role_uid}")
                        else:
                            self._traverse_refs[key] = remaining
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            for fd in fds[len(entries):]:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

    def _release_traverse(self, entries: List[Tuple[int, Tuple[int, int, int]]],
                          setfacl: str, role_uid: int) -> List[str]:
        errors: List[str] = []
        for fd, key in entries:
            try:
                with self._acl_lock:
                    remaining = self._traverse_refs.get(key, 0) - 1
                    if remaining <= 0:
                        self._traverse_refs.pop(key, None)
                        _setfacl(setfacl, fd, "-x", f"u:{role_uid}")
                    else:
                        self._traverse_refs[key] = remaining
            except Exception as exc:
                errors.append(str(exc))
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return errors

    def _acquire_output_acl(self, role: str, output_fd: int, role_uid: int) -> None:
        with self._acl_lock:
            if self._active.get(role, 0) == 0:
                _setfacl(str(self.config["setfacl"]), output_fd, "-m",
                          f"u:{role_uid}:r-x")
            self._active[role] = self._active.get(role, 0) + 1

    def _release_output_acl(self, role: str, output_fd: int, role_uid: int) -> None:
        with self._acl_lock:
            remaining = self._active.get(role, 0) - 1
            if remaining <= 0:
                self._active.pop(role, None)
                _setfacl(str(self.config["setfacl"]), output_fd, "-x",
                          f"u:{role_uid}")
            else:
                self._active[role] = remaining

    def register_outer(self, outer: int,
                       proxy_tokens: Dict[str, Dict[str, str]]) -> None:
        if outer < 1 or set(proxy_tokens) != set(_ROLE_SEATS):
            raise ValueError("invalid role broker outer registration")
        normalized: Dict[Tuple[int, str, str, str], Dict[str, Any]] = {}
        seen_tokens = set()
        for role, seats in _ROLE_SEATS.items():
            supplied = proxy_tokens.get(role)
            if not isinstance(supplied, dict) or set(supplied) != seats:
                raise ValueError("invalid role broker seat registration")
            for seat in seats:
                value = supplied.get(seat)
                # 固定 root 评估（docs/14 §5.5）需要一个独立的 planner 实例：
                # planner 的 plan 座可以带第二个 token（list/tuple 长度 2）。
                tokens = list(value) if isinstance(value, (list, tuple)) \
                    else [value]
                if role != "planner" or seat != "plan":
                    if len(tokens) != 1:
                        raise ValueError("invalid role broker seat registration")
                elif len(tokens) not in (1, 2):
                    raise ValueError("invalid role broker seat registration")
                for idx, token in enumerate(tokens):
                    token = str(token)
                    if (not token.startswith("sk-gan-")
                            or len(token.encode("utf-8")) > 4096 or "\x00" in token
                            or token in seen_tokens):
                        raise ValueError("invalid role proxy capability")
                    seen_tokens.add(token)
                    instance = f"outer_{int(outer)}" + ("_eval" if idx == 1 else "")
                    normalized[(int(outer), role, seat, instance)] = {
                        "role": role,
                        "seat": seat,
                        "outer": int(outer),
                        "instance": instance,
                        "proxy_token": token,
                        "launch_count": 0,
                        "state": "idle",
                        "active_pid": None,
                        "cleanup_error": None,
                    }
        with self._lock:
            if any(key in self._scopes for key in normalized) or outer in self._outers:
                raise ValueError("role broker outer was already registered")
            self._scopes.update(normalized)

    def activate_outer(self, outer: int, supervisor_pid: int) -> None:
        if outer < 1 or supervisor_pid <= 1:
            raise ValueError("invalid role broker outer activation")
        _parent, starttime = _proc_parent_starttime(supervisor_pid)
        real_uid, real_gid = _proc_real_ids(supervisor_pid)
        if (real_uid != int(self.config["owner_uid"])
                or real_gid != int(self.config["owner_gid"])):
            raise PermissionError("outer supervisor has an unexpected owner")
        with self._lock:
            if not any(key[0] == outer for key in self._scopes):
                raise ValueError("role broker outer was not registered")
            if outer in self._outers:
                raise ValueError("role broker outer was already activated")
            self._outers[outer] = {
                "pid": int(supervisor_pid), "starttime": int(starttime),
            }

    def release_outer(self, outer: int) -> None:
        tokens = []
        with self._lock:
            self._outers.pop(int(outer), None)
            for key in [key for key in self._scopes if key[0] == int(outer)]:
                scope = self._scopes.pop(key, None)
                if scope:
                    tokens.append(str(scope["proxy_token"]))
        for token in tokens:
            self._revoke_proxy(token)

    def _revoke_proxy(self, token: str) -> None:
        from gan.framework.llm_proxy import revoke_token
        revoke_token(
            str(self.config["proxy_socket"]),
            str(self.config["proxy_control_token"]), token,
        )

    def _rotate_proxy(self, token: str) -> str:
        from gan.framework.llm_proxy import rotate_role_token
        return rotate_role_token(
            str(self.config["proxy_socket"]),
            str(self.config["proxy_control_token"]), token,
        )

    def reserve(self, role: str, seat: str, instance: str, *,
                peer_pid: int, peer_uid: int, peer_gid: int) -> Dict[str, Any]:
        match = _INSTANCE_RE.fullmatch(instance)
        if role not in _ROLE_SEATS or seat not in _ROLE_SEATS[role] or not match:
            raise PermissionError("invalid role launch scope")
        outer = int(match.group(1))
        owner_uid = int(self.config["owner_uid"])
        owner_gid = int(self.config["owner_gid"])
        if peer_uid != owner_uid or peer_gid != owner_gid:
            raise PermissionError("role broker peer has an unexpected identity")
        key = (outer, role, seat, instance)
        with self._lock:
            active = dict(self._outers.get(outer) or {})
            scope = self._scopes.get(key)
            if not active:
                raise _OuterNotActive("role broker outer is not active")
            if scope is None:
                raise PermissionError("invalid role launch capability descriptor")
            if not _is_descendant(
                    peer_pid, int(active["pid"]), int(active["starttime"])):
                raise PermissionError("role broker peer is outside the active outer process tree")
            state = str(scope.get("state", "idle"))
            if state in {"launching", "active", "cleanup"}:
                raise _RoleCleanupPending("role seat cleanup is still pending")
            if state == "failed":
                raise PermissionError(
                    f"role seat cleanup failed: {scope.get('cleanup_error') or 'unknown error'}")
            launch_count = int(scope.get("launch_count", 0))
            if launch_count >= 2:
                raise PermissionError("role seat restart budget exhausted")
            scope["state"] = "launching"
            scope["launch_count"] = launch_count + 1
            token = str(scope["proxy_token"])
            restart = launch_count == 1
        if restart:
            try:
                token = self._rotate_proxy(token)
            except Exception as exc:
                with self._lock:
                    current = self._scopes.get(key)
                    if current is not None and current.get("state") == "launching":
                        current["state"] = "idle"
                        current["launch_count"] = launch_count
                raise _RoleCleanupPending(
                    f"role proxy capability rotation is not ready: {exc}") from exc
            with self._lock:
                current = self._scopes.get(key)
                if current is None or current.get("state") != "launching":
                    self._revoke_proxy(token)
                    raise PermissionError("role launch scope was released during restart")
                current["proxy_token"] = token
                scope = current
        return dict(scope)

    @staticmethod
    def _scope_key(scope: Dict[str, Any]) -> Tuple[int, str, str, str]:
        return (int(scope["outer"]), str(scope["role"]),
                str(scope["seat"]), str(scope["instance"]))

    def _mark_active(self, scope: Dict[str, Any], pid: int) -> None:
        key = self._scope_key(scope)
        with self._lock:
            current = self._scopes.get(key)
            if current is None or current.get("state") != "launching":
                raise RuntimeError("role launch scope is no longer active")
            current["state"] = "active"
            current["active_pid"] = int(pid)
            self._scope_by_pid[int(pid)] = key

    def _begin_reclaim(self, pid: int) -> None:
        with self._lock:
            key = self._scope_by_pid.get(int(pid))
            scope = self._scopes.get(key) if key is not None else None
            if scope is not None:
                scope["state"] = "cleanup"

    def _finish_reclaim(self, pid: int, errors: List[str]) -> None:
        with self._lock:
            key = self._scope_by_pid.pop(int(pid), None)
            scope = self._scopes.get(key) if key is not None else None
            if scope is None:
                return
            scope["active_pid"] = None
            scope["cleanup_error"] = "; ".join(errors[:5]) if errors else None
            scope["state"] = "failed" if errors else "idle"

    def _finish_failed_launch(self, scope: Dict[str, Any], errors: List[str], *,
                              launched: bool) -> None:
        key = self._scope_key(scope)
        with self._lock:
            current = self._scopes.get(key)
            if current is None:
                return
            if not launched:
                current["launch_count"] = max(
                    0, int(current.get("launch_count", 1)) - 1)
            current["active_pid"] = None
            current["cleanup_error"] = "; ".join(errors[:5]) if errors else None
            current["state"] = "failed" if errors else "idle"

    def launch(self, role: str, seat: str, instance: str, *,
               peer_pid: int, peer_uid: int,
               peer_gid: int) -> Tuple[subprocess.Popen[bytes], List[int]]:
        scope = self.reserve(
            role, seat, instance,
            peer_pid=peer_pid, peer_uid=peer_uid, peer_gid=peer_gid,
        )
        cfg = self.config
        owner_uid, owner_gid = int(cfg["owner_uid"]), int(cfg["owner_gid"])
        account = dict(cfg["accounts"])[role]
        role_uid, role_gid = int(account["uid"]), int(account["gid"])
        output_dir, code_root = str(cfg["output_dir"]), str(cfg["code_root"])
        try:
            runtime_root = _copy_runtime_projection(code_root, role)
        except Exception:
            self._finish_failed_launch(scope, [], launched=False)
            raise
        outer = int(scope["outer"])
        # Trajectory write root follows the INSTANCE tag (D1): main "outer_N"
        # -> "outer_N" (unchanged), suffixed eval-round instances
        # "outer_N_eval" -> their own tree. Mirrors
        # BaseRole.traj_outer (the writer) and role_process._prepare_paths
        # (the pre-creation), so all three resolve the SAME root.
        traj_root = (f"outer_{instance.split('_', 1)[1]}"
                     if instance.startswith("outer_") else f"outer_{outer}")
        writable = [
            os.path.join("toolsets", role, instance, seat),
            os.path.join("logs", "roles", role, instance, seat),
            os.path.join("trajectory", traj_root, "roles", role, seat),
        ]
        if seat != "evaluate":
            writable.insert(0, os.path.join("workspaces", role, f"{instance}__{seat}"))
        if seat == "self_improve":
            writable.append(os.path.join("ckpt", "design", role))

        try:
            output_fd = _open_dir(output_dir)
        except Exception:
            shutil.rmtree(runtime_root, ignore_errors=True)
            self._finish_failed_launch(scope, [], launched=False)
            raise
        try:
            runtime_fd = _open_dir(runtime_root)
        except Exception:
            os.close(output_fd)
            shutil.rmtree(runtime_root, ignore_errors=True)
            self._finish_failed_launch(scope, [], launched=False)
            raise
        if (os.fstat(output_fd).st_uid != owner_uid
                or os.fstat(runtime_fd).st_uid != os.geteuid()):
            os.close(runtime_fd)
            os.close(output_fd)
            shutil.rmtree(runtime_root, ignore_errors=True)
            self._finish_failed_launch(scope, [], launched=False)
            raise RuntimeError("role sandbox roots have an unexpected owner")
        # The projection is created by the root broker with tempfile's 0700
        # default. bwrap resolves its /proc/self/fd source after setpriv has
        # dropped to the role UID, so that UID needs traversal only on the
        # projection root; all projected files remain read-only.
        _setfacl(str(cfg["setfacl"]), runtime_fd, "-m", f"u:{role_uid}:r-x")
        writable_fds: List[int] = []
        readonly_file_fds: List[int] = []
        secret_fds: List[int] = []
        traverse_entries: List[Tuple[int, Tuple[int, int, int]]] = []
        process: subprocess.Popen[bytes] | None = None
        allowed_uids = {owner_uid, role_uid}
        output_acl = False
        try:
            self._acquire_output_acl(role, output_fd, role_uid)
            output_acl = True
            ancestor_fds = _open_absolute_parent_dirs(output_dir, {0, owner_uid})
            traverse_entries.extend(
                self._acquire_traverse(ancestor_fds, str(cfg["setfacl"]), role_uid)
            )
            for relative in writable:
                fd = _open_beneath(output_fd, relative, allowed_uids)
                writable_fds.append(fd)
                _grant_tree(fd, str(cfg["setfacl"]), owner_uid, role_uid, allowed_uids)
                parent_fds = _open_parent_dirs(output_fd, relative, allowed_uids)
                traverse_entries.extend(
                    self._acquire_traverse(parent_fds, str(cfg["setfacl"]), role_uid)
                )
            sandbox_etc_dir = validate_sandbox_etc(str(cfg["sandbox_etc_dir"]))
            command = [
                str(cfg["bwrap"]), "--die-with-parent", "--new-session",
                "--unshare-all", "--unshare-net", "--cap-drop", "ALL",
                "--hostname", SANDBOX_HOSTNAME,
            ]
            created = {"/"}
            for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
                if os.path.exists(path):
                    command.extend(["--ro-bind", path, path])
                    created.add(path)
            command.extend(["--ro-bind", sandbox_etc_dir, "/etc"])
            created.add("/etc")
            prefix = str(cfg["python_prefix"])
            if not any(prefix == root or prefix.startswith(root + os.sep)
                       for root in ("/usr", "/bin", "/sbin", "/lib", "/lib64")):
                _add_empty_parents(command, prefix, created)
                command.extend(["--ro-bind", prefix, prefix])
                created.add(prefix)
            output_parent = os.path.dirname(output_dir.rstrip(os.sep)) or os.sep
            _add_empty_parents(command, output_parent, created)
            command.extend(["--tmpfs", output_parent])
            created.add(output_parent)
            _add_empty_parents(command, output_dir, created)
            command.extend(["--tmpfs", output_dir])
            created.add(output_dir)
            _add_empty_parents(command, code_root, created)
            command.extend(["--ro-bind", f"/proc/self/fd/{runtime_fd}", code_root])
            created.add(code_root)
            # The role only receives its own design configuration.  Other
            # designs and checkpoints remain outside the namespace.
            design_rel = os.path.join("ckpt", "design", role)
            if seat != "self_improve":
                design_fd = _open_beneath(output_fd, design_rel, allowed_uids)
                config_fd = None
                try:
                    config_fd = os.open(
                        "config.json",
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=design_fd,
                    )
                    config_info = os.fstat(config_fd)
                    if (not stat.S_ISREG(config_info.st_mode)
                            or config_info.st_uid not in allowed_uids
                            or config_info.st_nlink != 1):
                        raise RuntimeError(
                            "role design config has an unexpected owner or type"
                        )
                except FileNotFoundError:
                    pass
                except Exception:
                    if config_fd is not None:
                        os.close(config_fd)
                    raise
                finally:
                    os.close(design_fd)
                if config_fd is not None:
                    readonly_file_fds.append(config_fd)
                    config_target = os.path.join(output_dir, design_rel, "config.json")
                    _add_empty_parents(command, config_target, created)
                    command.extend(["--ro-bind-data", str(config_fd), config_target])
            for relative, fd in zip(writable, writable_fds):
                _add_empty_parents(command, os.path.join(output_dir, relative), created)
                command.extend(["--bind", f"/proc/self/fd/{fd}", os.path.join(output_dir, relative)])
            command.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])
            proxy_socket = str(cfg["proxy_socket"])
            _add_empty_parents(command, proxy_socket, created)
            command.extend(["--ro-bind", proxy_socket, proxy_socket])
            proxy_token_fd = _sealed_secret_fd(str(scope["proxy_token"]))
            secret_fds.append(proxy_token_fd)
            proxy_token_path = "/run/rsi-gan/proxy-token"
            _add_empty_parents(command, proxy_token_path, created)
            command.extend(["--ro-bind-data", str(proxy_token_fd), proxy_token_path])
            command.extend([
                "--chdir", code_root, "--setenv", "HOME", "/tmp",
                "--setenv", "PYTHONPATH", code_root,
                "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                "--setenv", "GAN_ROLE_BOUNDARY", "1",
                "--setenv", "GAN_ROLE_NAME", role,
                "--setenv", "GAN_ROLE_SEAT", seat,
                "--setenv", "GAN_ROLE_INSTANCE", instance,
                "--setenv", "GAN_ROLE_PROXY_TOKEN_FILE", proxy_token_path,
                "--setenv", "GAN_SECURE_CAPABILITY_HOLDER", "1",
                "--setenv", "GAN_SECURE_MODE", "1",
                "--setenv", "PYTHON_DOTENV_DISABLED", "1",
                "--setenv", "GAN_LLM_PROXY_UNIX", proxy_socket,
                "--setenv", "GAN_LLM_TIMEOUT_S", str(cfg["llm_timeout_s"]),
                str(cfg["python"]), "-m", "gan.framework.role_process", "--worker",
            ])
            pass_fds = tuple([
                output_fd, runtime_fd, *writable_fds, *readonly_file_fds, *secret_fds,
            ])
            process = subprocess.Popen(
                _setpriv_command(str(cfg["setpriv"]), role_uid, role_gid, command),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}, close_fds=True,
                pass_fds=pass_fds,
            )
            for fd in readonly_file_fds:
                os.close(fd)
            readonly_file_fds.clear()
            for fd in secret_fds:
                os.close(fd)
            secret_fds.clear()
            self._runtime_dirs[process.pid] = runtime_root
            self._proxy_tokens_by_pid[process.pid] = str(scope["proxy_token"])
            self._traverse_by_pid[process.pid] = traverse_entries
            self._mark_active(scope, process.pid)
            traverse_entries = []
            return process, [output_fd, runtime_fd, *writable_fds]
        except Exception as launch_error:
            reclaim_errors = []
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for fd in writable_fds:
                try:
                    _reclaim_tree(fd, str(cfg["setfacl"]), owner_uid, owner_gid, role_uid)
                except Exception as exc:
                    reclaim_errors.append(str(exc))
            if output_acl:
                try:
                    self._release_output_acl(role, output_fd, role_uid)
                except Exception as exc:
                    reclaim_errors.append(str(exc))
            for fd in [output_fd, runtime_fd, *writable_fds,
                       *readonly_file_fds, *secret_fds]:
                try:
                    os.close(fd)
                except OSError:
                    pass
            reclaim_errors.extend(
                self._release_traverse(traverse_entries, str(cfg["setfacl"]), role_uid)
            )
            shutil.rmtree(runtime_root, ignore_errors=True)
            if process is not None:
                try:
                    self._revoke_proxy(str(scope["proxy_token"]))
                except Exception as exc:
                    reclaim_errors.append(f"role proxy revoke failed: {exc}")
            self._finish_failed_launch(
                scope, reclaim_errors, launched=process is not None)
            if reclaim_errors:
                raise RuntimeError(
                    "role sandbox launch failed and ACL reclaim also failed: "
                    f"{type(launch_error).__name__}: {launch_error}; "
                    + "; ".join(reclaim_errors[:5])
                ) from launch_error
            raise

    def reclaim(self, role: str, fds: List[int], pid: int | None = None) -> None:
        cfg = self.config
        role_uid = int(dict(cfg["accounts"])[role]["uid"])
        errors = []
        if pid is not None:
            self._begin_reclaim(pid)
        try:
            for fd in fds[2:]:
                try:
                    _reclaim_tree(fd, str(cfg["setfacl"]), int(cfg["owner_uid"]),
                                  int(cfg["owner_gid"]), role_uid)
                except Exception as exc:  # keep revoking the remaining roots
                    errors.append(str(exc))
            traverse_entries = self._traverse_by_pid.pop(pid, []) if pid is not None else []
            errors.extend(self._release_traverse(traverse_entries, str(cfg["setfacl"]), role_uid))
            try:
                self._release_output_acl(role, fds[0], role_uid)
            except Exception as exc:
                errors.append(str(exc))
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if pid is not None:
                proxy_token = self._proxy_tokens_by_pid.pop(pid, None)
                if proxy_token:
                    try:
                        self._revoke_proxy(proxy_token)
                    except Exception as exc:
                        errors.append(f"role proxy revoke failed: {exc}")
                runtime_root = self._runtime_dirs.pop(pid, None)
                if runtime_root:
                    shutil.rmtree(runtime_root, ignore_errors=True)
                self._finish_reclaim(pid, errors)
        if errors:
            raise RuntimeError("; ".join(errors[:5]))


def _reply(handler: socketserver.StreamRequestHandler, payload: Dict[str, Any]) -> None:
    handler.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
    handler.wfile.flush()


class _RoleRequestHandler(socketserver.StreamRequestHandler):
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
        raw = self.rfile.readline(_MAX_AUTH_BYTES + 1)
        if not raw or len(raw) > _MAX_AUTH_BYTES:
            _reply(self, {"ok": False, "error": "invalid role broker request"})
            return
        self._clear_deadline()
        self.request.settimeout(None)
        process = None
        fds: List[int] = []
        role = ""
        authenticated = False
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict) or set(request) != {"role", "seat", "instance"}:
                raise ValueError("unexpected role broker request fields")
            if not hasattr(socket, "SO_PEERCRED"):
                raise PermissionError("role broker peer credentials are unavailable")
            credentials = self.request.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            peer_pid, peer_uid, peer_gid = struct.unpack("3i", credentials)
            role = str(request["role"])
            process, fds = self.server.state.launch(  # type: ignore[attr-defined]
                role, str(request["seat"]), str(request["instance"]),
                peer_pid=peer_pid, peer_uid=peer_uid, peer_gid=peer_gid,
            )
            _reply(self, {"ok": True})
            authenticated = True
            _relay(self.request, process)
        except _OuterNotActive as exc:
            _reply(self, {"ok": False, "error": str(exc), "retry": True})
        except _RoleCleanupPending as exc:
            _reply(self, {"ok": False, "error": str(exc), "retry": True})
        except PermissionError as exc:
            _reply(self, {"ok": False, "error": str(exc), "denied": True})
        except Exception as exc:  # noqa: BLE001
            if authenticated:
                print(f"[ERROR] role sandbox relay failed: {exc}", flush=True)
            else:
                try:
                    _reply(self, {"ok": False, "error": str(exc)[:2000]})
                except OSError:
                    pass
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if fds:
                try:
                    self.server.state.reclaim(role, fds, process.pid)  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001
                    print(f"[ERROR] role sandbox ACL reclaim failed: {exc}", flush=True)


_UnixStreamServer = getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)


class _RoleUnixServer(socketserver.ThreadingMixIn, _UnixStreamServer):
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


def _cleanup_ipc_dir(config: Dict[str, Any], socket_path: str) -> None:
    ipc_dir = os.path.realpath(str(config["ipc_dir"]))
    if os.path.dirname(os.path.realpath(socket_path)) != ipc_dir:
        raise RuntimeError("role broker IPC directory does not contain its socket")
    info = os.lstat(ipc_dir)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise RuntimeError("role broker IPC directory has an unexpected owner or type")
    shutil.rmtree(ipc_dir)


def broker_process_main(control: Connection, socket_path: str, config: Dict[str, Any]) -> None:
    _set_parent_death_signal()
    server = None
    thread = None
    try:
        if os.path.lexists(socket_path):
            raise RuntimeError(f"role broker socket path already exists: {socket_path}")
        state = _RoleBrokerState(config)
        server = _RoleUnixServer(socket_path, _RoleRequestHandler)
        server.state = state  # type: ignore[attr-defined]
        os.chown(socket_path, int(config["owner_uid"]), int(config["owner_gid"]))
        os.chmod(socket_path, 0o600)
        thread = threading.Thread(target=server.serve_forever,
                                  name="rsi-gan-role-broker", daemon=True)
        thread.start()
        control.send({"ok": True})
        while True:
            message = control.recv()
            operation = message.get("op") if isinstance(message, dict) else None
            if operation == "register":
                try:
                    state.register_outer(int(message["outer"]), dict(message["proxy_tokens"]))
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
        try:
            _cleanup_ipc_dir(config, socket_path)
        except Exception as exc:  # noqa: BLE001 -- preserve the protected directory
            print(f"[ERROR] role broker IPC directory cleanup failed: {exc}", flush=True)
        control.close()
