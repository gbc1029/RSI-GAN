"""Sanitized diff summary + feedback schema for role isolation.

The evaluator must NOT receive the planner's free-text rationale (prompt
injection channel). It only receives a structural summary: which operators were
applied (ops-only since B9, batch 32).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from gan.framework.receipt import PROMPT_FACT_KEYS  # B29 prompt facts

FEEDBACK_SCHEMA_VERSION = "v1"


def build_diff_summary(
    plan_records: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """B9 (batch 32): the summary is OPS-ONLY.

    ``files`` exited and the embedded ``schema_version`` dead key with it:
    - "which files changed" is an OUTCOME fact owned by ``task_patch_files``
      (computed from the actually-landed patch), not by this intent-level
      projection -- a second copy here could only diverge;
    - grant paths under a "changes" label told the evaluator that VIEWED
      paths (often whole directories) and the evidence file (``report.json``)
      were "changed" -- active misinformation, not just redundancy (B32).
    Grant facts ride ``ops[].paths`` under the op's own semantics; the caller
    no longer feeds ``report_path`` (the parameter is gone, not merely unused).
    """
    ops: List[Dict[str, Any]] = []
    # L4 (batch 24): the six dead-operator parsers (tune_param / apply_config /
    # add_config / set_tool_enabled / swap_module / code_edit) are DELETED --
    # no producer exists since the registry unification (batches 6/10), so the
    # branches could only ever render `{"op": <dead>, "key": null}`-style ghost
    # rows. Unknown ops fall through to the passthrough below ({"op": name}),
    # and structured fields ride explicitly-named branches only (extend for
    # LIVE ops on purpose; update_component/unregister_component carry their
    # position facts in the receipt, so no branch is warranted yet).
    for r in plan_records or []:
        op = r.get("op")
        entry: Dict[str, Any] = {"op": op}
        if op == "set_config":
            # B14 minimal subset (batch 5): the live operator's structured fields.
            entry["key"] = r.get("key")
            if r.get("selected") is not None:
                entry["selected"] = list(r.get("selected") or [])
        elif op in ("select_component", "deselect_component"):
            entry["slot"] = r.get("slot")
            entry["name"] = r.get("name")
        elif op == "request_source_access":
            entry["paths"] = list(r.get("paths", []) or [])  # reason intentionally stripped
        elif op == "edit_source":
            # Batch 23: the deep edit surface now records its mutations. Same
            # branch shape as request_source_access: structured position facts
            # only (workspace REL -- the口径 the patch builder and covers()
            # already consume); the free text/grep evidence channels stay out.
            if r.get("command"):
                entry["command"] = r["command"]
            if r.get("path"):
                entry["path"] = r["path"]
        # B29: prompt facts ride whichever op produced them (set_prompt, or
        # set_config with key="prompt"); copied by presence, default-deny.
        for k in PROMPT_FACT_KEYS:
            if k in r:
                entry[k] = r[k]
        ops.append(entry)
    return {"ops": ops}


# B13 (batch 8): the ONLY planner→evaluator projection of issue responses.
# `feedback` is the planner's rationale (audit-only: it stays in the planner's
# session record and events) and is dropped HERE — before the reward module
# sees the data — so the digest builder physically cannot leak it, no matter
# how it is refactored. Derived statistics of the text (length etc.) are
# equally out of scope: any function of the raw text keeps the text in the
# pipeline. Legacy/missing stances normalize to "unspecified" rather than
# masquerading as a real stance.
_RESPONSE_KEYS_FOR_EVALUATOR = ("issue_id", "accepted", "response_kind")


def project_responses_for_evaluator(
    responses: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in (responses or []):
        if not isinstance(r, dict):
            continue
        item = {k: r.get(k) for k in _RESPONSE_KEYS_FOR_EVALUATOR}
        item["response_kind"] = str(item.get("response_kind") or "unspecified")
        out.append(item)
    return out


def make_feedback(
    issues: Optional[List[Dict[str, Any]]] = None,
    diff_summary: Optional[Dict[str, Any]] = None,
    penalties: Optional[Dict[str, Any]] = None,
    schema_version: str = FEEDBACK_SCHEMA_VERSION,
) -> Dict[str, Any]:
    return {
        "schema_version": schema_version,
        "issues": list(issues or []),
        "diff_summary": diff_summary or {},
        "penalties": dict(penalties or {}),
    }


def validate_feedback(feedback: Optional[Dict[str, Any]]) -> bool:
    if not feedback:
        return True
    return feedback.get("schema_version") == FEEDBACK_SCHEMA_VERSION
