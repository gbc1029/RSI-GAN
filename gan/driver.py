"""Outer-loop driver (glue): one worker subprocess per outer.

The driver runs on the real repo (labels live here) and only orchestrates:
- materialize the per-run code tree once;
- spawn ``gan.outer_worker`` for each outer (cwd/PYTHONPATH = code_root);
- audit everything to ``logs/events.jsonl``.

Role self-edit patches are NOT applied here: they are applied + committed **by the
worker itself** (``loop._apply_self_patch`` ->
``code_repo.apply_self_patch``, allowlist + compile validated, rolled back on
failure; events ``self_improve_commit`` / ``self_improve_apply_failed``).
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
import json
import multiprocessing
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

from gan.framework import paths
from gan.framework.sandbox_etc import (
    SANDBOX_HOSTNAME,
    create_sandbox_etc,
    validate_sandbox_etc,
)


_OUTPUT_SOURCE_MARKERS = frozenset({
    ".env", ".git", "agent", "baselines", "datasets", "domains", "gan",
})


def _private_driver_umask(func: Any) -> Any:
    @wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        previous = os.umask(0o077)
        try:
            return func(*args, **kwargs)
        finally:
            os.umask(previous)

    return wrapped


@contextmanager
def _clean_broker_start_environment():
    """Expose only non-secret process settings during spawn inheritance."""
    previous = dict(os.environ)
    allowed = {
        "PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ",
        "PYTHONDONTWRITEBYTECODE",
    }
    clean = {
        key: value for key, value in previous.items()
        if key in allowed or key.startswith("LC_")
    }
    clean.update({
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "GAN_SECURE_MODE": "1",
        "PYTHON_DOTENV_DISABLED": "1",
    })
    os.environ.clear()
    os.environ.update(clean)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _validate_run_paths(repo_root: str, output_dir: str) -> tuple[str, str]:
    """Reject output trees that overlap the source or expose sensitive roots."""
    repo = os.path.realpath(os.path.abspath(os.path.expanduser(repo_root)))
    output_input = os.path.abspath(os.path.expanduser(output_dir))
    current = output_input
    while True:
        if os.path.lexists(current) and os.path.islink(current):
            raise RuntimeError(f"output_dir path must not contain a symlink: {current}")
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    output = os.path.realpath(output_input)
    if not os.path.isdir(repo):
        raise RuntimeError(f"repo_root is not an existing directory: {repo}")
    if output == os.path.abspath(os.path.sep):
        raise RuntimeError("output_dir must not be filesystem root")
    if os.name == "posix":
        tmp_root = os.path.realpath("/tmp")
        if os.path.commonpath([output, tmp_root]) == tmp_root:
            raise RuntimeError("output_dir must not be /tmp or below /tmp")
        repo_info = os.stat(repo, follow_symlinks=False)
        repo_identity = (repo_info.st_dev, repo_info.st_ino)
        current = output_input
        while not os.path.lexists(current):
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        while True:
            info = os.stat(current, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == repo_identity:
                raise RuntimeError("output_dir is inside a bind-mounted repo_root alias")
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        if os.path.exists(output):
            output_info = os.stat(output, follow_symlinks=False)
            output_identity = (output_info.st_dev, output_info.st_ino)
            current = repo
            while True:
                info = os.stat(current, follow_symlinks=False)
                if (info.st_dev, info.st_ino) == output_identity:
                    raise RuntimeError(
                        "output_dir is a bind-mounted alias of a repo_root ancestor"
                    )
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent
    homes = {os.path.realpath(os.path.expanduser("~"))}
    raw_uid = os.environ.get("SUDO_UID", "")
    if os.name == "posix" and raw_uid.isdigit():
        import pwd
        homes.add(os.path.realpath(pwd.getpwuid(int(raw_uid)).pw_dir))
    for home in homes:
        try:
            contains_home = os.path.commonpath([output, home]) == output
        except ValueError:
            contains_home = False
        if contains_home:
            raise RuntimeError(
                "output_dir must not be a home directory or contain one: "
                f"{output}"
            )
        if os.name == "posix" and os.path.exists(output):
            output_info = os.stat(output, follow_symlinks=False)
            output_identity = (output_info.st_dev, output_info.st_ino)
            current = home
            while True:
                info = os.stat(current, follow_symlinks=False)
                if (info.st_dev, info.st_ino) == output_identity:
                    raise RuntimeError(
                        "output_dir is a bind-mounted alias of a home ancestor"
                    )
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent
    try:
        overlap = os.path.commonpath([repo, output]) in {repo, output}
    except ValueError:
        overlap = False
    if overlap:
        raise RuntimeError(
            "output_dir must be a separate tree from repo_root "
            f"(repo_root={repo}, output_dir={output})"
        )
    if os.path.exists(output):
        if not os.path.isdir(output):
            raise RuntimeError("output_dir exists but is not a directory")
        present = sorted(name for name in _OUTPUT_SOURCE_MARKERS
                         if os.path.lexists(os.path.join(output, name)))
        if present:
            raise RuntimeError(
                "output_dir contains source/credential markers: "
                + ", ".join(present)
            )
    return repo, output


def _log_event(output_dir: str, event: Dict[str, Any]) -> None:
    from utils import trajectory_log
    trajectory_log.append(paths.events_path(output_dir), event)


def _load_events(output_dir: str) -> List[Dict[str, Any]]:
    from utils import trajectory_log
    ev_path = paths.events_path(output_dir)
    if not os.path.exists(ev_path):
        return []
    out: List[Dict[str, Any]] = []
    for line in trajectory_log.read_all(ev_path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _cost_role_of(e: Dict[str, Any]) -> str:
    # genid-scoped calls are the task child's; outer-scoped carry role directly.
    return "task" if e.get("scope_type") == "genid" else str(e.get("role") or "unknown")


def _cost_rollup(output_dir: str, *, label: str, since_ts: float = 0.0) -> float:
    """Log a per-outer llm-cost rollup event (root-side, from the proxy audit).

    ``delta`` covers calls since the previous rollup; ``cumulative`` covers the
    whole run. The per-call records stay in events.jsonl (single source); this
    is a monitoring convenience, never an authority.
    Returns the newest call timestamp seen (the next rollup's ``since_ts``)."""
    calls = [e for e in _load_events(output_dir) if e.get("type") == "llm_call"]

    def agg(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for role in ("planner", "evaluator", "task", "unknown"):
            sel = [e for e in items if _cost_role_of(e) == role]
            if not sel:
                continue
            out[role] = {
                "calls": len(sel),
                "prompt_tokens": int(sum(e.get("prompt_tokens") or 0 for e in sel)),
                "completion_tokens": int(sum(e.get("completion_tokens") or 0 for e in sel)),
                "total_tokens": int(sum(e.get("total_tokens") or 0 for e in sel)),
                "latency_s": round(sum(e.get("latency_ms") or 0 for e in sel) / 1000.0, 1),
                "failures": sum(1 for e in sel if e.get("status") != 200),
            }
        return out

    delta_calls = [e for e in calls if (e.get("timestamp") or 0) > since_ts]
    _log_event(output_dir, {
        "type": "cost_rollup", "scope": label,
        "delta": agg(delta_calls),
        "cumulative": agg(calls),
        "delta_calls": len(delta_calls), "cumulative_calls": len(calls),
    })
    return max((e.get("timestamp") or 0.0 for e in calls), default=since_ts)


def _outer_env(base: Dict[str, str], *, task_broker_socket: str, outer: int,
               role_broker_socket: str,
               role_accounts: Dict[str, Dict[str, Any]],
               code_root: str,
               proxy_socket: Optional[str] = None) -> Dict[str, str]:
    """Return the small non-secret environment inherited by outer_worker."""
    del proxy_socket  # compatibility argument; the outer must not receive this endpoint
    allowed = {
        "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
        "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
        "CUDA_VISIBLE_DEVICES",
        # litellm offline cost map (no github egress); set by run_gan.py
        "LITELLM_LOCAL_MODEL_COST_MAP",
    }
    env = {k: v for k, v in base.items()
           if k in allowed or k.startswith("LC_")}
    env["PYTHONPATH"] = code_root + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GAN_SECURE_MODE"] = "1"
    env["PYTHON_DOTENV_DISABLED"] = "1"
    # Per-LLM-call client budget consumed by agent/llm.py (roles +, via the
    # task env allowlist, the task child). Must exceed the proxy upstream
    # timeout so a retry cannot overlap an orphaned upstream call.
    env["GAN_LLM_TIMEOUT_S"] = str(
        os.environ.get("GAN_LLM_TIMEOUT_S", "600"))
    env["GAN_TASK_BROKER_UNIX"] = task_broker_socket
    env["GAN_TASK_BROKER_OUTER"] = str(int(outer))
    env["GAN_ROLE_BROKER_UNIX"] = role_broker_socket
    for role in ("planner", "evaluator"):
        env[f"GAN_ROLE_{role.upper()}_UID"] = str(role_accounts[role]["uid"])
        env[f"GAN_ROLE_{role.upper()}_GID"] = str(role_accounts[role]["gid"])
    return env


def _add_empty_parents(command: List[str], path: str, created: set) -> None:
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


def _outer_worker_command(cmd: List[str], repo_root: str, output_dir: str,
                          code_root: str,
                          proxy_socket: Optional[str] = None,
                          task_broker_socket: Optional[str] = None,
                          role_broker_socket: Optional[str] = None,
                          env_mask_file: Optional[str] = None,
                          sandbox_etc_dir: Optional[str] = None) -> List[str]:
    """Run outer_worker in a no-network view with only its run inputs mounted."""
    del proxy_socket  # compatibility argument; role/task brokers mount it directly
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError(
            "GAN outer_worker network isolation requires bubblewrap ('bwrap'); "
            "refusing to run planner/evaluator with host networking")
    if not sandbox_etc_dir:
        raise RuntimeError("outer sandbox requires a sanitized /etc projection")
    sandbox_etc_dir = validate_sandbox_etc(sandbox_etc_dir)
    command = [
        bwrap, "--die-with-parent", "--new-session",
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
    prefix = os.path.realpath(sys.prefix)
    if not any(prefix == root or prefix.startswith(root + os.sep)
               for root in ("/usr", "/bin", "/sbin", "/lib", "/lib64")):
        _add_empty_parents(command, prefix, created)
        command.extend(["--ro-bind", prefix, prefix])
        created.add(prefix)
    for source, target, mode in (
        (repo_root, repo_root, "--ro-bind"),
        (output_dir, output_dir, "--bind"),
        (code_root, code_root, "--bind"),
    ):
        if not os.path.exists(source):
            raise RuntimeError(f"outer sandbox bind source is missing: {source}")
        _add_empty_parents(command, target, created)
        command.extend([mode, source, target])
    sockets = []
    if task_broker_socket:
        sockets.append(task_broker_socket)
    if role_broker_socket:
        sockets.append(role_broker_socket)
    for socket_path in sockets:
        if not os.path.exists(socket_path):
            raise RuntimeError(f"outer sandbox bind source is missing: {socket_path}")
    if "/tmp" not in created:
        command.extend(["--dir", "/tmp"])
        created.add("/tmp")
    command.extend([
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
    ])
    created = {path for path in created if not path.startswith("/tmp/")}
    for socket_path in sockets:
        _add_empty_parents(command, socket_path, created)
        command.extend(["--ro-bind", socket_path, socket_path])
    if env_mask_file:
        if not os.path.isfile(env_mask_file):
            raise RuntimeError(f"outer sandbox env mask is missing: {env_mask_file}")
        for root in (repo_root, code_root):
            target = os.path.join(root, ".env")
            if os.path.isfile(target):
                command.extend(["--ro-bind", env_mask_file, target])
    command.extend(["--chdir", code_root, "--setenv", "HOME", "/tmp"])
    command.extend(cmd)
    return command


class _TaskBrokerController:
    def __init__(self, process: Any, control: Any) -> None:
        self.process = process
        self.control = control

    def register_outer(self, outer: int, budget: int) -> None:
        self.control.send({"op": "register", "outer": int(outer),
                           "budget": int(budget)})
        if not self.control.poll(10):
            raise RuntimeError("privileged task broker did not register the outer budget")
        response = self.control.recv()
        if not response.get("ok"):
            raise RuntimeError(f"privileged task broker registration failed: "
                               f"{response.get('error', 'invalid response')}")

    def activate_outer(self, outer: int, supervisor_pid: int) -> None:
        self.control.send({"op": "activate", "outer": int(outer),
                           "supervisor_pid": int(supervisor_pid)})
        if not self.control.poll(10):
            raise RuntimeError("privileged task broker did not activate the outer")
        response = self.control.recv()
        if not response.get("ok"):
            raise RuntimeError(f"privileged task broker activation failed: "
                               f"{response.get('error', 'invalid response')}")

    def release_outer(self, outer: int) -> None:
        self.control.send({"op": "release", "outer": int(outer)})
        if not self.control.poll(10):
            raise RuntimeError("privileged task broker did not release the outer")
        response = self.control.recv()
        if not response.get("ok"):
            raise RuntimeError(f"privileged task broker release failed: "
                               f"{response.get('error', 'invalid response')}")

    def close(self) -> None:
        try:
            self.control.send({"op": "shutdown"})
            if self.control.poll(10):
                self.control.recv()
        except (BrokenPipeError, EOFError, OSError):
            pass
        finally:
            self.control.close()
        self.process.join(timeout=10)
        if self.process.is_alive():
            print("[WARN] privileged task broker did not exit; it will receive "
                  "the configured parent-death signal when this driver exits")


class _RoleBrokerController:
    def __init__(self, process: Any, control: Any) -> None:
        self.process = process
        self.control = control

    def register_outer(self, outer: int,
                       proxy_tokens: Dict[str, Dict[str, str]]) -> None:
        self.control.send({"op": "register", "outer": int(outer),
                           "proxy_tokens": dict(proxy_tokens)})
        if not self.control.poll(10):
            raise RuntimeError("privileged role broker did not register the outer")
        response = self.control.recv()
        if not response.get("ok"):
            raise RuntimeError(f"privileged role broker registration failed: "
                               f"{response.get('error', 'invalid response')}")

    def activate_outer(self, outer: int, supervisor_pid: int) -> None:
        self.control.send({"op": "activate", "outer": int(outer),
                           "supervisor_pid": int(supervisor_pid)})
        if not self.control.poll(10):
            raise RuntimeError("privileged role broker did not activate the outer")
        response = self.control.recv()
        if not response.get("ok"):
            raise RuntimeError(f"privileged role broker activation failed: "
                               f"{response.get('error', 'invalid response')}")

    def release_outer(self, outer: int) -> None:
        self.control.send({"op": "release", "outer": int(outer)})
        if not self.control.poll(10):
            raise RuntimeError("privileged role broker did not release the outer")
        response = self.control.recv()
        if not response.get("ok"):
            raise RuntimeError(f"privileged role broker release failed: "
                               f"{response.get('error', 'invalid response')}")

    def close(self) -> None:
        try:
            self.control.send({"op": "shutdown"})
            if self.control.poll(10):
                self.control.recv()
        except (BrokenPipeError, EOFError, OSError):
            pass
        finally:
            self.control.close()
        self.process.join(timeout=10)
        if self.process.is_alive():
            print("[WARN] privileged role broker did not exit cleanly")


def _sudo_invoking_identity() -> tuple[int, int, str]:
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError(
            "secure TaskAgent launch requires running the driver through sudo"
        )
    raw_uid = os.environ.get("SUDO_UID", "")
    raw_gid = os.environ.get("SUDO_GID", "")
    if not raw_uid.isdigit() or not raw_gid.isdigit():
        raise RuntimeError("secure TaskAgent launch requires SUDO_UID and SUDO_GID")
    uid, gid = int(raw_uid), int(raw_gid)
    if uid == 0 or gid == 0:
        raise RuntimeError("secure TaskAgent launch refuses a root invoking identity")
    import pwd
    try:
        account = pwd.getpwuid(uid)
    except KeyError as exc:
        raise RuntimeError(f"sudo invoking uid {uid} does not exist") from exc
    if int(account.pw_gid) != gid:
        raise RuntimeError("SUDO_GID does not match the invoking user's primary gid")
    return uid, gid, account.pw_name


def _drop_driver_privileges(uid: int, gid: int, username: str) -> None:
    """Permanently return the orchestration process to the sudo caller."""
    import pwd
    home = pwd.getpwuid(uid).pw_dir
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if os.geteuid() != uid or os.getegid() != gid:
        raise RuntimeError("driver failed to drop root privileges")
    os.environ.update({"HOME": home, "USER": username, "LOGNAME": username})


def _trusted_root_tool(name: str) -> str:
    """Resolve a root-owned, non-writable privilege-boundary executable."""
    candidate = shutil.which(name)
    if not candidate:
        raise RuntimeError(f"secure TaskAgent launch requires {name}")
    path = os.path.realpath(candidate)
    try:
        info = os.stat(path)
    except OSError as exc:
        raise RuntimeError(f"cannot stat trusted executable {path}: {exc}") from exc
    if (not os.path.isabs(path) or not stat.S_ISREG(info.st_mode) or
            info.st_uid != 0 or info.st_mode & 0o022 or
            not os.access(path, os.X_OK)):
        raise RuntimeError(
            f"secure TaskAgent launch rejects untrusted {name} executable: {path}"
        )
    return path


def _trusted_ipc_parent() -> str:
    """Choose a root-controlled parent that cannot rename the IPC directory."""
    for path in ("/run", "/tmp"):
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if (stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                and ((info.st_mode & 0o022) == 0
                     or (info.st_mode & stat.S_ISVTX) != 0)):
            return path
    raise RuntimeError("secure IPC requires a root-owned /run or sticky /tmp")


def _start_task_broker(socket_path: str, config: Dict[str, Any]) -> _TaskBrokerController:
    from gan.framework.task_broker import broker_process_main
    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe()
    process = context.Process(
        target=broker_process_main,
        args=(child_control, socket_path, config),
        name="rsi-gan-privileged-task-broker",
    )
    with _clean_broker_start_environment():
        process.start()
    child_control.close()
    if not parent_control.poll(10):
        process.terminate()
        process.join(timeout=5)
        raise RuntimeError("privileged task broker did not start")
    response = parent_control.recv()
    if not response.get("ok"):
        process.join(timeout=5)
        raise RuntimeError(f"privileged task broker failed to start: "
                           f"{response.get('error', 'invalid response')}")
    return _TaskBrokerController(process, parent_control)


def _start_role_broker(socket_path: str, config: Dict[str, Any]) -> _RoleBrokerController:
    from gan.framework.role_broker import broker_process_main
    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe()
    process = context.Process(
        target=broker_process_main,
        args=(child_control, socket_path, config),
        name="rsi-gan-privileged-role-broker",
    )
    with _clean_broker_start_environment():
        process.start()
    child_control.close()
    if not parent_control.poll(10):
        process.terminate()
        process.join(timeout=5)
        raise RuntimeError("privileged role broker did not start")
    response = parent_control.recv()
    if not response.get("ok"):
        process.join(timeout=5)
        raise RuntimeError(f"privileged role broker failed to start: "
                           f"{response.get('error', 'invalid response')}")
    return _RoleBrokerController(process, parent_control)


def _materialize_run_roots(repo_root: str, output_dir: str, layout: str):
    """Materialize the run layout; returns (code_root, roots).

    ``code_root`` is what workers/roles RUN from: the single git tree, or the
    derived composed view (base+task+roles) in split layout (docs/12). ``roots``
    carries the per-owner repos for git-state routing (resume checkout).
    """
    from gan.build import ensure_roots
    roots = ensure_roots(repo_root, output_dir, layout)
    if layout == "split":
        from gan.framework.compose import build_composed_view
        code_root = paths.compose_root(output_dir)
        build_composed_view(roots.base, roots.task, roots.roles, code_root)
    else:
        code_root = roots.task
    return code_root, roots


@_private_driver_umask
def run_gan_driver(
    repo_root: str,
    output_dir: str,
    domains: List[str],
    subset: str = "_filtered_100_train",
    num_samples: int = 2,
    inner: Optional[int] = None,
    cfg_overrides: Optional[dict] = None,
    preflight: bool = False,
    resume: bool = False,
    force: bool = False,
) -> str:
    from gan.framework import checkpoint as ckpt_mod
    from gan.framework import models as model_registry
    from gan.framework import task_execution as task_execution
    from gan.framework.llm_proxy import ParentLLMProxy
    from gan.framework.loader import load_gan_loop_config, load_registry, resolve_domain

    if resume and force:
        raise ValueError("--resume and --force are mutually exclusive")

    cfg = load_gan_loop_config(cfg_overrides)
    layout = str(cfg.get("loop.code_layout", "single"))
    G = int(cfg.get("loop.outer_generations", 2))
    I_max = int(cfg.get("loop.inner_max", 3))
    task_llm_calls_per_question = int(
        cfg.get("loop.task_llm_max_calls_per_question", 64))
    if task_llm_calls_per_question < 1:
        raise ValueError("loop.task_llm_max_calls_per_question must be positive")
    repo_root, output_dir = _validate_run_paths(repo_root, output_dir)
    env_file = os.path.join(repo_root, ".env")
    if os.path.isfile(env_file):
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_file, override=False)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"

    # Domain shape checks (the ONLY default lives in scripts/run_gan.py; here the
    # value is required input). Empty/None and multi-value both fail fast before
    # any task execution. Multi-domain evaluation is not implemented yet.
    if not domains or not [d for d in domains if str(d).strip()]:
        raise ValueError(f"--domains is required and must be non-empty (got {domains!r}); "
                         f"refusing to run without an explicit task domain")
    if len(domains) != 1:
        raise ValueError(f"--domains accepts exactly one domain (got {domains}); "
                         f"multi-domain evaluation is not implemented yet")
    domain = domains[0].strip() if isinstance(domains[0], str) else domains[0]
    if not domain:
        raise ValueError(f"--domains parses to an empty domain (got {domains!r})")
    domain = str(domain)
    reg = load_registry()
    registered = set((reg.get("domains", {}) or {}).keys())
    families = set((reg.get("families", {}) or {}).keys())
    unregistered = domain not in registered and not any(
        domain.startswith(family) for family in families
    )
    if unregistered:
        raise ValueError(
            f"domain {domain!r} has no registered sandbox profile; refusing secure launch"
        )
    domain_config = resolve_domain(reg, domain)
    sandbox_profile = task_execution.validate_domain_sandbox_profile(
        domain, domain_config.get("sandbox"), repo_root,
    )

    owner_uid, owner_gid, owner_username = _sudo_invoking_identity()
    sandbox_username = os.environ.get("GAN_TASK_SANDBOX_USER", "").strip()
    if not sandbox_username:
        raise RuntimeError("GAN_TASK_SANDBOX_USER must name the TaskAgent system user")
    import pwd
    try:
        sandbox_account = pwd.getpwnam(sandbox_username)
    except KeyError as exc:
        raise RuntimeError(f"sandbox user {sandbox_username!r} does not exist") from exc
    if int(sandbox_account.pw_uid) == 0:
        raise RuntimeError("TaskAgent sandbox user must not be root")
    role_accounts = {}
    for role, default_user in (("planner", "rsi_gan_planner"),
                               ("evaluator", "rsi_gan_evaluator")):
        env_name = f"GAN_{role.upper()}_SANDBOX_USER"
        username = os.environ.get(env_name, default_user).strip()
        try:
            account = pwd.getpwnam(username)
        except KeyError as exc:
            raise RuntimeError(f"role sandbox user {username!r} does not exist") from exc
        if int(account.pw_uid) == 0:
            raise RuntimeError(f"{role} sandbox user must not be root")
        role_accounts[role] = {"username": username, "uid": int(account.pw_uid),
                               "gid": int(account.pw_gid)}
    role_uids = {entry["uid"] for entry in role_accounts.values()}
    if len(role_uids) != 2 or int(sandbox_account.pw_uid) in role_uids or owner_uid in role_uids:
        raise RuntimeError("planner/evaluator/task/owner identities must be distinct")

    setpriv_path = _trusted_root_tool("setpriv")
    setfacl_path = _trusted_root_tool("setfacl")
    bwrap_path = _trusted_root_tool("bwrap")

    python_executable = os.path.realpath(sys.executable)
    python_prefix = os.path.realpath(sys.prefix)
    if not os.path.isfile(python_executable) or not os.access(python_executable, os.X_OK):
        raise RuntimeError(f"trusted Python executable is not executable: {python_executable}")
    if os.path.commonpath([python_prefix, python_executable]) != python_prefix:
        raise RuntimeError("trusted Python executable is outside its canonical prefix")

    models = {
        "task": model_registry.resolve("gan.task"),
        "planner": model_registry.resolve("gan.planner"),
        "evaluator": model_registry.resolve("gan.evaluator"),
    }
    proxy_dir = tempfile.mkdtemp(prefix="rsi-gan-proxy-", dir=_trusted_ipc_parent())
    os.chmod(proxy_dir, 0o711)
    try:
        sandbox_etc_dir = create_sandbox_etc(
            proxy_dir,
            owner=(owner_uid, owner_gid),
            task=(int(sandbox_account.pw_uid), int(sandbox_account.pw_gid)),
            planner=(int(role_accounts["planner"]["uid"]),
                     int(role_accounts["planner"]["gid"])),
            evaluator=(int(role_accounts["evaluator"]["uid"]),
                       int(role_accounts["evaluator"]["gid"])),
        )
    except Exception:
        shutil.rmtree(proxy_dir, ignore_errors=True)
        raise
    proxy_socket = os.path.join(proxy_dir, "p.sock")
    task_broker_socket = os.path.join(proxy_dir, "task.sock")
    role_broker_socket = os.path.join(proxy_dir, "role.sock")
    env_mask_file = os.path.join(proxy_dir, "empty.env")
    llm_client_timeout_s = float(cfg.get("loop.llm_client_timeout_s", 600))
    llm_proxy_timeout_s = float(cfg.get("loop.llm_proxy_timeout_s", 550))
    if (llm_proxy_timeout_s <= 0 or llm_client_timeout_s <= 0
            or llm_proxy_timeout_s >= llm_client_timeout_s):
        raise ValueError(
            "LLM timeouts must satisfy 0 < loop.llm_proxy_timeout_s "
            "< loop.llm_client_timeout_s")
    proxy = ParentLLMProxy(
        output_dir, proxy_socket,
        models=models,
        # Must stay below the client per-call budget (GAN_LLM_TIMEOUT_S,
        # default 600s). Otherwise the client retries while the old upstream
        # request is still running, creating overlap and a late broken pipe.
        timeout=llm_proxy_timeout_s,
        socket_uids=[
            int(sandbox_account.pw_uid),
            int(role_accounts["planner"]["uid"]),
            int(role_accounts["evaluator"]["uid"]),
        ],
        setfacl_path=setfacl_path,
    )
    proxy_control_token = proxy.issue_framework_scope(0, peer_uid=0)
    safe_broker_env = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
                   "PYTHONDONTWRITEBYTECODE"} or key.startswith("LC_")
    }
    domain_cfg = resolve_domain(reg, domain)
    safe_broker_env["GAN_SECURE_MODE"] = "1"
    safe_broker_env["PYTHON_DOTENV_DISABLED"] = "1"
    question_timeout_s = int(domain_config.get("question_timeout_s") or 300)
    harness_timeout = int(num_samples) * question_timeout_s + 900
    harness_num_workers = int(domain_cfg.get("harness_num_workers", 5))
    if harness_num_workers < 1:
        raise ValueError(f"domain {domain!r} harness_num_workers must be positive")
    broker_config = {
        "output_dir": output_dir,
        "repo_root": repo_root,
        "domain": domain,
        "subset": subset,
        "num_samples": int(num_samples),
        "model": models["task"],
        # Same models.yaml entry as ``model``; resolve_entry() validated it at
        # startup. Rides the broker config (like the model), never the env.
        "reasoning_effort": model_registry.resolve_entry("gan.task").get("reasoning_effort"),
        # The broker builds the task env ITSELF, so the per-question wall and
        # the per-call client budget must be pinned here: without them the
        # harness silently fell back to its 300s default (invisible for
        # paper_review, whose domain value IS 300; search_arena 600 / imo 1200
        # were being cut to 300 despite the parent-side total budget).
        "question_timeout_s": int(domain_cfg.get("question_timeout_s")
                                  or cfg.get("loop.question_timeout_s", 300)),
        "llm_client_timeout_s": llm_client_timeout_s,
        "harness_num_workers": harness_num_workers,
        "python": python_executable,
        "python_prefix": python_prefix,
        "timeout": int(domain_cfg.get("harness_timeout_s") or harness_timeout),
        "task_brief": (domain_cfg.get("task_brief") or ""),
        "parent_scored": task_execution.uses_parent_scoring(domain),
        # Question-sampling scheme (loop.yaml sampling.*). The broker reads the
        # run-persistent seed file at launch time (per generation) so a resume
        # reuses the same base seed without restart-order constraints.
        "sampling_mode": str((cfg.get("sampling") or {}).get("mode", "seeded")),
        "sandbox_profile": sandbox_profile,
        "owner_uid": owner_uid,
        "owner_gid": owner_gid,
        "setpriv": setpriv_path,
        "setfacl": setfacl_path,
        "bwrap": bwrap_path,
        "sandbox_username": sandbox_username,
        "sandbox_uid": int(sandbox_account.pw_uid),
        "proxy_socket": proxy_socket,
        "proxy_control_token": proxy_control_token,
        "sandbox_etc_dir": sandbox_etc_dir,
        "task_llm_max_calls": (
            max(1, int(num_samples)) *
            task_llm_calls_per_question
        ),
        "base_env": safe_broker_env,
    }
    try:
        task_broker = _start_task_broker(task_broker_socket, broker_config)
    except Exception:
        shutil.rmtree(proxy_dir, ignore_errors=True)
        raise
    try:
        role_broker = _start_role_broker(role_broker_socket, {
            "output_dir": output_dir,
            "code_root": (paths.compose_root(output_dir)
                          if layout == "split" else paths.code_root(output_dir)),
            "owner_uid": owner_uid,
            "owner_gid": owner_gid,
            "accounts": role_accounts,
            "python": python_executable,
            "python_prefix": python_prefix,
            "setpriv": setpriv_path,
            "setfacl": setfacl_path,
            "bwrap": bwrap_path,
            "proxy_socket": proxy_socket,
            "proxy_control_token": proxy_control_token,
            "llm_timeout_s": str(os.environ.get("GAN_LLM_TIMEOUT_S", "600")),
            "ipc_dir": proxy_dir,
            "sandbox_etc_dir": sandbox_etc_dir,
        })
    except Exception:
        task_broker.close()
        shutil.rmtree(proxy_dir, ignore_errors=True)
        raise
    try:
        with open(env_mask_file, "w", encoding="utf-8"):
            pass
        os.chmod(env_mask_file, 0o444)
        proxy.bind()
        _drop_driver_privileges(owner_uid, owner_gid, owner_username)
        os.makedirs(output_dir, exist_ok=True)
        output_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        output_fd = os.open(output_dir, output_flags)
        try:
            output_info = os.fstat(output_fd)
            if (not stat.S_ISDIR(output_info.st_mode)
                    or output_info.st_uid != owner_uid):
                raise RuntimeError(
                    "output_dir has an unexpected owner or type after privilege drop"
                )
            os.fchmod(output_fd, 0o700)
        finally:
            os.close(output_fd)
        if not os.access(output_dir, os.R_OK | os.W_OK | os.X_OK):
            raise RuntimeError(
                f"sudo invoking user cannot read/write/traverse output_dir: {output_dir}"
            )

        # Re-entry policy. Follow the newest completed attempt.
        newest = ckpt_mod.newest_outer_snapshot(output_dir)
        if newest and not (resume or force):
            raise RuntimeError(
                f"output_dir already contains outer snapshot(s) (latest completed "
                f"outer = {newest['index']}); re-running from outer 1 would pollute "
                f"the tree/UCB. Use --resume to continue at outer {newest['index'] + 1}, "
                f"or --force to re-run from outer 1 (legacy duplicate-prone behaviour, "
                f"explicit escape hatch).")

        code_root, roots = _materialize_run_roots(repo_root, output_dir, layout)

        # Question-sampling scheme (loop.yaml sampling.*). Created post-drop so
        # the seed file is owned by the run owner; idempotent across resume.
        sampling_cfg = cfg.get("sampling") or {}
        sampling_mode = str(sampling_cfg.get("mode", "seeded"))
        sample_seed_base: Optional[int] = None
        anchor_ids: list = []
        if sampling_mode == "seeded":
            # A FIXED base seed makes every ARM of an ablation matrix sample the
            # SAME questions (pairing / fairness); 0/absent keeps the old
            # random-per-run behaviour (default, no behaviour change).
            fixed_seed = int(sampling_cfg.get("base_seed") or 0)
            seed_info = task_execution.ensure_sampling_seed(
                output_dir, domain, subset, repo_root, int(num_samples),
                anchor_k=int(sampling_cfg.get("anchor_k", 2)),
                base_seed=fixed_seed or None,
            )
            sample_seed_base = int(seed_info["base_seed"])
            anchor_ids = list(seed_info.get("anchor_ids") or [])
            _log_event(output_dir, {"type": "sampling_seed_ready",
                                    "mode": "seeded",
                                    "base_seed": sample_seed_base,
                                    "fixed_by_config": bool(fixed_seed),
                                    "anchor_ids": anchor_ids})
        else:
            _log_event(output_dir, {"type": "sampling_seed_ready",
                                    "mode": sampling_mode})

        start = (int(newest["index"]) + 1) if (resume and newest) else 1
        _driver_t0 = time.time()
        # Arm identity rides the RUN, not just the directory name: a folder
        # called "B1" is not evidence of which switches were active. Only
        # NON-DEFAULT ablation values are recorded (the table is mostly
        # False/"parent"), and B1 lives outside ablation.* -- so it is folded
        # in explicitly. `cfg_overrides` is dumped verbatim, which makes a
        # `--set`/`--ablation` experiment reproducible from events.jsonl alone.
        _ablation_active = {
            k: v for k, v in (cfg.get("ablation") or {}).items()
            if v not in (False, None, "", "parent")
        }
        if bool(cfg.get("source_access.deny_deep", False)):
            _ablation_active["deny_deep"] = True
        # EFFECTIVE regime. When a switch is no longer pinned per arm (the ablation
        # driver stopped forcing --set outer_select.enabled), cfg_overrides no
        # longer proves which outer-level regime a run used. Record the resolved
        # values here so every arm stays auditable from events.jsonl alone.
        _effective = {
            "outer_select.enabled": bool(cfg.get("outer_select.enabled", False)),
            "mome.cold_start_min": int(cfg.get("mome.cold_start_min", 10)),
            "mome.novelty_k": int(cfg.get("mome.novelty_k", 5)),
            "eval.eval_samples": int(cfg.get("eval.eval_samples", 3)),
            "loop.inner_max": int(cfg.get("loop.inner_max", 3)),
        }
        _log_event(output_dir, {"type": "driver_start", "code_root": code_root,
                                "outer_generations": G,
                                "domain": domain,
                                "domains": list(domains),
                                "resume": resume, "force": force,
                                "latest_completed_outer": (newest["index"] if newest else None),
                                "start_outer": start,
                                "ablation": _ablation_active,
                                "cfg_overrides": cfg_overrides or {},
                                "effective": _effective,
                                "num_samples": num_samples,
                                "subset": subset})

        if resume:
            if start > G:
                _log_event(output_dir, {"type": "resume_complete",
                                        "latest_completed_outer": newest["index"],
                                        "outer_generations": G})
                print(f"Nothing to resume: outer {newest['index']} is the newest snapshot; "
                      f"outer_generations={G} already covered.")
                return code_root
            payload = ckpt_mod.load_checkpoint(output_dir, "outer") or {}
            commit = (payload.get("code") or {}).get("commit")
            if commit:
                from gan.framework import code_repo
                checkout_target = roots.task if layout == "split" else code_root
                current = code_repo.current_commit(checkout_target)
                if current != commit:
                    code_repo.checkout(checkout_target, commit)
                    if layout == "split":
                        from gan.framework.compose import build_composed_view
                        build_composed_view(roots.base, roots.task, roots.roles, code_root)
                    _log_event(output_dir, {"type": "code_state_restored",
                                            "from": current, "to": commit})
            else:
                _log_event(output_dir, {"type": "code_layer_absent",
                                        "hint": "checkpoint predates the code layer"})

        if preflight:
            from gan.framework import preflight as preflight_mod
            results = preflight_mod.preflight(["gan.task", "gan.planner", "gan.evaluator"])
            _log_event(output_dir, {"type": "preflight", "results": results})
            if not preflight_mod.all_ok(results):
                raise RuntimeError(f"model preflight failed: {results}")

        proxy.start()
        cost_since = 0.0  # llm_call timestamp watermark for per-outer deltas
        for outer in range(start, G + 1):
            role_proxy_tokens = {
                "planner": {
                    seat: proxy.issue_scope(
                        "planner", str(outer), models["planner"], seat=seat,
                        peer_uid=int(role_accounts["planner"]["uid"]))
                    for seat in ("plan", "self_improve")
                },
                "evaluator": {
                    seat: proxy.issue_scope(
                        "evaluator", str(outer), models["evaluator"], seat=seat,
                        peer_uid=int(role_accounts["evaluator"]["uid"]))
                    for seat in ("evaluate", "self_improve")
                },
            }
            # Fixed-root evaluation (docs/14 §5.5) needs a SECOND planner
            # instance per outer: the loop builds ``outer_<N>_eval``. Give the
            # planner's plan seat a second proxy token; the role broker registers
            # it as a separate launchable scope (outer_<N>_eval).
            role_proxy_tokens["planner"]["plan"] = [
                role_proxy_tokens["planner"]["plan"],
                proxy.issue_scope(
                    "planner", str(outer), models["planner"], seat="plan",
                    peer_uid=int(role_accounts["planner"]["uid"])),
            ]
            role_broker.register_outer(outer, role_proxy_tokens)
            proc = None
            try:
                task_broker.register_outer(outer, I_max)
                cmd = [
                    python_executable, "-m", "gan.outer_worker",
                    "--repo_root", repo_root, "--output_dir", output_dir,
                    "--outer", str(outer), "--domains", domain,
                    "--subset", subset, "--num_samples", str(num_samples),
                ]
                if sample_seed_base is not None:
                    cmd.extend(["--sample-seed-base", str(sample_seed_base)])
                    if anchor_ids:
                        cmd.extend(["--anchor-ids", ",".join(anchor_ids)])
                # Overrides must reach the per-outer worker: it rebuilds its own
                # loop config from loop.yaml + these overrides. Forwarding ONLY
                # `ablation` silently reverted every other section (outer_select.*,
                # fitness.*, mome.*, eval.*) to loop.yaml on the worker side —
                # observed as `--set outer_select.enabled=false` being ignored
                # while the working-tree YAML said true.
                worker_overrides: Dict[str, Any] = deepcopy(cfg_overrides or {})
                loop_ov = worker_overrides.get("loop")
                if isinstance(loop_ov, dict):
                    # The worker gets these as explicit args; drop the duplicates.
                    loop_ov.pop("outer_generations", None)
                    loop_ov.pop("inner_max", None)
                    if not loop_ov:
                        worker_overrides.pop("loop", None)
                ab = cfg.get("ablation") or {}
                if ab:
                    worker_overrides["ablation"] = dict(ab)
                # B1 rides source_access.deny_deep (its only consumer), not ablation.
                worker_overrides.setdefault("source_access", {})["deny_deep"] = \
                    bool(cfg.get("source_access.deny_deep", False))
                if worker_overrides:
                    cmd.extend(["--cfg-overrides", json.dumps(worker_overrides)])
                if inner is not None:
                    cmd.extend(["--inner", str(inner)])
                if resume:
                    cmd += ["--resume-boundary", "outer"]
                env = _outer_env(
                    os.environ, task_broker_socket=task_broker_socket,
                    outer=outer,
                    role_broker_socket=role_broker_socket,
                    role_accounts=role_accounts,
                    code_root=code_root,
                )
                proc = subprocess.Popen(
                    _outer_worker_command(
                        cmd, repo_root, output_dir, code_root,
                        task_broker_socket=task_broker_socket,
                        role_broker_socket=role_broker_socket,
                        env_mask_file=env_mask_file,
                        sandbox_etc_dir=sandbox_etc_dir,
                    ),
                    cwd=code_root, env=env,
                )
                task_broker.activate_outer(outer, proc.pid)
                role_broker.activate_outer(outer, proc.pid)
                returncode = proc.wait()
            except BaseException:
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                raise
            finally:
                failed = sys.exc_info()[0] is not None
                release_errors = []
                try:
                    task_broker.release_outer(outer)
                except Exception as exc:
                    release_errors.append(f"task broker: {exc}")
                try:
                    role_broker.release_outer(outer)
                except Exception as exc:
                    release_errors.append(f"role broker: {exc}")
                if release_errors:
                    if not failed:
                        raise RuntimeError("; ".join(release_errors))
                    print(f"[WARN] privileged broker release failed: "
                          f"{'; '.join(release_errors)}")
            if returncode != 0:
                _log_event(output_dir, {"type": "outer_worker_failed", "outer": outer,
                                        "rc": returncode})
                _cost_rollup(output_dir, label=f"outer_{outer}_failed",
                             since_ts=cost_since)
                raise RuntimeError(f"outer worker failed at outer {outer} (rc={returncode})")
            # Role self-edits are applied+committed by the worker itself.
            cost_since = _cost_rollup(output_dir, label=f"outer_{outer}",
                                      since_ts=cost_since)
        _log_event(output_dir, {"type": "driver_done", "code_root": code_root,
                                "duration_s": round(time.time() - _driver_t0, 1)})
        _cost_rollup(output_dir, label="run", since_ts=0.0)  # full-run cumulative
        return code_root
    finally:
        try:
            proxy.close()
        finally:
            try:
                task_broker.close()
            finally:
                role_broker.close()
                shutil.rmtree(proxy_dir, ignore_errors=True)
