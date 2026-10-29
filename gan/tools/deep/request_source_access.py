"""DEEP gate: the single entry for source-level changes (gated + audited).

Any change that is NOT expressible by shallow config/design operators (new
config keys, new/edited component implementations, new operator logic) must go
through here: request ``view``/``modify`` access, then edit the granted copies.
Requested paths are copied into the role workspace ``src/`` so they can then be
edited with the ``edit_source`` work tool.
"""
from gan.framework.access import SourceAccessDisabled
from gan.framework.context import (get_access_context, get_design_context,
                                   illegal_seat_error)


def tool_info():
    return {
        "name": "request_source_access",
        "description": (
            "DEEP change: request source access to VIEW or MODIFY the given paths. Use only "
            "when shallow design/config operators cannot express the change (e.g. adding a new "
            "config key, a new component implementation, or editing an implementation). "
            "Requesting a path you already hold KEEPS your local copy; pass refresh=true to "
            "discard local edits and re-copy the pristine repo version (the discarded files "
            "are backed up, and edits made against an older baseline are three-way merged "
            "at commit time instead of being rejected). Files you CREATE need a granted "
            "parent DIRECTORY (glob root) to ride the session patch — new files outside "
            "every granted path are dropped at the outer boundary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
                "intent": {"type": "string", "enum": ["view", "modify"]},
                "reason": {"type": "string"},
                "refresh": {
                    "type": "boolean",
                    "description": ("Replace your workspace copies with the pristine repo "
                                    "version, DISCARDING local edits made this session. "
                                    "Default false (existing copies are kept as-is)."),
                },
            },
            "required": ["paths", "reason"],
        },
    }


def tool_function(paths=None, intent="view", reason="", refresh=False, **kwargs):
    actx = get_access_context()
    dctx = get_design_context()
    if actx is None:
        return "Error: no access context"
    _bad_seat = illegal_seat_error(actx, "request_source_access")
    if _bad_seat:
        return _bad_seat
    if isinstance(paths, str):
        paths = [paths]
    # A deep WRITE verb needs a session with a patch channel; evaluate builds
    # nothing, so "modify" there would own copies that can never land.
    if intent == "modify" and dctx is None:
        return ("Error: intent='modify' is unavailable in this session -- there "
                "is no patch channel here (evaluate is read-only). view grants "
                "remain available; do NOT retry with modify.")
    # Default (if_absent): never overwrite a workspace copy that already exists -- it may
    # hold this session's edit_source edits or unregister_component deletions. refresh=True
    # is the explicit escape hatch that discards them and re-copies the repo version.
    try:
        granted = actx.broker.grant(actx.role, actx.node_id, paths or [],
                                    intent=intent, reason=reason,
                                    if_absent=not refresh, seat=actx.seat)
    except SourceAccessDisabled:
        return ("Deep source access is DISABLED for this run "
                "(source_access.deny_deep=true). Do not retry; continue with "
                "shallow design/config operators only.")
    kept = list((getattr(actx.broker, "last_result", None) or {}).get("skipped") or [])
    discarded = list((getattr(actx.broker, "last_result", None) or {}).get("discarded") or [])
    if dctx is not None:
        dctx.record("request_source_access", paths=granted, intent=intent, reason=reason)
    # Denied/missing paths ARE surfaced now -- as a CLASS per REQUESTED path
    # only (outside / readable-but-not-writable-here / no repo match). This
    # refines the earlier blanket non-disclosure: the agent already named these
    # paths, so classifying them leaks no NEW repo geography, while ending the
    # retry loops against grants that can never succeed. Nothing beyond the
    # requested paths is enumerated, and the trust anchor is never named.
    denied_note = _denied_note(actx, intent)
    if granted:
        try:
            src_root = actx.broker.src_dir(actx.role, actx.node_id)
        except Exception:
            src_root = None
        where = f" (workspace src: {src_root})" if src_root else ""
        kept_note = (f" Already in your workspace and kept as-is (not overwritten): {kept}."
                     if kept else "")
        # A refresh is destructive BY DESIGN (that is how a stale baseline is
        # escaped). Say so: the discarded files are backed up for audit, and the
        # edit has to be redone deliberately rather than silently vanishing.
        disc_note = (f" Discarded your local version of: {discarded} (backed up by the "
                     f"framework for audit). Redo those edits if you still need them."
                     if discarded else "")
        if intent == "modify":
            return (f"Granted {intent} access to: {granted}{where}. "
                    f"Edit these copies with `edit_source`. New files you create "
                    f"reach the commit only when a granted path (a parent "
                    f"DIRECTORY) covers them; scratch elsewhere is dropped at "
                    f"the outer boundary.{kept_note}{disc_note}{denied_note}")
        return f"Granted {intent} access to: {granted}{where}.{kept_note}{disc_note}{denied_note}"
    if paths:
        return (f"No accessible paths.{denied_note} "
                "Call `list_editable` to see which paths you may request.")
    return "No source paths were provided for this request."


def _denied_note(actx, intent: str) -> str:
    """One line per refusal CLASS over the REQUESTED paths (empty when none).

    Buckets come from ``broker.last_result`` (the documented audit surface --
    no new RPC). ``denied`` paths split by whether a VIEW grant could ever
    succeed for this seat: a modify request against a readable-but-not-writable
    path is correctable (re-request view), one outside the role surface is
    terminal (do not retry).
    """
    from gan.framework import frozen

    try:
        res = dict(getattr(actx.broker, "last_result", None) or {})
        denied = [str(p) for p in (res.get("denied") or [])]
        missing = [str(p) for p in (res.get("missing") or [])]
        outside, readable_only = [], []
        for rel in denied:
            try:
                if (str(intent) == "modify"
                        and frozen.is_allowed(actx.role, rel, "view", seat=actx.seat)):
                    readable_only.append(rel)
                else:
                    outside.append(rel)
            except Exception:  # noqa: BLE001 -- classify defensively
                outside.append(rel)
        parts = []
        if outside:
            parts.append(f" Denied (outside your role's surface -- cannot be granted, "
                         f"do not retry): {outside}.")
        if readable_only:
            parts.append(f" Denied (readable, but {actx.role}/{actx.seat} has no write "
                         f"root over it -- re-request with intent='view' to read): "
                         f"{readable_only}.")
        if missing:
            parts.append(f" No repo match (check spelling): {missing}.")
        return "".join(parts)
    except Exception:  # noqa: BLE001 -- the note must never break the grant answer
        return ""


op_info = tool_info
op_function = tool_function
