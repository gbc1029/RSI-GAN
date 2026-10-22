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

import hashlib
import importlib
import importlib.util
import json
import math
import os
import random
from pathlib import PurePosixPath
import shutil
import stat
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from gan.design.store import DesignStore
from gan.framework.sandbox_etc import SANDBOX_HOSTNAME, validate_sandbox_etc
from gan.patch import apply_patch
from gan.tools.assembly import assemble_tools_dir_reported

_PY_IGNORE = shutil.ignore_patterns(".env", "__pycache__", "*.pyc")
# Benchmark labels / heavy assets must NOT be present in a task run (leakage
# isolation). The parent harness reads labels from an explicit CLI path that is
# never forwarded to the sandboxed task worker.
_DOMAIN_IGNORE = shutil.ignore_patterns(
    "dataset*.csv", "*bench*.csv", "polyglot-benchmark", "SWE-bench",
    "saved", "predictions", "logs", ".env", "__pycache__", "*.pyc",
)
# Allowlist of what a task run actually needs (never copy the whole repo).
_RUNTIME_FILES = ["task_agent.py"]
_RUNTIME_DIRS = ["agent", "utils"]
# NOTE: no gan/ copy. The one framework-dependent component (write_file)
# imports its frozen write primitive from the BASE layer
# (``agent.sandbox_write``), and ``agent``/``utils`` are copied for every
# domain -- so the sandbox needs no per-domain framework condition and no
# framework bytes beyond the base runtime it already gets.
_HARNESS_FILES = [
    "domains/__init__.py",
    "domains/harness.py",
    "domains/report.py",
    "domains/task_worker.py",
]
_PARENT_SCORED_DOMAINS = {
    "paper_review", "search_arena", "imo_grading", "polyglot",
}
_SANDBOXED_BALROG_DOMAINS = {"balrog_babyai", "balrog_babaisai"}
_SANDBOXED_HARNESS_DOMAINS = _SANDBOXED_BALROG_DOMAINS | {
    "imo_proof", "imo_proof_grading",
}
_SANDBOX_USER_ENV = "GAN_TASK_SANDBOX_USER"
_SANDBOX_PROFILE_KEYS = {
    "enabled", "disabled_reason", "network", "gpu", "read_only_assets",
}

_TASK_ENV_NAMES = {
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "PYTHONDONTWRITEBYTECODE", "PYTHONPATH",
    "GAN_TASK_DESIGN", "GAN_TASK_TOOLS_DIR", "GAN_TOOLS_LOAD_REPORT",
    "GAN_TASK_KNOWLEDGE_DIR", "GAN_TASK_BRIEF",
    # context-management snapshot (docs/10 §11 D2): the frozen seam core and
    # the selected policy component live under .gan_runtime/cm (no gan/ bind);
    # GAN_CM_EVENTS is the child's seam-audit sidecar (rw bind, §11.5)
    "GAN_CM_CORE", "GAN_CM_POLICY", "GAN_CM_ASSEMBLY",
                      "GAN_CM_EVENTS",
    "GAN_SECURE_MODE", "PYTHON_DOTENV_DISABLED",
    # timeout plumbing (consumers: domains/harness.py QUESTION_TIMEOUT,
    # agent/llm.py per-call client budget)
    "GAN_QUESTION_TIMEOUT_S", "GAN_LLM_TIMEOUT_S",
    # litellm offline cost map (no github egress; set by run_gan.py)
    "LITELLM_LOCAL_MODEL_COST_MAP",
}
_TASK_PARENT_ENV_NAMES = _TASK_ENV_NAMES | {
    "GAN_LLM_PROXY_UNIX", "GAN_TASK_BROKER_UNIX", "GAN_TASK_BROKER_OUTER",
}

def _task_safe_env(base_env: Dict[str, str]) -> Dict[str, str]:
    """Keep only non-secret runtime configuration for the task child."""
    return {key: value for key, value in base_env.items()
            if key in _TASK_ENV_NAMES or key.startswith("LC_")}

def _task_parent_env(base_env: Dict[str, str]) -> Dict[str, str]:
    """Keep framework-only proxy metadata for the trusted harness parent."""
    return {key: value for key, value in base_env.items()
            if key in _TASK_PARENT_ENV_NAMES or key.startswith("LC_")}


def validate_domain_sandbox_profile(
    domain: str,
    profile: Any,
    repo_root: str,
) -> Dict[str, Any]:
    """Validate and canonicalize the trusted per-domain harness policy."""
    if not isinstance(profile, dict):
        raise RuntimeError(f"domain {domain!r} has no sandbox profile")
    unknown = set(profile) - _SANDBOX_PROFILE_KEYS
    missing = _SANDBOX_PROFILE_KEYS - set(profile)
    if unknown or missing:
        raise RuntimeError(
            f"domain {domain!r} has invalid sandbox profile fields "
            f"(unknown={sorted(unknown)}, missing={sorted(missing)})"
        )
    if not isinstance(profile["enabled"], bool):
        raise RuntimeError(f"domain {domain!r} sandbox enabled must be boolean")
    reason = profile["disabled_reason"]
    if reason is not None and not isinstance(reason, str):
        raise RuntimeError(f"domain {domain!r} sandbox disabled_reason must be text or null")
    if profile["network"] != "none":
        raise RuntimeError(f"domain {domain!r} sandbox network must be 'none'")
    if profile["gpu"] not in {"none", "wsl_cuda"}:
        raise RuntimeError(f"domain {domain!r} sandbox gpu mode is unsupported")
    assets = profile["read_only_assets"]
    if not isinstance(assets, list) or any(not isinstance(item, str) for item in assets):
        raise RuntimeError(f"domain {domain!r} sandbox read_only_assets must be a string list")

    root = os.path.realpath(os.path.abspath(repo_root))
    resolved_assets: List[str] = []
    for relative in assets:
        posix_path = PurePosixPath(relative)
        if (not relative or "\\" in relative or posix_path.is_absolute() or
                any(part in {"", ".", ".."} for part in posix_path.parts)):
            raise RuntimeError(
                f"domain {domain!r} has unsafe sandbox asset path: {relative!r}"
            )
        candidate = root
        for part in posix_path.parts:
            candidate = os.path.join(candidate, part)
            if os.path.islink(candidate):
                raise RuntimeError(
                    f"domain {domain!r} sandbox asset contains a symlink: {relative!r}"
                )
        resolved = os.path.realpath(candidate)
        try:
            contained = os.path.commonpath([root, resolved]) == root
        except ValueError:
            contained = False
        if not contained or resolved == root or not os.path.exists(resolved):
            raise RuntimeError(
                f"domain {domain!r} sandbox asset is missing or escapes repo_root: "
                f"{relative!r}"
            )
        if resolved in resolved_assets:
            raise RuntimeError(f"domain {domain!r} sandbox asset is duplicated: {relative!r}")
        resolved_assets.append(resolved)

    if not profile["enabled"]:
        detail = (reason or "no reason supplied").strip()
        raise RuntimeError(f"domain {domain!r} sandbox profile is disabled: {detail}")
    if profile["gpu"] != "none":
        raise RuntimeError(
            f"domain {domain!r} requests {profile['gpu']!r}, but GPU sandbox exposure "
            "is not configured or verified"
        )
    return {
        "network": "none",
        "gpu": "none",
        "read_only_assets": resolved_assets,
    }



# Keep this probe deliberately small. It runs under the exact credentials and
# interpreter used for the sandboxed harness, so an inaccessible venv or conda
# prefix is reported before Python emits an opaque init_fs_encoding error.
_SANDBOX_RUNTIME_PREFLIGHT = r"""
import errno
import json
import os
import sys

expected_euid = int(sys.argv[1])
expected_egid = int(sys.argv[2])
run_dir = sys.argv[3]
output_dir = sys.argv[4]
run_read_only = sys.argv[5] == "1"
errors = []
if os.geteuid() != expected_euid:
    errors.append("euid=%s (expected %s)" % (os.geteuid(), expected_euid))
if os.getegid() != expected_egid:
    errors.append("egid=%s (expected %s)" % (os.getegid(), expected_egid))

no_new_privs = None
capability_names = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
capabilities = {name: None for name in capability_names}
try:
    with open("/proc/self/status", encoding="ascii") as status_file:
        for line in status_file:
            if line.startswith("NoNewPrivs:"):
                no_new_privs = line.split(":", 1)[1].strip()
            else:
                name = line.split(":", 1)[0]
                if name in capabilities:
                    capabilities[name] = line.split(":", 1)[1].strip()
except OSError as exc:
    errors.append("cannot read /proc/self/status: %s" % exc)
if no_new_privs != "1":
    errors.append("NoNewPrivs=%s (expected 1)" % no_new_privs)
for name, value in capabilities.items():
    try:
        capability_value = int(value or "-1", 16)
    except ValueError:
        capability_value = -1
    if capability_value != 0:
        errors.append("%s=%s (expected 0)" % (name, value))

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

if not os.path.isdir(run_dir) or not os.access(run_dir, os.R_OK | os.X_OK):
    errors.append("run directory is not readable/traversable: %s" % run_dir)
elif run_read_only:
    read_only_probe = os.path.join(run_dir, ".gan_ro_probe_%s" % os.getpid())
    try:
        probe_fd = os.open(read_only_probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
            errors.append("unexpected run-directory write failure: %s" % exc)
    else:
        os.close(probe_fd)
        os.unlink(read_only_probe)
        errors.append("run directory is writable inside the harness sandbox")
if not os.path.isdir(output_dir) or not os.access(output_dir, os.R_OK | os.W_OK | os.X_OK):
    errors.append("output directory is not readable/writable/traversable: %s" % output_dir)
else:
    probe_path = os.path.join(output_dir, ".gan_acl_probe_%s" % os.getpid())
    try:
        probe_fd = os.open(probe_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(probe_fd)
        os.unlink(probe_path)
    except OSError as exc:
        errors.append("cannot create output ACL probe: %s" % exc)

print(json.dumps({
    "euid": os.geteuid(),
    "egid": os.getegid(),
    "no_new_privs": no_new_privs,
    "cap_eff": capabilities["CapEff"],
    "capabilities": capabilities,
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
    from gan.framework import cost_keys as _ck
    known = allowed_keys(role, code_root=code_root)
    stripped = []
    for key in list(config):
        if _ck.is_reserved_slot(key):
            # Belt and braces (docs/10 §10.3 判据 2): the reserved cost slot
            # survives the heal EVEN IF the schema declaration is ever missing
            # — the authority is the frozen cost_keys table, not this catalog.
            # A non-object slot is still removed (nothing consumable in it).
            if not isinstance(config[key], dict):
                stripped.append({"name": str(key), "reason": "cost slot must be an object"})
                del config[key]
            continue
        if key not in known:
            stripped.append({"name": str(key), "reason": "not declared in schema"})
            del config[key]
    return stripped


# -- design persistence -----------------------------------------------------
def persist_design(design_store: DesignStore, config: Dict[str, Any], genid: Any) -> str:
    return design_store.save(config or {}, "task", node_id=genid)


# --- context-management snapshot (docs/10 §11, D2/D13) ----------------------

# The frozen seam core copied verbatim into the sandbox. Copies only exist at
# materialization time (per-generation snapshot, knowledge-base mechanism):
# the frozen semantics hold by COPY TIMING + the snapshot event's sha, and the
# source paths sit in the trust anchor (gan/framework/*) so no agent patch can
# rewrite what the next snapshot would copy.
_CM_CORE_FILES = ("__init__.py", "blocks.py", "ops.py", "session.py",
                  "assembly.py", "state.py", "failopen.py", "traj_index.py",
                  "faces.py")
# Agent-owned CM modules are delivered under FIXED names (cm_policy.py /
# cm_assembly.py) so the child env pins are static — the task broker builds
# the child env itself and cannot learn per-generation filenames. A design
# name that collides with a core stem would be OVERWRITING the frozen core
# in the snapshot dir, and ``ctx_`` is the tool identity contract: both are
# refused, never silently rewritten.
_CM_RESERVED_STEMS = ({os.path.splitext(f)[0] for f in _CM_CORE_FILES}
                      | {"cm_policy", "cm_assembly"})


def materialize_cm_snapshot(run_dir: str, config: Dict[str, Any],
                            code_root: Optional[str] = None
                            ) -> Dict[str, Any]:
    """Copy the frozen CM core (+ the agent-owned CM modules the task design
    NAMES) into ``<run_dir>/.gan_runtime/cm/`` and return the snapshot report.

    Pool-A delivery for the task child: the run dir has no ``gan/``; the
    snapshot imports as top-level ``cm`` and depends on stdlib only. The
    agent modules are NAMED SLOTS resolved against the CODE ROOT the
    child's patch was (or was not) applied to — the committed bytes the
    generation runs with (the run-dir copy itself has no gan/; there is no
    workspace dual provenance for the child):

    - ``cost.cm_assembly`` (non-passthrough modes): the agent-owned
      assembly, delivered as ``cm_assembly.py``; it REPLACES the cm_mode
      preset composition, failing open to it;
    - ``cost.cm_policy`` (custom mode): the agent-owned policy, delivered
      as ``cm_policy.py``. Selection is BY NAME only; the sort-first scan
      survives as the loudly-annotated legacy path for unnamed designs
      (``policy_selection: "legacy_sort_first"``) — under sort order, adding
      a second candidate file silently changes which one runs.

    Every refusal lands in ``skipped`` with a reason — a named module that
    cannot be delivered is a LOUD delivery gap, never a silent fallback."""
    report: Dict[str, Any] = {"core_files": [], "policy": None, "skipped": [],
                              "assembly": None, "policy_selection": None}
    core_src = os.path.join(os.path.dirname(__file__), "cm")
    dst_root = os.path.join(run_dir, ".gan_runtime", "cm")
    os.makedirs(dst_root, exist_ok=True)
    import hashlib
    for name in _CM_CORE_FILES:
        src = os.path.join(core_src, name)
        if not os.path.isfile(src):
            report["skipped"].append({"file": name, "reason": "missing in framework"})
            continue
        shutil.copy2(src, os.path.join(dst_root, name))
        with open(src, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()[:12]
        report["core_files"].append({"file": name, "sha12": digest})

    cost = (config.get("cost") or {}) if isinstance(config, dict) else {}
    mode = cost.get("cm_mode")
    if mode not in ("slice", "compact_conservative", "custom"):
        # passthrough is the identity posture: no agent CM code is executed,
        # so none is delivered — but a NAMED assembly under passthrough is a
        # design claim that must stay visible (the session would ignore it).
        asm = cost.get("cm_assembly")
        if isinstance(asm, str) and asm.strip():
            report["skipped"].append({
                "file": asm.strip(),
                "reason": "passthrough is the identity posture; the named "
                          "assembly is not delivered and would be ignored"})
        return report

    from gan.framework.cm.failopen import policy_violations

    def _deliver(stem: str, kind: str) -> Optional[str]:
        """Gate + copy ONE named agent CM module under its fixed delivery
        name. Returns the delivery filename, or None with a skipped entry."""
        if stem in _CM_RESERVED_STEMS or stem.startswith("ctx_"):
            report["skipped"].append({
                "file": stem,
                "reason": (f"named {kind} refused: reserved name (core stem "
                           "or ctx_ tool identity)")})
            return None
        src = os.path.join(code_root, "gan", "components", "task", "cm",
                           stem + ".py") if code_root else None
        if not src or not os.path.isfile(src):
            report["skipped"].append({
                "file": stem,
                "reason": (f"named {kind} module missing in "
                           "gan/components/task/cm/ (no code root)"
                           if not code_root else
                           f"named {kind} module missing in "
                           "gan/components/task/cm/")})
            return None
        problems = policy_violations(src)
        if problems:
            report["skipped"].append({
                "file": stem,
                "reason": f"{kind} refused by AST gate: " + "; ".join(problems)[:160]})
            return None
        delivery = "cm_assembly.py" if kind == "assembly" else "cm_policy.py"
        shutil.copy2(src, os.path.join(dst_root, delivery))
        return delivery

    asm = cost.get("cm_assembly")
    if isinstance(asm, str) and asm.strip():
        if _deliver(asm.strip(), "assembly"):
            report["assembly"] = asm.strip()

    if mode == "custom":
        named = cost.get("cm_policy")
        if isinstance(named, str) and named.strip():
            if _deliver(named.strip(), "policy"):
                report["policy"] = named.strip()
                report["policy_selection"] = "named"
        else:
            # legacy path (unnamed design): the sort-first scan, loudly
            # annotated — under sort order adding a second candidate file
            # changes which one runs; the named slot prevents exactly that.
            policy_name = None
            if code_root:
                policy_dir = os.path.join(code_root, "gan", "components",
                                          "task", "cm")
                if os.path.isdir(policy_dir):
                    for fn in sorted(os.listdir(policy_dir)):
                        stem = os.path.splitext(fn)[0]
                        if fn.endswith(".py") and not fn.startswith("_") \
                                and fn != "__init__.py" \
                                and stem not in _CM_RESERVED_STEMS \
                                and not stem.startswith("ctx_"):
                            policy_name = fn
                            break
            if not policy_name:
                report["skipped"].append({"file": "policy",
                                          "reason": "custom mode has no policy component"})
                return report
            src = os.path.join(code_root, "gan", "components", "task", "cm",
                               policy_name)
            problems = policy_violations(src)
            if problems:
                report["skipped"].append({"file": policy_name,
                                          "reason": "policy refused by AST gate: "
                                                    + "; ".join(problems)[:160]})
                return report
            shutil.copy2(src, os.path.join(dst_root, "cm_policy.py"))
            report["policy"] = policy_name
            report["policy_selection"] = "legacy_sort_first"
    return report


def _cm_core_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cm")


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
    heal at EVERY self-improve exit (successful commit, exhausted/rejected,
    empty patch) and re-saves the design file when anything was stripped (see
    ``loop._heal_self_design``). This is the FORWARD direction; the reverse
    direction -- a name the session dropped for a removal that never landed --
    is ``heal_design_removals`` below.
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


# Deep ops whose INTENT is "this component should leave the system". A session
# that scheduled one of these AND dropped the name from the component slot has
# said "X is no longer used"; if the removal never landed (the patch was
# exhausted/rejected wholesale, so the committed registry still has X), the
# aligned design is the one that DECLARES X -- anything else is the
# reverse-dangling state (component real, design silent) that no forward heal
# can see and that the audit trail contradicts (records claim a removal).
_REMOVAL_INTENT_OPS = ("unregister_component",)


def heal_design_removals(config: Dict[str, Any], role: str,
                         code_root: Optional[str],
                         records: Optional[List[Dict[str, Any]]] = None,
                         pre_config: Optional[Dict[str, Any]] = None
                         ) -> List[Dict[str, str]]:
    """Put BACK the component selections this session dropped for a removal
    that never landed (the reverse-dangling alignment).

    ``heal_design_slots``/``heal_design_keys`` only strip forward: names the
    committed tree cannot deliver. This one is the mirror image and is the
    reason an exhausted exit cannot simply reuse them -- "component exists but
    the design no longer declares it" is not a dangling *reference*, so no strip
    rule can see it.

    Legality only, never an effect review (so a *deliberate* shallow edit is
    never touched). A name is restored iff ALL four hold:

    1. the session recorded a REMOVAL intent for it (``_REMOVAL_INTENT_OPS``),
    2. the session itself dropped it from the slot -- exact when ``pre_config``
       (the pre-session snapshot) declares the name; without the snapshot, a
       this-session ``deselect_component`` record is the only admissible
       evidence (absence alone is never a licence to add a name back),
    3. the committed registry STILL resolves it (the removal did not land --
       if it did land, the drop is the truth and nothing happens), and
    4. it is not already in the slot.

    Convergence: every condition is stated over the SAME predicate the strip
    direction uses ("does the committed tree resolve this name"), so a pass
    leaves ``slot subset-of resolvable`` and may only re-add a name it already
    resolved -- running this (or the whole heal) twice is a no-op, and no pass
    can manufacture a new dangling reference.

    ``records``/``pre_config`` are read-only inputs; the caller passes the
    session's structured records (never free text) and the design as it was
    BEFORE the session ran. Mutates ``config[slot]`` in place when it restores
    something (appending in pre-session order, so repeated heals are stable).
    Returns ``[{"name", "reason"}]`` (empty = untouched).
    """
    if not isinstance(config, dict) or role not in ("task", "planner", "evaluator"):
        return []
    intent: List[str] = []
    dropped_now: set = set()
    for r in records or []:
        if not isinstance(r, dict):
            continue
        op = str(r.get("op") or "")
        name = r.get("name")
        if not name:
            continue
        if op in _REMOVAL_INTENT_OPS:
            intent.append(str(name))
        elif op == "deselect_component":
            dropped_now.add(str(name))
    if not intent:
        return []
    slot = "tools"
    cur = config.get(slot)
    cur = [str(n) for n in cur] if isinstance(cur, (list, tuple)) else []
    cur_set = set(cur)
    pre_names: Optional[List[str]] = None
    if isinstance(pre_config, dict):
        pv = pre_config.get(slot)
        pre_names = [str(n) for n in pv] if isinstance(pv, (list, tuple)) else []
    from gan.registries.loader import load_registry_for_role
    from gan.tools.assembly import gan_roots
    _tools, rdir, cdir = gan_roots(code_root)
    reg = load_registry_for_role(role, registry_dir=rdir, components_dir=cdir)
    restored: List[Dict[str, str]] = []
    for name in (pre_names if pre_names is not None else sorted(set(intent))):
        if name not in intent or name in cur_set:
            continue  # no removal scheduled, or the design still declares it
        if pre_names is not None:
            if name not in pre_names:
                continue  # not declared before the session -> nothing was dropped
        elif name not in dropped_now:
            continue  # no snapshot and no in-session deselect evidence
        if reg.module_path(name) is None:
            continue  # the removal landed (or it never existed): the drop is truth
        restored.append({"name": name,
                         "reason": ("dropped for a removal that did not land -- the "
                                    "component is still committed, so the selection "
                                    "is restored")})
    if restored:
        config[slot] = cur + [r["name"] for r in restored if r["name"] not in cur_set]
    return restored


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
    env["GAN_SECURE_MODE"] = "1"
    env["PYTHON_DOTENV_DISABLED"] = "1"

    runtime_dir = os.path.join(run_dir, ".gan_runtime")
    os.makedirs(runtime_dir, exist_ok=True)
    shutil.copy2(design_path, os.path.join(runtime_dir, "design.json"))

    # Context-management snapshot (docs/10 §11 D2): the frozen seam core +
    # (non-passthrough modes) the design-selected policy component, copied
    # per generation. The report rides toolset_report.json -> the
    # task_toolset_assembled event; the snapshot is advisory — a failed
    # snapshot degrades to passthrough at the child (fail-open by absence).
    cm_report = materialize_cm_snapshot(run_dir, config, code_root=code_root)
    env["GAN_CM_CORE"] = "/workspace/.gan_runtime/cm"
    # Fixed delivery names (cm_policy.py / cm_assembly.py): the env pins are
    # static, so the broker can pin the SAME values for driver mode — a
    # generation without a delivery leaves the file absent, and the child
    # degrades loudly on the claim-vs-snapshot gap.
    if cm_report.get("policy"):
        env["GAN_CM_POLICY"] = "/workspace/.gan_runtime/cm/cm_policy.py"
    if cm_report.get("assembly"):
        env["GAN_CM_ASSEMBLY"] = "/workspace/.gan_runtime/cm/cm_assembly.py"
    # Task-child CM audit sidecar (docs/10 §11.5): PRE-CREATED so the
    # sandbox's rw --bind has an existing source; the child appends its seam
    # events (cm_view/cm_violation/...) and task_runner folds them into
    # events.jsonl after the child exits. Without the file the child simply
    # has no audit sink (advisory end to end).
    try:
        from gan.framework import paths as _paths
        open(_paths.cm_events_path(run_dir), "a").close()
        env["GAN_CM_EVENTS"] = "/workspace/cm_events.jsonl"
    except OSError as e:
        # The sidecar is the task child's ONLY durable audit channel — its
        # absence must be as visible as its activity (no-silent-fail rule).
        # There is no events path in scope here, so the snapshot report is
        # the trace vehicle: it rides toolset_report.json -> the
        # task_toolset_assembled event (task_runner folds the whole report).
        cm_report["sidecar_unavailable"] = str(e)[:160]
        print(f"[WARN] cm events sidecar unavailable ({e}); the task child "
              "has no cm audit sink this generation", flush=True)

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
    # Caps are RESERVED cost keys (docs/10 §10): the task runner clamps the
    # design's cost slot IN PLACE pre-persist, so this consumption point reads
    # effective values only; defaults equal the pre-C constants.
    knowledge_report = {"total": 0, "files": [], "skipped": []}
    knowledge_src = os.path.join(str(code_root or ""), "gan", "components", "task",
                                 "knowledge")
    knowledge_dir = os.path.join(runtime_dir, "knowledge")
    from gan.framework import cost_keys as _ck
    task_cost_eff, task_cost_clamps = _ck.effective_all("task", config)
    knowledge_max_file = int(task_cost_eff["knowledge_max_file_chars"])
    knowledge_max_total = int(task_cost_eff["knowledge_max_total_chars"])
    knowledge_report["caps"] = {"knowledge_max_file_chars": knowledge_max_file,
                                "knowledge_max_total_chars": knowledge_max_total}
    if task_cost_clamps:
        knowledge_report["cost_clamped"] = task_cost_clamps
    if os.path.isdir(knowledge_src):
        os.makedirs(knowledge_dir, exist_ok=True)
        total = 0
        for f in sorted(os.listdir(knowledge_src)):
            if not f.endswith(".md") or f.startswith("."):
                continue
            src = os.path.join(knowledge_src, f)
            try:
                text = open(src, "r", encoding="utf-8").read(knowledge_max_file + 1)
            except OSError as e:
                knowledge_report["skipped"].append({"name": f, "reason": f"unreadable: {e}"})
                continue
            if len(text) > knowledge_max_file:
                knowledge_report["skipped"].append({"name": f,
                                                    "reason": "note exceeds per-note cap"})
                continue
            if total + len(text) > knowledge_max_total:
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
        json.dump({**toolset_report, "knowledge": knowledge_report,
                   "cm": cm_report},
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
        if domain in _SANDBOXED_BALROG_DOMAINS:
            dom = "balrog"
        else:
            dom = domain.split("_")[0] if "imo_" in domain else domain
        src = os.path.join(source_root, "domains", dom)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(run_dir, "domains", dom), ignore=_DOMAIN_IGNORE)
    # NOTE: no gan/ copy (see the allowlist note above): the only
    # framework-dependent component imports its primitive from the base layer,
    # which is present in every run dir.

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
    task_list = [str(value) for value in df["question_id"].tolist()]
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


def derive_sample_seed(base_seed: int, genid: str) -> int:
    """Per-generation seed derived deterministically from the run base seed.

    Pure function of ``(base_seed, genid)``: resume re-derives identical seeds
    without extra state, and different generations never share a draw.
    """
    digest = hashlib.sha256(f"{int(base_seed)}:{str(genid)}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def load_dataset_ids(dataset_root: str, domain: str, subset: str) -> List[str]:
    """Pool of question ids for the domain/subset (parent side only)."""
    utils_prefix = domain.split("_", 1)[1] + "_" if domain.startswith("imo_") else ""
    domain_folder = domain.split("_")[0] if "imo_" in domain else domain
    utils_module = importlib.import_module(f"domains.{domain_folder}.{utils_prefix}utils")
    question_id_col = utils_module.QUESTION_ID
    if "imo_" in domain:
        rel = f"domains/imo/{domain.split('_')[-1]}bench{subset}.csv"
    else:
        rel = f"domains/{domain}/dataset{subset}.csv"
    dataset = pd.read_csv(os.path.join(os.path.abspath(dataset_root), rel), dtype=str)
    return [str(v) for v in dataset[question_id_col].tolist()]


def select_samples(
    dataset: pd.DataFrame,
    question_id_col: str,
    num_samples: int,
    sample_seed: Optional[int] = None,
    anchor_ids: Optional[List[str]] = None,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Question selection with two modes (replaces the fixed prefix slice).

    - ``sample_seed is None``: legacy behaviour -- the first ``num_samples``
      rows of the file (deterministic prefix; kept as the explicit A/B base).
    - ``sample_seed is not None``: anchor rows first (``anchor_ids`` present in
      the pool, dataset order, deduplicated), then the remainder drawn without
      replacement from the rest of the pool with ``random.Random(sample_seed)``.
      Selection is deterministic for a given (seed, pool) and recorded via the
      returned info dict. ``num_samples <= 0`` returns the full dataset.

    Returns ``(selected_dataset, info)`` where info carries the audit fields
    (mode / seed / anchor ids actually present / sampled ids in file order).
    """
    if num_samples is None or num_samples <= 0:
        return dataset, {"mode": "all", "sample_seed": None,
                         "anchor_ids": [], "sampled_ids": []}
    if sample_seed is None:
        selected = dataset.iloc[:num_samples]
        return selected, {"mode": "legacy_prefix", "sample_seed": None,
                          "anchor_ids": [],
                          "sampled_ids": [str(v) for v in selected[question_id_col].tolist()]}
    ids = [str(v) for v in dataset[question_id_col].tolist()]
    anchor_req = [str(a) for a in (anchor_ids or [])]
    anchor_set: Dict[str, int] = {}
    for pos, qid in enumerate(ids):
        if qid in anchor_req and qid not in anchor_set:
            anchor_set[qid] = pos
    anchor_positions = sorted(anchor_set.values())
    # Anchor core must never consume the whole budget: cap at half.
    anchor_positions = anchor_positions[: max(0, num_samples // 2)]
    n_anchor = len(anchor_positions)
    pool_positions = [p for p in range(len(ids)) if p not in set(anchor_positions)]
    n_rest = min(num_samples - n_anchor, len(pool_positions))
    rng = random.Random(int(sample_seed))
    drawn = sorted(rng.sample(pool_positions, n_rest)) if n_rest > 0 else []
    positions = sorted(anchor_positions + drawn)
    selected = dataset.iloc[positions]
    return selected, {
        "mode": "seeded",
        "sample_seed": int(sample_seed),
        "anchor_ids": [ids[p] for p in anchor_positions],
        "sampled_ids": [str(v) for v in selected[question_id_col].tolist()],
    }


def ensure_sampling_seed(
    output_dir: str,
    domain: str,
    subset: str,
    dataset_root: str,
    num_samples: int,
    anchor_k: int = 2,
    base_seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Create (or reload) the run-persistent sampling seed + anchor core.

    Idempotent: the seed file lives at ``<output_dir>/ckpt/sample_seed.json``
    and survives resume -- a re-run with the same output_dir reuses the exact
    base seed and anchor ids (comparability with already-scored generations).
    """
    seed_path = os.path.join(output_dir, "ckpt", "sample_seed.json")
    if os.path.isfile(seed_path):
        with open(seed_path, "r", encoding="utf-8") as f:
            return json.load(f)
    if base_seed is None:
        base_seed = int.from_bytes(os.urandom(8), "big")
    anchor_ids: List[str] = []
    try:
        pool = (
            [] if domain == "polyglot" or domain in _SANDBOXED_BALROG_DOMAINS
            else load_dataset_ids(dataset_root, domain, subset)
        )
        k = max(0, min(int(anchor_k), int(num_samples) // 2 if num_samples > 0 else 0))
        if pool and k > 0:
            rng = random.Random(f"{int(base_seed)}:anchor")
            anchor_ids = sorted(rng.sample(pool, min(k, len(pool))))
    except Exception as exc:  # noqa: BLE001 -- anchor drawing must never kill a run
        anchor_ids = []
        _log_soft_sample_note(f"anchor drawing failed ({exc}); continuing without anchors")
    info = {"base_seed": int(base_seed), "anchor_ids": anchor_ids,
            "domain": domain, "subset": subset}
    os.makedirs(os.path.dirname(seed_path), exist_ok=True)
    tmp = seed_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    os.replace(tmp, seed_path)
    return info


def _log_soft_sample_note(message: str) -> None:
    print(f"[sample] {message}")


def prepare_questions(
    dataset_root: str,
    run_dir: str,
    domain: str,
    subset: str,
    num_samples: int,
    sample_seed: Optional[int] = None,
    anchor_ids: Optional[List[str]] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Write questions-only input and retain ground truth in parent memory.

    Returns ``(questions_path, ground_truth_by_id, sample_info)``; ``sample_info``
    is the audit record from :func:`select_samples` (mode/seed/ids).
    """
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
    dataset, sample_info = select_samples(
        dataset, question_id_col, num_samples,
        sample_seed=sample_seed, anchor_ids=anchor_ids,
    )

    ground_truth_by_id = dict(
        zip(dataset[question_id_col].tolist(), dataset[ground_truth_key].tolist())
    )
    questions = dataset.drop(columns=[ground_truth_key])
    input_dir = os.path.join(run_dir, "input")
    os.makedirs(input_dir, exist_ok=True)
    questions_path = os.path.join(input_dir, "questions.csv")
    questions.to_csv(questions_path, index=False)
    return questions_path, ground_truth_by_id, sample_info


def _sandbox_identity(run_dir: str, run_id: str,
                      expected_owner_uid: Optional[int] = None,
                      setpriv_path: Optional[str] = None) -> Tuple[str, int, int]:
    """Validate the dedicated host identity without mutating the run tree."""
    if os.name != "posix":
        raise RuntimeError("G6b requires a POSIX host with setpriv")

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
            "G6b secure launch requires root or the configured sandbox uid"
        )
    setpriv = setpriv_path or shutil.which("setpriv")
    if not setpriv:
        raise RuntimeError("G6b requires the util-linux setpriv executable")
    if expected_owner_uid is not None:
        run_stat = os.lstat(run_dir)
        if run_stat.st_uid != int(expected_owner_uid) or os.path.islink(run_dir):
            raise RuntimeError("G6b task run directory has an unexpected owner or type")

    return setpriv, uid, gid


def _task_setfacl(setfacl: str, fd: int, operation: str, acl: str) -> None:
    result = subprocess.run(
        [setfacl, operation, acl, f"/proc/self/fd/{fd}"],
        pass_fds=(fd,), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "setfacl failed").strip()
        raise RuntimeError(detail[-2000:])


def _task_open_absolute_parent_dirs(path: str,
                                    allowed_uids: set[int]) -> List[int]:
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
                        f"task sandbox ancestor has an unexpected owner or type: {path}"
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


_TASK_TRAVERSE_LOCK = threading.Lock()
_TASK_TRAVERSE_REFS: Dict[Tuple[int, int, int], int] = {}


def _task_acquire_traverse(
    fds: List[int], setfacl: str, sandbox_uid: int,
) -> List[Tuple[int, Tuple[int, int, int]]]:
    entries: List[Tuple[int, Tuple[int, int, int]]] = []
    try:
        with _TASK_TRAVERSE_LOCK:
            for fd in fds:
                info = os.fstat(fd)
                key = (int(info.st_dev), int(info.st_ino), sandbox_uid)
                if _TASK_TRAVERSE_REFS.get(key, 0) == 0:
                    _task_setfacl(setfacl, fd, "-m", f"u:{sandbox_uid}:x")
                _TASK_TRAVERSE_REFS[key] = _TASK_TRAVERSE_REFS.get(key, 0) + 1
                entries.append((fd, key))
        return entries
    except Exception:
        _task_release_traverse(entries, setfacl, sandbox_uid)
        for fd in fds[len(entries):]:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _task_release_traverse(
    entries: List[Tuple[int, Tuple[int, int, int]]],
    setfacl: str,
    sandbox_uid: int,
) -> List[str]:
    errors: List[str] = []
    for fd, key in entries:
        try:
            with _TASK_TRAVERSE_LOCK:
                remaining = _TASK_TRAVERSE_REFS.get(key, 0) - 1
                if remaining <= 0:
                    _TASK_TRAVERSE_REFS.pop(key, None)
                    _task_setfacl(setfacl, fd, "-x", f"u:{sandbox_uid}")
                else:
                    _TASK_TRAVERSE_REFS[key] = remaining
        except Exception as exc:
            errors.append(str(exc))
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    return errors


def _task_walk_tree(root_fd: int, visit: Any, *, reclaim: bool = False) -> None:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC

    errors: List[str] = []

    def record(action: Any) -> None:
        try:
            action()
        except Exception as exc:
            if not reclaim:
                raise
            errors.append(str(exc))

    def walk(directory_fd: int) -> None:
        try:
            names = os.listdir(directory_fd)
        except OSError as exc:
            if not reclaim:
                raise
            errors.append(str(exc))
            return
        for name in names:
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                if not reclaim:
                    raise
                errors.append(str(exc))
                continue
            valid = stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            if (stat.S_ISLNK(info.st_mode) or not valid or
                    (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                if reclaim:
                    record(lambda name=name: os.unlink(name, dir_fd=directory_fd))
                    continue
                raise RuntimeError("task run tree contains a link or non-regular entry")
            flags = directory_flags if stat.S_ISDIR(info.st_mode) else file_flags
            try:
                child = os.open(name, flags, dir_fd=directory_fd)
            except OSError as exc:
                if not reclaim:
                    raise
                errors.append(str(exc))
                continue
            try:
                child_info = os.fstat(child)
                if (stat.S_ISDIR(child_info.st_mode) != stat.S_ISDIR(info.st_mode)
                        or (stat.S_ISREG(child_info.st_mode)
                            and child_info.st_nlink != 1)):
                    if reclaim:
                        errors.append("task run tree changed during traversal")
                        continue
                    raise RuntimeError("task run tree changed during traversal")
                if stat.S_ISDIR(child_info.st_mode):
                    walk(child)
                record(lambda: visit(child, child_info))
            finally:
                os.close(child)

    walk(root_fd)
    record(lambda: visit(root_fd, os.fstat(root_fd)))
    if errors:
        raise RuntimeError("; ".join(errors[:5]))


def reconcile_stale_task_acl(setfacl: str, output_dir: str,
                             owner_uid: int, owner_gid: int,
                             sandbox_uid: int) -> None:
    """Revoke task identity access left by a previously killed broker."""
    if os.geteuid() != 0:
        raise RuntimeError("task ACL recovery must run in the root broker")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    output_fd = os.open(output_dir, flags)
    errors: List[str] = []

    def revoke(fd: int, info: os.stat_result) -> None:
        if info.st_uid not in {owner_uid, sandbox_uid}:
            raise RuntimeError("task work tree contains an unexpected owner")
        access = f"u:{sandbox_uid}:---"
        remove = f"u:{sandbox_uid}"
        if stat.S_ISDIR(info.st_mode):
            access += f",d:u:{sandbox_uid}:---"
            remove += f",d:u:{sandbox_uid}"
        _task_setfacl(setfacl, fd, "-m", access)
        if info.st_uid == sandbox_uid:
            os.fchown(fd, owner_uid, owner_gid)
        _task_setfacl(setfacl, fd, "-x", remove)

    try:
        output_info = os.fstat(output_fd)
        if (not stat.S_ISDIR(output_info.st_mode)
                or output_info.st_uid != owner_uid):
            raise RuntimeError("task ACL recovery found an unexpected output root")
        try:
            _task_setfacl(setfacl, output_fd, "-m", f"u:{sandbox_uid}:---")
        except Exception as exc:
            errors.append(str(exc))
        try:
            work_fd = os.open("work", flags, dir_fd=output_fd)
        except FileNotFoundError:
            work_fd = -1
        except Exception as exc:
            errors.append(str(exc))
            work_fd = -1
        if work_fd >= 0:
            try:
                _task_walk_tree(work_fd, revoke, reclaim=True)
            except Exception as exc:
                errors.append(str(exc))
            finally:
                os.close(work_fd)
        try:
            _task_setfacl(setfacl, output_fd, "-x", f"u:{sandbox_uid}")
        except Exception as exc:
            errors.append(str(exc))
    finally:
        os.close(output_fd)
    if errors:
        raise RuntimeError("stale task ACL recovery failed: " + "; ".join(errors[:5]))


def _task_open_beneath(root_fd: int, relative: str, owner_uid: int) -> int:
    parts = [part for part in relative.split(os.sep) if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise RuntimeError("task output path is outside the run directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.dup(root_fd)
    try:
        for part in parts:
            child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
            info = os.fstat(current)
            if info.st_uid != owner_uid or not stat.S_ISDIR(info.st_mode):
                raise RuntimeError("task output path has an unexpected owner or type")
        return current
    except Exception:
        os.close(current)
        raise


class _TaskRunLease:
    def __init__(self, run_fd: int, output_fd: int, setfacl: str,
                 owner_uid: int, owner_gid: int, sandbox_uid: int,
                 traverse_entries: List[Tuple[int, Tuple[int, int, int]]]) -> None:
        self.run_fd = run_fd
        self.output_fd = output_fd
        self.setfacl = setfacl
        self.owner_uid = owner_uid
        self.owner_gid = owner_gid
        self.sandbox_uid = sandbox_uid
        self.traverse_entries = traverse_entries
        self.closed = False

    @classmethod
    def acquire(cls, setfacl: str, run_dir: str, output_dir: str,
                owner_uid: int, owner_gid: int,
                sandbox_uid: int) -> "_TaskRunLease":
        if os.geteuid() != 0:
            raise RuntimeError("task ACL lease must be created by the root broker")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        run_fd = os.open(run_dir, flags)
        output_fd = -1
        lease = None
        try:
            run_info = os.fstat(run_fd)
            if run_info.st_uid != owner_uid or not stat.S_ISDIR(run_info.st_mode):
                raise RuntimeError("task run directory has an unexpected owner or type")
            relative_output = os.path.relpath(output_dir, run_dir)
            output_fd = _task_open_beneath(run_fd, relative_output, owner_uid)
            ancestor_fds = _task_open_absolute_parent_dirs(run_dir, {0, owner_uid})
            traverse_entries = _task_acquire_traverse(
                ancestor_fds, setfacl, sandbox_uid)
            lease = cls(
                run_fd, output_fd, setfacl, owner_uid, owner_gid, sandbox_uid,
                traverse_entries,
            )
            lease._grant()
            return lease
        except Exception:
            if lease is not None:
                lease.close(strict=False)
            else:
                if output_fd >= 0:
                    os.close(output_fd)
                os.close(run_fd)
            raise

    def _grant(self) -> None:
        def grant_read(fd: int, info: os.stat_result) -> None:
            if info.st_uid != self.owner_uid:
                raise RuntimeError("task run tree contains an unexpected owner")
            permissions = "r-x" if stat.S_ISDIR(info.st_mode) else "r--"
            _task_setfacl(
                self.setfacl, fd, "-m", f"u:{self.sandbox_uid}:{permissions}")
            if stat.S_ISREG(info.st_mode) and os.fstat(fd).st_nlink != 1:
                raise RuntimeError("task run file was hard-linked during ACL grant")

        def grant_write(fd: int, info: os.stat_result) -> None:
            if info.st_uid != self.owner_uid:
                raise RuntimeError("task output tree contains an unexpected owner")
            if stat.S_ISDIR(info.st_mode):
                acl = (f"u:{self.sandbox_uid}:rwx,u:{self.owner_uid}:rwx,"
                       f"d:u:{self.sandbox_uid}:rwx,d:u:{self.owner_uid}:rwx")
            else:
                acl = f"u:{self.sandbox_uid}:rw-,u:{self.owner_uid}:rw-"
            _task_setfacl(self.setfacl, fd, "-m", acl)
            if stat.S_ISREG(info.st_mode) and os.fstat(fd).st_nlink != 1:
                raise RuntimeError("task output file was hard-linked during ACL grant")

        _task_walk_tree(self.run_fd, grant_read)
        _task_walk_tree(self.output_fd, grant_write)

    def close(self, *, strict: bool = True) -> None:
        if self.closed:
            return
        self.closed = True
        errors: List[str] = []

        def record(action: Any) -> None:
            try:
                action()
            except Exception as exc:  # keep revoking the remainder of the lease
                errors.append(str(exc))

        def remove_output_acl(fd: int, info: os.stat_result) -> None:
            if info.st_uid not in {self.owner_uid, self.sandbox_uid}:
                raise RuntimeError("task output owner changed unexpectedly")
            record(lambda: _task_setfacl(
                self.setfacl, fd, "-x", f"u:{self.owner_uid}"))
            if stat.S_ISDIR(info.st_mode):
                record(lambda: _task_setfacl(
                    self.setfacl, fd, "-x", f"d:u:{self.sandbox_uid}"))
                record(lambda: _task_setfacl(
                    self.setfacl, fd, "-x", f"d:u:{self.owner_uid}"))

        def reclaim(fd: int, info: os.stat_result) -> None:
            if info.st_uid not in {self.owner_uid, self.sandbox_uid}:
                raise RuntimeError("task run owner changed unexpectedly")
            record(lambda: _task_setfacl(
                self.setfacl, fd, "-x", f"u:{self.sandbox_uid}"))
            record(lambda: os.fchown(fd, self.owner_uid, self.owner_gid))

        try:
            record(lambda: _task_walk_tree(
                self.output_fd, remove_output_acl, reclaim=True))
            record(lambda: _task_walk_tree(self.run_fd, reclaim, reclaim=True))
        finally:
            errors.extend(_task_release_traverse(
                self.traverse_entries, self.setfacl, self.sandbox_uid))
            self.traverse_entries = []
            for fd in (self.output_fd, self.run_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
        if strict and errors:
            raise RuntimeError("task ACL reclaim failed: " + "; ".join(errors[:5]))


def _python_prefix_hint(python_executable: str) -> str:
    try:
        executable = os.path.abspath(python_executable)
        bin_dir = os.path.dirname(executable)
        if os.path.basename(bin_dir) == "bin":
            return os.path.dirname(bin_dir)
    except OSError:
        pass
    return str(getattr(sys, "prefix", "<unknown>"))


def _sandbox_setpriv_command(setpriv: str, uid: int, gid: int,
                             command: List[str]) -> List[str]:
    """Run *command* as the sandbox identity with no residual capabilities."""
    return [
        setpriv,
        "--reuid", str(uid),
        "--regid", str(gid),
        "--clear-groups",
        "--bounding-set=-all",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--no-new-privs",
        "--",
        *command,
    ]


def _add_bwrap_empty_parents(command: List[str], path: str, created: set) -> None:
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


def _harness_bwrap_command(
    bwrap: str,
    python_prefix: str,
    run_dir: str,
    output_dir: str,
    read_only_assets: List[str],
    proxy_socket: Optional[str],
    child_command: List[str],
    sandbox_etc_dir: Optional[str] = None,
    proxy_token_fd: Optional[int] = None,
    run_fd: Optional[int] = None,
    output_fd: Optional[int] = None,
) -> List[str]:
    """Build the fixed G6c mount/network policy around the domain harness."""
    if run_fd is None or output_fd is None:
        raise RuntimeError("secure task sandbox requires fixed run/output descriptors")
    if not sandbox_etc_dir:
        raise RuntimeError("secure task sandbox requires a sanitized /etc projection")
    sandbox_etc_dir = validate_sandbox_etc(sandbox_etc_dir)
    command = [
        bwrap, "--die-with-parent", "--new-session",
        "--unshare-all", "--unshare-net", "--cap-drop", "ALL",
        "--hostname", SANDBOX_HOSTNAME,
    ]
    created = {"/"}
    system_roots = ("/usr", "/bin", "/sbin", "/lib", "/lib64")
    for path in system_roots:
        if os.path.exists(path):
            command.extend(["--ro-bind", path, path])
            created.add(path)
    command.extend(["--ro-bind", sandbox_etc_dir, "/etc"])
    created.add("/etc")

    prefix = os.path.realpath(python_prefix)
    if not any(prefix == root or prefix.startswith(root + os.sep)
               for root in system_roots):
        _add_bwrap_empty_parents(command, prefix, created)
        command.extend(["--ro-bind", prefix, prefix])
        created.add(prefix)

    command.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])
    created = {path for path in created if not path.startswith("/tmp/")}
    created.add("/tmp")

    _add_bwrap_empty_parents(command, run_dir, created)
    command.extend(["--ro-bind", f"/proc/self/fd/{int(run_fd)}", run_dir])
    created.add(run_dir)
    command.extend(["--bind", f"/proc/self/fd/{int(output_fd)}", output_dir])

    for asset in read_only_assets:
        _add_bwrap_empty_parents(command, asset, created)
        command.extend(["--ro-bind", asset, asset])
        created.add(asset)

    if proxy_socket:
        _add_bwrap_empty_parents(command, proxy_socket, created)
        command.extend(["--ro-bind", proxy_socket, proxy_socket])
    if proxy_token_fd is not None:
        token_path = "/run/rsi-gan/task-proxy-token"
        _add_bwrap_empty_parents(command, token_path, created)
        command.extend(["--ro-bind-data", str(int(proxy_token_fd)), token_path])

    command.extend([
        "--chdir", run_dir,
        "--setenv", "HOME", "/tmp",
        "--setenv", "TMPDIR", "/tmp",
        *child_command,
    ])
    return command


def _sandbox_runtime_preflight(
    setpriv: str,
    uid: int,
    gid: int,
    username: str,
    python_executable: str,
    run_dir: str,
    output_dir: str,
    env: Dict[str, str],
    timeout: int,
    bwrap_path: Optional[str] = None,
    python_prefix: Optional[str] = None,
    read_only_assets: Optional[List[str]] = None,
    proxy_socket: Optional[str] = None,
    sandbox_etc_dir: Optional[str] = None,
    run_fd: Optional[int] = None,
    output_fd: Optional[int] = None,
) -> None:
    """Fail early if the sandbox uid cannot start the selected Python."""
    prefix_hint = python_prefix or _python_prefix_hint(python_executable)
    probe_child = [
        python_executable,
        "-c", _SANDBOX_RUNTIME_PREFLIGHT,
        str(uid),
        str(gid),
        run_dir,
        output_dir,
        "1" if bwrap_path else "0",
    ]
    if bwrap_path:
        if not python_prefix:
            raise RuntimeError("G6c sandbox runtime preflight is missing Python prefix")
        probe_child = _harness_bwrap_command(
            bwrap_path, python_prefix, run_dir, output_dir,
            list(read_only_assets or []), proxy_socket, probe_child,
            sandbox_etc_dir=sandbox_etc_dir,
            run_fd=run_fd, output_fd=output_fd,
        )
    probe_cmd = _sandbox_setpriv_command(setpriv, uid, gid, probe_child)
    descriptor = (
        f"sandbox username={username!r}, uid={uid}, gid={gid}, "
        f"Python executable={python_executable!r}, Python prefix={prefix_hint!r}"
    )
    try:
        probe = subprocess.run(
            probe_cmd,
            cwd="/" if bwrap_path else run_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(1, min(int(timeout), 30)),
            pass_fds=((int(run_fd), int(output_fd))
                      if run_fd is not None and output_fd is not None else ()),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"G6b/G6c sandbox runtime preflight failed ({descriptor}): timed out"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"G6b/G6c sandbox runtime preflight failed ({descriptor}): "
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
            f"G6b/G6c sandbox runtime preflight failed ("
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
    reasoning_effort: Optional[str] = None,
) -> Tuple[int, str]:
    """Run the frozen harness; parent-side reporting happens separately.

    ``reasoning_effort`` rides as a CLI argument (like ``--model``), never the
    env. In broker mode the caller's value is ignored ON PURPOSE: the broker's
    command was fixed at root-side registration from the same models.yaml
    entry, so both paths derive one consistent value by construction.
    """
    broker_socket = env.get("GAN_TASK_BROKER_UNIX")
    if broker_socket:
        outer = env.get("GAN_TASK_BROKER_OUTER")
        if not outer or not scope_id:
            raise RuntimeError("task broker requires an outer id and generation id")
        if (log_path and os.name == "posix" and hasattr(os, "geteuid") and
                os.geteuid() == 0):
            raise RuntimeError("refusing to write a task log from a root broker caller")
        from gan.framework.task_broker import request_task_run
        result = request_task_run(
            broker_socket, int(outer), str(scope_id), int(timeout),
        )
        if log_path:
            rc, output = result
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(
                    f"\n$ task-broker scope={scope_id} domain={domain} "
                    f"(cwd={run_dir}, rc={rc})\n{output[-4000:]}\n"
                )
        return result
    return _run_harness_and_report_local(
        python, run_dir, domain, run_id, subset, num_samples, model, env, timeout,
        questions_path=questions_path, dataset_root=dataset_root,
        scope_id=scope_id, log_path=log_path,
        reasoning_effort=reasoning_effort,
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
    proxy_token_fd: Optional[int] = None,
    expected_owner_uid: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
    sample_seed: Optional[int] = None,
    anchor_ids: Optional[List[str]] = None,
    expected_owner_gid: Optional[int] = None,
    setpriv_path: Optional[str] = None,
    setfacl_path: Optional[str] = None,
    bwrap_path: Optional[str] = None,
    python_prefix: Optional[str] = None,
    sandbox_etc_dir: Optional[str] = None,
    sandbox_profile: Optional[Dict[str, Any]] = None,
    agent_path: str = "./task_agent.py",
    proofs_dname: Optional[str] = None,
) -> Tuple[int, str]:
    """Run a fixed harness command; the privileged broker is its only secure caller.

    Two LLM credential transports, never mixed: ``proxy_token_fd`` (a sealed
    memfd descriptor minted by the root-side task broker) is the SECURE path --
    the token value never appears in argv, only its fixed /run file path does.
    ``proxy_token`` (a plain string, requested via the framework control
    capability) is the LOCAL non-broker transport (developer/test runs without
    the sandbox); a broker launch (``expected_owner_uid`` set) refuses it.

    ``sample_seed``/``anchor_ids`` apply ONLY to the ``dataset_root`` branch
    (domains that score in the harness): the questions read there are sliced by
    the harness itself. In the ``questions_path`` branch the parent already
    applied the selection in :func:`prepare_questions`, so no seed is forwarded
    (a second draw here would re-sample the already-sampled questions).
    """
    harness_args = [
        "-m", "domains.harness",
        "--agent_path", agent_path,
        "--domain", domain,
        "--model", model,
        "--run_id", run_id,
        "--subset", subset,
        "--num_samples", str(num_samples),
    ]
    if reasoning_effort:
        # Explicit CLI transport (mirrors --model); never an env variable.
        harness_args.extend(["--reasoning_effort", str(reasoning_effort)])
    proxy_socket = proxy_socket or env.get("GAN_LLM_PROXY_UNIX")
    if proxy_socket and proxy_token_fd is None and proxy_token is None:
        if not scope_id:
            raise RuntimeError("LLM proxy task scope requires a generation id")
        control_token = env.get("GAN_PROXY_CONTROL_TOKEN")
        if not control_token:
            raise RuntimeError("LLM proxy task scope requires framework control capability")
        from gan.framework.llm_proxy import request_task_token
        proxy_token = request_task_token(
            proxy_socket, control_token, str(scope_id), model)
    needs_sandbox_identity = questions_path is not None or (
        domain in _SANDBOXED_HARNESS_DOMAINS and dataset_root is not None
    )
    if needs_sandbox_identity:
        harness_args.extend(["--output_dir", "/proc/self/cwd/outputs"])

    def child_path(path: str) -> str:
        absolute = os.path.realpath(os.path.abspath(path))
        run_root = os.path.realpath(run_dir)
        # The broker enters run_dir before setpriv. Keep private ancestors
        # closed while letting the sandbox resolve its authorized run copy.
        if (needs_sandbox_identity and
                os.path.commonpath([run_root, absolute]) == run_root):
            return os.path.join("/proc/self/cwd", os.path.relpath(absolute, run_root))
        return absolute

    if questions_path:
        harness_args.extend(["--questions_path", child_path(questions_path)])
    elif dataset_root:
        # Compatibility path for domains with their own evaluator/harness.
        harness_args.extend(["--dataset_root", child_path(dataset_root)])
        if sample_seed is not None:
            harness_args.extend(["--sample_seed", str(int(sample_seed))])
            if anchor_ids:
                harness_args.extend(["--anchor_ids", ",".join(str(a) for a in anchor_ids)])
    if proofs_dname:
        harness_args.extend(["--proofs_dname", child_path(proofs_dname)])
    if proxy_token_fd is not None:
        harness_args.extend([
            "--proxy_socket", os.path.abspath(proxy_socket),
            "--proxy_token_file", "/run/rsi-gan/task-proxy-token",
        ])
    elif proxy_token is not None:
        harness_args.extend([
            "--proxy_socket", os.path.abspath(proxy_socket),
            "--proxy_token", proxy_token,
        ])

    broker_launch = expected_owner_uid is not None
    secure_launch = needs_sandbox_identity or broker_launch
    child_env = _task_safe_env(env)
    if needs_sandbox_identity and not broker_launch:
        child_env["PYTHONPATH"] = "/proc/self/cwd"
    lease = None
    primary_error: Optional[BaseException] = None
    try:
        if secure_launch:
            if (os.name == "posix" and hasattr(os, "geteuid") and
                    os.geteuid() == 0 and not broker_launch and
                    domain not in _SANDBOXED_HARNESS_DOMAINS):
                raise RuntimeError(
                    "refusing to launch a domain harness as root outside the secure task broker"
                )
            if broker_launch and proxy_token_fd is None:
                raise RuntimeError(
                    "secure task broker launch requires a sealed proxy token descriptor")
            identity_kwargs = (
                {"setpriv_path": setpriv_path} if setpriv_path is not None else {}
            )
            setpriv, uid, gid = _sandbox_identity(
                run_dir, run_id, expected_owner_uid=expected_owner_uid,
                **identity_kwargs,
            )
            python_executable = python if os.path.isabs(python) else shutil.which(python)
            if not python_executable:
                raise RuntimeError(f"G6b cannot resolve Python executable: {python}")
            output_dir = os.path.join(run_dir, "outputs", run_id)
            if broker_launch:
                if (expected_owner_gid is None or not setfacl_path or not bwrap_path or
                        not python_prefix or not sandbox_etc_dir
                        or not isinstance(sandbox_profile, dict)):
                    raise RuntimeError("secure task broker is missing G6b/G6c configuration")
                if (sandbox_profile.get("network") != "none" or
                        sandbox_profile.get("gpu") != "none" or
                        not isinstance(sandbox_profile.get("read_only_assets"), list)):
                    raise RuntimeError("secure task broker received an invalid sandbox profile")
                lease = _TaskRunLease.acquire(
                    str(setfacl_path), run_dir, output_dir,
                    int(expected_owner_uid), int(expected_owner_gid), uid,
                )
            harness_child = [python_executable, *harness_args]
            if broker_launch:
                harness_child = _harness_bwrap_command(
                    str(bwrap_path), str(python_prefix), run_dir, output_dir,
                    list(sandbox_profile["read_only_assets"]), proxy_socket,
                    harness_child, sandbox_etc_dir=sandbox_etc_dir,
                    proxy_token_fd=proxy_token_fd,
                    run_fd=lease.run_fd, output_fd=lease.output_fd,
                )
            if broker_launch or questions_path is not None:
                harness_cmd = _sandbox_setpriv_command(
                    setpriv, uid, gid, harness_child,
                )
            else:
                # These domains already isolate their worker internally; keep
                # their established non-broker identity-launch contract.
                harness_cmd = [
                    setpriv,
                    "--reuid", str(uid),
                    "--regid", str(gid),
                    "--clear-groups",
                    "--no-new-privs",
                    "--",
                    *harness_child,
                ]
            username = os.environ.get(_SANDBOX_USER_ENV, "").strip()
            _sandbox_runtime_preflight(
                setpriv, uid, gid, username, python_executable,
                run_dir, output_dir, child_env, timeout,
                bwrap_path=str(bwrap_path) if broker_launch else None,
                python_prefix=str(python_prefix) if broker_launch else None,
                read_only_assets=(list(sandbox_profile["read_only_assets"])
                                  if broker_launch else None),
                proxy_socket=proxy_socket if broker_launch else None,
                sandbox_etc_dir=sandbox_etc_dir if broker_launch else None,
                run_fd=lease.run_fd if lease is not None else None,
                output_fd=lease.output_fd if lease is not None else None,
            )
        else:
            if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0:
                raise RuntimeError(
                    "refusing to launch a domain harness as root outside the secure task broker"
                )
            harness_cmd = [python, *harness_args]

        inherited_fds = []
        if proxy_token_fd is not None:
            # bwrap --ro-bind-data reads from the descriptor's current offset.
            # A generation and its imo_proof grading pass intentionally reuse
            # one sealed memfd, so rewind it before every harness launch.
            os.lseek(int(proxy_token_fd), 0, os.SEEK_SET)
            inherited_fds.append(int(proxy_token_fd))
        if lease is not None:
            inherited_fds.extend([lease.run_fd, lease.output_fd])
        proc = subprocess.run(
            harness_cmd, cwd="/" if broker_launch else run_dir,
            env=child_env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
            pass_fds=tuple(inherited_fds),
        )
        out = proc.stdout or ""
        if log_path:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(
                    f"\n$ {' '.join(harness_cmd)} (cwd={run_dir})\n{out[-4000:]}\n"
                )
        return proc.returncode, out
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if lease is not None:
            try:
                lease.close()
            except Exception as reclaim_error:
                if primary_error is None:
                    raise
                raise RuntimeError(
                    "domain harness failed and task ACL reclaim also failed: "
                    f"{type(primary_error).__name__}: {primary_error}; {reclaim_error}"
                ) from primary_error



def _prepare_harness_output_dir(
    run_dir: str,
    run_id: str,
    expected_owner_uid: Optional[int] = None,
    expected_owner_gid: Optional[int] = None,
) -> str:
    """Create one harness output directory without following task-owned links."""
    output_dir = os.path.join(run_dir, "outputs", run_id)
    if expected_owner_uid is None:
        os.makedirs(output_dir, exist_ok=True)
        return output_dir
    if expected_owner_gid is None:
        raise RuntimeError("secure harness output creation requires the owner gid")
    if not run_id or os.path.basename(run_id) != run_id:
        raise RuntimeError(f"invalid harness run id: {run_id!r}")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    run_fd = os.open(run_dir, flags)
    outputs_fd = -1
    child_fd = -1
    try:
        run_info = os.fstat(run_fd)
        if run_info.st_uid != int(expected_owner_uid):
            raise RuntimeError("secure harness run directory has an unexpected owner")
        outputs_fd = os.open("outputs", flags, dir_fd=run_fd)
        outputs_info = os.fstat(outputs_fd)
        if outputs_info.st_uid != int(expected_owner_uid):
            raise RuntimeError("secure harness outputs directory has an unexpected owner")
        try:
            os.mkdir(run_id, mode=0o700, dir_fd=outputs_fd)
            created = True
        except FileExistsError:
            created = False
        child_fd = os.open(run_id, flags, dir_fd=outputs_fd)
        child_info = os.fstat(child_fd)
        allowed_uids = {int(expected_owner_uid)}
        if created:
            allowed_uids.add(0)
        if child_info.st_uid not in allowed_uids:
            raise RuntimeError("secure harness output directory has an unexpected owner")
        if created:
            os.fchown(child_fd, int(expected_owner_uid), int(expected_owner_gid))
            os.fchmod(child_fd, 0o700)
    finally:
        for fd in (child_fd, outputs_fd, run_fd):
            if fd >= 0:
                os.close(fd)
    return output_dir


def _write_proofgrader_shim(
    run_dir: str,
    expected_owner_uid: Optional[int] = None,
    expected_owner_gid: Optional[int] = None,
) -> str:
    """Create a framework-owned grader entry point inside the run copy."""
    runtime_dir = os.path.join(run_dir, ".gan_runtime")
    shim_path = os.path.join(runtime_dir, "proofgrader_task_agent.py")
    if expected_owner_uid is None:
        os.makedirs(runtime_dir, exist_ok=True)
        with open(shim_path, "w", encoding="utf-8") as stream:
            stream.write("from proofgrader.task_agent import TaskAgent\n")
        os.chmod(shim_path, 0o644)
        return "./.gan_runtime/proofgrader_task_agent.py"
    if expected_owner_gid is None:
        raise RuntimeError("secure proofgrader shim creation requires the owner gid")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    runtime_fd = os.open(runtime_dir, flags)
    shim_fd = -1
    try:
        runtime_info = os.fstat(runtime_fd)
        if runtime_info.st_uid != int(expected_owner_uid):
            raise RuntimeError("proofgrader runtime directory has an unexpected owner")
        try:
            shim_fd = os.open(
                "proofgrader_task_agent.py",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=runtime_fd,
            )
        except FileExistsError as exc:
            raise RuntimeError("proofgrader shim path already exists") from exc
        shim_info = os.fstat(shim_fd)
        if not stat.S_ISREG(shim_info.st_mode) or shim_info.st_nlink != 1:
            raise RuntimeError("proofgrader shim has an unexpected type or link count")
        if shim_info.st_uid not in {0, int(expected_owner_uid)}:
            raise RuntimeError("proofgrader shim has an unexpected owner")
        payload = b"from proofgrader.task_agent import TaskAgent\n"
        offset = 0
        while offset < len(payload):
            written = os.write(shim_fd, payload[offset:])
            if written <= 0:
                raise OSError("short write while creating proofgrader shim")
            offset += written
        os.fchown(shim_fd, int(expected_owner_uid), int(expected_owner_gid))
        os.fchmod(shim_fd, 0o644)
    finally:
        if shim_fd >= 0:
            os.close(shim_fd)
        os.close(runtime_fd)
    return "./.gan_runtime/proofgrader_task_agent.py"


def _require_sandboxable_proofgrader() -> None:
    """Require proofgrader to live inside the trusted Python prefix."""
    try:
        spec = importlib.util.find_spec("proofgrader.task_agent")
    except (ImportError, ModuleNotFoundError, ValueError) as exc:
        raise RuntimeError("proofgrader.task_agent is not importable") from exc
    origin = os.path.realpath(str(spec.origin or "")) if spec else ""
    prefix = os.path.realpath(sys.prefix)
    if not origin or not (
        origin == prefix
        or origin.startswith(prefix + os.sep)
        or origin.startswith(os.path.realpath("/usr") + os.sep)
    ):
        raise RuntimeError(
            "proofgrader.task_agent must be installed inside the trusted Python "
            "environment; editable source outside the sandbox is refused"
        )


def _open_grading_output(path: str, expected_owner_uid: Optional[int]) -> int:
    """Open one reclaimed grading output without following task-owned links."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError(f"grading output has an unexpected type: {path}")
        if expected_owner_uid is not None and info.st_uid != int(expected_owner_uid):
            raise RuntimeError(f"grading output has an unexpected owner: {path}")
        return fd
    except Exception:
        os.close(fd)
        raise


def _imo_proof_grading_output_error(
    grading_dir: str,
    expected_owner_uid: Optional[int],
) -> Optional[str]:
    """Reject isolated grader failures and labels outside the scoring contract."""
    failures_path = os.path.join(grading_dir, "agent_evals", "eval_failures.jsonl")
    if os.path.lexists(failures_path):
        failure_fd = _open_grading_output(failures_path, expected_owner_uid)
        try:
            if os.fstat(failure_fd).st_size > 0:
                return "proof grader recorded one or more per-question failures"
        finally:
            os.close(failure_fd)

    predictions_path = os.path.join(grading_dir, "predictions.csv")
    try:
        predictions_fd = _open_grading_output(predictions_path, expected_owner_uid)
    except FileNotFoundError:
        return "proof grader did not produce predictions.csv"
    try:
        with os.fdopen(predictions_fd, "r", encoding="utf-8", newline="") as stream:
            predictions_fd = -1
            grades = pd.read_csv(stream, dtype=str, keep_default_na=False)
    finally:
        if predictions_fd >= 0:
            os.close(predictions_fd)
    if "prediction" not in grades.columns or len(grades) == 0:
        return "proof grader produced no grading labels"
    allowed = {"incorrect", "partial", "almost", "correct"}
    invalid = sorted({str(value) for value in grades["prediction"] if value not in allowed})
    if invalid:
        return f"proof grader produced invalid or blank labels: {invalid[:3]}"
    return None


def _imo_proof_generation_output_error(
    predictions_dir: str,
    expected_owner_uid: Optional[int],
) -> Optional[str]:
    """Reject missing task proofs before they can be graded as incorrect."""
    failures_path = os.path.join(
        predictions_dir, "agent_evals", "eval_failures.jsonl",
    )
    if os.path.lexists(failures_path):
        failure_fd = _open_grading_output(failures_path, expected_owner_uid)
        try:
            if os.fstat(failure_fd).st_size > 0:
                return "proof generation recorded one or more per-question failures"
        finally:
            os.close(failure_fd)

    predictions_path = os.path.join(predictions_dir, "predictions.csv")
    try:
        predictions_fd = _open_grading_output(
            predictions_path, expected_owner_uid,
        )
    except FileNotFoundError:
        return "proof generation did not produce predictions.csv"
    try:
        with os.fdopen(predictions_fd, "r", encoding="utf-8", newline="") as stream:
            predictions_fd = -1
            predictions = pd.read_csv(stream, dtype=str, keep_default_na=False)
    finally:
        if predictions_fd >= 0:
            os.close(predictions_fd)
    if "prediction" not in predictions.columns or len(predictions) == 0:
        return "proof generation produced no predictions"
    blank_count = sum(
        1 for value in predictions["prediction"]
        if not isinstance(value, str) or not value.strip()
    )
    if blank_count:
        return f"proof generation produced {blank_count} blank prediction(s)"
    return None


def run_imo_proof_grading(
    python: str,
    run_dir: str,
    run_id: str,
    num_samples: int,
    model: str,
    env: Dict[str, str],
    timeout: int,
    proxy_socket: str,
    proxy_token: Optional[str] = None,
    log_path: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    proxy_token_fd: Optional[int] = None,
    expected_owner_uid: Optional[int] = None,
    expected_owner_gid: Optional[int] = None,
    setpriv_path: Optional[str] = None,
    setfacl_path: Optional[str] = None,
    bwrap_path: Optional[str] = None,
    python_prefix: Optional[str] = None,
    sandbox_etc_dir: Optional[str] = None,
    sandbox_profile: Optional[Dict[str, Any]] = None,
) -> Tuple[int, str]:
    """Grade generated proofs through the same scoped, sandboxed task path."""
    predictions_dir = os.path.join(run_dir, "outputs", run_id)
    generation_error = _imo_proof_generation_output_error(
        predictions_dir, expected_owner_uid,
    )
    if generation_error:
        return 1, f"[imo_proof_generation_error] {generation_error}"
    _require_sandboxable_proofgrader()
    grading_run_id = f"{run_id}_proof_grading"
    _prepare_harness_output_dir(
        run_dir, grading_run_id,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
    )
    agent_path = _write_proofgrader_shim(
        run_dir,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
    )
    grading_env = dict(env)
    grading_env.pop("GAN_TASK_BRIEF", None)
    rc, output = _run_harness_and_report_local(
        python,
        run_dir,
        "imo_proof_grading",
        grading_run_id,
        "",
        num_samples,
        model,
        grading_env,
        timeout,
        # ``proofs_dname`` supplies the rows; dataset_root deliberately selects
        # the sandboxed-agent path, where task_worker provides the scoped relay.
        dataset_root=run_dir,
        proxy_socket=proxy_socket,
        proxy_token=proxy_token,
        proxy_token_fd=proxy_token_fd,
        log_path=log_path,
        reasoning_effort=reasoning_effort,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
        setpriv_path=setpriv_path,
        setfacl_path=setfacl_path,
        bwrap_path=bwrap_path,
        python_prefix=python_prefix,
        sandbox_etc_dir=sandbox_etc_dir,
        sandbox_profile=sandbox_profile,
        agent_path=agent_path,
        proofs_dname=predictions_dir,
    )
    if rc != 0:
        return rc, output

    grading_dir = os.path.join(run_dir, "outputs", grading_run_id)
    grading_error = _imo_proof_grading_output_error(
        grading_dir, expected_owner_uid,
    )
    if grading_error:
        return 1, f"{output}\n[imo_proof_grading_error] {grading_error}"
    src = os.path.join(grading_dir, "report.json")
    if os.path.lexists(src):
        raise RuntimeError(f"imo_proof grading report path already exists: {src}")
    from domains.imo.proof_eval import report_proof_grading
    report_proof_grading(grading_dir)
    dst = os.path.join(predictions_dir, "report.json")
    if not os.path.isfile(src):
        raise RuntimeError(f"imo_proof grading report is missing: {src}")
    src_info = os.lstat(src)
    if not stat.S_ISREG(src_info.st_mode) or src_info.st_nlink != 1:
        raise RuntimeError("imo_proof grading report has an unexpected type or link count")
    if expected_owner_uid is not None:
        os.chown(
            src, int(expected_owner_uid), int(expected_owner_gid),
            follow_symlinks=False,
        )
    os.chmod(src, 0o644)
    os.replace(src, dst)
    return rc, output


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
        # GAN mode completes proof grading inside the privileged task broker;
        # never fall back to the legacy in-process proofgrader path here.
        return read_report(os.path.join(dname, "report.json"))
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


def extract_score(report: Optional[Dict[str, Any]], score_key: str,
                  domain: Optional[str] = None) -> Optional[float]:
    """Numeric objective score, or None when absent/NaN."""
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
    if domain in _SANDBOXED_BALROG_DOMAINS:
        f /= 100.0
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
