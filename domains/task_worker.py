"""Minimal TaskAgent entry point executed inside the per-question sandbox."""
from __future__ import annotations

import difflib
import importlib
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import socket
import time


_TASK_ENV_NAMES = {
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "PYTHONDONTWRITEBYTECODE", "PYTHONPATH",
    "GAN_TASK_DESIGN", "GAN_TASK_TOOLS_DIR", "GAN_TOOLS_LOAD_REPORT",
    "GAN_TASK_KNOWLEDGE_DIR", "GAN_TASK_BRIEF",
    # context-management snapshot (docs/10 §11 D2/D7) + the child's seam
    # audit sidecar (§11.5: rw bind in the sandbox; cwd fallback locally)
    "GAN_CM_CORE", "GAN_CM_POLICY", "GAN_CM_ASSEMBLY",
                      "GAN_CM_EVENTS",
    # timeout plumbing (consumer: agent/llm.py). The thinking-intensity knob
    # does NOT ride the env: it arrives inside the stdin payload.
    "GAN_QUESTION_TIMEOUT_S", "GAN_LLM_TIMEOUT_S",
    "GAN_SECURE_CAPABILITY_HOLDER",
    "GAN_SECURE_MODE", "PYTHON_DOTENV_DISABLED",
}


def _task_child_env(base_env):
    return {key: value for key, value in base_env.items()
            if key in _TASK_ENV_NAMES or key.startswith("LC_")}


class _LocalRelay:
    def __init__(self, unix_path, proxy_token, max_connections=32,
                 header_timeout_s=10.0):
        self.unix_path = unix_path
        self.proxy_token = proxy_token
        self.max_connections = int(max_connections)
        self.header_timeout_s = float(header_timeout_s)
        self.listener = None
        self.port = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._serve_thread = None
        self._bridge_threads = set()
        self._connections = set()

    def start(self):
        if self.listener is not None or self._stop.is_set():
            raise RuntimeError("LLM relay cannot be started twice")
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(self.max_connections)
        self.port = int(self.listener.getsockname()[1])
        self._serve_thread = threading.Thread(target=self._serve, daemon=True)
        self._serve_thread.start()
        return self.port

    def _serve(self):
        while not self._stop.is_set():
            listener = self.listener
            if listener is None:
                return
            try:
                client, _ = listener.accept()
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                client.close()
                continue
            thread = threading.Thread(target=self._bridge, args=(client,), daemon=True)
            with self._lock:
                self._connections.add(client)
                self._bridge_threads.add(thread)
            thread.start()

    def _bridge(self, client):
        upstream = None
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with self._lock:
                self._connections.add(upstream)
            upstream.settimeout(self.header_timeout_s)
            upstream.connect(self.unix_path)
            deadline = time.monotonic() + self.header_timeout_s
            header = b""
            while b"\r\n\r\n" not in header:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("relay request header deadline exceeded")
                client.settimeout(remaining)
                chunk = client.recv(65536)
                if not chunk or len(header) + len(chunk) > 128 * 1024:
                    raise OSError("invalid relay request header")
                header += chunk
            raw_head, pending = header.split(b"\r\n\r\n", 1)
            lines = raw_head.split(b"\r\n")
            if not lines or not lines[0]:
                raise OSError("invalid relay request line")
            filtered = []
            for line in lines[1:]:
                if not line or line[:1] in b" \t" or b":" not in line:
                    raise OSError("invalid relay request header")
                name, _value = line.split(b":", 1)
                if name.strip().lower() != b"authorization":
                    filtered.append(line)
            auth = f"Authorization: Bearer {self.proxy_token}".encode("ascii")
            upstream.sendall(b"\r\n".join([lines[0], *filtered, auth])
                             + b"\r\n\r\n" + pending)
            client.settimeout(None)
            upstream.settimeout(None)
            threads = []
            for source, target in ((client, upstream), (upstream, client)):
                thread = threading.Thread(target=self._copy, args=(source, target),
                                          daemon=True)
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()
        except (OSError, TimeoutError):
            pass
        finally:
            for connection in (client, upstream):
                if connection is None:
                    continue
                try:
                    connection.close()
                except OSError:
                    pass
                with self._lock:
                    self._connections.discard(connection)
            with self._lock:
                self._bridge_threads.discard(threading.current_thread())
            self._slots.release()

    @staticmethod
    def _copy(source, target):
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
        except OSError as exc:
            # A pump death is NEVER silent: it strands the client's in-flight
            # request (the parent proxy may still audit a clean 200), so it
            # must be visible in the harness-captured stderr.
            print(f"[relay] pump died ({type(exc).__name__}: {exc}); "
                  f"client in-flight request stranded", file=sys.stderr, flush=True)
            return

    def close(self):
        self._stop.set()
        listener, self.listener = self.listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        deadline = time.monotonic() + 5.0
        threads = ([self._serve_thread] if self._serve_thread is not None else [])
        with self._lock:
            threads.extend(self._bridge_threads)
        current = threading.current_thread()
        for thread in threads:
            if thread is current:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)


def _polyglot_git_env() -> dict:
    """Deterministic git env for the patch-base repo: no user/system config,
    no network, no gpg. The question sandbox binds /usr ro (git binary
    available) and grants CWD rw, so everything git needs is local."""
    return dict(
        os.environ,
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_TERMINAL_PROMPT="0",
        HOME=os.environ.get("HOME", "/tmp"),
    )


def _polyglot_git(cwd: str, *args: str, check: bool = False) -> "subprocess.CompletedProcess":
    proc = subprocess.run(
        ["git", "-C", cwd,
         "-c", "user.name=patch-base", "-c", "user.email=patch-base@invalid",
         "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false",
         *args],
        env=_polyglot_git_env(), capture_output=True, text=True, check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: "
                           f"{(proc.stderr or proc.stdout).strip()[:200]}")
    return proc


def _polyglot_prepare(inputs: dict) -> None:
    """Materialize the exercise workspace (Arch 2) and arm the write whitelist.

    Starter files land at CWD under their REPO-ROOT-RELATIVE paths so the
    generated diff applies with `git apply` at the exercise root inside the
    eval container. The starter state is committed into a patch-base repo at
    CWD so the prediction diff is `git diff` against the true bytes: git
    carries the per-line ``\\ No newline at end of file`` markers (skeleton
    files without trailing newlines used to GLUE diff lines under the
    difflib path) and refuses to textualize binary content (a .pyc hunk
    used to carry NUL bytes that made `git apply` reject the WHOLE patch).
    """
    files = inputs.get("files") or {}
    # Go offline pin (host toolchain bake 2026-10-29): the exercises are
    # stdlib-only (go.mod declares no deps) and the sandbox has no network --
    # make that explicit so nothing ever hangs on a toolchain/proxy fetch.
    # The build cache already lands in the sandbox-private tmpfs (HOME=/tmp),
    # outside the solution-pathspec diff; the pin only makes it deterministic
    # and fail-fast. setdefault: an explicit design/driver value wins.
    os.environ.setdefault("GOTOOLCHAIN", "local")
    os.environ.setdefault("GOPROXY", "off")
    os.environ.setdefault("GOTELEMETRY", "off")
    for rel, content in files.items():
        rel = os.path.normpath(rel).lstrip(os.sep)
        if rel.startswith(".."):
            raise ValueError(f"polyglot starter path escapes the workspace: {rel}")
        full = os.path.join(os.getcwd(), rel)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    os.environ["GAN_TASK_WRITE_ROOTS"] = json.dumps(
        inputs.get("solution_paths") or [])
    try:
        _polyglot_git(os.getcwd(), "init", "-q", check=True)
        _polyglot_git(os.getcwd(), "add", "-A", check=True)
        _polyglot_git(os.getcwd(), "commit", "-qm", "patch-base", check=True)
    except Exception as e:  # noqa: BLE001 -- the base repo is an optimization
        # its absence only costs the no-newline/binary correctness guarantees;
        # the patch falls back to the guarded difflib path below (LOUD).
        _cm_emit({"type": "patch_base_repo_failed", "reason": str(e)[:200]})
        print(f"[patch] patch-base repo unavailable ({e}); "
              f"falling back to difflib", flush=True)
        _remove_patch_base(os.getcwd())


def _remove_patch_base(cwd: str) -> None:
    shutil.rmtree(os.path.join(cwd, ".git"), ignore_errors=True)


def _polyglot_workspace_patch(inputs: dict) -> str:
    """Unified diff (a/ b/ prefixes) of the workspace vs the starter files.

    Solution-files-only by eval semantics: the eval container stashes ONLY
    the metadata solution files (`stash push`), resets --hard, and pops --
    every other change is discarded there, so anything else in the patch is
    pure noise at best (a stray package.json then fails the WHOLE patch with
    "already exists") and patch-rejecting binary hunks at worst (.pyc /
    cargo target fingerprints). The solution-file pathspec makes such noise
    structurally impossible.
    """
    files = inputs.get("files") or {}
    solution_paths = inputs.get("solution_paths") or []
    if isinstance(solution_paths, str):
        try:
            solution_paths = json.loads(solution_paths)
        except ValueError:
            solution_paths = []
    solution_paths = [os.path.normpath(str(p)).lstrip(os.sep)
                      for p in solution_paths if str(p).strip()]
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, ".git")):
        _polyglot_git(cwd, "add", "-A", "--", *solution_paths)
        proc = _polyglot_git(cwd, "diff", "--cached", "--no-color", "--",
                             *solution_paths)
        patch = proc.stdout or ""
        if "Binary files" in patch or "GIT binary patch" in patch:
            # A solution file with binary content cannot pass the tests
            # anyway; keep the patch applicable instead of feeding the eval
            # side an unappliable pseudo-diff (git diff without --binary
            # emits an unappliable "Binary files ... differ" stanza).
            patch = _drop_binary_stanzas(patch)
            _cm_emit({"type": "binary_solution_file"})
        return patch
    # Fallback ONLY when the patch-base repo is unavailable (prepare said so
    # loudly). Guarded difflib path: solution-files-only, NUL sentinel, and
    # per-line no-newline markers (the old code marked the end of the FILE
    # diff, which cannot repair a glued MID-diff line).
    _cm_emit({"type": "patch_difflib_fallback"})
    return _polyglot_workspace_patch_difflib(files, solution_paths)


def _drop_binary_stanzas(patch: str) -> str:
    """Drop whole per-file stanzas whose content is a bare 'Binary files ...
    differ' pseudo-diff (unappliable without --binary). A solution file with
    binary content fails its tests anyway; keeping the other stanzas
    applicable is strictly better than failing the whole patch."""
    stanzas: list = []
    cur: list = []
    for line in patch.split("\n"):
        if line.startswith("diff --git ") and cur:
            stanzas.append(cur)
            cur = []
        cur.append(line)
    if cur:
        stanzas.append(cur)
    out: list = []
    for st in stanzas:
        if any(l.startswith(("Binary files ", "GIT binary patch")) for l in st):
            continue
        out.extend(st)
    return "\n".join(out)


def _polyglot_workspace_patch_difflib(files: dict, solution_paths: list) -> str:
    """Fallback diff (no patch-base repo): difflib over the solution files
    only, with a NUL sentinel and PER-LINE no-newline markers."""
    solution_paths = {os.path.normpath(str(p)).lstrip(os.sep)
                      for p in (solution_paths or [])}
    current: dict = {}
    cwd = os.getcwd()
    for base, dirs, names in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (".git", ".gan_runtime")]
        for name in names:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, cwd).replace(os.sep, "/")
            if rel not in solution_paths:
                continue
            with open(full, "rb") as f:
                if b"\x00" in f.read(8192):
                    continue  # binary sentinel: never textualize
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                current[rel] = f.read()
    diffs: list = []
    for rel in sorted(set(files) | set(current)):
        if solution_paths and rel not in solution_paths:
            continue
        old = files.get(rel)
        new = current.get(rel)
        if old == new:
            continue
        if old is None:
            old_lines, old_label = [], "/dev/null"
        else:
            old_lines, old_label = old.splitlines(keepends=True), f"a/{rel}"
        if new is None:
            new_lines, new_label = [], "/dev/null"
        else:
            new_lines, new_label = new.splitlines(keepends=True), f"b/{rel}"
        patch = difflib.unified_diff(old_lines, new_lines, fromfile=old_label,
                                     tofile=new_label)
        body: list = []
        for line in patch:
            if line.startswith(("--- ", "+++ ", "@@ ")):
                body.append(line if line.endswith("\n") else line + "\n")
                continue
            if line.endswith("\n"):
                body.append(line)
            else:
                # the line ITSELF lacks a trailing newline: mark it right
                # here, where git expects the marker (the previous code
                # appended ONE marker at the end of the file diff, which
                # cannot repair a glued mid-diff line)
                body.append(line + "\n\\ No newline at end of file\n")
        diffs.append("".join(body))
    return "".join(diffs)


def _load_task_agent(agent_path: str):
    if agent_path.endswith(".py") or os.path.exists(agent_path):
        spec = importlib.util.spec_from_file_location(
            "sandboxed_task_agent", os.path.abspath(agent_path),
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load TaskAgent from {agent_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(agent_path)
    if not hasattr(module, "TaskAgent"):
        raise AttributeError(f"No TaskAgent found in {agent_path}")
    return module.TaskAgent


def _write_json_fd(fd: int, payload) -> None:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    view = memoryview(data)
    try:
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def _cm_events_path():
    """Where this child appends its seam events (docs/10 §11.5).

    Sandbox: GAN_CM_EVENTS is the rw-bound sidecar host file
    (/workspace/cm_events.jsonl <-> <run_dir>/cm_events.jsonl). Local
    (non-sandbox) runs: the env points at the sandbox view, which does
    not exist — fall back to the cwd (the run dir). Module-level so BOTH
    the seam session and the payload-forwarding degradations below can
    leave a durable trace (no-silent-fail rule)."""
    path = os.environ.get("GAN_CM_EVENTS") or ""
    if path and os.path.isdir(os.path.dirname(path) or "."):
        return path
    return os.path.join(os.getcwd(), "cm_events.jsonl")


def _cm_emit(event):
    """Audit sink for the child's seam events (cm_view/cm_violation/...):
    appended to the sidecar and folded into events.jsonl by task_runner.
    The sink must never break the session — but a failed append is not
    silent either: the line is preserved in a sibling fallback file and
    echoed to stderr (when the durable channel itself fails, these two are
    the only channels left in-process; the fallback file shares the work
    dir's lifetime, so it is best-effort by design)."""
    line = json.dumps(dict(event), ensure_ascii=False, default=str)
    try:
        with open(_cm_events_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return
    except OSError as e:
        fallback = os.path.join(os.getcwd(), "cm_events_failed.jsonl")
        landed = False
        try:
            with open(fallback, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            landed = True
        except OSError:
            pass
        print(f"[cm] sidecar append failed ({e}); event line "
              + (f"preserved at {fallback}" if landed else "follows")
              + f": {line[:300]}", flush=True)


def _degrade(reason: str, consequence: str = "; running without seam") -> None:
    """Fail-open LOUDLY: sidecar line + stderr print (the print alone is
    not durable — harness captures child stdout and the log keeps only a
    window; the sidecar line is what the audit fold sees)."""
    _cm_emit({"type": "cm_session_degraded", "reason": reason})
    print(f"[cm] {reason}{consequence}", flush=True)


def _build_cm_session():
    """Reserved cost keys (docs/10 §11 D7): build the context-management
    session for this child from the per-generation snapshot.

    ``GAN_CM_CORE`` (set by assemble_task_env / the task broker) points at
    the frozen core the parent materialized under ``.gan_runtime/cm``;
    ``GAN_CM_POLICY`` carries the AST-gated policy component (custom mode
    only). The ``cm_*`` keys come from the ALREADY-CLAMPED design snapshot —
    a type-guarded pass-through, never a second clamp authority. Any failure
    returns None: the chat runs with no seam at all (pre-D behaviour,
    fail-open) — but NEVER silently: every degradation writes a
    ``cm_session_degraded`` line into the audit sidecar (folded into
    events.jsonl by task_runner) AND keeps its stderr print. The one
    legit-abstain path (no GAN_CM_CORE anywhere) is a LOCAL/manual run, not
    a GAN generation — recorded for exactly that reason."""

    core = os.environ.get("GAN_CM_CORE") or ""
    if not os.path.isdir(core):
        alt = os.path.join(os.getcwd(), ".gan_runtime", "cm")
        if os.path.isdir(alt):
            core = alt
        else:
            _degrade(f"no CM core snapshot (GAN_CM_CORE={core!r} and no "
                     f".gan_runtime/cm under cwd); local/manual run or a "
                     f"broker env without the CM keys")
            return None
    parent = os.path.dirname(os.path.abspath(core))
    if parent not in sys.path:
        sys.path.insert(0, parent)
    try:
        import cm as cmcore  # the snapshot package (stdlib-only by contract)
    except Exception as e:  # noqa: BLE001 -- fail-open by absence
        _degrade(f"snapshot import failed ({e})")
        return None
    keys: dict = {}
    try:
        design_path = os.environ.get("GAN_TASK_DESIGN")
        if design_path and os.path.isfile(design_path):
            with open(design_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cost = cfg.get("cost") if isinstance(cfg, dict) else None
            if isinstance(cost, dict):
                keys = {k: v for k, v in cost.items()
                        if isinstance(k, str) and k.startswith("cm_")}
    except Exception as e:  # noqa: BLE001 -- fail-open, must SPEAK
        # The keys degrade to the snapshot defaults: an audible degrade with
        # a durable sidecar line (no-silent-fail rule; the print alone is
        # not durable). The seam still RUNS — only its configuration is
        # downgraded — so the consequence differs from a lost seam.
        _degrade(f"design cm keys unavailable ({e}); snapshot defaults apply",
                 consequence="; using default cm keys")
    mode = keys.get("cm_mode") if isinstance(keys.get("cm_mode"), str) else "passthrough"
    # Fixed-name delivery (cm_policy.py / cm_assembly.py in the snapshot):
    # the env pins are static (assemble_task_env and the broker pin the same
    # values), so resolution is deterministic — no sort-order scan. An env
    # value whose file is absent means THIS generation delivered nothing;
    # a design claim without delivery degrades LOUDLY (claim-vs-snapshot
    # gap), never silently.
    policy_path = None
    if mode == "custom":
        env_policy = os.environ.get("GAN_CM_POLICY") or ""
        if env_policy and os.path.isfile(env_policy):
            policy_path = env_policy
        else:
            fixed = os.path.join(core, "cm_policy.py")
            if os.path.isfile(fixed):
                # legacy/local-run recovery (no env pin): the snapshot's
                # fixed-name policy IS the only candidate — deterministic.
                policy_path = fixed
                if env_policy:
                    _degrade(f"GAN_CM_POLICY={env_policy!r} does not exist; "
                             "recovered the snapshot's cm_policy.py",
                             consequence="; seam runs on the recovered "
                                         "fixed-name policy")
            else:
                named = keys.get("cm_policy")
                _degrade("design requests cm_mode=custom but no policy is "
                         f"deliverable (env={env_policy!r}, snapshot has no "
                         f"cm_policy.py"
                         + (f", design names {named!r}" if named else "")
                         + "); running PASSTHROUGH instead")
                # The degrade CLAIMS passthrough — the keys must say it too,
                # or apply_keys would still build a custom session whose
                # every turn fails on the missing policy.
                mode = "passthrough"
                keys = dict(keys)
                keys["cm_mode"] = "passthrough"
    assembly_path = None
    env_asm = os.environ.get("GAN_CM_ASSEMBLY") or ""
    if env_asm and os.path.isfile(env_asm):
        assembly_path = env_asm
    elif keys.get("cm_assembly"):
        _degrade(f"design names cm_assembly={keys['cm_assembly']!r} but the "
                 f"snapshot has no cm_assembly.py (env={env_asm!r}); the "
                 f"baseline preset runs instead")

    try:
        return cmcore.build_session(keys, policy_path=policy_path,
                                    assembly_path=assembly_path,
                                    emit=_cm_emit)
    except Exception as e:  # noqa: BLE001 -- fail-open
        _degrade(f"session build failed ({e})")
        return None


def _run_agent_child(result_fd: int) -> None:
    if "GAN_DATASET_ROOT" in os.environ:
        raise RuntimeError("GAN_DATASET_ROOT must not enter the TaskAgent sandbox")

    payload = json.load(sys.stdin)
    TaskAgent = _load_task_agent(payload["agent_path"])
    # Reserved cost keys (docs/10 §10): the per-question budget fields ride the
    # payload (B21 chain, same transport as reasoning_effort). The TaskAgent is
    # the (agent-editable) artifact, so forward only what its __init__ accepts
    # and SAY SO when something is dropped -- a modified artifact silently
    # dropping its budget must stay visible (the parent-side cost_keys_applied
    # event + the per-question trajectories are the offline cross-check).
    budget_kwargs = {k: payload[k] for k in ("max_tool_calls", "token_budget")
                     if payload.get(k) is not None}
    try:
        params = inspect.signature(TaskAgent.__init__).parameters
        if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            dropped = [k for k in budget_kwargs if k not in params]
            if dropped:
                print(f"[cost] TaskAgent does not accept {dropped}; "
                      f"budget fields dropped", flush=True)
                # Print is not durable (PIPE-captured child stdout): the
                # sidecar line is what the audit fold sees (no-silent-fail).
                _cm_emit({"type": "cm_session_degraded",
                          "reason": f"TaskAgent does not accept {dropped}; "
                                    "budget fields dropped"})
                for k in dropped:
                    budget_kwargs.pop(k)
    except (TypeError, ValueError) as e:
        # Signature unreadable -> EVERY budget field is dropped: the largest
        # silent-budget regression this file can produce. Degrade loudly
        # (sidecar + stderr), never bare.
        _degrade(f"TaskAgent.__init__ signature unreadable ({e}); ALL payload "
                 "budget fields dropped", consequence="")
    # Context-management session (docs/10 §11 D7): forwarded like the budget
    # kwargs -- only when the artifact's __init__ accepts it; a modified
    # artifact dropping the seam degrades to the pre-D behaviour (the design
    # snapshot + the cm_view absence are the offline cross-check).
    cm_session = _build_cm_session()
    if cm_session is not None:
        try:
            params = inspect.signature(TaskAgent.__init__).parameters
            if any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in params.values()) or "cm" in params:
                budget_kwargs["cm"] = cm_session
            else:
                print("[cost] TaskAgent does not accept ['cm']; "
                      "running without the context-management seam", flush=True)
                # Durable trace: the session was built but is not forwarded,
                # so its own emit sink (the sidecar) never fires — without
                # this line the lost seam is indistinguishable from a
                # passthrough design (no-silent-fail rule).
                _cm_emit({"type": "cm_session_degraded",
                          "reason": "TaskAgent does not accept ['cm']; "
                                    "running without the context-management "
                                    "seam"})
        except (TypeError, ValueError) as e:
            _degrade(f"TaskAgent.__init__ signature unreadable ({e}); the "
                     "built cm session is NOT forwarded")
    agent = TaskAgent(
        model=payload["model"],
        chat_history_file=payload["trajectory_path"],
        # Thinking-intensity knob rides the stdin payload (explicit transport,
        # no env); agent/llm.py validates the value.
        reasoning_effort=payload.get("reasoning_effort"),
        **budget_kwargs,
    )
    inputs = payload["inputs"]
    if inputs.get("domain") == "polyglot":
        # Arch 2: rebuild the exercise workspace, let the agent write solution
        # files through the whitelisted tool, and turn the workspace diff into
        # the prediction (the eval container applies it with `patch -p1`).
        _polyglot_prepare(inputs)
        agent.forward(inputs)
        prediction = _polyglot_workspace_patch(inputs)
    else:
        prediction, _ = agent.forward(inputs)
    _write_json_fd(result_fd, {"prediction": prediction})


def _run_trusted_worker() -> None:
    if "GAN_DATASET_ROOT" in os.environ:
        raise RuntimeError("GAN_DATASET_ROOT must not enter the TaskAgent sandbox")

    payload = json.load(sys.stdin)
    parent_result_fd = int(payload.pop("result_fd"))
    relay = None

    try:
        child_env = _task_child_env(os.environ)
        proxy_socket = payload.get("proxy_socket")
        proxy_token = payload.pop("proxy_token", None)
        if proxy_socket:
            if not proxy_token:
                raise RuntimeError("Task proxy socket requires a scoped token")
            relay = _LocalRelay(proxy_socket, proxy_token)
            port = relay.start()
            child_env["OPENAI_API_BASE"] = f"http://127.0.0.1:{port}/v1"
            child_env["OPENAI_API_KEY"] = "rsi-gan-local-relay"
        with tempfile.TemporaryFile(mode="w+b") as child_result_file:
            child_result_fd = child_result_file.fileno()
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "domains.task_worker",
                    "--agent-child",
                    str(child_result_fd),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
                close_fds=True,
                pass_fds=(child_result_fd,),
            )
            child_stdout, child_stderr = child.communicate(
                json.dumps(payload, ensure_ascii=False, default=str)
            )

            if child_stdout:
                sys.stdout.write(child_stdout)
                sys.stdout.flush()
            if child_stderr:
                sys.stderr.write(child_stderr)
                sys.stderr.flush()
            if child.returncode != 0:
                raise RuntimeError(f"TaskAgent child failed (rc={child.returncode})")

            child_result_file.seek(0)
            raw_result = child_result_file.read()

        if not raw_result:
            raise RuntimeError("TaskAgent child returned no result")
        result = json.loads(raw_result.decode("utf-8"))
        if not isinstance(result, dict) or set(result) != {"prediction"}:
            raise RuntimeError("TaskAgent child returned an invalid result object")
        _write_json_fd(parent_result_fd, result)
    finally:
        if relay is not None:
            relay.close()
        try:
            os.close(parent_result_fd)
        except OSError:
            pass


def main() -> None:
    if "--agent-child" in sys.argv:
        pos = sys.argv.index("--agent-child") + 1
        if pos >= len(sys.argv):
            raise ValueError("--agent-child requires a result fd")
        _run_agent_child(int(sys.argv[pos]))
        return
    _run_trusted_worker()


if __name__ == "__main__":
    main()
