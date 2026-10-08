"""Task execution (FRAMEWORK, frozen).

Design persistence + harness glue for one task-agent generation, moved out of
``gan/task_runner.py`` so the execution/measurement path is part of the frozen
framework. Role agents must not modify how a design is persisted or how the
domain harness/report is invoked.

Responsibilities:
- persist the task design snapshot (``DesignStore``);
- assemble the task toolset and runtime env (GAN_TASK_*);
- prepare the run dir (copy repo + apply patch when a deep change was requested);
- prepare questions-only input and parent-owned ground truth;
- invoke ``domains.harness`` and score its predictions in the parent process;
- read the report and compute the objective ``report_summary``
  (contract / coverage facts — NOT an evaluator eval point).
"""
from __future__ import annotations

import importlib
import json
import math
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from gan.design.store import DesignStore
from gan.patch import apply_patch
from gan.tools.assembly import assemble_tools_dir_reported

_PY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")
# Benchmark labels / heavy assets must NOT be present in a task run (leakage
# isolation). The parent harness reads labels from an explicit CLI path that is
# never forwarded to the sandboxed task worker.
_DOMAIN_IGNORE = shutil.ignore_patterns(
    "dataset*.csv", "*bench*.csv", "polyglot-benchmark", "SWE-bench",
    "saved", "predictions", "logs", "__pycache__", "*.pyc",
)
# Allowlist of what a task run actually needs (never copy the whole repo).
_RUNTIME_FILES = ["task_agent.py"]
_RUNTIME_DIRS = ["agent", "utils"]
# Polyglot (Arch 2): the FROZEN framework layer must be importable by task
# toolset components inside the per-question bwrap sandbox (the write_file
# component delegates to gan.framework.sandbox_write). Copied whole-directory
# -- the same trust tier as agent/ and utils/ -- minus yamls: the child needs
# none, and this keeps domain score keys out of the sandbox entirely.
_SANDBOX_FRAMEWORK_IGNORE = shutil.ignore_patterns("*.yaml", "__pycache__", "*.pyc")
_HARNESS_FILES = [
    "domains/__init__.py",
    "domains/harness.py",
    "domains/report.py",
    "domains/task_worker.py",
]
_PARENT_SCORED_DOMAINS = {"paper_review", "search_arena", "imo_grading"}
_SANDBOX_USER_ENV = "GAN_TASK_SANDBOX_USER"

_TASK_ENV_NAMES = {
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "PYTHONDONTWRITEBYTECODE", "PYTHONPATH",
    "GAN_TASK_DESIGN", "GAN_TASK_TOOLS_DIR", "GAN_TOOLS_LOAD_REPORT",
    "GAN_TASK_KNOWLEDGE_DIR", "GAN_TASK_BRIEF",
}
_TASK_PARENT_ENV_NAMES = _TASK_ENV_NAMES | {
    "GAN_LLM_PROXY_UNIX", "GAN_TASK_BROKER_UNIX", "GAN_TASK_BROKER_TOKEN",
}

def _task_safe_env(base_env: Dict[str, str]) -> Dict[str, str]:
    """Keep only non-secret runtime configuration for the task child."""
    return {key: value for key, value in base_env.items()
            if key in _TASK_ENV_NAMES or key.startswith("LC_")}

def _task_parent_env(base_env: Dict[str, str]) -> Dict[str, str]:
    """Keep framework-only proxy metadata for the trusted harness parent."""
    return {key: value for key, value in base_env.items()
            if key in _TASK_PARENT_ENV_NAMES or key.startswith("LC_")}



# Keep this probe deliberately small. It runs under the exact credentials and
# interpreter used for the questions-only harness, so an inaccessible venv or
# conda prefix is reported before Python emits an opaque init_fs_encoding error.
_SANDBOX_RUNTIME_PREFLIGHT = r"""
import json
import os
import sys

expected_euid = int(sys.argv[1])
expected_egid = int(sys.argv[2])
errors = []
if os.geteuid() != expected_euid:
    errors.append("euid=%s (expected %s)" % (os.geteuid(), expected_euid))
if os.getegid() != expected_egid:
    errors.append("egid=%s (expected %s)" % (os.getegid(), expected_egid))

no_new_privs = None
cap_eff = None
try:
    with open("/proc/self/status", encoding="ascii") as status_file:
        for line in status_file:
            if line.startswith("NoNewPrivs:"):
                no_new_privs = line.split(":", 1)[1].strip()
            elif line.startswith("CapEff:"):
                cap_eff = line.split(":", 1)[1].strip()
except OSError as exc:
    errors.append("cannot read /proc/self/status: %s" % exc)
if no_new_privs != "1":
    errors.append("NoNewPrivs=%s (expected 1)" % no_new_privs)
try:
    effective_capabilities = int(cap_eff or "-1", 16)
except ValueError:
    effective_capabilities = -1
if effective_capabilities != 0:
    errors.append("CapEff=%s (expected 0)" % cap_eff)

try:
    import encodings  # noqa: F401
except Exception as exc:
    errors.append("cannot import encodings: %s" % exc)

if not os.path.isabs(sys.executable):
    errors.append("sys.executable is not absolute: %s" % sys.executable)
elif not os.path.isfile(sys.executable) or not os.access(sys.executable, os.X_OK):
    errors.append("sys.executable is not executable: %s" % sys.executable)

if not sys.prefix or not os.path.isdir(sys.prefix):
    errors.append("sys.prefix is not a directory: %s" % sys.prefix)
elif not os.access(sys.prefix, os.R_OK | os.X_OK):
    errors.append("sys.prefix is not accessible: %s" % sys.prefix)

print(json.dumps({
    "euid": os.geteuid(),
    "egid": os.getegid(),
    "no_new_privs": no_new_privs,
    "cap_eff": cap_eff,
    "executable": sys.executable,
    "prefix": sys.prefix,
    "errors": errors,
}, sort_keys=True))
raise SystemExit(1 if errors else 0)
"""


def heal_design_keys(config: Dict[str, Any], role: str,
                     code_root: Optional[str]) -> List[Dict[str, str]]:
    """Strip dynamic config keys not declared by the committed schema catalog."""
    if not isinstance(config, dict) or role not in ("task", "planner", "evaluator"):
        return []
    from gan.design.schema import allowed_keys
    known = allowed_keys(role, code_root=code_root)
    stripped = []
    for key in list(config):
        if key not in known:
            stripped.append({"name": str(key), "reason": "not declared in schema"})
            del config[key]
    return stripped



def persist_design(design_store: DesignStore, config: Dict[str, Any], genid: Any) -> str:
    return design_store.save(config or {}, "task", node_id=genid)


def heal_design_slots(config: Dict[str, Any], role: str,
                      code_root: Optional[str]) -> List[Dict[str, str]]:
    """Strip slot names the committed tree cannot deliver.

    A design can reference a component that only exists in the session workspace
    (selected before the patch landed) or was inherited from a parent whose patch
    was rejected. Assembly resolves names against the **committed** tree, so a
    name that cannot resolve there is a dangling reference: the child silently
    loses the capability while the design keeps claiming it, and the name
    propagates down the design inheritance chain (``config_dict`` -> next plan).

    This helper removes exactly those names -- **only removes**: it never adds
    names, never rewrites code and never replays patches (restoring deleted
    source is nobody's job, per the ``register_component`` contract). The caller
    must run it AFTER the task patch has been applied or rolled back, so
    ``code_root`` is then the authority for what this generation can assemble.

    Mutates ``config`` in place on purpose: ``task_runner`` holds the same dict
    object the loop stores as ``config_dict``, so one in-place heal fixes the
    persisted design file, the node meta and the parent->child chain together.
    Returns the stripped ``[{"name", "reason"}]`` (empty = untouched).

    Every role's design is healed. Role self-designs select against the
    effective (workspace-first) registry, so they accumulate dangling names
    exactly like the task design -- the loop's ``_apply_self_patch`` runs this
    heal at the SUCCESSFUL apply exit (where the committed registry is the new
    authority) and re-saves the design file when anything was stripped. The
    exhausted/rejected exit persists the session's un-healed design draft; an
    exit heal there remains backlog (docs/7 section 6.1).
    """
    if not isinstance(config, dict):
        return []
    # The single component slot is ``tools`` for every role; every role's design
    # is healed (loader resolves each role's own registry; entry_reason's
    # role-directory binding applies as usual)
    slot = "tools"
    if role not in ("task", "planner", "evaluator"):
        return []
    names = config.get(slot)
    if not isinstance(names, (list, tuple)) or not names:
        return []
    from gan.registries.loader import load_registry_for_role
    from gan.tools.assembly import always_on_index, gan_roots
    _tools, rdir, cdir = gan_roots(code_root)
    reg = load_registry_for_role(role, registry_dir=rdir, components_dir=cdir)
    # An always-on tool needs no selection (it is assembled
    # regardless), so "not registered" would read as a lost capability. Name it.
    always_on = set(always_on_index(role, code_root=code_root)) if role != "task" else set()
    kept: List[str] = []
    stripped: List[Dict[str, str]] = []
    for name in names:
        name = str(name)
        if reg.module_path(name) is not None:
            kept.append(name)
        else:
            reason = reg.reason(name) or "not resolvable"
            if reason == "not registered" and f"{name}.py" in always_on:
                reason = ("always-on tool (no selection needed -- it is assembled "
                          "for this role regardless)")
            stripped.append({"name": name, "reason": reason})
    if stripped:
        config[slot] = kept
    return stripped


def assemble_task_env(
    base_env: Dict[str, str],
    *,
    design_path: str,
    run_dir: str,
    config: Dict[str, Any],
    code_root: Optional[str] = None,
    task_brief: Optional[str] = None,
) -> Dict[str, str]:
    """Build the environment and sandbox-visible runtime assets for TaskAgent.

    The design, the assembled toolset and the curated knowledge base are copied
    below ``run_dir`` and referenced through their paths inside the task sandbox.
    Benchmark paths are explicitly removed from the inherited environment.
    """
    env = dict(base_env)
    env.pop("GAN_DATASET_ROOT", None)

    runtime_dir = os.path.join(run_dir, ".gan_runtime")
    os.makedirs(runtime_dir, exist_ok=True)
    shutil.copy2(design_path, os.path.join(runtime_dir, "design.json"))

    tools_dir = os.path.join(runtime_dir, "tools")
    # Report what the design selected vs what actually got assembled, PER
    # inner generation (the task toolset is re-assembled here, not only at
    # outer startup). The report rides the runtime dir; task_runner surfaces it
    # into node meta + the task_toolset_assembled event. Assembly gaps in the
    # task child are capability degradations to be RECORDED and passed to the
    # planner, not fatal for the generation.
    toolset_report = assemble_tools_dir_reported(
        "task", tools_dir, config=config, include_always_on=False,
        code_root=code_root)

    # Knowledge base: materialize the COMMITTED base whole -- the base
    # is the base; the planner curates it by authoring/removing md files through
    # the deep patch channel, not by toggling a config list. Text-only data,
    # per-file and total caps, skips reported like assembly gaps.
    knowledge_report = {"total": 0, "files": [], "skipped": []}
    knowledge_src = os.path.join(str(code_root or ""), "gan", "components", "task",
                                 "knowledge")
    knowledge_dir = os.path.join(runtime_dir, "knowledge")
    _KNOWLEDGE_MAX_FILE = 20_000          # chars per note (mirrors the tool's cap)
    _KNOWLEDGE_MAX_TOTAL = 200_000        # chars per generation
    if os.path.isdir(knowledge_src):
        os.makedirs(knowledge_dir, exist_ok=True)
        total = 0
        for f in sorted(os.listdir(knowledge_src)):
            if not f.endswith(".md") or f.startswith("."):
                continue
            src = os.path.join(knowledge_src, f)
            try:
                text = open(src, "r", encoding="utf-8").read(_KNOWLEDGE_MAX_FILE + 1)
            except OSError as e:
                knowledge_report["skipped"].append({"name": f, "reason": f"unreadable: {e}"})
                continue
            if len(text) > _KNOWLEDGE_MAX_FILE:
                knowledge_report["skipped"].append({"name": f,
                                                    "reason": "note exceeds per-note cap"})
                continue
            if total + len(text) > _KNOWLEDGE_MAX_TOTAL:
                knowledge_report["skipped"].append({"name": f,
                                                    "reason": "knowledge total cap reached"})
                continue
            with open(os.path.join(knowledge_dir, f), "w", encoding="utf-8") as out:
                out.write(text)
            total += len(text)
            knowledge_report["total"] = total
            knowledge_report["files"].append(f[:-3])

    with open(os.path.join(runtime_dir, "toolset_report.json"), "w",
              encoding="utf-8") as f:
        json.dump({**toolset_report, "knowledge": knowledge_report},
                  f, ensure_ascii=False, indent=2)
    env["GAN_TASK_DESIGN"] = "/workspace/.gan_runtime/design.json"
    env["GAN_TASK_TOOLS_DIR"] = "/workspace/.gan_runtime/tools"
    # The child writes the LOAD outcome of every tool file here (relative to
    # its cwd = run_dir, so it resolves identically with or without a container
    # mount). The assembly report only proves a file was copied; without this the
    # parent could not distinguish "assembled" from "actually loadable".
    env["GAN_TOOLS_LOAD_REPORT"] = ".gan_runtime/tools_load_report.json"
    env["GAN_TASK_KNOWLEDGE_DIR"] = "/workspace/.gan_runtime/knowledge"
    if task_brief:
        env["GAN_TASK_BRIEF"] = task_brief
    return env


# -- run dir ----------------------------------------------------------------
def prepare_run_dir(source_root: str, node_dir: str, patch_str: str, domain: Optional[str] = None) -> Tuple[str, bool]:
    """Return (run_dir, patch_applied).

    Builds a **minimal allowlist copy** of the code (agent runtime + the domain
    package, minus datasets) from ``source_root`` (the per-run code tree), so a
    task run cannot see benchmark labels and is far smaller than a full repo
    copy. With a per-run code tree the patch is already committed there and the
    copy is materialized from it; the copy-side apply is the no-code-tree
    fallback.
    """
    run_dir = os.path.join(node_dir, "repo")
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    os.makedirs(run_dir, exist_ok=True)

    for name in _RUNTIME_FILES + _HARNESS_FILES:
        src = os.path.join(source_root, name)
        if os.path.isfile(src):
            dst = os.path.join(run_dir, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    for name in _RUNTIME_DIRS:
        src = os.path.join(source_root, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(run_dir, name), ignore=_PY_IGNORE)
    if domain:
        dom = domain.split("_")[0] if "imo_" in domain else domain
        src = os.path.join(source_root, "domains", dom)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(run_dir, "domains", dom), ignore=_DOMAIN_IGNORE)
    if domain == "polyglot":
        gan_init = os.path.join(source_root, "gan", "__init__.py")
        fw_src = os.path.join(source_root, "gan", "framework")
        if os.path.isfile(gan_init) and os.path.isdir(fw_src):
            os.makedirs(os.path.join(run_dir, "gan"), exist_ok=True)
            shutil.copy2(gan_init, os.path.join(run_dir, "gan", "__init__.py"))
            shutil.copytree(fw_src, os.path.join(run_dir, "gan", "framework"),
                            ignore=_SANDBOX_FRAMEWORK_IGNORE)

    applied = bool(apply_patch(run_dir, patch_str)) if (patch_str or "").strip() else False
    return run_dir, applied


# -- dataset / harness / report --------------------------------------------
def prepare_polyglot_questions(
    dataset_root: str,
    run_dir: str,
    domain_cfg: Dict[str, Any],
    num_samples: int,
) -> str:
    """Polyglot (Arch 2): questions.csv straight from the metadata json.

    Each row embeds the exercise's starter files + the writable solution-path
    whitelist (see domains.polyglot.gan_utils). No label column exists: the
    objective outcome is produced by the parent-side eval container.
    """
    from domains.polyglot import gan_utils as pgu
    task_list = domain_cfg.get("eval_task_list") or None
    rows = pgu.load_instances(dataset_root, task_list=task_list,
                              num_samples=num_samples)
    if not rows:
        raise ValueError("polyglot: no instances selected (check eval_task_list)")
    input_dir = os.path.join(run_dir, "input")
    os.makedirs(input_dir, exist_ok=True)
    questions_path = os.path.join(input_dir, "questions.csv")
    pd.DataFrame(rows).to_csv(questions_path, index=False)
    return questions_path


def run_polyglot_eval(
    predictions_path: str,
    domain_cfg: Dict[str, Any],
    dataset_root: str,
    output_path: str,
) -> Dict[str, Any]:
    """Parent-side polyglot scoring (Arch 2): docker eval of the child patches.

    Each non-blank prediction is a unified diff produced by the task child;
    the eval-only container applies it to the exercise repo and runs the
    language test command. Score = resolved/total; the harness's own
    aggregation json stays next to the per-instance artifacts as evidence.
    """
    df = pd.read_csv(predictions_path, dtype=str)
    patches: Dict[str, str] = {}
    for _, row in df.iterrows():
        pred = row.get("prediction")
        if isinstance(pred, str) and pred.strip():
            patches[str(row["question_id"])] = pred
    task_list = domain_cfg.get("eval_task_list") or None
    from domains.polyglot.harness import harness as polyglot_harness
    run_id = "gan_polyglot"
    polyglot_harness(
        dataset_path=os.path.join(dataset_root, "domains", "polyglot",
                                  "polyglot_benchmark_metadata.json"),
        test_task_list=task_list, num_samples=0,
        model_name_or_path=run_id, model=None, model_patch_paths=None,
        eval_only=True, patches=patches,
        pred_dname=output_path, output_dir=output_path, root_dir=None,
    )
    with open(os.path.join(output_path, f"{run_id}_0.0.json"), "r",
              encoding="utf-8") as f:
        agg = json.load(f)
    total = len(task_list) if task_list else int(agg.get("submitted_instances") or 0)
    resolved = list(agg.get("resolved_ids") or [])
    unresolved = list(agg.get("unresolved_ids") or [])
    empty = list(agg.get("empty_patch_ids") or [])
    incomplete = list(agg.get("incomplete_ids") or [])
    evaluated = len(resolved) + len(unresolved)
    score = (len(resolved) / total) if total else None
    report = {
        "resolved_rate": score,
        "resolved": resolved,
        "unresolved": unresolved,
        "empty_patch": empty,
        "incomplete": incomplete,
        "total": total,
        "coverage": (evaluated / total) if total else None,
    }
    with open(os.path.join(output_path, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


def uses_parent_scoring(domain: str) -> bool:
    return domain in _PARENT_SCORED_DOMAINS


def prepare_questions(
    dataset_root: str,
    run_dir: str,
    domain: str,
    subset: str,
    num_samples: int,
) -> Tuple[str, Dict[str, Any]]:
    """Write questions-only input and retain ground truth in parent memory."""
    utils_prefix = domain.split("_", 1)[1] + "_" if domain.startswith("imo_") else ""
    domain_folder = domain.split("_")[0] if "imo_" in domain else domain
    utils_module = importlib.import_module(
        f"domains.{domain_folder}.{utils_prefix}utils"
    )
    question_id_col = utils_module.QUESTION_ID
    ground_truth_key = utils_module.GROUND_TRUTH_KEY

    if "imo_" in domain:
        rel = f"domains/imo/{domain.split('_')[-1]}bench{subset}.csv"
    else:
        rel = f"domains/{domain}/dataset{subset}.csv"
    dataset = pd.read_csv(os.path.join(os.path.abspath(dataset_root), rel), dtype=str)
    if num_samples > 0:
        dataset = dataset[:num_samples]

    ground_truth_by_id = dict(
        zip(dataset[question_id_col].tolist(), dataset[ground_truth_key].tolist())
    )
    questions = dataset.drop(columns=[ground_truth_key])
    input_dir = os.path.join(run_dir, "input")
    os.makedirs(input_dir, exist_ok=True)
    questions_path = os.path.join(input_dir, "questions.csv")
    questions.to_csv(questions_path, index=False)
    return questions_path, ground_truth_by_id


def _sandbox_identity(run_dir: str, run_id: str,
                      expected_owner_uid: Optional[int] = None) -> Tuple[str, int, int]:
    """Prepare a dedicated host uid and return (setpriv, uid, gid)."""
    if os.name != "posix" or not hasattr(os, "chown"):
        raise RuntimeError("the sandbox identity path requires a POSIX host with setpriv and chown")

    username = os.environ.get(_SANDBOX_USER_ENV, "").strip()
    if not username:
        raise RuntimeError(
            f"{_SANDBOX_USER_ENV} must name the non-root TaskAgent system user"
        )
    try:
        import pwd
        account = pwd.getpwnam(username)
    except (ImportError, KeyError) as exc:
        raise RuntimeError(f"sandbox user {username!r} does not exist") from exc

    uid, gid = int(account.pw_uid), int(account.pw_gid)
    if uid == 0:
        raise RuntimeError("refusing to run TaskAgent as uid 0")
    current_uid = int(os.geteuid())
    if current_uid not in (0, uid):
        raise RuntimeError(
            "chown of the run copy requires root or the configured sandbox uid"
        )
    setpriv = shutil.which("setpriv")
    if not setpriv:
        raise RuntimeError("G6b requires the util-linux setpriv executable")
    if expected_owner_uid is not None:
        run_stat = os.lstat(run_dir)
        if run_stat.st_uid != int(expected_owner_uid) or os.path.islink(run_dir):
            raise RuntimeError("G6b task run directory has an unexpected owner or type")

    output_dir = os.path.join(run_dir, "outputs", run_id)
    os.makedirs(output_dir, exist_ok=True)
    for root, dirs, files in os.walk(run_dir, followlinks=False):
        os.chown(root, uid, gid, follow_symlinks=False)
        for name in dirs + files:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                os.chown(path, uid, gid, follow_symlinks=False)
    return setpriv, uid, gid


def _python_prefix_hint(python_executable: str) -> str:
    try:
        executable = os.path.abspath(python_executable)
        bin_dir = os.path.dirname(executable)
        if os.path.basename(bin_dir) == "bin":
            return os.path.dirname(bin_dir)
    except OSError:
        pass
    return str(getattr(sys, "prefix", "<unknown>"))


def _sandbox_runtime_preflight(
    setpriv: str,
    uid: int,
    gid: int,
    username: str,
    python_executable: str,
    run_dir: str,
    env: Dict[str, str],
    timeout: int,
) -> None:
    """Fail early if the sandbox uid cannot start the selected Python."""
    prefix_hint = _python_prefix_hint(python_executable)
    probe_cmd = [
        setpriv,
        "--reuid", str(uid),
        "--regid", str(gid),
        "--clear-groups",
        "--no-new-privs",
        "--",
        python_executable,
        "-c", _SANDBOX_RUNTIME_PREFLIGHT,
        str(uid),
        str(gid),
    ]
    descriptor = (
        f"sandbox username={username!r}, uid={uid}, gid={gid}, "
        f"Python executable={python_executable!r}, Python prefix={prefix_hint!r}"
    )
    try:
        probe = subprocess.run(
            probe_cmd,
            cwd=run_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(1, min(int(timeout), 30)),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"G6b sandbox runtime preflight failed ({descriptor}): timed out"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"G6b sandbox runtime preflight failed ({descriptor}): "
            f"could not launch probe: {exc}"
        ) from exc

    stdout = (probe.stdout or "").strip()
    stderr = (probe.stderr or "").strip()
    payload: Dict[str, Any] = {}
    for line in reversed(stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break

    prefix = str(payload.get("prefix") or prefix_hint)
    probe_errors = payload.get("errors")
    if isinstance(probe_errors, list):
        reason = "; ".join(str(item) for item in probe_errors if str(item))
    else:
        reason = ""
    if probe.returncode != 0:
        reason = reason or stderr or stdout or f"probe exited with rc={probe.returncode}"
    elif not payload:
        reason = stderr or stdout or "probe returned no diagnostic payload"
    if probe.returncode != 0 or not payload or reason:
        raise RuntimeError(
            f"G6b sandbox runtime preflight failed ("
            f"sandbox username={username!r}, uid={uid}, gid={gid}, "
            f"Python executable={python_executable!r}, Python prefix={prefix!r}"
            f"): {reason[-2000:]}"
        )


def run_harness_and_report(
    python: str,
    run_dir: str,
    domain: str,
    run_id: str,
    subset: str,
    num_samples: int,
    model: str,
    env: Dict[str, str],
    timeout: int,
    questions_path: Optional[str] = None,
    dataset_root: Optional[str] = None,
    scope_id: Optional[str] = None,
    log_path: Optional[str] = None,
) -> Tuple[int, str]:
    """Run the frozen harness; parent-side reporting happens separately."""
    broker_socket = env.get("GAN_TASK_BROKER_UNIX")
    if broker_socket:
        broker_token = env.get("GAN_TASK_BROKER_TOKEN")
        if not broker_token or not scope_id:
            raise RuntimeError("task broker requires a launch capability and generation id")
        from gan.framework.task_broker import request_task_run
        return request_task_run(
            broker_socket, broker_token, str(scope_id), int(timeout),
        )
    return _run_harness_and_report_local(
        python, run_dir, domain, run_id, subset, num_samples, model, env, timeout,
        questions_path=questions_path, dataset_root=dataset_root,
        scope_id=scope_id, log_path=log_path,
    )


def _run_harness_and_report_local(
    python: str,
    run_dir: str,
    domain: str,
    run_id: str,
    subset: str,
    num_samples: int,
    model: str,
    env: Dict[str, str],
    timeout: int,
    questions_path: Optional[str] = None,
    dataset_root: Optional[str] = None,
    scope_id: Optional[str] = None,
    log_path: Optional[str] = None,
    proxy_socket: Optional[str] = None,
    proxy_token: Optional[str] = None,
    expected_owner_uid: Optional[int] = None,
) -> Tuple[int, str]:
    """Run a fixed harness command; the privileged broker is its only secure caller."""
    harness_cmd = [
        python, "-m", "domains.harness",
        "--domain", domain,
        "--model", model,
        "--run_id", run_id,
        "--subset", subset,
        "--num_samples", str(num_samples),
    ]
    proxy_socket = proxy_socket or env.get("GAN_LLM_PROXY_UNIX")
    if proxy_socket and proxy_token is None:
        if not scope_id:
            raise RuntimeError("LLM proxy task scope requires a generation id")
        control_token = env.get("GAN_PROXY_CONTROL_TOKEN")
        if not control_token:
            raise RuntimeError("LLM proxy task scope requires framework control capability")
        from gan.framework.llm_proxy import request_task_token
        proxy_token = request_task_token(
            proxy_socket, control_token, str(scope_id), model)
    if questions_path:
        setpriv, uid, gid = _sandbox_identity(
            run_dir, run_id, expected_owner_uid=expected_owner_uid,
        )
        python_executable = python if os.path.isabs(python) else shutil.which(python)
        if not python_executable:
            raise RuntimeError(f"cannot resolve Python executable: {python}")
        harness_cmd = [
            setpriv,
            "--reuid", str(uid),
            "--regid", str(gid),
            "--clear-groups",
            "--no-new-privs",
            "--",
            python_executable,
            "-m", "domains.harness",
            "--domain", domain,
            "--model", model,
            "--run_id", run_id,
            "--subset", subset,
            "--num_samples", str(num_samples),
        ]
        harness_cmd.extend(["--questions_path", os.path.abspath(questions_path)])
    elif dataset_root:
        # Compatibility path for domains with their own evaluator/harness.
        harness_cmd.extend(["--dataset_root", os.path.abspath(dataset_root)])
    if proxy_token:
        harness_cmd.extend([
            "--proxy_socket", os.path.abspath(proxy_socket),
            "--proxy_token", proxy_token,
        ])
    child_env = _task_safe_env(env)
    if questions_path:
        username = os.environ.get(_SANDBOX_USER_ENV, "").strip()
        _sandbox_runtime_preflight(
            setpriv, uid, gid, username, python_executable,
            run_dir, child_env, timeout,
        )
    proc = subprocess.run(
        harness_cmd, cwd=run_dir, env=child_env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
    )
    out = proc.stdout or ""
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n$ {' '.join(harness_cmd)} (cwd={run_dir})\n{out[-4000:]}\n")
    return proc.returncode, out


def write_parent_report(
    predictions_path: str,
    report_path: str,
    domain: str,
    ground_truth_by_id: Dict[str, Any],
) -> Dict[str, Any]:
    """Score prediction-only output and persist the existing report schema."""
    from domains.report import compute_report_from_predictions

    predictions = pd.read_csv(predictions_path, dtype=str)
    report = compute_report_from_predictions(
        predictions, domain, ground_truth_by_id,
    )
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=4)
    return report


def run_existing_domain_report(domain: str, dname: str, model: str) -> Optional[Dict[str, Any]]:
    """Call the existing independent-domain report entry point in this parent."""
    if domain == "imo_proof":
        from domains.report import report_imo_proof
        report_imo_proof(dname=dname, model=model)
    elif "balrog" in domain:
        from domains.balrog.eval import report_balrog
        report_balrog(output_dir=dname)
    elif "genesis" in domain:
        from domains.genesis.eval import report_genesis
        report_genesis(output_dir=dname)
    return read_report(os.path.join(dname, "report.json"))


def read_report(report_path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(report_path):
        return None
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def extract_score(report: Optional[Dict[str, Any]], score_key: str) -> Optional[float]:
    """Numeric objective score, or None when absent/NaN (score standard unchanged)."""
    if not isinstance(report, dict) or not score_key:
        return None
    val = report.get(score_key)
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


# -- objective contract / coverage facts ------------------------------------
def _normalize(value: Any, contract: Dict[str, Any]) -> str:
    if value is None:
        return ""
    s = str(value)
    if contract.get("strip", True):
        s = s.strip()
    if contract.get("case_insensitive", True):
        s = s.lower()
    return s


def compute_report_summary(
    report: Optional[Dict[str, Any]],
    requested: int,
    contract: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Framework-computed objective facts injected into run_summary.

    This is measurement, not an evaluator eval point.
    """
    contract = contract or {}
    requested = int(requested) if (requested and requested > 0) else None
    summary: Dict[str, Any] = {
        "sample_n_requested": requested,
        "sample_n_valid": None,
        "coverage": None,
        "invalid_rate": None,
        "contract_ok": None,
        "label_distribution": None,
        "overall_accuracy": None,
        "random_guess_accuracy": None,
    }
    if not isinstance(report, dict):
        return summary

    summary["overall_accuracy"] = report.get("overall_accuracy")
    summary["random_guess_accuracy"] = report.get("random_guess_accuracy")
    summary["label_distribution"] = report.get("label_distribution")

    n_valid = report.get("total")
    if isinstance(n_valid, (int, float)):
        n_valid = int(n_valid)
        summary["sample_n_valid"] = n_valid
        if requested:
            coverage = n_valid / requested
            summary["coverage"] = coverage
            summary["invalid_rate"] = 1.0 - coverage
    else:
        n_valid = None

    if contract.get("kind") == "label":
        allowed = {_normalize(a, contract) for a in (contract.get("allowed_labels") or [])}
        dist = (summary.get("label_distribution") or {}).get("prediction") or {}
        if dist:
            total_pred = sum(dist.values()) or 1
            ok = sum(v for k, v in dist.items() if _normalize(k, contract) in allowed)
            summary["contract_ok"] = bool(ok == total_pred)
        else:
            summary["contract_ok"] = None  # nothing to judge
    return summary
