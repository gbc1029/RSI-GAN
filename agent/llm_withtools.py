import re
import json

from agent.llm import get_response_from_llm
from agent.tools import load_tools
from utils import trajectory_log

def get_tooluse_prompt(tool_infos=[]):
    """
    Get the prompt for using the available tools.
    """
    # If no tools are available, return an empty string
    if not tool_infos or len(tool_infos) == 0:
        return ""
    # Create the prompt
    tools_available = [str(tool_info) for tool_info in tool_infos]
    tools_available = '\n\n'.join(tools_available) if tools_available else 'None'
    tooluse_prompt = """Here are the available tools:
```
{tools_available}
```

Use only one tool (if needed) in this format:
<json>
{{
    "tool_name": ...,
    "tool_input": ...
}}
</json>

ONLY USE ONE TOOL PER RESPONSE, AND STRICTLY FOLLOW THE FORMAT OF TOOL_NAME AND TOOL_INPUT ABOVE.
DO NOT HALLUCINATE OR MAKE UP ANYTHING.
""".format(tools_available=tools_available)
    return tooluse_prompt.strip()

def should_retry_tool_use(response, tool_uses=None):
    """
    Check if the response attempts to use a tool,
    but ran out of output context.
    """
    # If there are tool uses, we don't need to check for retry
    if tool_uses is not None and len(tool_uses) > 0:
        return False

    # Find positions of the markers
    json_pos = response.find("<json>")
    tool_name_pos = response.find("tool_name")
    tool_input_pos = response.find("tool_input")

    # Check ordering and length condition
    if (
        json_pos != -1
        and tool_name_pos != -1
        and tool_input_pos != -1
        and json_pos < tool_name_pos < tool_input_pos
        and len(response) >= 2000
    ):
        return True

    # No retry
    return False

def check_for_tool_uses(response):
    """
    Checks if the response contains one or more tool calls in json code blocks.

    Returns ``(tool_uses_or_None, malformed_count)``. A-level sweep: malformed
    blocks are no longer a fully silent skip — the caller feeds a bounded
    structured error back to the model so it can self-correct instead of
    burning turns unaware that its calls were ignored.
    """
    pattern = r'<json>\s*(\{.*?\})\s*</json>'
    matches = re.findall(pattern, response, re.DOTALL)
    tool_uses = []
    malformed = 0
    for match in matches:
        try:
            tool_use = json.loads(match)
            if 'tool_name' not in tool_use or 'tool_input' not in tool_use:
                malformed += 1  # invalid shape: treated like malformed
                continue
            tool_uses.append(tool_use)
        except json.JSONDecodeError:
            malformed += 1
    return (tool_uses if tool_uses else None), malformed

def process_tool_call(tools_dict, tool_name, tool_input):
    try:
        if tool_name in tools_dict:
            return tools_dict[tool_name]['function'](**tool_input)
        else:
            return f"Error: Tool '{tool_name}' not found"
    except Exception as e:
        return f"Error executing tool '{tool_name}': {str(e)}"

def _emit(logging, trajectory_file, kind, **fields):
    """Emit one structured record (or fall back to plain logging)."""
    if trajectory_file:
        trajectory_log.append(trajectory_file, {"kind": kind, **fields})
    else:
        logging(f"{kind}: {json.dumps(fields, ensure_ascii=False, default=str)[:4000]}")

def chat_with_agent(
    msg,
    model,
    msg_history=None,
    logging=print,
    tools_available=[],  # Empty list means no tools, 'all' means all tools
    multiple_tool_calls=False,  # Whether to allow multiple tool calls in a single response
    max_tool_calls=40,  # Maximum number of tool calls allowed in a single response, -1 for unlimited
    tools_dir=None,  # Optional custom tools directory (GAN roles use their own toolsets)
    trajectory_file=None,  # Optional structured JSONL trajectory sink
    return_info=False,  # If True, also return {"truncated", "tool_calls"}
):
    get_response_fn = get_response_from_llm
    # Construct message
    if msg_history is None:
        msg_history = []
    new_msg_history = msg_history
    num_tool_calls = 0
    truncated = False

    try:
        # Load all tools
        all_tools = load_tools(logging=logging, names=tools_available, tools_dir=tools_dir)
        tools_dict = {tool['info']['name']: tool for tool in all_tools}
        system_msg = f"{get_tooluse_prompt([tool['info'] for tool in all_tools])}\n\n"

        # Call API
        _emit(logging, trajectory_file, "input", text=msg)
        response, new_msg_history, info = get_response_fn(
            msg=system_msg + msg,
            model=model,
            msg_history=new_msg_history,
        )
        _emit(logging, trajectory_file, "output", text=response)

        # Tool use
        tool_uses, malformed = check_for_tool_uses(response)
        retry_tool_use = should_retry_tool_use(response, tool_uses)
        malformed_feedback = 0
        while tool_uses or retry_tool_use or malformed:
            # Check for max tool calls
            if max_tool_calls > 0 and num_tool_calls >= max_tool_calls:
                # Do NOT end on a half-executed tool call: give the model one
                # final turn (no tools) to summarise what it learned.
                logging("Error: Maximum number of tool calls reached; requesting final summary.")
                truncated = True
                try:
                    response, new_msg_history, info = get_response_fn(
                        msg=(system_msg + "\n\n# Tool budget exhausted\n"
                             "Do NOT call any tool. Provide your final answer/summary now."),
                        model=model,
                        msg_history=new_msg_history,
                    )
                    _emit(logging, trajectory_file, "output", text=response)
                except Exception as e:
                    logging(f"Error during final summary turn: {e}")
                break

            # A-level sweep: malformed tool-call JSON used to vanish silently;
            # give the model one bounded structured feedback turn so it can
            # self-correct (never unbounded: capped rounds, still subject to
            # the tool budget check above).
            if tool_uses is None and malformed and malformed_feedback < 2:
                malformed_feedback += 1
                logging(f"Error: {malformed} malformed tool-call JSON block(s) ignored.")
                _emit(logging, trajectory_file, "tool_output", tool="<malformed_json>",
                      output=f"{malformed} malformed tool-call JSON block(s) ignored")
                response, new_msg_history, info = get_response_fn(
                    msg=(system_msg + f"\n\nError: {malformed} tool-call JSON block(s) "
                         f"in your last message were malformed (invalid JSON, or missing "
                         f"tool_name/tool_input) and were IGNORED. Either re-issue the "
                         f"call with valid JSON or stop calling tools and give your "
                         f"final answer now."),
                    model=model, msg_history=new_msg_history)
                _emit(logging, trajectory_file, "output", text=response)
                tool_uses, malformed = check_for_tool_uses(response)
                retry_tool_use = should_retry_tool_use(response, tool_uses)
                continue
            if tool_uses is None and malformed:
                logging("Error: repeated malformed tool-call JSON; ending tool loop.")
                break

            tool_msgs = []

            # Process tool uses
            if tool_uses:
                tool_uses = tool_uses if multiple_tool_calls else tool_uses[:1]
                for tool_use in tool_uses:
                    tool_name = tool_use['tool_name']
                    tool_input = tool_use['tool_input']
                    _emit(logging, trajectory_file, "tool_call", tool=tool_name, input=tool_input)
                    tool_output = process_tool_call(tools_dict, tool_name, tool_input)
                    num_tool_calls += 1
                    _emit(logging, trajectory_file, "tool_output", tool=tool_name,
                          output=str(tool_output))
                    tool_msg = f'''<json>
    {{
        "tool_name": "{tool_name}",
        "tool_input": {tool_input},
        "tool_output": "{tool_output}"
    }}
    </json>'''.strip()
                    tool_msgs.append(tool_msg)

            # Check for retry
            if retry_tool_use:
                logging("Error: Output context exceeded. Please try again.")
                tool_msgs.append("Error: Output context exceeded. Please try again.")

            # Get tool response
            response, new_msg_history, info = get_response_fn(
                msg=system_msg + '\n\n'.join(tool_msgs),
                model=model,
                msg_history=new_msg_history,
            )
            _emit(logging, trajectory_file, "output", text=response)

            # Check for next tool use
            tool_uses, malformed = check_for_tool_uses(response)
            retry_tool_use = should_retry_tool_use(response, tool_uses)

    except Exception as e:
        logging(f"Error: {str(e)}")
        raise e

    if return_info:
        return new_msg_history, {"truncated": truncated, "tool_calls": num_tool_calls}
    return new_msg_history

if __name__ == "__main__":
    import sys
    model = sys.argv[1] if len(sys.argv) > 1 else "openai/gpt-4o-mini"
    new_msg_history = chat_with_agent(msg="hello", model=model)
