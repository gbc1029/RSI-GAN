"""Shared optional skill: editor (re-export of the base ``agent/tools/edit``)."""
from agent.tools.edit import tool_info, tool_function  # noqa: F401

op_info = tool_info
op_function = tool_function
