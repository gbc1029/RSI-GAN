import json
import os

from agent.base_agent import AgentSystem
from agent.llm_withtools import chat_with_agent
from utils.common import extract_jsons

_DEFAULT_DESIGN = {"prompt": "You are an agent.", "tools": [], "params": {}}


def _load_design():
    """Load the framework-injected task design (evolved prompt + selected tools).

    A design file that EXISTS but is unparseable must abort the task run (C4):
    silently falling back to ``_DEFAULT_DESIGN`` would run this generation on
    the seed prompt while looking successful — the evolved design would be
    silently lost with zero signal. A missing env/file still falls back to the
    default (plain HyperAgents-style direct runs have no design file).
    """
    path = os.environ.get("GAN_TASK_DESIGN")
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            raise RuntimeError(
                f"GAN_TASK_DESIGN={path} exists but is unparseable ({e}); refusing "
                f"to silently fall back to the default design (this generation "
                f"would lose its evolved prompt invisibly)"
            ) from e
    return dict(_DEFAULT_DESIGN)


class TaskAgent(AgentSystem):
    def forward(self, inputs):
        """
        A design-driven agent that solves a given task.

        The shallow design (prompt + selected skills + params) comes from the
        design config passed via the ``GAN_TASK_DESIGN`` env var; tools are
        loaded from ``GAN_TASK_TOOLS_DIR``.

        Args:
            inputs (dict): input data for the task.

        Returns:
            tuple: (prediction, new_msg_history)
        """
        design = _load_design()
        prompt = design.get("prompt") or "You are an agent."
        # batch 6: the design slot is ``tools``; accept the legacy ``skills`` key
        # so a design file produced by an older run still assembles its tools
        tools = list(design.get("tools") or design.get("skills") or [])
        tools_dir = os.environ.get("GAN_TASK_TOOLS_DIR") or os.environ.get("GAN_TASK_SKILLS_DIR")
        task_brief = os.environ.get("GAN_TASK_BRIEF") or ""

        brief_block = f"{task_brief}\n\n" if task_brief else ""
        instruction = f"""{prompt}

{brief_block}Task input:
```
{inputs}
```

Respond in JSON format with the following schema (wrap the object in <json>...</json> tags; no markdown fences; no text outside the tags; write math in plain text without LaTeX backslash escapes):
<json>
{{
    "response": ...
}}
</json>"""

        new_msg_history = chat_with_agent(
            instruction,
            model=self.model,
            msg_history=[],
            logging=self.log,
            tools_available=(tools if tools else []),
            tools_dir=(tools_dir if tools else None),
            trajectory_file=getattr(self, "trajectory_file", None),
            # C1: task child lives under the harness QUESTION_TIMEOUT (300s) --
            # a wedged tool must fail at the call level well before the whole
            # question subprocess is killed (S2 fixed the outer timeout; this
            # keeps a single slow tool from spending the entire question).
            tool_timeout_s=240,
        )

        # Extract the response. A BLANK prediction (not the string "None") marks
        # extraction failure: the harness resume path retries blank predictions
        # and the report drops them, so the sentinel can never pass as an
        # answer; the reason rides the trajectory log instead of vanishing.
        prediction = ""
        try:
            extracted_jsons = extract_jsons(new_msg_history[-1]['text'], logging=self.log)
            if extracted_jsons is None:
                self.log("prediction_extract failed: no parseable JSON (see json_extract log above)")
            elif "response" not in extracted_jsons[-1]:
                obj = extracted_jsons[-1]
                keys = list(obj)[:8] if isinstance(obj, dict) else "-"
                self.log(f"prediction_extract failed: missing key 'response' "
                         f"(got {type(obj).__name__}, keys={keys})")
            else:
                prediction = extracted_jsons[-1]['response']
        except Exception as e:
            self.log(f"Error extracting prediction: {e}")
            prediction = ""

        return prediction, new_msg_history
