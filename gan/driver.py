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

import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

from gan.framework import paths


def _log_event(output_dir: str, event: Dict[str, Any]) -> None:
    from utils import trajectory_log
    trajectory_log.append(paths.events_path(output_dir), event)


def _outer_env(base: Dict[str, str], *, proxy_socket: str,
               planner_token: str, evaluator_token: str,
               task_broker_socket: str, task_broker_token: str,
               code_root: str) -> Dict[str, str]:
    """Return the small non-secret environment inherited by outer_worker."""
    allowed = {
        "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
        "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
        "CUDA_VISIBLE_DEVICES",
    }
    env = {k: v for k, v in base.items()
           if k in allowed or k.startswith("LC_")}
    env["PYTHONPATH"] = code_root + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Per-LLM-call client budget consumed by agent/llm.py (roles +, via the
    # task env allowlist, the task child). Must stay <= the proxy upstream
    # timeout so the proxy is never the first to cut a healthy call.
    env["GAN_LLM_TIMEOUT_S"] = str(
        os.environ.get("GAN_LLM_TIMEOUT_S", "600"))
    env["GAN_LLM_PROXY_UNIX"] = proxy_socket
    env["GAN_PROXY_PLANNER_TOKEN"] = planner_token
    env["GAN_PROXY_EVALUATOR_TOKEN"] = evaluator_token
    env["GAN_TASK_BROKER_UNIX"] = task_broker_socket
    env["GAN_TASK_BROKER_TOKEN"] = task_broker_token
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
                          code_root: str, proxy_socket: str,
                          task_broker_socket: Optional[str] = None,
                          env_mask_file: Optional[str] = None) -> List[str]:
    """Run outer_worker in a no-network view with only its run inputs mounted."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError(
            "GAN outer_worker network isolation requires bubblewrap ('bwrap'); "
            "refusing to run planner/evaluator with host networking")
    command = [
        bwrap, "--die-with-parent", "--new-session",
        "--unshare-all", "--unshare-net", "--cap-drop", "ALL",
    ]
    created = {"/"}
    for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"):
        if os.path.exists(path):
            command.extend(["--ro-bind", path, path])
            created.add(path)
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
    sockets = [proxy_socket]
    if task_broker_socket:
        sockets.append(task_broker_socket)
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

    def register_outer(self, budget: int) -> str:
        self.control.send({"op": "register", "budget": int(budget)})
        if not self.control.poll(10):
            raise RuntimeError("privileged task broker did not register the outer budget")
        response = self.control.recv()
        if not response.get("ok") or not response.get("token"):
            raise RuntimeError(f"privileged task broker registration failed: "
                               f"{response.get('error', 'invalid response')}")
        return str(response["token"])

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


def _start_task_broker(socket_path: str, config: Dict[str, Any]) -> _TaskBrokerController:
    if "fork" not in multiprocessing.get_all_start_methods():
        raise RuntimeError("secure TaskAgent launch requires POSIX fork support")
    from gan.framework.task_broker import broker_process_main
    context = multiprocessing.get_context("fork")
    parent_control, child_control = context.Pipe()
    process = context.Process(
        target=broker_process_main,
        args=(child_control, socket_path, config),
        name="rsi-gan-privileged-task-broker",
    )
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
    from gan.build import ensure_code_root
    from gan.framework import checkpoint as ckpt_mod
    from gan.framework import models as model_registry
    from gan.framework import task_execution as task_execution
    from gan.framework.llm_proxy import ParentLLMProxy
    from gan.framework.loader import load_gan_loop_config, load_registry, resolve_domain

    if resume and force:
        raise ValueError("--resume and --force are mutually exclusive")

    cfg = load_gan_loop_config(cfg_overrides)
    G = int(cfg.get("loop.outer_generations", 2))
    I_max = int(cfg.get("loop.inner_max", 3))
    repo_root = os.path.realpath(os.path.abspath(repo_root))
    output_dir = os.path.realpath(os.path.abspath(output_dir))

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
    proxy_dir = tempfile.mkdtemp(prefix="rsi-gan-proxy-")
    os.chmod(proxy_dir, 0o755)
    os.chown(proxy_dir, owner_uid, owner_gid)
    proxy_socket = os.path.join(proxy_dir, "p.sock")
    task_broker_socket = os.path.join(proxy_dir, "task.sock")
    env_mask_file = os.path.join(proxy_dir, "empty.env")
    proxy = ParentLLMProxy(
        output_dir, proxy_socket,
        models=models,
        # Must exceed the client per-call budget (GAN_LLM_TIMEOUT_S, default
        # 600s) so the proxy is never the first to cut a healthy call
        # (observed: default 120s cut thinking calls at 121s -> 502 -> the
        # question budget burned on doomed retries).
        timeout=float(cfg.get("loop.llm_proxy_timeout_s", 700)),
    )
    proxy_control_token = proxy.issue_framework_scope(0)
    safe_broker_env = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
                   "PYTHONDONTWRITEBYTECODE"} or key.startswith("LC_")
    }
    broker_config = {
        "output_dir": output_dir,
        "repo_root": repo_root,
        "domain": domain,
        "subset": subset,
        "num_samples": int(num_samples),
        "model": models["task"],
        "python": python_executable,
        "python_prefix": python_prefix,
        "timeout": 1800,
        "task_brief": (resolve_domain(reg, domain).get("task_brief") or ""),
        "parent_scored": task_execution.uses_parent_scoring(domain),
        "owner_uid": owner_uid,
        "owner_gid": owner_gid,
        "sandbox_username": sandbox_username,
        "proxy_socket": proxy_socket,
        "proxy_control_token": proxy_control_token,
        "base_env": safe_broker_env,
    }
    task_broker = _start_task_broker(task_broker_socket, broker_config)
    proxy_started = False
    try:
        _drop_driver_privileges(owner_uid, owner_gid, owner_username)
        os.makedirs(output_dir, exist_ok=True)
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

        if unregistered:
            _log_event(output_dir, {"type": "domain_unregistered", "domain": domain,
                                    "hint": "not in domains.yaml domains/families; "
                                            "task_brief/output_contract fall back to default"})

        code_root = ensure_code_root(repo_root, output_dir)
        start = (int(newest["index"]) + 1) if (resume and newest) else 1
        _driver_t0 = time.time()
        _log_event(output_dir, {"type": "driver_start", "code_root": code_root,
                                "outer_generations": G,
                                "domain": domain,
                                "domains": list(domains),
                                "resume": resume, "force": force,
                                "latest_completed_outer": (newest["index"] if newest else None),
                                "start_outer": start})

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
            if code_root and commit:
                from gan.framework import code_repo
                current = code_repo.current_commit(code_root)
                if current != commit:
                    code_repo.checkout(code_root, commit)
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

        with open(env_mask_file, "w", encoding="utf-8"):
            pass
        os.chmod(env_mask_file, 0o400)
        proxy.start()
        proxy_started = True
        for outer in range(start, G + 1):
            tokens = {
                "planner": proxy.issue_scope(
                    "planner", str(outer), models["planner"]),
                "evaluator": proxy.issue_scope(
                    "evaluator", str(outer), models["evaluator"]),
            }
            task_broker_token = task_broker.register_outer(I_max)
            cmd = [
                python_executable, "-m", "gan.outer_worker",
                "--repo_root", repo_root, "--output_dir", output_dir,
                "--outer", str(outer), "--domains", domain,
                "--subset", subset, "--num_samples", str(num_samples),
            ]
            if inner is not None:
                cmd.extend(["--inner", str(inner)])
            if resume:
                cmd += ["--resume-boundary", "outer"]
            env = _outer_env(
                os.environ, proxy_socket=proxy_socket,
                planner_token=tokens["planner"],
                evaluator_token=tokens["evaluator"],
                task_broker_socket=task_broker_socket,
                task_broker_token=task_broker_token,
                code_root=code_root,
            )
            proc = subprocess.run(
                _outer_worker_command(
                    cmd, repo_root, output_dir, code_root, proxy_socket,
                    task_broker_socket=task_broker_socket,
                    env_mask_file=env_mask_file,
                ),
                cwd=code_root, env=env,
            )
            if proc.returncode != 0:
                _log_event(output_dir, {"type": "outer_worker_failed", "outer": outer,
                                        "rc": proc.returncode})
                raise RuntimeError(f"outer worker failed at outer {outer} (rc={proc.returncode})")
            # Role self-edits are applied+committed by the worker itself.
        _log_event(output_dir, {"type": "driver_done", "code_root": code_root,
                                "duration_s": round(time.time() - _driver_t0, 1)})
        return code_root
    finally:
        if proxy_started:
            proxy.close()
        task_broker.close()
        shutil.rmtree(proxy_dir, ignore_errors=True)
