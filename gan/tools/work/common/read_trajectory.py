"""Work tool: read a prior task generation's (redacted) trajectory.

The visible set is resolved by the loop and passed via ``AccessContext
.trajectory_genids`` (no reliance on the node already being in the tree).
Default (no ``genid``) reads the first entry of that set.

Slice mode: when the SESSION's design ``cost.cm_mode`` is not ``passthrough``,
the default read returns the STRUCTURAL INDEX (segments with ids/sizes/flags,
no content) and ``fetch`` materializes one segment by ``seg_id`` — a
genid-qualified ``<genid>/<file>:<line>`` address. Segment addresses are the
SAME space the CM universe's ``traj_seg`` blocks use: when the session
materialized the trajectory index, both faces (this tool and
``ctx_index``/``ctx_fetch``) serve from that one scan; without a
materialization the tool builds its own index over the same visible files.
``kind=``/``tool=``/``flag=`` narrow the index and ``offset=`` pages it —
real parameters, matching the index footer. Selection can only shrink the
visible universe: the index exposes line-level metadata only, and fetch
resolves through the same visibility set.
"""
from __future__ import annotations

import os
from typing import Optional

from gan.framework.context import get_access_context


def tool_info():
    return {
        "name": "read_trajectory",
        "description": (
            "Read the (redacted) execution trajectory of a task generation that is visible "
            "to you (see the allowed genids in your prompt). Omit 'genid' for the default "
            "(current/parent). Benchmark scores / reports are redacted. In slice mode "
            "(your design's cost.cm_mode) the default response is a STRUCTURAL INDEX — "
            "narrow it with kind=/tool=/flag= and page with offset= — and "
            "fetch=<seg_id> materializes one segment's content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "genid": {"description": "Target generation id (must be visible)."},
                "max_chars": {"type": "integer"},
                "fetch": {"description": "Materialize one segment by seg_id "
                                        "(any mode: the slice index lists the "
                                        "seg_ids; without it the segment is "
                                        "resolved directly from the archived "
                                        "trajectory)."},
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


_CM_MODES = ("passthrough", "slice", "compact_conservative", "custom")


def _session_cm_mode(actx) -> Optional[str]:
    """The session's effective ``cost.cm_mode`` — PRESENTATION ONLY.

    Primary source: the loop's per-session stamp on the AccessContext (the
    session OWNER's design, already clamped by the frozen authority). Fallback:
    the session's design context (legacy wiring). This never touches
    permissions: the visible genid set and redaction are decided by
    ``AccessContext.trajectory_genids`` + the archive pipeline and stay
    untouched — selection can only shrink the universe."""
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


def _traj_path(output_dir, genid):
    from gan.framework import trajectory as traj
    return traj._session_file(output_dir, genid, "task")


def _active_traj_session():
    """The active CM session when it materialized a trajectory payload.

    The payload is the single scan both trajectory faces serve from (this
    tool's index/fetch and the ctx faces' traj_seg blocks); None means the
    tool builds its own index over the same visible files."""
    try:
        from gan.framework.cm.state import current_session
        s = current_session()
    except Exception:  # noqa: BLE001 -- advisory; self-build fallback
        return None
    if s is None or getattr(s, "traj_index", None) is None:
        return None
    return s


def _norm_filters(kind, tool, flag, offset):
    """Validate the index-filter parameters into canonical form (None drops
    a filter). Kind and flag draw on the frozen segment vocabulary; the tool
    filter is open-vocabulary (any tool name the rows may carry)."""
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


def _payload_segments(sess, genid):
    """The session payload's segments for ONE genid (label prefix match), or
    None when the snapshot does not cover it (caller self-builds)."""
    prefix = f"{genid}/"
    segs = [s for s in (sess.traj_index.get("segments") or [])
            if str(s.get("file", "")).startswith(prefix)]
    return segs or None


def _serve_index(output_dir, genid, cap, filt, sess, mode):
    """Slice-mode default read: the structural index for one genid."""
    segs = _payload_segments(sess, genid) if sess is not None else None
    if segs is not None:
        index = {"files": sorted({s["file"] for s in segs}),
                 "segments": segs, "total_chars": sum(s["chars"] for s in segs)}
    else:
        path = _traj_path(output_dir, genid)
        if not path or not os.path.isfile(path):
            return f"No archived trajectory for generation {genid}."
        from gan.framework.cm.traj_index import build_traj_index
        index = build_traj_index([path], labels=[f"{genid}/{os.path.basename(path)}"])
    from gan.framework.cm.traj_index import render_index_text
    header = (f"# trajectory index genid={genid} (mode={mode}, redacted; "
              "fetch=<seg_id> for content)")
    return header + "\n" + render_index_text(
        index, max_chars=cap, kinds=filt["kind"], tools=filt["tool"],
        flags=filt["flag"], offset=filt["offset"])


def _serve_fetch(output_dir, fetch, allowed, genid_param, cap, sess):
    """Slice-mode fetch: materialize one segment through the SAME address
    space the index rendered (session snapshot first, fresh read fallback)."""
    from gan.framework.cm.traj_index import parse_seg_id, segment_entry
    try:
        addr, line_no = parse_seg_id(fetch)
    except ValueError as e:
        return f"Error: {e}"
    if "/" in addr:
        genid, fname = addr.split("/", 1)
        if str(genid) not in [str(g) for g in allowed]:
            return f"Error: generation '{genid}' is not visible (allowed: {allowed})."
    else:
        genid = str(genid_param) if genid_param is not None else str(allowed[0])
        fname = addr
    if not fname:
        # bare line address: the genid's (single) trajectory file
        path0 = _traj_path(output_dir, genid)
        fname = os.path.basename(path0 or "")
        if not fname:
            return f"Error: segment '{fetch}' not found."
    seg_id = f"{genid}/{fname}:{line_no}"
    # session snapshot: the addressed segment must exist in the SAME scan the
    # index came from — an address outside it is refused, never re-resolved
    # against a possibly-changed file (content binding).
    if sess is not None:
        segs = _payload_segments(sess, genid)
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
    path = _traj_path(output_dir, genid)
    if fname and os.path.basename(path or "") != fname:
        return f"Error: segment '{seg_id}' not in the visible files."
    entry = segment_entry(path, line_no) if path else None
    if entry is None:
        return f"Error: segment '{seg_id}' not found."
    meta = (f"# segment {seg_id} kind={entry['kind']}"
            + (f" tool={entry['tool']}" if entry.get("tool") else "")
            + f" chars={entry['chars']}")
    return f"{meta}\n{entry['text'][:cap]}"


def tool_function(genid=None, max_chars=6000, fetch=None, kind=None, tool=None,
                  flag=None, offset=0, **kwargs):
    actx = get_access_context()
    if actx is None:
        return "Error: no access context"
    broker = actx.broker
    # Sandboxed seat workers carry the RPC broker (method present);
    # in-process sessions carry the plain AccessBroker and read files
    # directly. Both are valid -- only a missing broker is fatal.
    if broker is None:
        return "Error: parent trajectory reader is unavailable"
    allowed = list(getattr(actx, "trajectory_genids", []) or [])
    if not allowed:
        return "No task trajectory is visible for this session."
    if genid is None:
        genid = allowed[0]
    if genid not in allowed and str(genid) not in [str(g) for g in allowed]:
        return f"Error: generation '{genid}' is not visible (allowed: {allowed})."
    output_dir = getattr(actx.broker, "output_dir", None) if actx.broker is not None else None
    if hasattr(broker, "session_trajectory_index"):
        # Sandboxed seat worker: the blob crosses the parent-side RPC and
        # redaction stays parent-side; kind=/tool=/flag=/offset=/fetch= are
        # in-process slice-mode faces and degrade here (noted, never ignored).
        extra = ("# note: kind=/tool=/flag=/offset=/fetch= are in-process "
                 "slice-mode faces; a sandboxed seat worker reads the full "
                 "redacted blob\n") if (kind or tool or flag or offset or fetch) else ""
        blob = broker.read_trajectory(actx.role, actx.node_id, genid,
                                      int(max_chars or 6000), seat=actx.seat)
        if not blob:
            return f"No archived trajectory for generation {genid}."
        return extra + f"# generation {genid} trajectory\n{blob}"

    if not output_dir:
        return "Error: parent trajectory root is unavailable"
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
    sess = _active_traj_session() if slice_mode else None

    if slice_mode and not fetch:
        return _serve_index(output_dir, genid, cap, filt, sess, mode)
    if fetch:
        return _serve_fetch(output_dir, fetch, allowed, genid, cap, sess)

    blob = read(output_dir, genid, max_chars=cap, role="task")
    if not blob:
        return f"No archived trajectory for generation {genid}."
    return note + f"# trajectory genid={genid} (redacted)\n{blob}"


op_info = tool_info
op_function = tool_function
