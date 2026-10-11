"""Trajectory index builder (docs/10 §5.2.1, 冻结).

The structured index over a session's OWN trajectory file (p/e read -
session_trajectory face) or over the visible task trajectory files
(read_trajectory face). Built ONCE per session by the caller (the material
is invariant within a session: trajectories are archived before evaluation),
then served through ``ctx_index``/``fetch`` — the ``index`` primitive on the
trajectory source.

Only VISIBLE blocks (``trajectory_genids`` ∩ redacted files) enter the
index; selection can only shrink the universe (§5.2 权限统一). stdlib-only.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

SEGMENT_KINDS = ("turn_output", "tool_call", "tool_output", "plan_ops",
                 "stop_record")


def _iter_lines(path: str):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _classify(row: Dict[str, Any]) -> Optional[str]:
    kind = row.get("kind")
    if kind in ("input", "output"):
        return "turn_output"
    if kind == "tool_call":
        return "tool_call"
    if kind in ("tool_output", "tool_stdout", "tool_stderr",
                "tool_output_truncated"):
        return "tool_output"
    if kind == "plan_operations":
        return "plan_ops"
    if kind in ("stop_record", "token_budget_stop", "tool_repeat_break"):
        return "stop_record"
    return None


def build_traj_index(paths: List[str]) -> Dict[str, Any]:
    """Structural index over the given (already-visible) trajectory files.

    Each segment: ``{seg_id, file, kind, chars, token_est, line_no, tool,
    flag}`` — metadata only, NO content. ``flag`` marks exceptions/errors so
    an index-wide scan can find failure points without reading bodies."""
    segments: List[Dict[str, Any]] = []
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        rel = os.path.basename(path)
        for lineno, row in enumerate(_iter_lines(path), start=1):
            kind = _classify(row)
            if kind is None:
                continue
            spec = _segment_content(row)
            if spec is None:
                continue
            text, tool = spec
            segments.append({
                "seg_id": f"{rel}:{lineno}",
                "file": rel, "line_no": lineno, "kind": kind,
                "tool": tool,
                "chars": len(text), "token_est": len(text) // 4,
                "flag": _flag(row),
            })
    return {"files": [os.path.basename(p) for p in paths if os.path.isfile(p)],
            "segments": segments, "total_chars": sum(s["chars"] for s in segments)}


def _segment_content(row: Dict[str, Any]) -> Optional[tuple]:
    kind = row.get("kind")
    if kind == "input":
        return str(row.get("text") or ""), None
    if kind == "output":
        return str(row.get("text") or ""), None
    if kind == "tool_call":
        return json.dumps(row.get("input") or {}, ensure_ascii=False), row.get("tool")
    if kind in ("tool_output", "tool_stdout", "tool_stderr"):
        return str(row.get("output") or row.get("text") or ""), row.get("tool")
    if kind == "tool_output_truncated":
        return str(row.get("original_chars") or ""), row.get("tool")
    if kind == "plan_operations":
        return json.dumps(row.get("operations") or [], ensure_ascii=False), None
    if kind in ("stop_record", "token_budget_stop", "tool_repeat_break"):
        return json.dumps(row, ensure_ascii=False), None
    return None


def _flag(row: Dict[str, Any]) -> Optional[str]:
    if row.get("kind") == "tool_output_truncated":
        return "truncated"
    if row.get("kind") == "token_budget_stop":
        return "budget_stop"
    if row.get("kind") == "tool_repeat_break":
        return "repeat_break"
    if str(row.get("kind")) == "tool_output" and "Traceback" in str(row.get("output") or ""):
        return "traceback"
    return None


def segment_text(path: str, line_no: int) -> Optional[str]:
    """Materialize one segment (the fetch of a traj_seg) by line number."""
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            if i != line_no:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                return None
            spec = _segment_content(row)
            return spec[0] if spec else None
    return None


# ---------------------------------------------------------------------------
# slice front-end payload (what read_trajectory renders in slice mode)
# ---------------------------------------------------------------------------

def render_index_text(index: Dict[str, Any], max_segments: int = 80,
                      max_chars: int = 6000) -> str:
    """Human/agent-readable index rendering (the slice-mode first response).
    Metadata only; every segment is addressable by its ``seg_id``."""
    lines = [f"# trajectory index ({len(index.get('segments', []))} segments, "
             f"{index.get('total_chars', 0)} chars total)"]
    for seg in index.get("segments", [])[:max_segments]:
        flag = f" [flag={seg['flag']}]" if seg.get("flag") else ""
        lines.append(f"- {seg['seg_id']} kind={seg['kind']} {seg['chars']}c"
                     + (f" tool={seg['tool']}" if seg.get("tool") else "")
                     + flag)
    if len(index.get("segments", [])) > max_segments:
        lines.append(f"... {len(index['segments']) - max_segments} more segments "
                     "(refine with kind=/tool= filters)")
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n...[index truncated; use filters]"
    return out
