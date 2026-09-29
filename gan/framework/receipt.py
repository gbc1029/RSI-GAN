"""Round receipt (FRAMEWORK, frozen).

A deterministic, structured summary of what a session/round changed -- used to
tell the *next* round both what succeeded (shallow design changes that DID take
effect) and what failed (a rejected code patch + why), plus budget state and
pointers to the attempt's evidence. No LLM involved.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


# B29 (batch 15): the STRUCTURAL vocabulary of a prompt rewrite. Deliberately
# numbers/hashes only -- never text, never a substring, never a reversible
# encoding. This is the *artifact* being evolved (like source code, for which
# `files`/diffstat are already exposed), NOT the planner's rationale: the batch-8
# rule that even derived statistics of `respond_issue.feedback` stay out is NOT
# relaxed by this list.
PROMPT_FACT_KEYS = (
    "target_role", "changed", "chars", "prev_chars", "lines", "prev_lines",
    "added_lines", "removed_lines", "similarity", "changed_from", "changed_to",
    "sha256_12", "prev_sha256_12", "empty", "equals_seed",
)


def prompt_change_facts(prev: Any, new: Any, seed: Any = None) -> Dict[str, Any]:
    """Structural facts of a prompt rewrite (B29).

    Computed AT RECORD TIME on purpose: the projections only receive ``records``
    (no design config, no previous value), so the facts must be self-contained.
    ``prev`` is the value in the session's design draft before the operator
    overwrote it -- exactly the inherited prompt.

    No text leaves this function. See ``PROMPT_FACT_KEYS`` for why that line is
    drawn here and not at ``respond_issue.feedback``.

    ``changed_from``/``changed_to`` are 1-based line numbers on the **old** prompt
    (``0`` = no change). A pure insertion has an EMPTY span: ``changed_to ==
    changed_from - 1`` (e.g. appending after line 3 reports ``from=4, to=3``) --
    that is the honest reading "inserted after line 3", not a claim that a line
    changed. ``equals_seed`` is ``None`` when the caller did not supply the seed
    (unknown, never a false ``False``).
    """
    import difflib
    import hashlib

    def _sha12(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

    a_s, b_s = str(prev or ""), str(new or "")
    a, b = a_s.splitlines(), b_s.splitlines()
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    added = removed = 0
    first = last = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
        if first == 0:
            first = i1 + 1          # 1-based; 0 = no change
        last = max(last, i2)
    seed_s = None if seed is None else str(seed)
    return {
        "changed": a_s != b_s,
        "chars": len(b_s), "prev_chars": len(a_s),
        "lines": len(b), "prev_lines": len(a),
        "added_lines": added, "removed_lines": removed,
        "similarity": (round(sm.ratio(), 2) if (a or b) else 1.0),
        "changed_from": first, "changed_to": last,
        "sha256_12": _sha12(b_s), "prev_sha256_12": _sha12(a_s),
        "empty": not b_s.strip(),
        "equals_seed": (None if seed_s is None else (b_s == seed_s)),
    }


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
        # B29: prompt facts are copied ONLY when a producer recorded them
        # (default-deny by presence, like every other projection here)
        for k in PROMPT_FACT_KEYS:
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
    component_drift: Optional[List[Dict[str, Any]]] = None,
    catalog_stale: Optional[List[Dict[str, Any]]] = None,
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
        # B30 (b)/(c-L4): catalog bookkeeping for the role that can act on it.
        # Deliberately NOT in _receipt_for_evaluator: a stale description cannot
        # change this generation's capability or score, so it is not a task fact.
        "component_drift": list(component_drift or []),
        "catalog_stale": list(catalog_stale or []),
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
        for o in (design.get("ops") or []):
            if o.get("op") in ("set_prompt", "set_config") and o.get("changed"):
                parts.append(
                    f"prompt rewritten: {o.get('prev_lines')}->{o.get('lines')} lines, "
                    f"+{o.get('added_lines')}/-{o.get('removed_lines')}, "
                    f"similarity {o.get('similarity')}"
                    + (f" (reset to the {o.get('target_role')} seed)"
                       if o.get("equals_seed") else ""))
                break
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
    drift = receipt.get("component_drift") or []
    if drift:
        names = ", ".join(
            f"'{d.get('name')}'" + ("" if d.get("description_stale_after") is False
                                    else " (catalog description now stale)")
            for d in drift)
        parts.append(f"catalog note: this patch changed registered component(s) {names} "
                     f"— call update_component if the change is user-visible "
                     f"(otherwise the description keeps describing the old behavior)")
    elif receipt.get("catalog_stale"):
        parts.append(f"catalog note: {len(receipt['catalog_stale'])} component(s) have a "
                     f"STALE catalog description (module changed after the description "
                     f"was written) — update_component refreshes it")
    if receipt.get("next_hint"):
        parts.append(f"hint: {receipt['next_hint']}")
    if not parts:
        return ""
    return "## Previous round receipt\n- " + "\n- ".join(parts)
