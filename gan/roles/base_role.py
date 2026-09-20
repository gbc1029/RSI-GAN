"""Base role: shared plumbing for planner / evaluator."""
from __future__ import annotations

import os
from typing import Any, Optional

from agent.base_agent import AgentSystem
from agent.llm_withtools import chat_with_agent
from gan.design.store import DesignStore
from gan.framework import paths
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
        instance: Optional[str] = None,
        code_root: Optional[str] = None,
    ):
        self.role = role
        self.output_dir = os.path.abspath(output_dir)
        self.code_root = code_root
        # Per-generation instance key (e.g. "outer_3"). A role instance is
        # refreshed every outer generation: fresh design load, fresh toolset,
        # fresh chat history. None keeps the legacy single-instance layout.
        self.instance = instance
        self.outer = None
        if instance and str(instance).startswith("outer_"):
            try:
                self.outer = int(str(instance).split("_")[1])
            except (IndexError, ValueError):
                self.outer = None
        self.prompt_text = prompt_text or ""
        self.design_store = DesignStore(paths.design_root(self.output_dir))
        self.registry = self._load_registry()
        chf = chat_history_file or self.default_chat_path(role)
        os.makedirs(os.path.dirname(chf), exist_ok=True)
        super().__init__(model=model, chat_history_file=chf)
        # tools = always-on (work/design/deep) + opt-in components selected by own design
        self.refresh_tools()

    # -- instance layout ---------------------------------------------------
    def _load_registry(self):
        if self.code_root:
            gan = os.path.join(self.code_root, "gan")
            return load_registry_for_role(
                self.role,
                registry_dir=os.path.join(gan, "registries"),
                components_dir=os.path.join(gan, "components"),
            )
        return load_registry_for_role(self.role)

    def instance_key(self) -> str:
        return self.instance or "default"

    def access_key(self, node_id=None):
        """Source-workspace key: one workspace per role *instance* (per outer)."""
        return self.instance or node_id

    def default_chat_path(self, role: str) -> str:
        # outer-level trajectory file (also used by self_improve)
        return self.session_trajectory(None)

    def session_trajectory(self, genid: Any = None) -> str:
        """JSONL trajectory file for one session (outer-level if genid is None)."""
        return paths.session_traj_file(self.output_dir, self.outer, genid, self.role)

    def tools_dir_for(self) -> str:
        base = os.path.join(self.output_dir, "toolsets", self.role)
        return os.path.join(base, self.instance) if self.instance else base

    def refresh_tools(self) -> str:
        """(Re)assemble this instance's toolset from the current design.

        Rebuilds from scratch so that deselected components actually disappear.
        This is the fix for "eval_points changed but tools_dir not synced".
        """
        self.tools_dir = assemble_tools_dir(
            self.role,
            self.tools_dir_for(),
            config=self.design_store.load(self.role),
            clear=True,
            code_root=self.code_root,
        )
        return self.tools_dir

    def current_prompt(self) -> str:
        cfg = self.design_store.load(self.role)
        return cfg.get("prompt") or self.prompt_text

    def run(
        self,
        instruction: str,
        msg_history: Optional[list] = None,
        tools_available: Any = "all",
        max_tool_calls: int = 40,
        trajectory_file: Optional[str] = None,
    ):
        full_msg = f"{self.current_prompt()}\n\n# Task\n{instruction}"
        path = trajectory_file or getattr(self, "trajectory_file", None)
        hist, info = chat_with_agent(
            full_msg,
            model=self.model,
            msg_history=msg_history or [],
            logging=self.log,
            tools_available=tools_available,
            tools_dir=self.tools_dir,
            max_tool_calls=max_tool_calls,
            trajectory_file=path,
            return_info=True,
        )
        # expose the last run's outcome (truncated/tool_calls) to callers
        self.last_run_info = info
        # redact the just-written session (frozen policy; consistent with task)
        try:
            from gan.framework import trajectory as _traj
            _traj.redact_file(path)
        except Exception:
            pass
        return hist

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
