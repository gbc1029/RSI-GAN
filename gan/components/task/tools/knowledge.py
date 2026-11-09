"""Task tool: read the role's curated knowledge base (md, pull-based).

The knowledge base is a set of markdown files under ``gan/components/task/knowledge``
authored by the planner through the deep patch channel. It is NOT a registry
category: knowledge files are DATA (no ``name``/stem contract, no ``tool_info``),
and the design config carries no knowledge list. The whole committed base is
materialized into the task sandbox each generation
(``gan/framework/task_execution.py`` copies it to ``.gan_runtime/knowledge`` and
exports ``GAN_TASK_KNOWLEDGE_DIR``); this tool enumerates and returns it on
demand, so "which knowledge is active" is a runtime decision of the task agent
itself -- recorded naturally in the task trajectory as ordinary tool calls.

Text only, directory-confined (``os.path.basename``), per-call size-capped. This
module executes inside the sandboxed task child, so its blast radius is the child.
"""
from __future__ import annotations

import os

from agent.tools.clip import clip_head

_MAX_CHARS_PER_READ = 20_000


def tool_info():
    return {
        "name": "knowledge",
        "description": (
            "Read your curated knowledge base (markdown notes the planner wrote for "
            "you). Call with name='' to LIST the available notes; call with a name to "
            "READ that note's full text. A read returns up to 20000 chars — longer "
            "notes are clipped and marked with shown/total counts. Use it when a "
            "note looks relevant to the current task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Knowledge note name (without .md); empty = list."},
            },
        },
    }


def tool_function(name="", **kwargs):
    base = os.environ.get("GAN_TASK_KNOWLEDGE_DIR")
    if not base or not os.path.isdir(base):
        return "Error: no knowledge base available"
    if not name:
        names = sorted(f[:-3] for f in os.listdir(base)
                       if f.endswith(".md") and not f.startswith("."))
        if not names:
            return "(knowledge base is empty)"
        return "available knowledge notes:\n" + "\n".join(f"- {n}" for n in names)
    fname = os.path.basename(str(name))
    if fname.startswith("."):
        return "Error: unknown knowledge note"
    path = os.path.join(base, fname + ".md")
    if not os.path.isfile(path):
        return f"Error: no knowledge note named '{fname}' (call with name='' to list)"
    try:
        # Full read (not the old cap+1 probe): the marker's total must be the
        # NOTE's real size, not the probe's -- notes are materialized under
        # the cost key knowledge_max_file_chars, so a full read is bounded.
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return f"Error: cannot read knowledge note '{fname}': {e}"
    # Counted clip (docs/17 §17): cap == the cost key knowledge_max_file_chars
    # default == the chat loop's per-tool outer entry for this tool, so the
    # inner marker (with counts) always fires before any outer cut.
    return clip_head(text, _MAX_CHARS_PER_READ)


op_info = tool_info
op_function = tool_function
