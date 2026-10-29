"""Workspace confinement helpers (FRAMEWORK, frozen).

Shared by the role work tools (read_file / list_dir / grep) so
every source access is confined to the instance's granted workspace ``src``.

``path_class`` / ``access_hint`` power the tool ERROR messages: they classify a
not-in-workspace path with the SAME primitives and order as
``AccessBroker.grant`` (deny_paths first, then ``frozen.is_allowed``), so the
message cannot drift from the gate. Reads stay role-wide while writes are
seat-narrowed, and the hint says which of the two applies.
"""
from __future__ import annotations

import fnmatch
import os
from typing import Optional


def src_root(actx) -> str:
    return os.path.realpath(actx.broker.src_dir(actx.role, actx.node_id))


def resolve(actx, path: str) -> str:
    """Resolve ``path`` inside the caller's workspace ``src`` root.

    Raises ValueError if the resolved path escapes the root.
    """
    root = src_root(actx)
    p = str(path or "").replace("\\", "/")
    cand = os.path.realpath(p) if os.path.isabs(p) else os.path.realpath(
        os.path.join(root, p.lstrip("/"))
    )
    if cand != root and not cand.startswith(root + os.sep):
        raise ValueError(f"path escapes the granted workspace: {path}")
    return cand


def path_class(actx, rel: str) -> str:
    """Classify a repo-relative path against THIS session's surface.

    Mirror of the grant decision (``AccessBroker.grant`` checks ``deny_paths``
    then ``frozen.is_allowed``), so the classes cannot drift from the gate:

    * ``outside``  -- not readable by the role at all (runtime deny list, role
      read allowlist, or trust anchor). Repo EXISTENCE is never answered for
      these: confirming it would leak repo geography past the allowlist.
    * ``readable`` -- role-readable, but THIS seat has no write root over it
      (reads are role-wide; writes are seat-narrowed -- e.g. the planner plan
      seat over ``PLANNER_SELF`` paths, or the whole evaluator evaluate seat).
    * ``writable`` -- role-readable AND covered by this seat's write roots.
    """
    from gan.framework import frozen

    role, seat = actx.role, getattr(actx, "seat", "")
    broker = actx.broker
    deny = list(getattr(broker, "deny_paths", None) or [])
    if any(fnmatch.fnmatch(rel, pat) for pat in deny):
        return "outside"
    if not frozen.is_allowed(role, rel, "view", seat=seat):
        return "outside"
    if frozen.is_allowed(role, rel, "modify", seat=seat):
        return "writable"
    return "readable"


def access_hint(actx, abs_path: str) -> Optional[str]:
    """An actionable error for a path that has NO workspace copy.

    Used by read_file / list_dir / grep / edit_source(view) when the resolved
    path does not exist in the granted workspace. Repo existence is probed only
    for paths INSIDE the role allowlist. Best-effort: returns ``None`` whenever
    classification is impossible, and the caller keeps its previous message as
    the fallback -- a hint must never break the tool it decorates.
    """
    try:
        root = src_root(actx)
        rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
        if rel.startswith(".."):
            return None
        cls = path_class(actx, rel)
        if cls == "outside":
            return (f"Error: not visible to role {actx.role}: outside your role's "
                    "read allowlist -- request_source_access cannot grant it "
                    "(list_editable shows the visible set).")
        repo_root = str(getattr(actx.broker, "repo_root", "") or "")
        repo_path = os.path.join(repo_root, rel)
        if not os.path.exists(repo_path):
            return ("Error: no workspace copy of this path, and no file at this "
                    "repo path -- check spelling (list_dir a granted parent, or "
                    "list_editable).")
        if os.path.isdir(repo_path):
            return ("Error: this repo path is a directory -- use list_dir (grant "
                    "it with request_source_access to list its contents).")
        where = f"{actx.role}/{actx.seat}" if getattr(actx, "seat", "") else actx.role
        if cls == "readable":
            return (f"Error: no workspace copy of this path yet. It is readable by "
                    f"your role but NOT writable from this seat ({where}): "
                    "request_source_access(paths=[...], intent='view') to read it; "
                    "edits from this seat will always be refused.")
        return (f"Error: no workspace copy of this path yet. It is writable from "
                f"this seat ({where}): request_source_access(paths=[...], "
                "intent='modify') to read+edit, then re-read.")
    except Exception:  # noqa: BLE001 -- a hint must never break the tool
        return None
