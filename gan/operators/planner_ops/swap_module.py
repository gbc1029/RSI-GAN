"""swap_module - structural recombination of two modules (recorded action)."""
from gan.operators.context import get_plan_context


def tool_info():
    return {
        "name": "swap_module",
        "description": (
            "Recombine two harness/agent modules (structural operator). The action is "
            "recorded and applied by the loop; use for topology-level changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "module_a": {"type": "string"},
                "module_b": {"type": "string"},
            },
            "required": ["module_a", "module_b"],
        },
    }


def tool_function(module_a, module_b, **kwargs):
    ctx = get_plan_context()
    if ctx is None:
        return "Error: no plan context"
    ctx.record("swap_module", module_a=module_a, module_b=module_b)
    return f"recorded swap of '{module_a}' and '{module_b}'"


op_info = tool_info
op_function = tool_function
