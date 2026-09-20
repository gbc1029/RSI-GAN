"""Work tool: read a prior task generation's (redacted) trajectory.

The visible set is resolved by the loop and passed via ``AccessContext
.trajectory_genids`` (no reliance on the node already being in the tree).
Default (no ``genid``) reads the first entry of that set.
"""
from gan.framework.context import get_access_context
from gan.framework.trajectory import read


def tool_info():
    return {
        "name": "read_trajectory",
        "description": (
            "Read the (redacted) execution trajectory of a task generation that is visible "
            "to you (see the allowed genids in your prompt). Omit 'genid' for the default "
            "(current/parent). Benchmark scores / reports are redacted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "genid": {"description": "Target generation id (must be visible)."},
                "max_chars": {"type": "integer"},
            },
        },
    }


def tool_function(genid=None, max_chars=6000, **kwargs):
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
    blob = read(output_dir, genid, max_chars=int(max_chars or 6000), role="task")
    if not blob:
        return f"No archived trajectory for generation {genid}."
    return f"# trajectory genid={genid} (redacted)\n{blob}"


op_info = tool_info
op_function = tool_function
