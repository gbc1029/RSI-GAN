"""Base role: shared plumbing for planner / evaluator."""
from __future__ import annotations

import os
from typing import Any, Optional

from agent.base_agent import AgentSystem
from agent.llm_withtools import chat_with_agent
from gan.design.store import DesignStore
from gan.registries.loader import load_registry_for_role
from gan.tools.assembly import assemble_tools_dir


class Role(AgentSystem):
    def __init__(
        self,
        role: str,
        model: str,
        output_dir: str,
        prompt_text: str = "",
        chat_history_file: Optional[str] = None,
    ):
        self.role = role
        self.output_dir = os.path.abspath(output_dir)
        self.prompt_text = prompt_text or ""
        self.design_store = DesignStore(os.path.join(self.output_dir, "design"))
        self.registry = load_registry_for_role(role)
        chf = chat_history_file or os.path.join(self.output_dir, f"{role}_chat_history.md")
        os.makedirs(os.path.dirname(chf), exist_ok=True)
        super().__init__(model=model, chat_history_file=chf)
        # tools = always-on (work/design/deep) + opt-in components selected by own design
        self.tools_dir = assemble_tools_dir(
            role, os.path.join(self.output_dir, "toolsets", role), config=self.design_store.load(role)
        )

    def current_prompt(self) -> str:
        cfg = self.design_store.load(self.role)
        return cfg.get("prompt") or self.prompt_text

    def run(
        self,
        instruction: str,
        msg_history: Optional[list] = None,
        tools_available: Any = "all",
        max_tool_calls: int = 40,
    ):
        full_msg = f"{self.current_prompt()}\n\n# Task\n{instruction}"
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

    # -- role self-design (for self_improve) -------------------------------
    def self_design_path(self) -> str:
        return self.design_store.path(self.role)

    def load_self_config(self) -> dict:
        return self.design_store.load(self.role)

    def save_self_config(self, cfg: dict) -> str:
        return self.design_store.save(cfg, self.role)
