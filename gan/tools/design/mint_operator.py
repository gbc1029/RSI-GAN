"""Design operator: mint a NEW operator by COMPOSING existing ones (shallow).

Composition-only operators are declarative and may be minted at the shallow
layer. An operator that needs new logic is a source-level (deep) change.
"""
from gan.context import get_design_context


def tool_info():
    return {
        "name": "mint_operator",
        "description": (
            "Mint a new operator by composing existing operators/actions (declarative). "
            "Provide a name and a composition spec. New *logic* (not composition) requires "
            "a source-level change."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "composition": {"description": "e.g. a list of {op, args} steps or a short spec."},
            },
            "required": ["name", "composition"],
        },
    }


def tool_function(name, composition=None, **kwargs):
    ctx = get_design_context()
    if ctx is None:
        return "Error: no design context"
    ops = ctx.config.setdefault("operators", [])
    ops.append({"name": name, "composition": composition})
    ctx.record("mint_operator", name=name)
    return f"minted operator '{name}' (declarative composition)"


op_info = tool_info
op_function = tool_function
