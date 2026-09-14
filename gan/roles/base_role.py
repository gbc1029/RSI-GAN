"""Base role: shared plumbing for planner / evaluator."""
from __future__ import annotations

import json
import os
from typing import Any, List, Optional

from agent.base_agent import AgentSystem
from agent.llm_withtools import chat_with_agent
from gan.operators.registry import assemble_tools_dir, default_dirs


class Role(AgentSystem):
    def __init__(
        self,
        role: str,
        model: str,
        output_dir: str,
        prompt_text: str,
        ops_dirs: Optional[List[str]] = None,
        shared_dirs: Optional[List[str]] = None,
        chat_history_file: Optional[str] = None,
    ):
        self.role = role
        self.output_dir = os.path.abspath(output_dir)
        self.prompt_text = prompt_text or ""
        d_ops, d_shared = default_dirs(role)
        self.ops_dirs = ops_dirs if ops_dirs is not None else [d_ops]
        self.shared_dirs = shared_dirs if shared_dirs is not None else [d_shared]
        chf = chat_history_file or os.path.join(self.output_dir, f"{role}_chat_history.md")
        os.makedirs(os.path.dirname(chf), exist_ok=True)
        super().__init__(model=model, chat_history_file=chf)
        self.tools_dir = assemble_tools_dir(
            role,
            os.path.join(self.output_dir, "toolsets", role),
            self.ops_dirs,
            self.shared_dirs,
        )

    def run(
        self,
        instruction: str,
        msg_history: Optional[list] = None,
        tools_available: Any = "all",
        max_tool_calls: int = 40,
    ):
        full_msg = f"{self.prompt_text}\n\n# Task\n{instruction}"
        return chat_with_agent(
            full_msg,
            model=self.model,
            msg_history=msg_history or [],
            logging=self.log,
            tools_available=tools_available,
            tools_dir=self.tools_dir,
            max_tool_calls=max_tool_calls,
        )

    # AgentSystem ABC requires forward(); roles expose richer plan()/evaluate().
    def forward(self, instruction: str, msg_history: Optional[list] = None, **kwargs):
        return self.run(instruction, msg_history=msg_history, **kwargs)

    # -- role self-configuration (for self_improve) ------------------------
    def _self_config_path(self) -> str:
        return os.path.join(self.output_dir, f"{self.role}_self_config.json")

    def load_self_config(self):
        from gan.config.loader import Config
        p = self._self_config_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return Config(json.load(f))
        return Config({})

    def save_self_config(self, cfg: Any) -> None:
        with open(self._self_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2)
