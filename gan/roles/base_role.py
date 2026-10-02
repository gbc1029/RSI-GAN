"""Base role: shared plumbing for planner / evaluator."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from agent.base_agent import AgentSystem
from agent.llm_withtools import chat_with_agent
from gan.design.store import DesignStore
from gan.framework import paths
from gan.registries.loader import load_registry_for_role
from gan.tools.assembly import assemble_tools_dir_reported


def _load_failures(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Failure entries of a load report, classified by owner (B27).

    Delegates to the single predicate/classifier source
    (``gan.tools.assembly.load_failures``); the ``_role``/``_code_root`` keys
    were stamped by ``_merge_load_report`` before this call.
    """
    from gan.tools.assembly import load_failures as _shared
    return _shared(report, role=str(report.get("_role") or ""),
                   code_root=report.get("_code_root"))


def warn_dropped_workspace_edits(broker, role: str, key: Any, records, output_dir: str,
                                 patch_str: str, *, exhausted: bool = False,
                                 rejected: Optional[str] = None) -> None:
    """Loud safety net for the silent-drop class (R1).

    Two independent probes (batch 24 / P2-A):

    1. **granted-root leftover** -- a session that left edits UNDER a granted
       path but built no patch has dropped them. The known cause was a deep
       tool that forgot to record its op; ``has_deep_write`` now covers the
       tools we ship, and this probe makes any future omission visible (one
       ``deep_edit_dropped`` event) instead of silent. Batch 13 (B41): the
       retry-exhausted / abandoned patch path is its own silent drop --
       ``patch_str`` is non-empty but DISCARDED wholesale, so when
       ``exhausted``/``rejected`` is passed the probe runs regardless of the
       gates: any workspace content then means edits that will not reach any
       commit.

    2. **gan-data outside every grant** -- the batch-23 guard now prevents
       blind edits ON granted copies, but files created OUTSIDE every granted
       path (knowledge md, catalog README, a new helper) never reach the patch
       builder's walk at all -- and probe 1 cannot see them either, precisely
       in the mixed sessions where a legal modify record also exists. Since
       the knowledge base is DATA (no registry gate will ever refuse its
       absence), a gan/-prefixed file that no grant covers and that the patch
       did not carry is reported SPECIFICALLY (`gan_data_left_unpatched`), so
       authoring a knowledge note never vanishes quietly. Files outside
       ``gan/**`` are scratch BY DESIGN and stay unprobed (no false alarms).

    Advisory only: a failure to compute either probe must never fail the
    session.
    """
    from gan.patch import build_patch_from_workspace, has_deep_write
    from utils.soft_fail import soft_fail
    forced = bool(exhausted or rejected)
    if broker is None:
        return
    # -- probe 1: granted-root leftover (unchanged semantics) ----------------
    if forced or not (patch_str or has_deep_write(records)):
        try:
            leftover = build_patch_from_workspace(broker, role, key)
        except Exception:  # noqa: BLE001 -- advisory probe must not fail the session
            leftover = ""
        if leftover:
            soft_fail(
                (f"{role}: workspace has {len(leftover)} bytes of deep edits that will not reach "
                 f"any commit"
                 + (f" -- the retry loop exhausted/abandoned the proposed patch"
                    if forced else
                    " but the session recorded no deep-write op")
                 + "; the edits were dropped"),
                event_path=paths.events_path(output_dir),
                event_type="deep_edit_dropped", role=role,
                phase=("exhausted" if forced else "no_record"),
            )
    # -- probe 2: gan/** files outside every granted path (batch 24) ---------
    # Runs ALWAYS: the whole point is mixed sessions where the record-based
    # gates above are already closed.
    try:
        _warn_uncovered_gan_files(broker, role, key, output_dir, patch_str)
    except Exception:  # noqa: BLE001 -- advisory probe must not fail the session
        pass


def _warn_uncovered_gan_files(broker, role: str, key: Any, output_dir: str,
                              patch_str: str) -> None:
    """Report ``gan/**`` workspace files that NO granted path covers (P2-A).

    The batch-23 edit guard protects edits ON granted copies; this closes the
    complementary hole: files created OUTSIDE every granted path never enter
    the patch builder's walk, and for DATA like the knowledge base no other
    gate (registry diff / orphan check) will ever refuse their absence. A
    `gan_data_left_unpatched` event keeps such authoring from vanishing
    quietly. Files outside ``gan/**`` are scratch by design -- never probed.
    """
    from gan.patch import build_patch_from_workspace
    from utils.soft_fail import soft_fail
    granted_paths = [str(g).replace("\\", "/").rstrip("/")
                     for g in (broker.granted_paths(role, key) or [])]
    pending = []
    for root, _dirs, files in os.walk(broker.src_dir(role, key)):
        for name in files:
            if name.endswith((".pyc", ".pyo")) or name.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(root, name),
                                  broker.src_dir(role, key)).replace(os.sep, "/")
            if not rel.startswith("gan/"):
                continue
            covered = any(g and (rel == g or rel.startswith(g + "/"))
                          for g in granted_paths)
            if not covered:
                pending.append(rel)
    if not pending:
        return
    soft_fail(
        f"{role}: {len(pending)} file(s) under gan/** exist in the workspace but are "
        f"covered by NO granted path, so the session patch cannot carry them "
        f"and they will be dropped at the outer boundary: "
        f"{sorted(pending)[:8]}. If these were meant to land (knowledge note / "
        f"catalog file), re-request their parent directories with "
        f"intent='modify' and the next patch will carry them.",
        event_path=paths.events_path(output_dir),
        event_type="gan_data_left_unpatched", role=role,
        files=sorted(pending)[:20],
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

    def _merge_load_report(self, path: str) -> None:
        """B27: fold the session's load outcome into ``assembly_report``.

        Best-effort and never fatal: the report is advisory evidence about the
        session's REAL toolset. When anything failed to load, the role also gets a
        loud event, and the failure list is split into agent-fixable items vs
        frozen-framework items so a consumer can route them correctly.
        """
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                report = json.load(f)
        except Exception:  # noqa: BLE001 -- advisory
            return
        report["_role"] = self.role
        report["_code_root"] = self.code_root
        failed = _load_failures(report)
        report = {k: v for k, v in report.items() if not k.startswith("_")}
        report["failed"] = failed
        if self.assembly_report is None:
            self.assembly_report = {}
        self.assembly_report["load"] = report
        if not failed:
            return
        try:
            from gan.framework import paths as _paths
            from utils import trajectory_log as _tlog
            _tlog.append(_paths.events_path(self.output_dir), {
                "type": "role_tools_loaded", "role": self.role,
                "instance": self.instance, "loaded": report.get("loaded") or [],
                "failed": failed,
                "framework_failed": [f for f in failed
                                     if f.get("kind") == "always_on_frozen"],
            })
        except Exception:  # noqa: BLE001 -- audit write must not fail the session
            pass

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
        # B27: sink for the LOAD outcome of this session's toolset. The assembly
        # report only proves what was copied; this catches the files that never
        # imported (or lost their tool API) and folds them into `assembly_report`
        # so the role's next session sees the real capability set.
        load_report_path = os.path.join(
            paths.logs_dir(self.output_dir),
            f"tools_load_{self.role}_{self.instance or 'default'}.json")
        prev_sink = os.environ.get("GAN_TOOLS_LOAD_REPORT")
        os.environ["GAN_TOOLS_LOAD_REPORT"] = load_report_path
        try:
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
        finally:
            if prev_sink is None:
                os.environ.pop("GAN_TOOLS_LOAD_REPORT", None)
            else:
                os.environ["GAN_TOOLS_LOAD_REPORT"] = prev_sink
        self._merge_load_report(load_report_path)
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
