"""Work tool: read YOUR OWN session trajectories for the current outer.

Available to planner/evaluator during self-improvement. Sessions are the role's
own ``plan``/``evaluate`` sessions for each inner generation of the current outer
(``trajectory/outer_<O>/<genid>/<role>.jsonl``), redacted. Omit ``genid`` to read
all of this outer's sessions (bounded).

Slice mode: when the SESSION's own design ``cost.cm_mode`` is not
``passthrough``, the default response is the STRUCTURAL INDEX of the selected
session file(s) — ids/sizes/flags, no content — and ``fetch`` materializes one
segment by ``seg_id`` (a genid-qualified ``<genid>/<file>:<line>`` address;
bare file names are refused when several visible files share one basename).
Segment addresses are the SAME space the CM universe's ``traj_seg`` blocks
use: a materialized session serves both faces from one scan; without one the
tool scans the same visible files itself. ``kind=``/``tool=``/``flag=`` narrow
the index, ``offset=`` pages it. Only metadata in the index; selection only
shrinks the visible surface.
"""
from __future__ import annotations

import glob
import os

from gan.framework.context import get_access_context
from gan.framework.trajectory import outer_session_index, read_session


def tool_info():
    return {
        "name": "read_session_trajectory",
        "description": (
            "Read YOUR OWN session trajectories from the current outer generation "
            "(redacted). Omit 'genid' to read all of this outer's sessions. In "
            "slice mode (your design's cost.cm_mode) the default response is a "
            "STRUCTURAL INDEX — narrow it with kind=/tool=/flag= and page with "
            "offset= — and fetch=<seg_id> materializes one segment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "genid": {"description": "Inner generation id. Omit for all of this outer."},
                "max_chars": {"type": "integer"},
                "fetch": {"description": "Materialize one segment by seg_id "
                                        "(slice mode only)."},
                "kind": {"description": "Index filter: segment kind "
                                        "(turn_output|tool_call|tool_output|plan_ops|stop_record)."},
                "tool": {"description": "Index filter: tool name."},
                "flag": {"description": "Index filter: exception flag "
                                        "(traceback|budget_stop|repeat_break|truncated)."},
                "offset": {"type": "integer",
                           "description": "Index pagination: skip this many matching segments."},
            },
        },
    }


def _outer_of(actx):
    node = str(getattr(actx, "node_id", ""))
    if node.startswith("outer_"):
        return node.split("_", 1)[1]
    return None


_CM_MODES = ("passthrough", "slice", "compact_conservative", "custom")


def _session_cm_mode(actx) -> str | None:
    """The session's effective ``cost.cm_mode`` — PRESENTATION ONLY.

    Primary source: the loop's per-session stamp on the AccessContext (the
    session OWNER's design). Fallback: the session's design context (legacy
    wiring). Never a permission surface: the visible session files and
    redaction stay untouched."""
    try:
        stamped = getattr(actx, "session_cm_mode", None)
        if isinstance(stamped, str) and stamped in _CM_MODES:
            return stamped
        from gan.framework.context import get_design_context
        from gan.framework.cost_keys import cost_config
        dctx = get_design_context()
        if dctx is None:
            return None
        mode = cost_config(dctx.config).get("cm_mode")
        if isinstance(mode, str) and mode in _CM_MODES:
            return mode
    except Exception:  # noqa: BLE001 -- advisory; degrade to passthrough
        return None
    return None


def _session_entries(output_dir, outer, role, genid):
    """The visible session files as ``(label, path)`` pairs.

    Labels are genid-qualified (``<genid>/<file>``) so the index and every
    seg_id stay unambiguous across generations — several generations' files
    share basenames, and an address that cannot say WHICH generation it means
    would fetch from the wrong one. Canonical + attempt siblings per genid,
    newest first (mirrors trajectory.read_session)."""
    d = os.path.join(output_dir, "trajectory", f"outer_{outer}")
    if genid is not None:
        genids = [str(genid)]
    else:
        genids = [str(e["genid"])
                  for e in outer_session_index(output_dir, outer, role)]
    out = []
    for g in genids:
        for p in sorted(glob.glob(os.path.join(d, g, f"{role}*.jsonl")),
                        reverse=True):
            if os.path.isfile(p):
                out.append((f"{g}/{os.path.basename(p)}", p))
    return out


def _active_traj_session():
    """The active CM session when it materialized a trajectory payload (the
    single scan this tool's index/fetch and the ctx faces serve from)."""
    try:
        from gan.framework.cm.state import current_session
        s = current_session()
    except Exception:  # noqa: BLE001 -- advisory; self-build fallback
        return None
    if s is None or getattr(s, "traj_index", None) is None:
        return None
    return s


def _payload_segments(sess, labels):
    """The session payload's segments for the given label set, or None when
    the snapshot does not cover them (caller self-builds)."""
    want = {label for label, _p in labels}
    segs = [s for s in (sess.traj_index.get("segments") or [])
            if s.get("file") in want]
    return segs or None


def _norm_filters(kind, tool, flag, offset):
    """Validate the index-filter parameters (kind/flag draw on the frozen
    segment vocabulary; the tool filter is open-vocabulary)."""
    from gan.framework.cm.traj_index import SEGMENT_KINDS, SEGMENT_FLAGS
    def _one(v, allowed):
        if v is None or v == "":
            return None
        vals = v if isinstance(v, (list, tuple)) else [v]
        vals = [str(x) for x in vals if str(x)]
        if allowed:
            bad = [x for x in vals if x not in allowed]
            if bad:
                raise ValueError(f"unknown filter value {bad[0]!r} (allowed: {list(allowed)})")
        return vals or None
    try:
        off = int(offset or 0)
    except (TypeError, ValueError):
        raise ValueError("offset must be an integer")
    if off < 0:
        raise ValueError("offset must be >= 0")
    return {"kind": _one(kind, SEGMENT_KINDS), "tool": _one(tool, None),
            "flag": _one(flag, SEGMENT_FLAGS), "offset": off}


def tool_function(genid=None, max_chars=6000, fetch=None, kind=None, tool=None,
                  flag=None, offset=0, **kwargs):
    actx = get_access_context()
    if actx is None:
        return "Error: no access context"
    output_dir = getattr(actx.broker, "output_dir", None) if actx.broker is not None else None
    if not output_dir:
        return "Error: no output directory available"
    outer = _outer_of(actx)
    if outer is None:
        return "Error: session trajectories are only available during self-improvement."
    role = actx.role
    cap = int(max_chars or 6000)

    mode = _session_cm_mode(actx)
    slice_mode = mode in ("slice", "compact_conservative", "custom")
    try:
        filt = _norm_filters(kind, tool, flag, offset)
    except ValueError as e:
        return f"Error: {e}"
    has_filter = any(filt[k] is not None for k in ("kind", "tool", "flag")) \
        or filt["offset"] > 0
    note = ""
    if not slice_mode and has_filter:
        note = (f"# note: kind=/tool=/flag=/offset= apply in slice mode only "
                f"(this session runs {mode or 'passthrough'})\n")

    entries = _session_entries(output_dir, outer, role, genid)
    labels = {label for label, _p in entries}

    if slice_mode and not fetch:
        paths = [p for _l, p in entries]
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            return "No session trajectories for this outer yet."
        sess = _active_traj_session()
        segs = _payload_segments(sess, entries) if sess is not None else None
        if segs is not None:
            index = {"files": sorted({s["file"] for s in segs}),
                     "segments": segs,
                     "total_chars": sum(s["chars"] for s in segs)}
        else:
            from gan.framework.cm.traj_index import build_traj_index
            index = build_traj_index(paths, labels=sorted(labels))
        from gan.framework.cm.traj_index import render_index_text
        header = (f"# your session index (mode={mode}, redacted; "
                  "fetch=<seg_id> for content)")
        return header + "\n" + render_index_text(
            index, max_chars=cap, kinds=filt["kind"], tools=filt["tool"],
            flags=filt["flag"], offset=filt["offset"])

    if fetch:
        from gan.framework.cm.traj_index import parse_seg_id, segment_entry
        try:
            addr, line_no = parse_seg_id(fetch)
        except ValueError as e:
            return f"Error: {e}"
        if addr not in labels:
            legacy = [label for label in labels
                      if label.rsplit("/", 1)[-1] == addr]
            if len(legacy) == 1:
                addr = legacy[0]
            elif len(legacy) > 1:
                return (f"Error: seg_id '{fetch}' is ambiguous across "
                        f"{len(legacy)} visible sessions sharing the basename "
                        "'<file>'; use the genid-qualified "
                        "'<genid>/<file>:<line>'.")
            elif not addr:
                if len(labels) == 1:
                    addr = next(iter(labels))
                else:
                    return (f"Error: bare seg_id '{fetch}' is ambiguous across "
                            f"{len(labels)} visible sessions; use "
                            "'<genid>/<file>:<line>'.")
            else:
                return f"Error: segment '{fetch}' not in the visible sessions."
        seg_id = f"{addr}:{line_no}"
        sess = _active_traj_session()
        if sess is not None:
            segs = _payload_segments(sess, [(label, "") for label in labels])
            if segs is not None:
                seg = next((s for s in segs if s["seg_id"] == seg_id), None)
                if seg is None:
                    return (f"Error: segment '{seg_id}' is not in this session's "
                            "materialized index (re-read the index; the file may "
                            "have changed).")
                bid = sess.traj_segments.get(seg_id)
                block = sess.universe.get(bid) if bid else None
                if block is not None:
                    meta = (f"# segment {seg_id} kind={seg['kind']}"
                            + (f" tool={seg['tool']}" if seg.get("tool") else "")
                            + f" chars={seg['chars']}")
                    return f"{meta}\n{block.text[:cap]}"
        src = dict(entries).get(addr)
        if src is None:
            return f"Error: segment '{fetch}' not in the visible sessions."
        entry = segment_entry(src, line_no)
        if entry is None:
            return f"Error: segment '{seg_id}' not found."
        meta = (f"# segment {seg_id} kind={entry['kind']}"
                + (f" tool={entry['tool']}" if entry.get("tool") else "")
                + f" chars={entry['chars']}")
        return f"{meta}\n{entry['text'][:cap]}"

    if genid is not None:
        blob = read_session(output_dir, outer, genid, role, cap)
        if not blob:
            return f"No session trajectory for genid {genid}."
        return f"# your session genid={genid}\n{blob}"
    idx = outer_session_index(output_dir, outer, role)
    if not idx:
        return "No session trajectories for this outer yet."
    blob = "\n\n".join(
        read_session(output_dir, outer, e["genid"], role, cap) for e in idx
    )
    return f"# your sessions this outer: {[e['genid'] for e in idx]}\n{blob[:cap]}"


op_info = tool_info
op_function = tool_function
