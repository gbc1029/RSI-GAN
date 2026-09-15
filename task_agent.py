import json
import os

from agent.base_agent import AgentSystem
from agent.llm_withtools import chat_with_agent
from utils.common import extract_jsons

_DEFAULT_DESIGN = {"prompt": "You are an agent.", "skills": [], "memory": None, "params": {}}


def _load_design():
    path = os.environ.get("GAN_TASK_DESIGN")
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return dict(_DEFAULT_DESIGN)


class TaskAgent(AgentSystem):
    def forward(self, inputs):
        """
        A design-driven agent that solves a given task.

        The shallow design (prompt + selected skills + memory) comes from the
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

        instruction = f"""{prompt}

Task input:
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
