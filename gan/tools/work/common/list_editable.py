"""Work tool: list the concrete source files this role may read / modify.

Roles do not know the allowlist a priori; ``list_editable`` expands the role's
read roots (role-wide) and its **seat-specific** write roots to **concrete
existing files** under the code tree, so the agent can request real paths (the
gate treats a glob as a literal path). The modify set follows
``AccessContext.seat`` -- e.g. an ``evaluate`` session has no write roots.
"""
import json

from gan.framework import frozen
from gan.framework.context import get_access_context, illegal_seat_error


def tool_info():
    return {
        "name": "list_editable",
        "description": (
            "List the concrete source files YOU may read and modify. Reads are "
            "role-wide; the modify set follows your session seat (an evaluate "
            "session has none). Use these exact paths in request_source_access (do "
            "NOT pass glob patterns for EXISTING files). To CREATE a NEW file, "
            "request its parent DIRECTORY (glob root) instead — a file outside every "
            "granted path cannot reach the commit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"cap": {"type": "integer"}},
        },
    }


def tool_function(cap=300, **kwargs):
    actx = get_access_context()
    if actx is None:
        return "Error: no access context"
    role = getattr(actx, "role", None)
    if not role:
        return "Error: no role in access context"
    broker = getattr(actx, "broker", None)
    if broker is None:
        return "Error: no access broker"
    if hasattr(broker, "editable_paths"):
        try:
            paths = broker.editable_paths(role, actx.node_id, actx.seat,
                                          int(cap or 300))
            return json.dumps({"role": role, "seat": actx.seat,
                               "read_files": paths.get("read_files", []),
                               "write_files": paths.get("write_files", [])},
                              ensure_ascii=False, indent=2)
        except Exception as exc:
            return f"Error: parent source catalog unavailable: {exc}"
    root = getattr(broker, "repo_root", None)
    if not root:
        return "Error: no code root available"
    # Writes are seat-specific (reads stay role-wide): an `evaluate` seat has no
    # write roots. The seat is set by framework code, so a missing/illegal one is a
    # framework bug -- refuse instead of guessing a (necessarily wider) surface.
    _bad_seat = illegal_seat_error(actx, "list_editable")
    if _bad_seat:
        return _bad_seat
    seat = actx.seat
    cap = int(cap or 300)
    read_files = frozen.expand_roots(root, frozen.read_roots(role), cap=cap)
    write_files = frozen.expand_roots(root, frozen.write_roots(role, seat), cap=cap)
    return json.dumps({
        "role": role,
        "seat": seat,
        "read_files": read_files,
        "write_files": write_files,
    }, ensure_ascii=False, indent=2)


op_info = tool_info
op_function = tool_function
