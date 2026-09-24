import json
import os

from agent.base_agent import AgentSystem
from agent.llm_withtools import chat_with_agent
from utils.common import extract_jsons

_DEFAULT_DESIGN = {"prompt": "You are an agent.", "skills": [], "params": {}}


def _load_design():
    """Load the framework-injected task design (evolved prompt + skills).

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
        design config passed via the ``GAN_TASK_DESIGN`` env var; skills are
        loaded from ``GAN_TASK_SKILLS_DIR``.

        Args:
            inputs (dict): input data for the task.

        Returns:
            tuple: (prediction, new_msg_history)
        """
        design = _load_design()
        prompt = design.get("prompt") or "You are an agent."
        skills = list(design.get("skills") or [])
        tools_dir = os.environ.get("GAN_TASK_SKILLS_DIR")
        task_brief = os.environ.get("GAN_TASK_BRIEF") or ""

        brief_block = f"{task_brief}\n\n" if task_brief else ""
        instruction = f"""{prompt}

{brief_block}Task input:
```
{inputs}
```

Respond in JSON format with the following schema:
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
            tools_available=(skills if skills else []),
            tools_dir=(tools_dir if skills else None),
            trajectory_file=getattr(self, "trajectory_file", None),
        )

        # Extract the response
        prediction = "None"
        try:
            extracted_jsons = extract_jsons(new_msg_history[-1]['text'])
            if extracted_jsons is not None and "response" in extracted_jsons[-1]:
                prediction = extracted_jsons[-1]['response']
        except Exception as e:
            self.log(f"Error extracting prediction: {e}")
            prediction = "None"

        return prediction, new_msg_history
