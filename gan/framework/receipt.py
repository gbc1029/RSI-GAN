"""Round receipt (FRAMEWORK, frozen).

A deterministic, structured summary of what a session/round changed -- used to
tell the *next* round both what succeeded (shallow design changes that DID take
effect) and what failed (a rejected code patch + why), plus budget state and
pointers to the attempt's evidence. No LLM involved.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _ops_summary(records: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        op = r.get("op")
        entry: Dict[str, Any] = {"op": op}
        for k in ("slot", "name", "key", "intent", "paths"):
            if k in r:
                entry[k] = r[k]
        out.append(entry)
    return out


def _design_diff(config: Optional[Dict[str, Any]], parent_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = config if isinstance(config, dict) else {}
    par = parent_config if isinstance(parent_config, dict) else {}
    before_skills = set(par.get("skills") or [])
    after_skills = set(cfg.get("skills") or [])
    before_pts = set(par.get("eval_points") or [])
    after_pts = set(cfg.get("eval_points") or [])
    return {
        "prompt_changed": (cfg.get("prompt") != par.get("prompt")),
        "skills_added": sorted(after_skills - before_skills),
        "skills_removed": sorted(before_skills - after_skills),
        "eval_points_added": sorted(after_pts - before_pts),
        "eval_points_removed": sorted(before_pts - after_pts),
    }


def _next_hint(reason: Optional[str]) -> str:
    r = (reason or "").lower()
    if not r:
        return ""
    if "module" in r and ("missing required field" in r or "module file not found" in r):
        return ("A component entry is invalid: add the missing 'module' field and the module "
                "file under gan/components/, or use `unregister_component` to remove it.")
    if "new invalid component" in r:
        return "Fix the newly-invalid component(s) or `unregister_component` them."
    if "unparseable" in r:
        return "The registry JSON is not parseable; restore valid JSON."
    if "compile" in r or "syntax" in r:
        return "Fix the compile/syntax error in the changed files."
    if "non-editable" in r:
        return "The patch touched a path you may not modify; only edit your editable roots."
    return "Fix the reported problem, then stop; do not repeat the same action."


def build_receipt(
    *,
    genid: Any = None,
    role: str = "planner",
    stage: str = "plan",
    records: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
    parent_config: Optional[Dict[str, Any]] = None,
    patch: str = "",
    patch_applied: bool = False,
    commit: Optional[str] = None,
    rejected_reason: Optional[str] = None,
    budget: Optional[Dict[str, Any]] = None,
    grants: Optional[List[Dict[str, Any]]] = None,
    trajectory_refs: Optional[List[str]] = None,
    toolset: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    design = _design_diff(config, parent_config)
    design["applied"] = bool(records)
    design["ops"] = _ops_summary(records)
    return {
        "genid": str(genid) if genid is not None else None,
        "role": role,
        "stage": stage,
        "design": design,
        "toolset": toolset or {},
        "code_patch": {
            "proposed": bool((patch or "").strip()),
            "applied": bool(patch_applied),
            "commit": commit,
            "rejected_reason": rejected_reason,
        },
        "budget": budget or {},
        "grants": grants or [],
        "trajectory_refs": trajectory_refs or [],
        "next_hint": _next_hint(rejected_reason),
    }


def render_receipt(receipt: Optional[Dict[str, Any]], max_chars: int = 1500) -> str:
    """Compact text form for injection into a prompt; empty if nothing to say."""
    if not receipt:
        return ""
    design = receipt.get("design") or {}
    cp = receipt.get("code_patch") or {}
    budget = receipt.get("budget") or {}
    parts: List[str] = []
    if design.get("applied"):
        ops = ", ".join(sorted({str(o.get("op")) for o in design.get("ops") or [] if o.get("op")}))
        parts.append(f"applied design changes: {ops or 'yes'}")
        if design.get("prompt_changed"):
            parts.append("prompt changed")
        if design.get("skills_added"):
            parts.append(f"skills added: {design['skills_added']}")
        if design.get("skills_removed"):
            parts.append(f"skills removed: {design['skills_removed']}")
    if cp.get("applied"):
        parts.append(f"code patch applied (commit {str(cp.get('commit'))[:8]})")
    elif cp.get("proposed") and not cp.get("applied"):
        parts.append(f"code patch REJECTED: {cp.get('rejected_reason')}")
    if budget.get("exhausted"):
        parts.append(f"budget exhausted ({budget.get('kind')}, attempts={budget.get('attempts')})")
    ts = receipt.get("toolset") or {}
    skipped = ts.get("skipped") or []
    if skipped:
        # B7: the role must see the design-vs-assembly gap in the channel it
        # consumes every decision round — the receipt.
        names = ", ".join(f"'{s.get('name')}' ({str(s.get('reason'))[:60]})"
                          for s in skipped)
        parts.append(f"capability note: design-selected tools SKIPPED at assembly: {names} "
                     f"— inspect with list_components; fix via register_component / "
                     f"select_component / deselect_component")
    if receipt.get("next_hint"):
        parts.append(f"hint: {receipt['next_hint']}")
    if not parts:
        return ""
    return "## Previous round receipt\n- " + "\n- ".join(parts)
