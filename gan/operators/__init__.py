"""Operator / eval-point registries and per-session contexts.

Operators are just tools (``tool_info`` / ``tool_function``) so they can be
loaded by ``agent.tools.load_tools`` with a custom ``tools_dir`` and driven by
``chat_with_agent``. ``op_info`` / ``op_function`` aliases are provided so a
module can be described either way.
"""
from gan.operators.context import (  # noqa: F401
    EvalContext,
    PlanContext,
    get_eval_context,
    get_plan_context,
    reset_eval_context,
    reset_plan_context,
    set_eval_context,
    set_plan_context,
)
