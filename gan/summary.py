"""Sanitized diff summary + feedback schema for role isolation.

The evaluator must NOT receive the planner's free-text rationale (prompt
injection channel). It only receives a structural summary: which operators were
applied and which files changed.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

FEEDBACK_SCHEMA_VERSION = "v1"


def build_diff_summary(
    plan_records: Optional[List[Dict[str, Any]]] = None,
    patch_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    ops: List[Dict[str, Any]] = []
    files: set = set()
    for r in plan_records or []:
        op = r.get("op")
        entry: Dict[str, Any] = {"op": op}
        if op in ("tune_param", "apply_config"):
            entry["key"] = r.get("key")
        elif op == "set_prompt":
            entry["section"] = r.get("section")
        elif op == "set_tool_enabled":
            entry["name"] = r.get("name")
            entry["enabled"] = r.get("enabled")
        elif op == "swap_module":
            entry["module_a"] = r.get("module_a")
            entry["module_b"] = r.get("module_b")
        elif op == "add_config":
            entry["key"] = r.get("key")            # rationale intentionally stripped
        elif op in ("code_edit", "request_source_access"):
            entry["paths"] = list(r.get("paths", []) or [])  # reason intentionally stripped
            for p in r.get("paths", []) or []:
                files.add(p)
        ops.append(entry)
    for f in patch_files or []:
        files.add(os.path.basename(f))
    return {"schema_version": FEEDBACK_SCHEMA_VERSION, "ops": ops, "files": sorted(files)}


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
