"""Work tool: read YOUR OWN session trajectories for the current outer.

Available to planner/evaluator during self-improvement. Sessions are the role's
own ``plan``/``evaluate`` sessions for each inner generation of the current outer
(``trajectory/outer_<O>/<genid>/<role>.jsonl``), redacted. Omit ``genid`` to read
all of this outer's sessions (bounded).

Slice mode (docs/10 §11 D9): when the SESSION's own design ``cost.cm_mode``
is not ``passthrough``, the default response is the STRUCTURAL INDEX of the
selected session file(s) — ids/sizes/flags, no content — and ``fetch`` then
materializes one segment by ``seg_id``. Only metadata; selection only shrinks
the visible surface.
"""
from __future__ import annotations

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
            "STRUCTURAL INDEX; use fetch=<seg_id> to materialize one segment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "genid": {"description": "Inner generation id. Omit for all of this outer."},
                "max_chars": {"type": "integer"},
                "fetch": {"description": "Materialize one segment by seg_id "
                                        "(slice mode only)."},
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
    session OWNER's design; docs/10 §11.5 档位归座). Fallback: the session's
    design context (legacy wiring). Never a permission surface: the visible
    session files and redaction stay untouched."""
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


def _session_paths(output_dir, outer, role, genid):
    """The visible session file(s) for this read (canonical + attempt
    siblings, newest first — mirrors trajectory.read_session)."""
    import glob
    d = os.path.join(output_dir, "trajectory", f"outer_{outer}")
    if genid is not None:
        pats = [os.path.join(d, str(genid), f"{role}*.jsonl")]
    else:
        pats = [os.path.join(d, str(e["genid"]), f"{role}*.jsonl")
                for e in (outer_session_index(output_dir, outer, role))]
    hits = []
    for pat in pats:
        hits += sorted(glob.glob(pat), reverse=True)
    return hits


def tool_function(genid=None, max_chars=6000, fetch=None, **kwargs):
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
    if mode in ("slice", "compact_conservative", "custom"):
        paths = _session_paths(output_dir, outer, role, genid)
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            return "No session trajectories for this outer yet."
        from gan.framework.cm.traj_index import (build_traj_index,
                                                 render_index_text,
                                                 segment_text)
        if fetch:
            fname, _, line = str(fetch).rpartition(":")
            try:
                line_no = int(line)
            except ValueError:
                return f"Error: bad seg_id '{fetch}' (expected '<file>:<line>')."
            src = next((p for p in paths
                        if os.path.basename(p) == fname), None)
            if src is None:
                return f"Error: segment '{fetch}' not in the visible sessions."
            text = segment_text(src, line_no)
            if text is None:
                return f"Error: segment '{fetch}' not found."
            return f"# segment {fetch}\n{text[:cap]}"
        index = build_traj_index(paths)
        header = (f"# your session index (mode={mode}, redacted; "
                  "fetch=<seg_id> for content)")
        return f"{header}\n{render_index_text(index, max_chars=cap)}"

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
