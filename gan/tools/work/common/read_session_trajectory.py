"""Work tool: read YOUR OWN session trajectories over the DECLARED outers.

Available to planner/evaluator during self-improvement. Sessions are the role's
own ``plan``/``evaluate`` sessions per inner generation of the visible outers
(``trajectory/outer_<O>/<genid>/<role>.jsonl``), redacted. The visible outer
set is DECLARED by the framework (``AccessContext.session_outers`` — the self
seat: this outer + the previous outer); omit ``genid`` to read all of them
(bounded). ``genid`` accepts an optional ``outer_<O>/`` prefix (the same
address space as the labels); a bare genid means the current outer.

Slice mode: when the SESSION's own design ``cost.cm_mode`` is not
``passthrough``, the default response is the STRUCTURAL INDEX of the selected
session file(s) — ids/sizes/flags, no content — and ``fetch`` materializes one
segment by ``seg_id`` (a qualified ``[outer_<O>/]<genid>/<file>:<line>`
address; bare file names are refused when several visible files share one
basename). Segment addresses are the SAME space the CM universe's ``traj_seg``
blocks use: a materialized session serves both faces from one scan; without
one the tool scans the same declared files itself. ``kind=``/``tool=``/
``flag=`` narrow the index, ``offset=`` pages it. Only metadata in the index;
selection only shrinks the visible surface.

Quarantined (unredactable) sessions are REPORTED in BOTH modes — a
``[unavailable]`` refusal inline (passthrough) or a quarantine note on the
index (slice) — never served: the absence stays attributable.
"""
from __future__ import annotations

import glob
import os

from gan.framework.context import get_access_context
from gan.framework.trajectory import (outer_session_index, read_session,
                                      session_label, session_universe)
from agent.tools.clip import clip_head


def tool_info():
    return {
        "name": "read_session_trajectory",
        "description": (
            "Read YOUR OWN session trajectories over your visible outers "
            "(redacted; the self seat: this outer and the previous outer). "
            "Omit 'genid' to read all of them. In slice mode (your design's "
            "cost.cm_mode) the default response is a STRUCTURAL INDEX — narrow "
            "it with kind=/tool=/flag= and page with offset= — and "
            "fetch=<seg_id> materializes one segment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "genid": {"description": "Inner generation id; optional "
                                         "'outer_<O>/' prefix for a previous "
                                         "outer. Omit for all visible outers."},
                "max_chars": {"type": "integer",
                              "description": "Render cap in chars. Default 6000; "
                                             "requests are honored up to 120000 "
                                             "(ceiling)."},
                "fetch": {"description": "Materialize one segment by seg_id "
                                        "(any mode: the slice index lists the "
                                        "seg_ids; without it the segment is "
                                        "resolved directly from the archived "
                                        "session files)."},
                "kind": {"description": "Index filter: segment kind "
                                        "(turn_output|tool_call|tool_output|plan_ops|stop_record)."},
                "tool": {"description": "Index filter: tool name."},
                "flag": {"description": "Index filter: exception flag "
                                        "(traceback|budget_stop|repeat_break|truncated)."},
                "offset": {"type": "integer",
                           "description": "Index pagination: skip this many matching segments."},
                "tail": {"type": "integer",
                         "description": "Serve the LAST N segments' content as "
                                        "one page (any mode; the forensic common "
                                        "case)."},
                "head": {"type": "integer",
                         "description": "Serve the FIRST N segments' content as "
                                        "one page (any mode)."},
                "grep": {"description": "Regex search over the session files' "
                                        "segment texts (server-side): returns "
                                        "matching seg_ids + the first matching "
                                        "line, so grep -> fetch composes. Bad "
                                        "regex is a loud error."},
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


def _declared_outers(actx, outer):
    """The declared own-session outer set (strings, current outer first).

    Explicit ``AccessContext.session_outers`` when the framework declared one
    (the self seat: {O, O-1}); ``None`` keeps the legacy single-current-outer
    behavior for every other seat/path."""
    declared = getattr(actx, "session_outers", None)
    if not declared:
        return [outer]
    others = sorted((str(o) for o in declared if str(o) != str(outer)),
                    key=lambda x: (0, int(x)) if x.isdigit() else (1, x))
    return [str(outer)] + others


def _parse_genid_ref(genid, current_outer):
    """Split an optional ``outer_<O>/`` prefix off a genid reference (the same
    address space as the materialized labels). Returns ``(outer, genid)``;
    a bare genid means the CURRENT outer (backward compatible)."""
    if genid is None:
        return None, None
    s = str(genid)
    if s.startswith("outer_") and "/" in s:
        head, _, tail = s.partition("/")
        digits = head.split("_", 1)[1]
        if not digits.isdigit() or not tail:
            raise ValueError(f"malformed genid '{genid}' "
                             "(expected 'outer_<O>/<genid>')")
        return digits, tail
    return current_outer, s


def _quarantined_labels(output_dir, current_outer, outers, role):
    """Labels of quarantined (unredactable) sessions across the declared
    outers. Batch-4 ruling C: BOTH presentation modes report them — the
    absence of a session must stay attributable (no-silent-fail corollary);
    the content itself is NEVER served (read_session fail-closes)."""
    return [session_label(current_outer, o, e["genid"], f"{role}.jsonl")
            for o in outers
            for e in outer_session_index(output_dir, o, role)
            if e.get("quarantined")]


def _quarantine_note(labels):
    if not labels:
        return ""
    return ("\n# quarantined (unredactable; never served; audit trail: "
            "events.jsonl session_redaction_failed): " + ", ".join(labels))


def _session_entries(output_dir, current_outer, outers, role, genid,
                     *, seat=None):
    """The visible session files as ``(label, path)`` pairs.

    Built from ``trajectory.session_universe`` — the ONE resolver shared with
    trajectory materialization, so this fallback and the session's
    materialized index address the same files identically (the hand-rolled
    legacy glob that used to live here rebuilt paths that a seat-keyed run
    never writes: docs/19 §12.12 F1). ``genid`` may carry an ``outer_<O>/``
    prefix; a bare genid narrows to the CURRENT outer only."""
    req_outer, req_genid = _parse_genid_ref(genid, current_outer)
    extras = [o for o in ([req_outer] if req_genid is not None
                          else list(outers))
              if str(o) != str(current_outer)]
    pairs = session_universe(output_dir, role, current_outer,
                             extra_outers=extras, seat=seat)
    if req_genid is None:
        return pairs
    # genid narrowing: a label is ``<genid>/<file>`` (or
    # ``outer_<O>/<genid>/<file>``) for per-generation seats and a bare file
    # name for the self seat, whose files are outer-level.
    want = str(req_genid)
    keep = []
    for label, path in pairs:
        head = label.split("/")[0]
        if head == want or (head.startswith("outer_") and
                            label.split("/")[1:2] == [want]):
            keep.append((label, path))
    return keep


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
                  flag=None, offset=0, tail=None, head=None, grep=None,
                  **kwargs):
    actx = get_access_context()
    if actx is None:
        return "Error: no access context"
    broker = actx.broker
    # Sandboxed seat workers carry the RPC broker (method present);
    # in-process sessions carry the plain AccessBroker and read files
    # directly. Both are valid -- only a missing broker is fatal.
    if broker is None:
        return "Error: parent session reader is unavailable"
    outer = _outer_of(actx)
    if outer is None:
        return "Error: session trajectories are only available during self-improvement."
    outers = _declared_outers(actx, outer)
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

    output_dir = getattr(actx.broker, "output_dir", None) if actx.broker is not None else None

    if hasattr(broker, "session_trajectory_index"):
        # Sandboxed seat worker: every byte crosses the parent-side RPC
        # (redaction stays parent-side; this seat cannot see other outers'
        # files). §12.24 (CM-08): the slice faces cross as a structured
        # ``query`` — the root-side broker serves index/fetch/tail/head/grep
        # over the SAME declared files (visibility unchanged; the
        # materialized payload stays reachable via ctx_index/ctx_fetch).
        # Old root-side brokers refuse the unknown kwarg with TypeError and
        # the branch degrades to the legacy blob path LOUDLY, never silently.
        cap = int(max_chars or 6000)
        query = None
        if fetch or tail or head or grep:
            query = {"fetch": fetch, "tail": tail, "head": head, "grep": grep,
                     "mode": mode}
        elif slice_mode:
            query = {"kind": filt["kind"], "tool": filt["tool"],
                     "flag": filt["flag"], "offset": filt["offset"],
                     "mode": mode}

        def _render(req_outer, req_genid, result):
            where = "" if str(req_outer) == str(outer) else f" (outer_{req_outer})"
            if isinstance(result, dict):
                serve = result.get("serve")
                if serve == "error":
                    return str(result.get("text") or "Error:")
                if serve == "segment":
                    entry = result.get("entry") or {}
                    meta = (f"# segment {result.get('seg_id')} "
                            f"kind={entry.get('kind')}"
                            + (f" tool={entry.get('tool')}" if entry.get("tool") else "")
                            + f" chars={entry.get('chars')}")
                    return f"{meta}\n{clip_head(str(entry.get('text') or ''), cap)}"
                if serve == "page":
                    return str(result.get("text") or "")
                if serve == "index":
                    from gan.framework.cm.traj_index import render_index_text
                    header = (f"# your session index genid={req_genid}{where} "
                              f"(mode={mode}, redacted; fetch=<seg_id> for content)")
                    return header + "\n" + render_index_text(
                        result.get("index") or {}, max_chars=cap,
                        kinds=filt["kind"], tools=filt["tool"],
                        flags=filt["flag"], offset=filt["offset"])
                return f"Error: unknown serve envelope {serve!r}"
            blob = str(result or "")
            if not blob:
                return ""
            return f"# your session genid={req_genid}{where}\n{blob}"

        try:
            if genid is not None:
                try:
                    req_outer, req_genid = _parse_genid_ref(genid, outer)
                except ValueError as e:
                    return f"Error: {e}"
                parts = [_render(req_outer, req_genid,
                                 broker.read_session_trajectory(
                                     role, actx.node_id, req_outer, req_genid,
                                     cap, seat=actx.seat, query=query))]
            else:
                parts = [_render(o, None,
                                 broker.read_session_trajectory(
                                     role, actx.node_id, o, None, cap,
                                     seat=actx.seat, query=query))
                         for o in outers]
        except PermissionError as e:
            # A refusal is DATA, not an exception the agent has to decode
            # (docs/19 §12.11: every out-of-set request is refused and the
            # refusal is reported).
            return (f"Error: {e}\n"
                    f"(your session may read outers: {outers})")
        except TypeError:
            # Old root-side broker (pre-§12.24): no query face. Retry the
            # legacy blob path and say so — degradation is never silent.
            if genid is not None:
                try:
                    req_outer, req_genid = _parse_genid_ref(genid, outer)
                except ValueError as e:
                    return f"Error: {e}"
                blob = broker.read_session_trajectory(
                    role, actx.node_id, req_outer, req_genid, cap,
                    seat=actx.seat)
                parts = [f"# your session genid={req_genid}\n{blob}"]
            else:
                pairs = []
                for o in outers:
                    idx = broker.session_trajectory_index(role, actx.node_id, o,
                                                          seat=actx.seat)
                    for e in (idx or []):
                        if e.get("quarantined"):
                            continue
                        if e.get("name") and e.get("genid") is None:
                            pairs.append((session_label(outer, o, None, e["name"]),
                                          o, None))
                        else:
                            pairs.append((session_label(outer, o, e["genid"],
                                                        f"{role}.jsonl"),
                                          o, e["genid"]))
                blob = "\n\n".join(
                    broker.read_session_trajectory(role, actx.node_id, o, g, cap,
                                                   seat=actx.seat)
                    for _l, o, g in pairs)
                parts = [(f"# your sessions (outers={outers}): "
                          f"{[p[0] for p in pairs]}\n{clip_head(blob, cap)}")]
            return ("# note: the parent-side broker predates the segment query "
                    "face (docs/19 §12.24); the full redacted blob is served "
                    "instead\n" + "\n\n".join(parts))
        parts = [p for p in parts if p and p.strip()]
        if not parts:
            return "No session trajectories in your visible outers yet."
        return note + "\n\n".join(parts)

    if not output_dir:
        return "Error: parent trajectory root is unavailable"
    try:
        entries = _session_entries(output_dir, outer, outers, role, genid,
                                   seat=getattr(actx, "seat", None))
    except ValueError as e:
        return f"Error: {e}"
    labels = {label for label, _p in entries}

    if slice_mode and not fetch:
        paths = [p for _l, p in entries]
        paths = [p for p in paths if os.path.isfile(p)]
        qnote = _quarantine_note(_quarantined_labels(output_dir, outer,
                                                     outers, role))
        if not paths:
            # Nothing servable — but if the void hides quarantined sessions,
            # the absence must stay attributable (both modes report, batch 4
            # ruling C).
            msg = "No session trajectories in your visible outers yet."
            return msg + ("\n" + qnote if qnote else "")
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
            flags=filt["flag"], offset=filt["offset"]) + qnote

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
                        "'<file>'; use the qualified "
                        "'[outer_<O>/]<genid>/<file>:<line>'.")
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
                    return f"{meta}\n{clip_head(block.text, cap)}"
        src = dict(entries).get(addr)
        if src is None:
            return f"Error: segment '{fetch}' not in the visible sessions."
        entry = segment_entry(src, line_no)
        if entry is None:
            return f"Error: segment '{seg_id}' not found."
        meta = (f"# segment {seg_id} kind={entry['kind']}"
                + (f" tool={entry['tool']}" if entry.get("tool") else "")
                + f" chars={entry['chars']}")
        return f"{meta}\n{clip_head(entry['text'], cap)}"

    if tail or head or grep:
        # §12.36 (R2): content ops serve in ANY mode on BOTH arms — the
        # in-process ladder previously dropped these three parameters into
        # the index/blob path with no note. One scan over the visible
        # sessions, payload first, archived-file fallback (the seat arm's
        # Tier 1/Tier 2 sources and caps).
        sess = _active_traj_session()
        segs = _payload_segments(sess, entries) if sess is not None else None
        if segs is not None:
            def _lookup(seg):
                bid = sess.traj_segments.get(seg["seg_id"])
                blk = sess.universe.get(bid) if bid else None
                return blk.text if blk is not None else None
        else:
            paths = [p for _l, p in entries if os.path.isfile(p)]
            if not paths:
                qnote = _quarantine_note(
                    _quarantined_labels(output_dir, outer, outers, role))
                msg = "No session trajectories in your visible outers yet."
                return msg + ("\n" + qnote if qnote else "")
            from gan.framework.cm.traj_index import (build_traj_index,
                                                     segment_entry)
            segs = (build_traj_index(paths, labels=sorted(labels))
                    .get("segments") or [])
            by_label = dict(entries)

            def _lookup(seg):
                src = by_label.get(seg.get("file"))
                entry = (segment_entry(src, seg.get("line_no"))
                         if src else None)
                return entry["text"] if entry else None
        from gan.framework.cm.traj_index import grep_page, tail_head_page
        if grep:
            try:
                return grep_page(segs, _lookup, grep)
            except ValueError as e:
                return f"Error: {e}"
        return tail_head_page(segs, _lookup, n=int(tail or head or 1),
                              tail=bool(tail), max_chars=cap)

    if genid is not None:
        try:
            req_outer, req_genid = _parse_genid_ref(genid, outer)
        except ValueError as e:
            return f"Error: {e}"
        blob = read_session(output_dir, req_outer, req_genid, role, cap)
        if not blob:
            return f"No session trajectory for genid {genid}."
        # The passthrough filter notice must ride the response (it was
        # computed but never prepended — the exact silent-ignore class this
        # tool's sibling read_trajectory fixed): empty by default, so the
        # unfiltered default call stays byte-identical.
        where = "" if str(req_outer) == str(outer) else f" (outer_{req_outer})"
        return note + f"# your session genid={req_genid}{where}\n{blob}"
    pairs = []
    for o in outers:
        for e in outer_session_index(output_dir, o, role):
            pairs.append((session_label(outer, o, e["genid"], f"{role}.jsonl"),
                          o, e["genid"]))
    if not pairs:
        return "No session trajectories in your visible outers yet."
    blob = "\n\n".join(
        read_session(output_dir, o, g, role, cap) for _l, o, g in pairs
    )
    return note + (f"# your sessions (outers={outers}): "
                   f"{[p[0] for p in pairs]}\n{clip_head(blob, cap)}")


op_info = tool_info
op_function = tool_function
