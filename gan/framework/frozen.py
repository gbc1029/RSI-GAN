"""Single source of truth for the source-access boundary (FRAMEWORK, frozen).

Policy is an **allowlist by role** (everything not listed is frozen by default).
We intentionally do NOT maintain a "frozen directory" list: the accessible /
modifiable surface is far smaller than the repo, so it is safer and less
error-prone to declare only what MAY be read/written.

- ``task``      : no source access at all (it may only call its own tools).
- ``planner``   : read + write the task source (t) and its own source (p).
- ``evaluator`` : read the task source (t, read-only, for cheating/rule checks)
                  + read/write its own source (e).

The gate (``gan/framework/access.py``) enforces this; requested paths outside the
role's roots are denied. The complement (the trust anchor, e.g. ``gan/framework``,
``agent/llm*.py``, ``agent/base_agent.py``, ``domains/harness.py``,
``domains/task_worker.py``, ``domains/report.py``, design operators/plumbing) is
therefore frozen implicitly.

Plumbing tools (``gan/tools/design``, ``gan/tools/deep``, ``gan/tools/work/common``)
and ``gan/framework/*`` are NOT in any role's roots -> not modifiable.
"""
from __future__ import annotations

import fnmatch
import glob as _glob
import os
from typing import Dict, List

# --- path sets -------------------------------------------------------------
TASK_SOURCE: List[str] = [
    "task_agent.py",
    "gan/components/task/**",
    "gan/registries/task.json",
    "gan/design/schema_ext/task.json",
]

PLANNER_SELF: List[str] = [
    "gan/roles/planner.py",
    "gan/tools/work/planner/**",
    "gan/components/planner/**",
    "gan/registries/planner.json",
    "gan/design/seeds/planner.md",
    "gan/design/schema_ext/planner.json",
]

EVALUATOR_SELF: List[str] = [
    "gan/roles/evaluator.py",
    "gan/tools/work/evaluator/**",
    "gan/components/evaluator/**",
    "gan/registries/evaluator.json",
    "gan/design/seeds/evaluator.md",
    "gan/design/schema_ext/evaluator.json",
]

# Seat-specific write roots.  Reads remain role-wide; only writes are narrowed.
# ``legacy`` preserves direct callers until every session constructor supplies a seat.
SEAT_WRITE: Dict[str, Dict[str, List[str]]] = {
    "planner": {
        "plan": TASK_SOURCE,
        "self_improve": PLANNER_SELF,
        "legacy": TASK_SOURCE + PLANNER_SELF,
    },
    "evaluator": {
        "evaluate": [],
        "self_improve": EVALUATOR_SELF,
        "legacy": EVALUATOR_SELF,
    },
    "task": {"task": [], "legacy": []},
}

# role -> {"read": [...], "write": [...]}
ACCESS: Dict[str, Dict[str, List[str]]] = {
    "task": {"read": [], "write": []},
    "planner": {"read": TASK_SOURCE + PLANNER_SELF,
                "write": TASK_SOURCE + PLANNER_SELF},
    "evaluator": {"read": TASK_SOURCE + EVALUATOR_SELF,
                  "write": EVALUATOR_SELF},
}

# Explicit trust anchor. NOT part of the ACCESS allowlist (no role lists it), but
# enforced as an extra deny filter in is_allowed() below and passed to the broker
# as the runtime deny list (deny_paths()). Currently redundant (defence-in-depth):
# none of these paths appear in any role's roots.
TRUST_ANCHOR: List[str] = [
    "gan/framework/*",
    "agent/llm.py",
    "agent/llm_withtools.py",
    "agent/base_agent.py",
    "domains/harness.py",
    "domains/task_worker.py",
    "domains/report.py",
]


def _norm(rel: str) -> str:
    return str(rel).replace("\\", "/").lstrip("/")


def _matches(rel: str, roots: List[str]) -> bool:
    rel = _norm(rel)
    for pat in roots:
        if fnmatch.fnmatch(rel, pat):
            return True
        # allow a glob root like "gan/components/**" to match the dir itself
        if pat.endswith("/**") and fnmatch.fnmatch(rel, pat[:-3]):
            return True
    return False


def read_roots(role: str) -> List[str]:
    return list(ACCESS.get(role, {}).get("read", []))


def write_roots(role: str, seat: str = "legacy") -> List[str]:
    return list(SEAT_WRITE.get(role, {}).get(str(seat or "legacy"),
                                             ACCESS.get(role, {}).get("write", [])))


def is_allowed(role: str, rel: str, intent: str = "view", seat: str = "legacy") -> bool:
    """Allowlist check with an optional session seat.

    Reads remain role-wide. Modifying requests use seat-specific write roots;
    ``legacy`` preserves direct callers until every session supplies a seat.
    """
    if not rel:
        return False
    raw = str(rel).replace("\\", "/")
    if raw.startswith("/") or any(part == ".." for part in raw.split("/")):
        return False
    rel = raw
    roots = read_roots(role) if intent == "view" else write_roots(role, seat)
    if not _matches(rel, roots):
        return False
    return not _matches(rel, TRUST_ANCHOR)


def has_glob(rel: str) -> bool:
    return any(ch in str(rel) for ch in "*?[")


def expand_roots(root_dir: str, roots: List[str], cap: int = 500) -> List[str]:
    """Expand allowlist globs to concrete existing files under ``root_dir``.

    Used by ``list_editable`` so agents can request real paths instead of
    echoing glob patterns back (which the gate treats as literal paths).
    """
    out: List[str] = []
    seen = set()
    for pat in roots:
        for m in _glob.glob(os.path.join(root_dir, pat), recursive=True):
            if not os.path.isfile(m):
                continue
            rel = os.path.relpath(m, root_dir).replace(os.sep, "/")
            # Compiled caches are not editable/readable source -- offering them
            # (or granting/patch-diffing them) is noise the agent must never see.
            if "/__pycache__/" in f"/{rel}" or rel.endswith((".pyc", ".pyo")):
                continue
            if rel not in seen:
                seen.add(rel)
                out.append(rel)
                if len(out) >= cap:
                    return out
    return out


# -- back-compat ------------------------------------------------------------
def deny_paths() -> List[str]:
    """Legacy deny list = the explicit trust anchor (kept for audit/tests)."""
    return list(TRUST_ANCHOR)


def is_frozen(rel_path: str) -> bool:
    return _matches(rel_path, TRUST_ANCHOR)
