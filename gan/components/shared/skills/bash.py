"""Shared optional skill: bash (re-export of the base ``agent/tools/bash``)."""
from agent.tools.bash import tool_info, tool_function  # noqa: F401

op_info = tool_info
op_function = tool_function
