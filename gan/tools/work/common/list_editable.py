"""Work tool: list the concrete source files this role may read / modify.

Roles do not know the allowlist a priori; ``list_editable`` expands the role's
read/write allowlist globs to **concrete existing files** under the code tree,
so the agent can request real paths (the gate treats a glob as a literal path).
"""
import json

from gan.framework import frozen
from gan.framework.context import get_access_context


def tool_info():
    return {
        "name": "list_editable",
        "description": (
            "List the concrete source files YOU may read and modify. Use these exact "
            "paths in request_source_access (do NOT pass glob patterns)."
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
    root = getattr(getattr(actx, "broker", None), "repo_root", None)
    if not root:
        return "Error: no code root available"
    cap = int(cap or 300)
    read_files = frozen.expand_roots(root, frozen.read_roots(role), cap=cap)
    write_files = frozen.expand_roots(root, frozen.write_roots(role), cap=cap)
    return json.dumps({
        "role": role,
        "read_files": read_files,
        "write_files": write_files,
    }, ensure_ascii=False, indent=2)


op_info = tool_info
op_function = tool_function
