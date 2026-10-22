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
    def __init__(self, model, chat_history_file='./outputs/trajectory.jsonl',
                 reasoning_effort=None, max_tool_calls=None, token_budget=None,
                 cm=None):
        super().__init__(model, chat_history_file=chat_history_file)
        # Thinking-intensity knob for this agent's every LLM call (valid values:
        # agent.llm.REASONING_EFFORTS, gateway contract low|high|max). Threaded
        # EXPLICITLY from the harness payload / CLI -- no env transport. None
        # keeps the gateway default.
        self.reasoning_effort = reasoning_effort
        # Reserved cost keys (docs/10 §10, B21 chain: harness payload ->
        # task_worker -> here). None keeps the framework defaults (40 tool
        # calls / unlimited tokens = the pre-C behaviour). The framework reads
        # these from the design; this class only FORWARDS them -- dropping the
        # forwarding would be visible offline (parent cost_keys_applied event
        # vs the per-question trajectories).
        self.max_tool_calls = max_tool_calls
        self.token_budget = token_budget
        # Context-management session (docs/10 §11, D6/D7): framework-built
        # (task_worker, from the .gan_runtime snapshot). None = no seam
        # (pre-D behaviour). Forwarding only -- no policy lives in the artifact.
        self.cm = cm

    def _extract_prediction(self, msg_history):
        if not msg_history or not isinstance(msg_history[-1], dict):
            return "", "missing final assistant message"
        text = msg_history[-1].get("text")
        if not isinstance(text, str):
            return "", "final assistant message has no text"
        extracted_jsons = extract_jsons(text, logging=self.log)
        if not extracted_jsons:
            return "", "no parseable JSON response"
        obj = extracted_jsons[-1]
        if not isinstance(obj, dict) or "response" not in obj:
            keys = list(obj)[:8] if isinstance(obj, dict) else "-"
            return "", ("missing key 'response' "
                        f"(got {type(obj).__name__}, keys={keys})")
        prediction = obj["response"]
        if not isinstance(prediction, str) or not prediction.strip():
            return "", "response must be a non-blank string"
        return prediction, None

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
            reasoning_effort=self.reasoning_effort,
            # C1: task child lives under the harness QUESTION_TIMEOUT (300s) --
            # a wedged tool must fail at the call level well before the whole
            # question subprocess is killed (S2 fixed the outer timeout; this
            # keeps a single slow tool from spending the entire question).
            tool_timeout_s=240,
            # Reserved cost keys (docs/10 §10): forwarded only when set --
            # None keeps the chat-loop defaults (40 tool calls / no token stop).
            **({"max_tool_calls": self.max_tool_calls}
               if self.max_tool_calls is not None else {}),
            **({"max_total_tokens": self.token_budget}
               if self.token_budget is not None else {}),
            # Context-management session (docs/10 §11 D6): forwarded only
            # when set -- None keeps the chat loop seam-free.
            **({"cm": self.cm} if self.cm is not None else {}),
        )

        # A final format slip should not discard otherwise useful work. Give
        # the model one no-tools repair turn, then keep the blank sentinel if
        # it still cannot produce the required response contract.
        try:
            prediction, extraction_error = self._extract_prediction(new_msg_history)
        except Exception as e:
            prediction, extraction_error = "", f"extraction error: {e}"
        if extraction_error:
            self.log(f"prediction_extract failed: {extraction_error}; "
                     "requesting one format-repair turn")
            repair_instruction = f"""Your previous final output could not be accepted: {extraction_error}.

Do not call any tool. Return the COMPLETE final answer from your prior work as exactly one tagged JSON object, with no markdown fences and no text outside the tags. The response value must be a non-empty JSON string; write math in plain text without LaTeX backslash escapes.
<json>
{{
    "response": "complete final answer"
}}
</json>"""
            try:
                new_msg_history = chat_with_agent(
                    repair_instruction,
                    model=self.model,
                    msg_history=new_msg_history,
                    logging=self.log,
                    tools_available=[],
                    trajectory_file=getattr(self, "trajectory_file", None),
                    reasoning_effort=self.reasoning_effort,
                    max_tool_calls=0,
                    **({"cm": self.cm} if self.cm is not None else {}),
                )
                prediction, extraction_error = self._extract_prediction(new_msg_history)
            except Exception as e:
                prediction, extraction_error = "", f"format-repair turn failed: {e}"
            if extraction_error:
                self.log(f"prediction_extract repair failed: {extraction_error}")

        return prediction, new_msg_history
