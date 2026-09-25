"""Base role: shared plumbing for planner / evaluator."""
from __future__ import annotations

import os
from typing import Any, Optional

from agent.base_agent import AgentSystem
from agent.llm_withtools import chat_with_agent
from gan.design.store import DesignStore
from gan.framework import paths
from gan.registries.loader import load_registry_for_role
from gan.tools.assembly import assemble_tools_dir_reported


def warn_dropped_workspace_edits(broker, role: str, key: Any, records, output_dir: str,
                                 patch_str: str) -> None:
    """Loud safety net for the silent-drop class (R1).

    A session that left edits in the workspace but built no patch has DROPPED them.
    The known cause was a deep tool that forgot to record its op; ``has_deep_write``
    now covers the tools we ship, and this probe makes any future omission visible
    (one ``deep_edit_dropped`` event) instead of silent. Advisory only: a failure to
    compute the diff must never fail the session.
    """
    from gan.patch import build_patch_from_workspace, has_deep_write
    if broker is None or patch_str or has_deep_write(records):
        return
    try:
        leftover = build_patch_from_workspace(broker, role, key)
    except Exception:  # noqa: BLE001 -- advisory probe must not fail the session
        return
    if not leftover:
        return
    from utils.soft_fail import soft_fail
    soft_fail(
        f"{role}: workspace has {len(leftover)} bytes of uncommitted deep edits but "
        f"the session recorded no deep-write op; the edits were dropped",
        event_path=paths.events_path(output_dir),
        event_type="deep_edit_dropped", role=role,
    )


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
        # Run-attempt id (set by GanLoop._refresh_roles); keys the OUTER-level
        # trajectory file so re-running an outer never truncates a prior attempt.
        self.attempt_id = None
        self.assembly_report: Optional[dict] = None  # B7: last assembly report
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
        return paths.session_traj_file(self.output_dir, self.outer, genid, self.role,
                                       attempt=getattr(self, "attempt_id", None))

    def tools_dir_for(self) -> str:
        base = os.path.join(self.output_dir, "toolsets", self.role)
        return os.path.join(base, self.instance) if self.instance else base

    def refresh_tools(self) -> str:
        """(Re)assemble this instance's toolset from the current design.

        Rebuilds from scratch so that deselected components actually disappear.
        This is the fix for "eval_points changed but tools_dir not synced".

        B7: every assembly also produces a REPORT (design claims vs actual),
        attached to the instance (``self.assembly_report`` — consumed by the
        loop's self-improve receipts) and audited as a ``toolset_assembled``
        event. An assembly gap is information for the role's NEXT design
        decision, never silence.
        """
        rpt = assemble_tools_dir_reported(
            self.role,
            self.tools_dir_for(),
            config=self.design_store.load(self.role),
            clear=True,
            code_root=self.code_root,
        )
        self.tools_dir = rpt["dest"]
        self.assembly_report = rpt
        try:
            from utils import trajectory_log as _tlog
            _tlog.append(paths.events_path(self.output_dir), dict(
                {"type": "toolset_assembled", "instance": self.instance}, **rpt))
        except Exception as e:  # noqa: BLE001 -- audit write must not break assembly
            print(f"[WARN] toolset_assembled event write failed: {e}")
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
        # redact the just-written session (frozen policy; consistent with task).
        # B2: a redaction failure must NEVER leave the raw session file servable.
        # Semantics: one immediate retry (transient IO), then QUARANTINE -- the
        # raw file is renamed out of every reader's exact-name reach -- plus an
        # audit event and a stderr line. Deliberately NOT raised: the session's
        # design/eval work is sound; what fails closed here is the exposure
        # channel itself. (Switch to `raise` if the policy should bind the whole
        # session to redaction success.)
        if path:
            from gan.framework import trajectory as _traj
            last_err: Optional[Exception] = None
            for _redact_try in range(2):
                try:
                    _traj.redact_file(path)
                    break
                except Exception as e:
                    last_err = e
            if last_err is not None:
                quar = ""
                try:
                    quar = _traj.quarantine_unredacted(path, str(last_err))
                except Exception as qe:  # noqa: BLE001 -- quarantine must not mask
                    print(f"[ERROR] session quarantine failed for {path}: {qe}")
                try:
                    from gan.framework import paths as _paths
                    from utils import trajectory_log as _tlog
                    _tlog.append(_paths.events_path(self.output_dir), {
                        "type": "session_redaction_failed",
                        "path": os.path.abspath(path), "quarantined": quar,
                        "error": str(last_err)[:300]})
                except Exception:  # audit-log write failure: stderr covers it
                    pass
                print(f"[WARN] session redaction failed; quarantined={quar or 'FAILED'} "
                      f"-> {path}: {last_err}")
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
