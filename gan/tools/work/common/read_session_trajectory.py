"""Work tool: read YOUR OWN session trajectories for the current outer.

Available to planner/evaluator during self-improvement. Sessions are the role's
own ``plan``/``evaluate`` sessions for each inner generation of the current outer
(``trajectory/outer_<O>/<genid>/<role>.jsonl``), redacted. Omit ``genid`` to read
all of this outer's sessions (bounded).
"""
from gan.framework.context import get_access_context
from gan.framework.trajectory import outer_session_index, read_session


def tool_info():
    return {
        "name": "read_session_trajectory",
        "description": (
            "Read YOUR OWN session trajectories from the current outer generation "
            "(redacted). Omit 'genid' to read all of this outer's sessions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "genid": {"description": "Inner generation id. Omit for all of this outer."},
                "max_chars": {"type": "integer"},
            },
        },
    }


def _outer_of(actx):
    node = str(getattr(actx, "node_id", ""))
    if node.startswith("outer_"):
        return node.split("_", 1)[1]
    return None


def tool_function(genid=None, max_chars=6000, **kwargs):
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
