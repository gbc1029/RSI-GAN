"""Trajectory index builder (frozen core).

A structural index over trajectory JSONL files: one pass classifies every row
into a SEGMENT (turn text, tool call, tool output, plan operations, stop
record) and records metadata only — id, source file, line number, kind, tool,
size, token estimate, exception flag. NO content enters the index itself; the
materialization entry point (``build_traj_materialization``) returns segment
texts separately so the index payload and the content snapshot travel apart.

Segment addresses (``seg_id``) are ``<label>:<line>`` where the label is
CALLER-QUALIFIED — normally ``<genid>/<file>`` so one session holding several
generations' files keeps a unique address space. ``parse_seg_id`` is the
single parser for that form (a bare ``<file>:<line>`` without a genid part is
the legacy shape and only unambiguous for a single-file reader).

Serving faces (same scan, two consumers):
- the CM universe: ``materialize_traj`` registers each segment as a pinned
  ``traj_seg`` block — ``ctx_index``/``ctx_fetch`` address them like any block;
- the read tools: ``read_trajectory``/``read_session_trajectory`` render the
  index (metadata only) and materialize one segment on ``fetch``.

The material is invariant within a session (trajectories are archived before
they become readable), so both faces see the same segments for the session's
lifetime. Only VISIBLE files reach this module — the caller passes exactly the
files the seat may read; selection can only shrink that universe. stdlib-only.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

SEGMENT_KINDS = ("turn_output", "tool_call", "tool_output", "plan_ops",
                 "stop_record")
SEGMENT_FLAGS = ("truncated", "budget_stop", "repeat_break", "traceback")


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


def _scan_file(path: str, label: str) -> List[Dict[str, Any]]:
    """One pass over one file -> segment dicts (metadata + ``text``).

    Every segment is addressable by its ``seg_id`` (``<label>:<line>``) and
    carries its content under ``text`` — the caller projects metadata-only
    views from the same dicts, so classification never runs twice."""
    segs: List[Dict[str, Any]] = []
    for lineno, row in enumerate(_iter_lines(path), start=1):
        kind = _classify(row)
        if kind is None:
            continue
        spec = _segment_content(row)
        if spec is None:
            continue
        text, tool = spec
        segs.append({
            "seg_id": f"{label}:{lineno}",
            "file": label, "line_no": lineno, "kind": kind,
            "tool": tool,
            "chars": len(text), "token_est": len(text) // 4,
            "flag": _flag(row), "text": text,
        })
    return segs


def _pair(paths: List[str], labels: Optional[List[str]]) -> List[Tuple[str, str]]:
    labels = list(labels) if labels is not None else [os.path.basename(p) for p in paths]
    if len(labels) != len(paths):
        raise ValueError("labels must match paths one-to-one")
    return list(zip(paths, labels))


def build_traj_index(paths: List[str],
                     labels: Optional[List[str]] = None) -> Dict[str, Any]:
    """Structural index over the given (already-visible) trajectory files.

    Each segment: ``{seg_id, file, kind, chars, token_est, line_no, tool,
    flag}`` — metadata only, NO content. ``flag`` marks exceptions/errors so
    an index-wide scan can find failure points without reading bodies.
    ``labels`` qualifies each file's address space (genid-qualified labels
    keep several generations' files unambiguous in one index)."""
    segments: List[Dict[str, Any]] = []
    used: List[str] = []
    for path, label in _pair(paths, labels):
        if not path or not os.path.isfile(path):
            continue
        used.append(label)
        for seg in _scan_file(path, label):
            seg.pop("text", None)
            segments.append(seg)
    return {"files": used, "segments": segments,
            "total_chars": sum(s["chars"] for s in segments)}


def build_traj_materialization(files: List[Tuple[str, str]]
                               ) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Single-scan materialization source: (index payload, seg_id -> text).

    The payload is the SAME shape ``build_traj_index`` returns (metadata
    only); the texts travel separately so the caller registers content as
    blocks while the index face renders metadata. Missing files are skipped
    (and absent from ``files`` in the payload)."""
    paths = [p for _l, p in files]
    labels = [l for l, _p in files]
    segments: List[Dict[str, Any]] = []
    texts: Dict[str, str] = {}
    used: List[str] = []
    for path, label in _pair(paths, labels):
        if not path or not os.path.isfile(path):
            continue
        used.append(label)
        for seg in _scan_file(path, label):
            texts[seg["seg_id"]] = seg.pop("text")
            segments.append(seg)
    return ({"files": used, "segments": segments,
             "total_chars": sum(s["chars"] for s in segments)}, texts)


def parse_seg_id(text: str) -> Tuple[str, int]:
    """Split a segment address into (label, line_no).

    The canonical form is ``<label>:<line>`` with a genid-qualified label
    (``<genid>/<file>``); a bare ``<file>:<line>`` comes back with whatever
    label it carries and the caller resolves ambiguity against its own file
    set. Raises ValueError on a missing/non-integer line part — a malformed
    address must fail loudly, never fetch by guesswork."""
    s = str(text or "").strip()
    label, _, line = s.rpartition(":")
    if not line.isdigit():
        raise ValueError(f"bad seg_id {text!r} (expected '<label>:<line>')")
    return label, int(line)


def segment_entry(path: str, line_no: int) -> Optional[Dict[str, Any]]:
    """Materialize ONE segment with its metadata (the fetch of a traj_seg).

    Returns ``{kind, tool, chars, flag, text}`` for the addressed line, or
    None when the file/line is absent or the row does not classify — a
    stale address surfaces as a loud miss, never as neighboring content."""
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
            kind = _classify(row)
            if kind is None:
                return None
            spec = _segment_content(row)
            if spec is None:
                return None
            text, tool = spec
            return {"kind": kind, "tool": tool, "chars": len(text),
                    "flag": _flag(row), "text": text}
    return None


def segment_text(path: str, line_no: int) -> Optional[str]:
    """Materialize one segment's text by line number (metadata dropped)."""
    entry = segment_entry(path, line_no)
    return entry["text"] if entry else None


# ---------------------------------------------------------------------------
# slice front-end payload (what read_trajectory renders in slice mode)
# ---------------------------------------------------------------------------

def render_index_text(index: Dict[str, Any], max_segments: int = 80,
                      max_chars: int = 6000, kinds: Optional[List[str]] = None,
                      tools: Optional[List[str]] = None,
                      flags: Optional[List[str]] = None,
                      offset: int = 0) -> str:
    """Human/agent-readable index rendering (the slice-mode first response).

    Metadata only; every segment is addressable by its ``seg_id``. The
    kind/tool/flag filters and the offset window operate on the same segment
    list the fetch addresses — narrowing the view never changes the address
    space. The truncation footer names the REAL parameters (an index that
    advertises refinement paths the reader does not have is a lie)."""
    segs = index.get("segments", [])
    selected = [s for s in segs
                if (not kinds or s.get("kind") in kinds)
                and (not tools or s.get("tool") in tools)
                and (not flags or s.get("flag") in flags)]
    hidden_head = max(0, int(offset or 0))
    window = selected[hidden_head:hidden_head + max_segments]
    shown_chars = sum(s["chars"] for s in window)
    lines = [f"# trajectory index ({len(selected)} segments, "
             f"{shown_chars} chars shown of {index.get('total_chars', 0)} total)"]
    for seg in window:
        flag = f" [flag={seg['flag']}]" if seg.get("flag") else ""
        lines.append(f"- {seg['seg_id']} kind={seg['kind']} {seg['chars']}c"
                     + (f" tool={seg['tool']}" if seg.get("tool") else "")
                     + flag)
    hidden = len(selected) - hidden_head - len(window)
    if hidden > 0:
        lines.append(f"... {hidden} more segments "
                     "(narrow with kind=/tool=/flag=, page with offset=)")
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n...[index truncated; use filters/offset]"
    return out
