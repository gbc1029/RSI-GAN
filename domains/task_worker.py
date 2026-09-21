"""Minimal TaskAgent entry point executed inside the per-question sandbox."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys


_TASK_RESULT_PREFIX = "__RSI_TASK_RESULT__"


def _load_task_agent(agent_path: str):
    if agent_path.endswith(".py") or os.path.exists(agent_path):
        spec = importlib.util.spec_from_file_location(
            "sandboxed_task_agent", os.path.abspath(agent_path),
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load TaskAgent from {agent_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(agent_path)
    if not hasattr(module, "TaskAgent"):
        raise AttributeError(f"No TaskAgent found in {agent_path}")
    return module.TaskAgent


def main() -> None:
    if "GAN_DATASET_ROOT" in os.environ:
        raise RuntimeError("GAN_DATASET_ROOT must not enter the TaskAgent sandbox")

    payload = json.load(sys.stdin)
    TaskAgent = _load_task_agent(payload["agent_path"])
    agent = TaskAgent(
        model=payload["model"],
        chat_history_file=payload["trajectory_path"],
    )
    prediction, _ = agent.forward(payload["inputs"])
    print(
        _TASK_RESULT_PREFIX
        + json.dumps({"prediction": prediction}, ensure_ascii=False, default=str),
        flush=True,
    )


if __name__ == "__main__":
    main()
