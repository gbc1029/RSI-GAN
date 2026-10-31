"""Design operator: READ the session's design config (read-only).

Returns exactly what ``set_prompt``'s rewrite gate compares against
(``DesignContext.config`` -- the session's live design draft), plus the
generation-0 seed so the documented "restore the seed verbatim" escape is
actually executable, and an enumeration of the config keys this session may
set (static slots + dynamic declarations, resolved through the SAME
session-overlay semantics ``set_config`` validates against).

A read mutates nothing and leaves no record: a read is not a change (symmetric
with the rule that a REFUSED write must not look like one). The evaluate seat
has no design context and is refused here naturally; no prompt TEXT is ever
projected to an evaluator-facing surface by this tool -- it answers the session
that EDITS the design (B29 draws the isolation line planner->evaluator, not
author->own-draft).
"""
import json

from gan.framework.context import get_design_context


def _seed_prompt(role: str):
    """The generation-0 prompt for ``role`` -- the SAME authority the loop
    seeds with (``gan.design.initial_config``), so "reset to the seed" is
    judged against exactly the value a fresh run would start from."""
    try:
        from gan.design import initial_config
        return initial_config(role).get("prompt")
    except Exception:  # noqa: BLE001 -- a reader must never break the session
        return None


def _keys_view(ctx) -> dict:
    """Enumerate the config keys THIS session may set (set_config semantics).

    Resolves dynamic declarations through the session overlay -- the same
    workspace-first resolution ``set_config`` validates against -- so a key
    declared via ``add_config_key`` earlier in THIS session already shows up
    here. Best-effort: a schema failure yields an empty view, never an error.
    """
    from gan.design.schema import ROLE_SCHEMAS, allowed_keys, load_dynamic_specs
    from gan.framework.context import session_overlay_root

    role = ctx.role
    try:
        overlay = session_overlay_root()
        dynamic = load_dynamic_specs(role, overlay_root=overlay)
        allowed = allowed_keys(role, overlay_root=overlay)
    except Exception:  # noqa: BLE001 -- advisory view
        return {}
    cfg = dict(ctx.config or {})
    present = sorted(cfg)
    return {
        "static_keys": sorted(set(ROLE_SCHEMAS.get(role, {})) - set(dynamic)),
        "dynamic_keys": {k: {"value_type": s.get("value_type"),
                             "consumer": s.get("consumer"),
                             "description": s.get("description"),
                             "enum": s.get("enum")}
                         for k, s in sorted(dynamic.items())},
        "allowed_keys": sorted(allowed),
        "present": present,
        "unset_allowed": sorted(set(allowed) - set(present)),
    }


def tool_info():
    return {
        "name": "get_config",
        "description": (
            "Read the design config THIS session edits. TWO modes: (a) no "
            "argument -> the KEYS VIEW only (static/dynamic keys with "
            "value_type/enum/consumer, what is present vs unset) plus "
            "framework-owned facts -- the task's output contract when one is "
            "declared (read-only, outside set_config keys); (b) "
            "get_config(key=\"...\") -> that key's value; for \"prompt\" this "
            "includes the generation-0 seed (write it back verbatim to roll "
            "back a drifted prompt) and prompt_equals_seed; for enum-typed "
            "dynamic keys it includes the allowed enum values. Read-only."),
        "input_schema": {"type": "object",
                         "properties": {"key": {"type": "string",
                                                "description": "config key to read (e.g. \"prompt\")"}},
                         "required": []},
    }


def tool_function(key=None):
    ctx = get_design_context()
    if ctx is None:
        return "Error: no design context"
    try:
        keys = _keys_view(ctx)
    except Exception:  # noqa: BLE001 -- advisory view must never fail the read
        keys = {}
    if key is None:
        # Keys-only view: values (notably the <=20k prompt) are fetched
        # explicitly per key, so a design read costs no full-text dumps.
        payload = {"role": ctx.role, "keys": keys}
        oc = getattr(ctx, "output_contract", None)
        if oc:
            payload["output_contract"] = oc
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    key = str(key)
    cfg = dict(ctx.config or {})
    allowed = set((keys or {}).get("allowed_keys") or [])
    if key not in cfg and key not in allowed:
        known = ", ".join(sorted(set(cfg) | allowed)) or "(none)"
        return f"Error: unknown config key '{key}' (readable keys: {known})"
    payload = {"role": ctx.role, "key": key, "value": cfg.get(key)}
    if key == "prompt":
        # The rewrite-gate context travels with the prompt read, not with
        # every keys view (the seed is prompt-specific, ~6k chars).
        seed = _seed_prompt(ctx.role)
        payload["seed"] = seed
        payload["prompt_equals_seed"] = (cfg.get("prompt") == seed) if seed is not None else None
    dyn = (keys or {}).get("dynamic_keys", {}).get(key)
    if dyn and dyn.get("enum") is not None:
        payload["enum"] = dyn["enum"]
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


op_info = tool_info
op_function = tool_function
