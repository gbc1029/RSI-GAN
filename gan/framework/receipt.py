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


# B12 (batch 8): grants ride into the receipt WITHOUT agent-authored free text.
# The allowlist of structural fields kept for the receipt's consumers.
_GRANT_KEYS_FOR_RECEIPT = ("role", "paths", "skipped", "missing", "intent")


def _grants_summary(grants: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Grants WITHOUT the agent-authored free text ``reason`` (audit-only field).

    ``reason`` serves human authorization audit in ``events.jsonl`` and must not
    ride back into any decision context: the receipt is persisted per node
    (``patch_receipt.json`` / ``child.meta["receipt"]``) and its renderer feeds
    the planner's next prompt. Structural facts (paths/intent/skipped/missing)
    stay: the agent needs them to know what it can still touch. Mirror of
    ``_ops_summary`` (rationale stripped) and ``build_diff_summary`` (reason
    stripped). ``denied`` is dropped as well — its reasons were never surfaced
    to agents.
    """
    out: List[Dict[str, Any]] = []
    for g in grants or []:
        if not isinstance(g, dict):
            continue
        out.append({k: g.get(k) for k in _GRANT_KEYS_FOR_RECEIPT if k in g})
    return out


def _design_diff(config: Optional[Dict[str, Any]], parent_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = config if isinstance(config, dict) else {}
    par = parent_config if isinstance(parent_config, dict) else {}
    # batch 6: one ``tools`` slot per role; accept the legacy keys so a diff
    # against an older run's parent config still reports its selections
    def _tools(d):
        v = d.get("tools")
        if isinstance(v, (list, tuple)) and v:
            return set(str(x) for x in v)
        v = (d.get("skills") or []) + (d.get("eval_points") or [])
        return set(str(x) for x in v)
    before, after = _tools(par), _tools(cfg)
    return {
        "prompt_changed": (cfg.get("prompt") != par.get("prompt")),
        "tools_added": sorted(after - before),
        "tools_removed": sorted(before - after),
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
    design_stripped: Optional[List[Dict[str, Any]]] = None,
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
        # H11/B24 (batch 5): slot names the framework removed from the design
        # right before persisting, because the committed tree cannot deliver them
        # (a patch that was rejected, or an inherited dangling reference).
        "design_stripped": design_stripped or [],
        "code_patch": {
            "proposed": bool((patch or "").strip()),
            "applied": bool(patch_applied),
            "commit": commit,
            "rejected_reason": rejected_reason,
        },
        "budget": budget or {},
        "grants": _grants_summary(grants),
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
        if design.get("tools_added"):
            parts.append(f"tools added: {design['tools_added']}")
        if design.get("tools_removed"):
            parts.append(f"tools removed: {design['tools_removed']}")
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
    stripped = receipt.get("design_stripped") or []
    if stripped:
        # H11/B24: the design must never keep claiming what the tree cannot
        # deliver; the role sees exactly what was dropped and why.
        snames = ", ".join(f"'{s.get('name')}' ({str(s.get('reason'))[:60]})"
                           for s in stripped)
        parts.append(f"design note: dangling slot names STRIPPED before persist: {snames} "
                     f"— re-add only after the component is committed "
                     f"(register_component + patch)")
    if receipt.get("next_hint"):
        parts.append(f"hint: {receipt['next_hint']}")
    if not parts:
        return ""
    return "## Previous round receipt\n- " + "\n- ".join(parts)
