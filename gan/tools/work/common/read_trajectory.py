"""Work tool: read a prior task generation's (redacted) trajectory.

The visible set is resolved by the loop and passed via ``AccessContext
.trajectory_genids`` (no reliance on the node already being in the tree).
Default (no ``genid``) reads the first entry of that set.

Slice mode (docs/10 §11 D9): when the SESSION's design ``cost.cm_mode`` is
not ``passthrough``, the default read returns the STRUCTURAL INDEX (segments
with ids/sizes/flags, no content) instead of the text blob; ``fetch`` then
materializes one segment by its ``seg_id`` (a line number into the archived
file). Selection can only shrink the visible universe — the index is built
from the same redacted file ``read`` would serve, and only line-level
metadata is exposed.
"""
from __future__ import annotations

import os
from typing import Optional

from gan.framework.context import get_access_context
from gan.framework.trajectory import read


def tool_info():
    return {
        "name": "read_trajectory",
        "description": (
            "Read the (redacted) execution trajectory of a task generation that is visible "
            "to you (see the allowed genids in your prompt). Omit 'genid' for the default "
            "(current/parent). Benchmark scores / reports are redacted. In slice mode "
            "(your design's cost.cm_mode) the default response is a STRUCTURAL INDEX; "
            "use fetch=<seg_id> to materialize one segment's content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "genid": {"description": "Target generation id (must be visible)."},
                "max_chars": {"type": "integer"},
                "fetch": {"description": "Materialize one segment by seg_id "
                                        "(slice mode only)."},
            },
        },
    }


_CM_MODES = ("passthrough", "slice", "compact_conservative", "custom")


def _session_cm_mode(actx) -> Optional[str]:
    """The session's effective ``cost.cm_mode`` — PRESENTATION ONLY.

    Primary source: the loop's per-session stamp on the AccessContext (the
    session OWNER's design, already clamped by the frozen authority; docs/10
    §11.5 档位归座). Fallback: the session's design context (legacy wiring).
    This never touches permissions: the visible genid set and redaction are
    decided by ``AccessContext.trajectory_genids`` + the archive pipeline and
    stay untouched — selection can only shrink the universe."""
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


def tool_function(genid=None, max_chars=6000, fetch=None, **kwargs):
    actx = get_access_context()
    if actx is None:
        return "Error: no access context"
    output_dir = getattr(actx.broker, "output_dir", None) if actx.broker is not None else None
    if not output_dir:
        return "Error: no output directory available"
    allowed = list(getattr(actx, "trajectory_genids", []) or [])
    if not allowed:
        return "No task trajectory is visible for this session."
    if genid is None:
        genid = allowed[0]
    if genid not in allowed and str(genid) not in [str(g) for g in allowed]:
        return f"Error: generation '{genid}' is not visible (allowed: {allowed})."
    cap = int(max_chars or 6000)

    mode = _session_cm_mode(actx)
    if mode in ("slice", "compact_conservative", "custom") and not fetch \
            and genid is not None:
        # slice front-end: structural index instead of the text blob
        path = _traj_path(output_dir, genid)
        if not path or not os.path.isfile(path):
            return f"No archived trajectory for generation {genid}."
        from gan.framework.cm.traj_index import build_traj_index, render_index_text
        index = build_traj_index([path])
        header = (f"# trajectory index genid={genid} (mode={mode}, redacted; "
                  "fetch=<seg_id> for content)")
        return f"{header}\n{render_index_text(index, max_chars=cap)}"
    if fetch:
        path = _traj_path(output_dir, genid)
        if not path or not os.path.isfile(path):
            return f"No archived trajectory for generation {genid}."
        try:
            line_no = int(str(fetch).rsplit(":", 1)[-1])
        except ValueError:
            return f"Error: bad seg_id '{fetch}' (expected '<file>:<line>')."
        from gan.framework.cm.traj_index import segment_text
        text = segment_text(path, line_no)
        if text is None:
            return f"Error: segment '{fetch}' not found."
        text = text[:cap]
        return f"# segment {fetch}\n{text}"

    blob = read(output_dir, genid, max_chars=cap, role="task")
    if not blob:
        return f"No archived trajectory for generation {genid}."
    return f"# trajectory genid={genid} (redacted)\n{blob}"


op_info = tool_info
op_function = tool_function