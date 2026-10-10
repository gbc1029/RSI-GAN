"""Root-only launcher for planner/evaluator seat processes."""
from __future__ import annotations

import ctypes
import json
import os
import re
import secrets
import selectors
import signal
import socket
import socketserver
import stat
import subprocess
import threading
import shutil
import tempfile
from multiprocessing.connection import Connection
from typing import Any, Dict, List, Tuple

_INSTANCE_RE = re.compile(r"outer_(0|[1-9][0-9]{0,11})\Z")
_MAX_AUTH_BYTES = 16 * 1024
_ROLE_SEATS = {
    "planner": {"plan", "self_improve"},
    "evaluator": {"evaluate", "self_improve"},
}


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
                "gan/roles/__init__.py", "gan/roles/base_role.py",
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
        self._tokens: Dict[str, Dict[str, Any]] = {}
        self._runtime_dirs: Dict[int, str] = {}
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

    def register_outer(self, outer: int, proxy_tokens: Dict[str, str]) -> Dict[str, str]:
        if outer < 1 or set(proxy_tokens) != set(_ROLE_SEATS):
            raise ValueError("invalid role broker outer registration")
        issued = {}
        with self._lock:
            for role, seats in _ROLE_SEATS.items():
                token = "rb-gan-" + secrets.token_urlsafe(32)
                self._tokens[token] = {
                    "role": role, "outer": int(outer), "seats": set(seats),
                    "proxy_token": str(proxy_tokens[role]),
                }
                issued[role] = token
        return issued

    def reserve(self, token: str, role: str, seat: str, instance: str) -> Dict[str, Any]:
        match = _INSTANCE_RE.fullmatch(instance)
        if role not in _ROLE_SEATS or seat not in _ROLE_SEATS[role] or not match:
            raise PermissionError("invalid role launch scope")
        with self._lock:
            scope = self._tokens.get(token)
            if scope is None or scope["role"] != role or scope["outer"] != int(match.group(1)):
                raise PermissionError("invalid role launch capability")
            if seat not in scope["seats"]:
                raise PermissionError("role seat was already launched")
            scope["seats"].remove(seat)
            return dict(scope)

    def launch(self, token: str, role: str, seat: str,
               instance: str) -> Tuple[subprocess.Popen[bytes], List[int]]:
        scope = self.reserve(token, role, seat, instance)
        cfg = self.config
        owner_uid, owner_gid = int(cfg["owner_uid"]), int(cfg["owner_gid"])
        account = dict(cfg["accounts"])[role]
        role_uid, role_gid = int(account["uid"]), int(account["gid"])
        output_dir, code_root = str(cfg["output_dir"]), str(cfg["code_root"])
        runtime_root = _copy_runtime_projection(code_root, role)
        outer = int(scope["outer"])
        writable = [
            os.path.join("toolsets", role, instance, seat),
            os.path.join("logs", "roles", role, instance, seat),
            os.path.join("trajectory", f"outer_{outer}", "roles", role, seat),
        ]
        if seat != "evaluate":
            writable.insert(0, os.path.join("workspaces", role, f"{instance}__{seat}"))
        if seat == "self_improve":
            writable.append(os.path.join("ckpt", "design", role))

        output_fd = _open_dir(output_dir)
        try:
            runtime_fd = _open_dir(runtime_root)
        except Exception:
            os.close(output_fd)
            shutil.rmtree(runtime_root, ignore_errors=True)
            raise
        if (os.fstat(output_fd).st_uid != owner_uid
                or os.fstat(runtime_fd).st_uid != os.geteuid()):
            os.close(runtime_fd)
            os.close(output_fd)
            shutil.rmtree(runtime_root, ignore_errors=True)
            raise RuntimeError("role sandbox roots have an unexpected owner")
        # The projection is created by the root broker with tempfile's 0700
        # default. bwrap resolves its /proc/self/fd source after setpriv has
        # dropped to the role UID, so that UID needs traversal only on the
        # projection root; all projected files remain read-only.
        _setfacl(str(cfg["setfacl"]), runtime_fd, "-m", f"u:{role_uid}:r-x")
        writable_fds: List[int] = []
        readonly_file_fds: List[int] = []
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
            command = [
                str(cfg["bwrap"]), "--die-with-parent", "--new-session",
                "--unshare-all", "--unshare-net", "--cap-drop", "ALL",
            ]
            created = {"/"}
            for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"):
                if os.path.exists(path):
                    command.extend(["--ro-bind", path, path])
                    created.add(path)
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
            command.extend([
                "--chdir", code_root, "--setenv", "HOME", "/tmp",
                "--setenv", "PYTHONPATH", code_root,
                "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                "--setenv", "GAN_ROLE_SEAT", seat,
                "--setenv", "GAN_LLM_PROXY_UNIX", proxy_socket,
                "--setenv", f"GAN_PROXY_{role.upper()}_TOKEN", str(scope["proxy_token"]),
                "--setenv", "GAN_LLM_TIMEOUT_S", str(cfg["llm_timeout_s"]),
                str(cfg["python"]), "-m", "gan.framework.role_process", "--worker",
            ])
            pass_fds = tuple([
                output_fd, runtime_fd, *writable_fds, *readonly_file_fds,
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
            self._runtime_dirs[process.pid] = runtime_root
            self._traverse_by_pid[process.pid] = traverse_entries
            traverse_entries = []
            return process, [output_fd, runtime_fd, *writable_fds]
        except Exception:
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
                except Exception:
                    pass
            if output_acl:
                try:
                    self._release_output_acl(role, output_fd, role_uid)
                except Exception:
                    pass
            for fd in [output_fd, runtime_fd, *writable_fds, *readonly_file_fds]:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._release_traverse(traverse_entries, str(cfg["setfacl"]), role_uid)
            shutil.rmtree(runtime_root, ignore_errors=True)
            raise

    def reclaim(self, role: str, fds: List[int], pid: int | None = None) -> None:
        cfg = self.config
        role_uid = int(dict(cfg["accounts"])[role]["uid"])
        errors = []
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
                runtime_root = self._runtime_dirs.pop(pid, None)
                if runtime_root:
                    shutil.rmtree(runtime_root, ignore_errors=True)
        if errors:
            raise RuntimeError("; ".join(errors[:5]))


def _reply(handler: socketserver.StreamRequestHandler, payload: Dict[str, Any]) -> None:
    handler.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
    handler.wfile.flush()


class _RoleRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(_MAX_AUTH_BYTES + 1)
        if not raw or len(raw) > _MAX_AUTH_BYTES:
            _reply(self, {"ok": False, "error": "invalid role broker request"})
            return
        process = None
        fds: List[int] = []
        role = ""
        authenticated = False
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict) or set(request) != {"token", "role", "seat", "instance"}:
                raise ValueError("unexpected role broker request fields")
            role = str(request["role"])
            process, fds = self.server.state.launch(  # type: ignore[attr-defined]
                str(request["token"]), role, str(request["seat"]), str(request["instance"])
            )
            _reply(self, {"ok": True})
            authenticated = True
            _relay(self.request, process)
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


def broker_process_main(control: Connection, socket_path: str, config: Dict[str, Any]) -> None:
    _set_parent_death_signal()
    server = None
    thread = None
    try:
        if os.path.lexists(socket_path):
            os.unlink(socket_path)
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
                    tokens = state.register_outer(int(message["outer"]), dict(message["proxy_tokens"]))
                    control.send({"ok": True, "tokens": tokens})
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
