import contextlib
import contextvars
import concurrent.futures as _cfutures
import io
import re
import sys
import json

from agent.llm import get_response_from_llm
from agent.tools import load_tools
from utils import trajectory_log

# --- tool-execution packaging (C1/C2/C3) -------------------------------------
# The tool loop's ONLY execution chokepoint is ``process_tool_call``, so the
# dispatch-level safety net lives here, not inside each tool:
#   C1 -- one wedged call must not sink the session: hard timeout per call
#         (the killer thread CANNOT be cancelled -- Python threads are not
#         killable -- so a timed-out call keeps running in the background and
#         the per-tool wedge breaker below bounds the leak to
#         ``max_workers + per-tool wedge limit``). Known residue: worker
#         threads are NON-daemon, so interpreter exit joins them -- a wedged
#         tool can delay process shutdown by up to its own completion time.
#         Bounded in practice: the harness/outer timeouts already cap the
#         enclosing process.
#   C2 -- a tool returning megabytes must not eat the context window: a
#         CENTRAL cap with an explicit in-band marker (never silent).
#   C3 -- tool ``print`` output is captured out of the loop's log stream and
#         merged into the tool result (visible to the agent, absent from
#         stdout/stderr mixing).
_TOOL_CALL_TIMEOUT_S = 600          # default per-call budget (roles; task passes its own)
_TOOL_OUTPUT_CAP = 16_000           # chars, tool return text
_WEDGE_DISABLE_LIMIT = 2            # wedges per tool before it is disabled for the session
_TOOL_EXECUTOR = None               # lazily built module-level executor (thread reuse)


def _tool_executor():
    global _TOOL_EXECUTOR
    if _TOOL_EXECUTOR is None:
        _TOOL_EXECUTOR = _cfutures.ThreadPoolExecutor(max_workers=4)
    return _TOOL_EXECUTOR


class _CaptureProxy:
    """Thread-safe stdout/stderr capture proxy (C3 + C1 interplay).

    ``sys.stdout``/``sys.stderr`` are PROCESS-GLOBAL: a ``redirect_stdout``
    running inside a wedged tool thread would keep swallowing the MAIN
    thread's output after the call was abandoned. The proxy starts in
    capture mode; the moment the main thread abandons the call it flips the
    proxy to pass-through (``release()``), so nothing prints into a lost
    buffer afterwards. Non-wedge calls release the proxy naturally in the
    thread's own ``finally`` (tool prints stay merged, C3 semantics).
    """

    def __init__(self, real):
        self._real = real
        self._buf = io.StringIO()
        self._capture = True          # flipped by release(): main thread ONLY

    def write(self, s):
        if self._capture:
            try:
                self._buf.write(s)
            except Exception:                       # noqa: BLE001 -- never fail the tool on capture
                try:
                    self._real.write(s)
                except Exception:                   # noqa: BLE001 -- last resort: drop
                    pass
        else:
            try:
                self._real.write(s)
            except Exception:                       # noqa: BLE001
                pass
        return len(s)

    def flush(self):
        try:
            self._real.flush()
        except Exception:                           # noqa: BLE001
            pass

    def isatty(self):
        try:
            return bool(self._real.isatty())
        except Exception:                           # noqa: BLE001
            return False

    def value(self) -> str:
        return self._buf.getvalue()

    def release(self):
        self._capture = False


def _run_captured(fn, tool_input, proc_out, proc_err):
    """Run ONE tool function with stdout/stderr captured (C3).

    Runs INSIDE the executor thread through PRE-CONSTRUCTED proxies (main
    thread owns them, so an abandoned call can ALWAYS flip them back; see
    the ``_CaptureProxy`` docstring). Returns ``(result, proc_out, proc_err)``.
    """
    try:
        with contextlib.redirect_stdout(proc_out), contextlib.redirect_stderr(proc_err):
            return (fn(**tool_input), proc_out, proc_err)
    except BaseException as e:                       # noqa: BLE001 -- the loop classifies below
        return (e, proc_out, proc_err)
    finally:
        # natural release in the normal path; a wedged call relies on the
        # main thread's release() (abandon = proxy becomes pass-through)
        proc_out.release()
        proc_err.release()


def _audit_text(proxy) -> "str | None":
    """Captured text of one released proxy (None when nothing was printed)."""
    try:
        return proxy.value() or None
    except Exception:                               # noqa: BLE001
        return None


def _dispatch_precheck(tool_name, tool_input):
    """Validate declared source-edit calls before entering tool code."""
    if tool_name != "edit_source":
        return None
    if not isinstance(tool_input, dict):
        return "Error: edit_source input must be an object"
    command = tool_input.get("command")
    path = tool_input.get("path")
    if command not in {"create", "str_replace", "insert", "undo_edit", "view"}:
        return "Error: edit_source command is invalid"
    if not isinstance(path, str) or not path.strip():
        return "Error: edit_source requires a non-empty string path"
    if command == "str_replace" and not isinstance(tool_input.get("old_str"), str):
        return "Error: edit_source str_replace requires old_str"
    if command == "insert" and not isinstance(tool_input.get("insert_line"), int):
        return "Error: edit_source insert requires integer insert_line"
    if command == "create" and not isinstance(tool_input.get("file_text"), str):
        return "Error: edit_source create requires file_text"
    return None


def process_tool_call(tools_dict, tool_name, tool_input, timeout_s=None):
    """Execute one tool call with the dispatch-level safety net.

    Returns ``(tool_output_text, audit)``. ``audit`` carries the structured
    facts the tool loop records into the trajectory (nothing here is silent):

    - ``wedged`` / ``wedge_limit`` / ``tool_disabled`` -- C1 timeouts and the
      per-tool breaker;
    - ``truncated`` / ``original_chars``             -- C2 central cap;
    - ``tool_stdout`` / ``tool_stderr``              -- C3 captured output
      (stderr means the tool escaped its own error handling; audit-only).

    Contexts (AccessContext / PlanContext / ...) are SEEDED explicitly: tools
    read their session state via contextvars, and a bare executor thread would
    start with an empty context and break every context-dependent tool.
    """
    denied = _dispatch_precheck(tool_name, tool_input)
    if denied:
        return denied, {"dispatch_rejected": True}
    timeout_s = int(timeout_s or _TOOL_CALL_TIMEOUT_S)
    if tool_name not in tools_dict:
        return f"Error: Tool '{tool_name}' not found", {}
    entry = tools_dict[tool_name]
    meta = entry.setdefault("_meta", {})
    if meta.get("wedges", 0) >= _WEDGE_DISABLE_LIMIT:
        return (f"Error: tool '{tool_name}' is DISABLED for this session: it wedged "
                f"{meta['wedges']} time(s). Fix the tool's hang cause before retrying "
                f"(or use another tool to make progress).", {"tool_disabled": True})

    fn = entry["function"]
    ctx = contextvars.copy_context()
    # proxies are built HERE (main thread) so an abandoned call can always
    # flip them back -- even if the executor thread has not started yet
    proc_out, proc_err = _CaptureProxy(sys.stdout), _CaptureProxy(sys.stderr)
    try:
        fut = _tool_executor().submit(ctx.run, _run_captured, fn,
                                      dict(tool_input or {}), proc_out, proc_err)
        result, out_proxy, err_proxy = fut.result(timeout=timeout_s)
    except _cfutures.TimeoutError:
        # the call thread keeps running -- its capture proxies MUST stop
        # swallowing the main thread's output immediately
        proc_out.release()
        proc_err.release()
        meta["wedges"] = meta.get("wedges", 0) + 1
        wedged, limit = meta["wedges"], _WEDGE_DISABLE_LIMIT
        if wedged >= limit:
            return (f"Error: tool '{tool_name}' exceeded {timeout_s}s ({wedged}/{limit} "
                    f"wedges) and is now DISABLED for this session.",
                    {"wedged": wedged, "wedge_limit": limit})
        return (f"Error: tool '{tool_name}' exceeded the {timeout_s}s call budget and was "
                f"abandoned ({wedged}/{limit}); the call may still be running in the "
                f"background. Make progress with other tools.",
                {"wedged": wedged, "wedge_limit": limit})
    if isinstance(result, BaseException):
        return (f"Error executing tool '{tool_name}': {result}",
                {"tool_stdout": _audit_text(out_proxy), "tool_stderr": _audit_text(err_proxy)})

    audit = {}
    printed = _audit_text(out_proxy)
    errd = _audit_text(err_proxy)
    if printed:
        result = f"{result}\n-- tool stdout (captured) --\n{printed}"
        audit["tool_stdout"] = printed
    if errd:
        audit["tool_stderr"] = errd

    text = result if isinstance(result, str) else str(result)
    if len(text) > _TOOL_OUTPUT_CAP:
        audit["truncated"] = True
        audit["original_chars"] = len(text)
        text = (text[:_TOOL_OUTPUT_CAP]
                + f"\n...[truncated: {len(text)} chars total, showing first "
                  f"{_TOOL_OUTPUT_CAP}; use read_file/grep with tighter bounds]")
    return text, audit

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
            if not isinstance(tool_use, dict) or 'tool_name' not in tool_use:
                # A JSON block that parses but carries no tool_name is NOT a
                # malformed tool call -- it is the agent's ANSWER payload (the
                # task template answers as <json>{"response": ...}</json>).
                # Counting it as malformed made the tool loop nag the model
                # until it abandoned its answer format entirely (observed
                # pr4: the final answer "accept" arrived bare, unparsable).
                continue
            if 'tool_input' not in tool_use:
                malformed += 1  # invalid shape: treated like malformed
                continue
            tool_uses.append(tool_use)
        except json.JSONDecodeError:
            malformed += 1
    return (tool_uses if tool_uses else None), malformed

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
    tool_timeout_s=None,  # C1: per-CALL budget in seconds (None -> module default 600).
                          # A wedged tool is abandoned at the call level (its thread
                          # keeps running; a per-tool breaker disables repeat offenders)
                          # instead of hanging the whole session.
    trajectory_file=None,  # Optional structured JSONL trajectory sink
    return_info=False,  # If True, also return {"truncated", "tool_calls"}
    tool_repeat_limit: int = 3,  # Anti-thrash: >= this many CONSECUTIVE identical
                          # (tool, input, output) calls ends the session -- identical
                          # input + identical output cannot produce new information.
                          # Each tool's FIRST call is always exempt (a seat's mandatory
                          # eval-points therefore always complete). 0 disables.
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
        _last_fp = None
        _repeat_count = 0
        repeat_break = False
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
                    tool_output, tool_audit = process_tool_call(
                        tools_dict, tool_name, tool_input, timeout_s=tool_timeout_s)
                    num_tool_calls += 1
                    if tool_audit.get("wedged"):
                        _emit(logging, trajectory_file, "tool_wedged", tool=tool_name,
                              wedged=tool_audit["wedged"], wedge_limit=tool_audit["wedge_limit"])
                    if tool_audit.get("tool_disabled"):
                        _emit(logging, trajectory_file, "tool_disabled", tool=tool_name)
                    if tool_audit.get("truncated"):
                        _emit(logging, trajectory_file, "tool_output_truncated", tool=tool_name,
                              original_chars=tool_audit["original_chars"])
                    if tool_audit.get("tool_stdout"):
                        _emit(logging, trajectory_file, "tool_stdout", tool=tool_name,
                              text=str(tool_audit["tool_stdout"])[:2000])
                    if tool_audit.get("tool_stderr"):
                        _emit(logging, trajectory_file, "tool_stderr", tool=tool_name,
                              text=str(tool_audit["tool_stderr"])[:2000])
                    _emit(logging, trajectory_file, "tool_output", tool=tool_name,
                          output=str(tool_output))
                    # Anti-thrash breaker: identical (tool, input, output) in a row
                    # cannot produce new information. State-changing re-verification
                    # is exempt automatically -- a changed workspace changes the
                    # output hash, which resets the counter.
                    _fp = (tool_name,
                           json.dumps(tool_input, sort_keys=True, ensure_ascii=False,
                                      default=str),
                           str(tool_output))
                    if _fp == _last_fp:
                        _repeat_count += 1
                    else:
                        _last_fp = _fp
                        _repeat_count = 1
                    if tool_repeat_limit > 0 and _repeat_count >= tool_repeat_limit:
                        logging("Error: identical tool call repeated "
                                f"{_repeat_count}x with the same result; ending session.")
                        _emit(logging, trajectory_file, "tool_repeat_break",
                              tool=tool_name, repeats=_repeat_count)
                        truncated = True
                        repeat_break = True
                        try:
                            response, new_msg_history, info = get_response_fn(
                                msg=(system_msg + "\n\n# Repeated identical tool call\n"
                                     "The same tool call (same input) returned the same "
                                     f"result {_repeat_count} times in a row. The result "
                                     "will not change unless the state changes. Do NOT "
                                     "call any tool. Provide your final answer/summary "
                                     "now."),
                                model=model, msg_history=new_msg_history)
                            _emit(logging, trajectory_file, "output", text=response)
                        except Exception as e:
                            logging(f"Error during final summary turn: {e}")
                        tool_uses, malformed = None, 0
                        retry_tool_use = False
                        break
                    tool_msg = f'''<json>
    {{
        "tool_name": "{tool_name}",
        "tool_input": {tool_input},
        "tool_output": "{tool_output}"
    }}
    </json>'''.strip()
                    tool_msgs.append(tool_msg)

            # Check for retry
            if repeat_break:
                break
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
        return new_msg_history, {"truncated": truncated, "tool_calls": num_tool_calls,
                                 "repeat_break": repeat_break}
    return new_msg_history

if __name__ == "__main__":
    import sys
    model = sys.argv[1] if len(sys.argv) > 1 else "openai/gpt-4o-mini"
    new_msg_history = chat_with_agent(msg="hello", model=model)
