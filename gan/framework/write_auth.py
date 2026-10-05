"""Frozen write authorization shared by the dispatch and edit surfaces."""
from __future__ import annotations

import os
from typing import Any, Optional


def _root(actx: Any) -> str:
    return os.path.realpath(actx.broker.src_dir(actx.role, actx.node_id))


def resolve_workspace_path(actx: Any, path: str) -> str:
    root = _root(actx)
    raw = str(path or "")
    candidate = (os.path.realpath(raw) if os.path.isabs(raw)
                 else os.path.realpath(os.path.join(root, raw.lstrip("/"))))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise ValueError(f"path escapes the granted workspace: {path}")
    return candidate


def covering_intent(actx: Any, abs_path: str) -> Optional[str]:
    rel = os.path.relpath(abs_path, _root(actx)).replace(os.sep, "/")
    best = None
    for rec in actx.broker.grants.get((actx.role, str(actx.node_id)), []):
        for grant in rec.get("paths", []):
            grant = str(grant).replace("\\", "/").rstrip("/")
            if grant and (rel == grant or rel.startswith(grant + "/")):
                if rec.get("intent") == "modify":
                    return "modify"
                best = best or "view"
    return best


def authorize_write(actx: Any, path: str, operation: str = "write") -> Optional[str]:
    """Return an actionable error, or ``None`` when this write is authorized."""
    if actx is None or getattr(actx, "broker", None) is None:
        return "Error: no access context"
    try:
        absolute = resolve_workspace_path(actx, path)
    except ValueError as exc:
        return f"Error: {exc}"
    from gan.framework.context import get_design_context
    if get_design_context() is None:
        return (f"Error: {operation} refused: this session has no patch channel "
                "(read-only); source edits cannot reach any commit.")
    if covering_intent(actx, absolute) != "modify":
        intent = covering_intent(actx, absolute)
        why = "only covered by a VIEW grant" if intent == "view" else "not covered by any grant"
        return (f"Error: {operation} refused: this path is {why}. "
                "Re-request the same path with intent='modify'; no refresh is needed.")
    return None
