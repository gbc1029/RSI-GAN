"""Concrete task runners (thin glue).

Heavy lifting (design persistence, env assembly, repo copy/patch, harness/report
invocation and the objective ``report_summary``) lives in the frozen framework:
``gan/framework/task_execution.py``.

``DomainTaskRunner`` runs the original per-domain harness and performs shared
label-domain scoring in the parent process before returning a task-tree
``Node``. The task agent is **design-driven**: its design config
(shallow, from the planner) is persisted by the framework and passed to the
harness through ``GAN_TASK_DESIGN``; the selected tools are exposed via
``GAN_TASK_TOOLS_DIR`` (``GAN_TASK_SKILLS_DIR`` is read only as a legacy
fallback). Deep changes are validated and committed to the per-run code tree,
and the generation's minimal repo copy is materialized from that tree; the
throwaway copy is the fallback when no code tree is in play.
"""
from __future__ import annotations

import csv
import os
import json
import shutil
import sys
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from gan.framework import task_execution as tx
from gan.framework import paths, scores, code_repo
from gan.framework.loader import load_registry, resolve_domain
from gan.design.store import DesignStore
from gan.framework.tree.store import Node, NodeValue

def _modify_depth(records: List[Dict[str, Any]]) -> int:
    from gan.patch import has_deep_write

    records = records or []
    if has_deep_write(records):
        return 2          # code-level change (incl. register/unregister)
    if records:
        return 1          # operator-level (shallow design)
    return 0


def _final_output_summary(
    predictions_path: str,
    max_rows: int = 50,
    tail_chars: int = 96,
) -> Dict[str, Any]:
    """Bounded, score-free evidence for checking apparent output truncation."""
    summary: Dict[str, Any] = {
        "source": "final_predictions.csv", "rows": [], "omitted_rows": 0,
    }
    try:
        with open(predictions_path, "r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = list(reader.fieldnames or [])
            id_col = "Problem ID" if "Problem ID" in fields else next(
                (name for name in fields if name != "prediction"), None)
            for index, row in enumerate(reader):
                if index >= max_rows:
                    summary["omitted_rows"] += 1
                    continue
                value = str(row.get("prediction") or "")
                summary["rows"].append({
                    "question_id": (
                        str(row.get(id_col) or index) if id_col else str(index)),
                    "blank": not bool(value.strip()),
                    "chars": len(value),
                    "tail": value[-tail_chars:],
                })
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"[:300]
    return summary


# Only a PROPOSED patch that produced no new code commit is a rejection.
# "No patch proposed this round" is the normal case and must not masquerade as
# one (it would report rejected:true to the receipt / events / evaluator
# surfaces and hand the planner a phantom "fix the reported problem" hint).
TASK_PATCH_REJECTED_NO_COMMIT = "no new code commit (patch rejected or absent)"


def _task_change_summary(submitted_patch: Any, config: Any,
                         parent: Optional[Node]) -> Dict[str, Any]:
    """Build a bounded, score-free summary for the fixed challenge gate.

    The adjudicator needs to judge the planner's actual task change, but it
    must not receive an unbounded trajectory or hidden grader data.  Config
    output is limited to changed keys against the persisted parent snapshot;
    source patches contain only added lines and are length-capped.
    """
    raw_patch = str(submitted_patch or "")
    added_patch = "\n".join(
        line[1:] for line in raw_patch.splitlines()
        if line.startswith("+") and not line.startswith("+++"))
    parent_config = {}
    if parent is not None and isinstance(parent.meta, dict):
        value = parent.meta.get("config_dict")
        if isinstance(value, dict):
            parent_config = value
    current_config = config if isinstance(config, dict) else {}
    changed = {}
    for key in sorted(set(parent_config) | set(current_config), key=str):
        before = parent_config.get(key)
        after = current_config.get(key)
        if before != after:
            try:
                encoded = json.dumps(after, ensure_ascii=False, sort_keys=True,
                                     default=str)
            except (TypeError, ValueError):
                encoded = str(after)
            changed[str(key)] = encoded[:2400]
    return {
        "submitted_patch_added": added_patch[:6000],
        "config_changed": changed,
        "patch_submitted": bool(raw_patch.strip()),
    }


def summarize_question_failures(path: str) -> Optional[Dict[str, Any]]:
    """Fold the harness's per-question failure sidecar into ONE event payload.

    Why this exists (docs/19 §12.54-A): the harness isolates a failed question
    into a blank prediction, so a generation in which EVERY question died (e.g.
    the task child could not be spawned at all) used to reach the loop as
    "score 0 / worse agent" — an infrastructure break wearing a task-quality
    costume. These counts are the durable evidence that separates the two:
    ``all_failed`` plus a single dominant ``error_type`` is a systemic break;
    the same failures spread over several error types are ordinary noise.

    Reads the sidecar the harness writes (``eval_failures.jsonl``: per-question
    records + one ``question_failures_summary`` line, docs/19 §12.54-A).
    Returns None when the file is missing/empty/record-free — absence is "no
    failures recorded", the only honest reading of a missing sidecar
    (None is not 0). Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().splitlines()
    except OSError:
        return None
    records: List[Dict[str, Any]] = []
    summary_line: Optional[Dict[str, Any]] = None
    # S (docs/19 §12.61): a torn line is TOLERATED (it must not sink the
    # summary) but never SILENT — a truncated tail is the trace of a writer
    # that died mid-append, and the fold is the only reader that can tell.
    # The count rides the returned payload so the loss is visible downstream.
    torn = 0
    non_dict = 0
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            torn += 1
            continue          # a torn last line must not sink the whole summary
        if not isinstance(row, dict):
            non_dict += 1
            continue
        if row.get("kind") == "question_failures_summary":
            summary_line = row
        else:
            records.append(row)
    if not records:
        # The summary line is appended AFTER the per-question records, so an
        # empty record half means either "no failures" or "the records did not
        # survive". Trust the harness's own counts in the latter case instead
        # of reading the absence as health (a missing record is not a healthy
        # round when the summary says N>0 died).
        failed = int((summary_line or {}).get("failed") or 0)
        if summary_line and failed > 0:
            out: Dict[str, Any] = {
                "failed": failed,
                "records_missing": True,
                "error_types": dict(summary_line.get("error_types") or {}),
            }
            if torn or non_dict:
                out["torn_lines"] = torn + non_dict
            out["attempted"] = summary_line.get("attempted")
            out["all_failed"] = bool(summary_line.get("all_failed"))
            if out["all_failed"] and len(out["error_types"]) == 1:
                out["systemic"] = True
                out["error_type"] = next(iter(out["error_types"]))
            return out
        return None
    error_types: Dict[str, int] = {}
    phases: Dict[str, int] = {}
    for row in records:
        kind = str(row.get("error_type") or "unknown")
        error_types[kind] = error_types.get(kind, 0) + 1
        phase = str(row.get("phase") or "unknown")
        phases[phase] = phases.get(phase, 0) + 1
    out = {
        "failed": len(records),
        "error_types": error_types,
        "phases": phases,
        "questions": [str(r.get("question_id")) for r in records][:20],
    }
    if torn or non_dict:
        # S (§12.61): conditional, like every other absence-means-clean field
        # — a truncated sidecar tail is now visible instead of silent.
        out["torn_lines"] = torn + non_dict
    if summary_line:
        # The harness's own denominator: it counts what THIS run attempted, so
        # "all failed" is exact instead of being inferred from predictions.csv.
        out["attempted"] = summary_line.get("attempted")
        out["all_failed"] = bool(summary_line.get("all_failed"))
    if out.get("all_failed") and len(error_types) == 1:
        # Every attempted question died the SAME way: no agent behaviour
        # produces that shape (it is one broken spawn/transport, not a bad
        # policy). Downstream issue-checkers consume it as an infra fact.
        out["systemic"] = True
        out["error_type"] = next(iter(error_types))
    return out


def pool_check(domain: str, domain_cfg: Dict[str, Any], num_samples: int,
               dataset_root: Optional[str] = None, subset: str = "") -> None:
    """Fail fast when a domain's candidate pool cannot satisfy --num_samples.

    smoke_1022_222630 lesson: a pool smaller than the requested sample count
    silently collapses the score denominator (resolved/2 surfaced as 0.5/1.0
    with coverage 0.4 under --num_samples 5, every row score_status="partial")
    -- refuse at startup instead. The same silent-shrink class exists for EVERY
    pool-based domain, so this check is domain-generic: polyglot resolves its
    pinned eval_task_list / full metadata pool; every other domain resolves its
    dataset CSV pool via ``tx.load_pool_ids`` (balrog has no pool semantics and
    is skipped by the empty-pool guard). An unreadable pool defers to the loud
    error at question-prep time (this check must never become the fragile part
    of startup).
    """
    requested = int(num_samples or 0)
    if requested <= 0:
        return
    label = f"{domain} candidate pool"
    if domain == "polyglot":
        pinned = domain_cfg.get("eval_task_list") or []
        if pinned:
            if requested > len(pinned):
                raise ValueError(
                    f"polyglot eval_task_list has {len(pinned)} tasks but "
                    f"--num_samples {requested}: the score denominator would "
                    f"silently shrink to {len(pinned)}. Extend "
                    f"gan/framework/domains.yaml eval_task_list to >= "
                    f"{requested} entries (per-task eval images build lazily "
                    f"on first eval) or lower --num_samples.")
            return
        label = "polyglot full pool"
    pool: Optional[List[str]] = None
    if dataset_root:
        try:
            pool = tx.load_pool_ids(domain, dataset_root, subset)
        except Exception:  # noqa: BLE001 -- question prep raises the real error
            return
    if pool and requested > len(pool):
        raise ValueError(
            f"{label} has {len(pool)} instances but --num_samples "
            f"{requested}: the score denominator would silently cap at "
            f"{len(pool)}. Lower --num_samples.")


def apply_domain_token_budget(config: Dict[str, Any],
                              domain_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Inject the domain-configured per-question token budget (docs/10 §10).

    ``domains.yaml`` ``token_budget_per_question`` is the operator-side
    enforcement value for the child's token SOFT STOP: it is written into the
    task design's RESERVED cost slot (framework-owned, in place pre-persist --
    the same pattern as the clamp below) so the persisted design.json stays
    the single source the sandboxed harness extracts (the run dir has no
    gan/). Precedence: min of the design-declared value (if any) and the
    domain value -- the domain config is operator governance; the evolving
    design may lower its own budget but never raise it above the operator's
    line. The gen ceiling (``token_budget_per_gen``) is applied afterwards by
    the frozen clamp (cost_keys.clamp_cost_config).

    Returns the injection record for the cost_keys_applied event, or None.
    """
    domain_value = domain_cfg.get("token_budget_per_question")
    if not domain_value:
        return None
    declared = (config.get("cost") or {}).get("per_question_token_budget")
    effective = int(domain_value)
    if type(declared) is int and declared > 0:
        effective = min(effective, int(declared))
    cost_slot = config.setdefault("cost", {})
    if cost_slot.get("per_question_token_budget") != effective:
        cost_slot["per_question_token_budget"] = effective
    return {"domain_value": int(domain_value), "design_declared": declared,
            "effective": effective}


def task_patch_rejection(patch_str: str, task_code_commit: Optional[str],
                         base_commit: Optional[str]) -> Optional[str]:
    """Classify the task-patch outcome: the rejection reason, or None.

    None covers BOTH success (a new commit landed) and "no patch was proposed";
    the two are distinguishable downstream via ``patch_applied`` and the
    receipt's ``code_patch.proposed`` -- never via this field. A proposed patch
    whose diff turns out empty also lands here (acted, produced nothing).
    """
    if not (patch_str or "").strip():
        return None
    if task_code_commit and base_commit and task_code_commit == base_commit:
        return TASK_PATCH_REJECTED_NO_COMMIT
    return None


class DomainTaskRunner:
    def __init__(
        self,
        repo_root: str,
        output_dir: str,
        domain: str = "paper_review",
        subset: str = "_filtered_100_train",
        num_samples: int = 2,
        default_model: str = "gpt-4o-mini",
        python: Optional[str] = None,
        timeout: int = 1800,
        code_root: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        sample_seed_base: Optional[int] = None,
        anchor_ids: Optional[list] = None,
        fixed_question_ids: Optional[list] = None,
        token_budget_per_gen: Optional[int] = None,
        ledger_writer: Optional[Any] = None,
        task_repo: Optional[str] = None,
        rebuild_view: Optional[Any] = None,
    ):
        self.repo_root = os.path.abspath(repo_root)
        self.output_dir = os.path.abspath(output_dir)
        self.code_root = os.path.abspath(code_root) if code_root else None
        # Split layout (docs/12): the task patch lands in the task repo, and the
        # derived composed view is re-materialized afterwards so the harness runs
        # on the just-applied code.  None in single layout (== self.code_root).
        self.task_repo = os.path.abspath(task_repo) if task_repo else None
        self.rebuild_view = rebuild_view
        self.domain = domain
        self.subset = subset
        self.num_samples = num_samples
        self.default_model = default_model
        # Sampling scheme (framework-owned): per-generation seed derived from
        # the run-persistent base seed; anchor_ids form the fixed paired core.
        # None base seed -> legacy prefix slice (explicit A/B baseline).
        self.sample_seed_base = sample_seed_base
        self.anchor_ids = list(anchor_ids or [])
        # Fixed evaluation question set (docs/14 section 5.5). When non-empty,
        # the parent-scored path bypasses ``select_samples`` entirely and runs
        # EXACTLY these ids -- the only way to hold the question set constant
        # across outers (``select_samples`` always adds a per-genid draw and
        # caps its anchor core at ``num_samples // 2``). Used by the separate
        # evaluation runner; the main-line runner leaves it empty.
        self.fixed_question_ids = [str(q) for q in (fixed_question_ids or [])]
        if self.fixed_question_ids and int(num_samples) != len(self.fixed_question_ids):
            # num_samples feeds the harness wall-clock budget, the coverage
            # denominator and the broker call budget: it must match the set
            # actually run, and the fixed set is the authority.
            self.num_samples = len(self.fixed_question_ids)
        # Thinking-intensity knob for the task child's every LLM call; rides
        # the stdin payload (explicit transport, never an env — same chain as
        # the budget fields; the CLI --reasoning_effort argument feeds it).
        # Validated upstream by models.resolve_entry(); None -> gateway default.
        self.reasoning_effort = reasoning_effort
        # Reserved cost-key ceiling (docs/10 §10): loop.yaml
        # ``cost.token_budget_per_gen`` bounds the task design's
        # per-question token budget (passed by gan/build.py from the loop cfg).
        self.token_budget_per_gen = token_budget_per_gen
        self.python = python or sys.executable
        self.timeout = timeout

        reg = load_registry()
        self.domain_cfg = resolve_domain(reg, domain)
        self.timeout = int(self.domain_cfg.get("harness_timeout_s") or timeout)
        self.score_key = self.domain_cfg.get("score_key")
        self.output_contract = self.domain_cfg.get("output_contract") or {}
        self.task_brief = self.domain_cfg.get("task_brief") or ""
        # Framework-owned final-answer requirement (F4/C1): one derivation,
        # three consumers — the child's explicit output-contract section, the
        # format-repair turn, and the planner's get_config visibility field.
        self.response_contract = tx.derive_response_contract(domain, self.domain_cfg)

        # Fail fast (smoke_1022_222630 lesson, see pool_check): the candidate
        # pool (a pinned eval_task_list, the full metadata pool for polyglot,
        # or the dataset CSV pool for every other domain) is NOT expanded by
        # --num_samples. Refuse at startup whenever the pool cannot satisfy
        # the requested sample count -- domain-generic since the same silent
        # denominator shrink exists for CSV domains.
        pool_check(self.domain, self.domain_cfg, self.num_samples,
                   dataset_root=self.repo_root, subset=self.subset)

        # B46 step-1 gate: the per-genid task design persist (persist_design)
        # writes through the run's WriterToken threaded from build_gan_loop.
        self.ledger_writer = ledger_writer
        self.design_store = DesignStore(paths.design_root(self.output_dir),
                                        writer=ledger_writer)
        self.log_path = paths.log_file(self.output_dir, "task_runner.log")

    # -- helpers -----------------------------------------------------------
    # NOTE: the model is NOT read from the (evolvable) design config any more.
    # It is resolved centrally by gan/framework/models.py and passed in as
    # ``default_model`` (frozen model selection).

    def _event(self, event_type: str, **fields: Any) -> None:
        """Append a timing/outcome event to the run's events.jsonl (advisory)."""
        try:
            from utils import trajectory_log as _tlog
            _tlog.append(paths.events_path(self.output_dir),
                         {"type": event_type, "domain": self.domain, **fields})
        except Exception as e:  # noqa: BLE001 -- audit write failure: stderr covers
            print(f"[WARN] {event_type} event write failed: {e}")

    def _ingest_cm_events(self, run_dir: str, run_id: str, genid: Any) -> None:
        """Fold the CM seam-event sidecar into events.jsonl (docs/10 §11.5,
        transport reworked by F5 P1 / docs/19 §12.28): each line is
        re-emitted with role=task attribution so the per-generation cost
        record and the offline reconcilers see the seam activity in the ONE
        audit stream. Advisory end to end: never a run failure -- but NEVER
        silent (no-silent-fail rule, AGENTS.md): a MISSING or EMPTY sidecar
        is itself a finding and gets its own event. The sidecar dies with
        the pruned work dir either way.

        F5 WRITER CHANGE: the sidecar is no longer child-written. The
        trusted harness receives the child's seam events over the run-scoped
        telemetry socket and appends them here (pre-created, so a
        zero-event run reads as present-but-empty); the child holds no
        write path to this file at all."""
        sidecar = paths.task_cm_events_path(run_dir, run_id)
        if not os.path.isfile(sidecar):
            self._event("cm_events_ingest_empty", genid=str(genid),
                        reason="sidecar file missing (harness failed before "
                               "staging the telemetry run)")
            return
        try:
            with open(sidecar, "r", encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except OSError as e:
            from utils.soft_fail import soft_fail
            soft_fail(f"cm events sidecar unreadable (genid {genid}): {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="cm_events_ingest_failed", genid=str(genid))
            return
        emitted = 0
        for ln in lines:
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue  # a torn last line must not sink the rest
            if not isinstance(e, dict):
                continue
            etype = str(e.pop("type", "cm_event"))
            self._event(etype, role="task", genid=str(genid), **e)
            emitted += 1
        try:
            os.remove(sidecar)
        except OSError:
            pass  # the work-dir prune is the backstop
        if emitted:
            self._event("cm_events_ingested", genid=str(genid), events=emitted)
        else:
            # A zero-event sidecar after a real harness run has TWO causes that
            # look identical from here (docs/19 §12.66): the child genuinely had
            # no seam session, OR the audit channel itself was down and the
            # child's events died with its tmpfs. Saying only the first
            # attribution sent a real investigation to the wrong layer for a
            # whole round, so the reason names the ambiguity and points at the
            # harness's own "[WARN] f5 audit channel delivered 0" line, which is
            # the discriminator.
            self._event("cm_events_ingest_empty", genid=str(genid),
                        reason="sidecar present but no seam events — either the "
                               "child ran without a cm session, or the audit "
                               "telemetry channel delivered nothing (see the "
                               "harness '[WARN] f5 audit channel delivered 0' "
                               "line in logs/task_runner.log)")

    def adjudicate_proof_challenges(
        self, parent: Node, child: Node, challenges: List[Dict[str, Any]],
        *, outer: int,
    ) -> Dict[str, Dict[str, Any]]:
        """Run the fixed parent/child adjudicator through the task broker."""
        if self.domain != "imo_proof" or not challenges:
            return {}
        scope_id = f"challenge_outer{int(outer)}_gen{int(child.genid)}"
        node_dir = paths.work_dir(self.output_dir, scope_id)
        source_root = self.code_root or self.repo_root
        run_dir, _ = tx.prepare_run_dir(
            source_root, node_dir, "", domain="imo_proof")
        os.makedirs(os.path.join(run_dir, ".gan_runtime"), exist_ok=True)
        run_id = f"gan_{scope_id}"
        output_dir = os.path.join(run_dir, "outputs", run_id)
        os.makedirs(output_dir, exist_ok=True)

        def proof_rows(node: Node) -> Dict[str, str]:
            path = os.path.join(
                paths.runs_dir(self.output_dir, node.genid), "predictions.csv")
            frame = pd.read_csv(path, dtype=str, keep_default_na=False)
            if "Problem ID" not in frame.columns or "prediction" not in frame.columns:
                raise RuntimeError("archived proof predictions lack id or prediction")
            if frame["Problem ID"].astype(str).duplicated().any():
                raise RuntimeError("archived proof predictions contain duplicate ids")
            return dict(zip(frame["Problem ID"].astype(str),
                            frame["prediction"].astype(str)))

        parent_proofs = proof_rows(parent)
        child_proofs = proof_rows(child)
        dataset, question_id_col, ground_truth_key = tx.load_dataset_frame(
            self.repo_root, "imo_proof", self.subset)
        trusted = dataset.copy()
        trusted[question_id_col] = trusted[question_id_col].astype(str)
        trusted = trusted.set_index(question_id_col, drop=False)
        rows = []
        unresolved: List[Dict[str, Any]] = []
        for challenge in challenges:
            qid = str(challenge.get("qid") or "")
            if qid not in trusted.index:
                unresolved.append({"challenge_id": challenge.get("challenge_id"),
                                   "qid": qid, "reason": "missing_trusted_qid"})
                continue
            missing_sides = []
            if qid not in parent_proofs:
                missing_sides.append("parent")
            if qid not in child_proofs:
                missing_sides.append("child")
            if missing_sides:
                unresolved.append({
                    "challenge_id": challenge.get("challenge_id"),
                    "qid": qid,
                    "reason": "missing_" + "_and_".join(missing_sides) + "_proof",
                })
                continue
            reference = trusted.loc[qid]
            rows.append({
                "Challenge ID": str(challenge.get("challenge_id") or ""),
                "Problem ID": qid,
                "Problem": str(reference["Problem"]),
                "Solution": str(reference[ground_truth_key]),
                "Grading guidelines": str(reference["Grading guidelines"]),
                "Parent Response": parent_proofs[qid],
                "Child Response": child_proofs[qid],
                "Defect Type": str(challenge.get("defect_type") or ""),
                "Claim": str(challenge.get("claim") or ""),
                "Evidence Ref": str(challenge.get("evidence_ref") or ""),
                "Repair Criterion": str(challenge.get("repair_criterion") or ""),
                "Task Change": json.dumps(
                    (child.meta or {}).get("task_change") or {},
                    ensure_ascii=False, sort_keys=True),
            })
        if unresolved:
            self._event(
                "challenge_adjudicator_unresolved",
                genid=str(child.genid), requested=len(challenges),
                resolved=len(rows), unresolved=unresolved,
            )
        if not rows:
            return {}
        input_dir = os.path.join(run_dir, "input")
        os.makedirs(input_dir, exist_ok=True)
        questions_path = os.path.join(input_dir, "questions.csv")
        pd.DataFrame(rows).to_csv(questions_path, index=False)

        env = {
            key: value for key, value in os.environ.items()
            if key.startswith("GAN_TASK_BROKER_")
        }
        rc, output = tx.run_harness_and_report(
            self.python, run_dir, "imo_challenge_adjudication", run_id,
            self.subset, len(rows), self.default_model, env, self.timeout,
            questions_path=questions_path, scope_id=scope_id,
            log_path=self.log_path, reasoning_effort=self.reasoning_effort,
        )
        if rc != 0:
            raise RuntimeError(
                "fixed challenge adjudicator failed: " + str(output)[-1000:])
        report_path = os.path.join(output_dir, "report.json")
        with open(report_path, "r", encoding="utf-8") as stream:
            report = json.load(stream)
        verdicts = report.get("challenge_adjudications")
        if not isinstance(verdicts, dict):
            raise RuntimeError("fixed challenge adjudicator report is malformed")
        evidence_dir = paths.runs_dir(self.output_dir, child.genid)
        os.makedirs(evidence_dir, exist_ok=True)
        shutil.copy2(
            report_path, os.path.join(evidence_dir, "challenge_adjudication.json"))
        determinate = sum(
            1 for verdict in verdicts.values()
            if isinstance(verdict, dict)
            and isinstance(verdict.get("parent_present"), bool)
            and isinstance(verdict.get("child_present"), bool)
            and verdict.get("actionable") is True)
        self._event(
            "challenge_adjudicator_done", genid=str(child.genid),
            requested=len(rows), parsed=len(verdicts), determinate=determinate)
        return {str(key): dict(value) for key, value in verdicts.items()
                if isinstance(value, dict)}

    # -- runner ------------------------------------------------------------
    def __call__(self, plan: Optional[Dict[str, Any]] = None, parent: Optional[Node] = None,
                 genid: Any = None, base: Optional[str] = None,
                 challenge_ids: Optional[List[Any]] = None) -> Node:
        plan = plan or {}
        active_challenge_ids = list(dict.fromkeys(
            str(q) for q in (challenge_ids or [])))
        records = plan.get("records", []) or []
        config = plan.get("config")
        if not isinstance(config, dict):
            config = {}
        patch_str = plan.get("patch", "") or ""
        submitted_patch = patch_str
        task_change = _task_change_summary(submitted_patch, config, parent)
        challenge_audit: Dict[str, Any] = {"triggered": False, "reason": None}
        if active_challenge_ids:
            from gan.framework import challenge_ledger
            challenge_audit = challenge_ledger.audit_single_question_hardcoding(
                active_challenge_ids, patch_str, config)
            if challenge_audit.get("triggered"):
                # Refuse before code_repo.apply_task_patch: the suspicious
                # response must never enter the task lineage.
                patch_str = ""

        node_dir = paths.work_dir(self.output_dir, genid)
        os.makedirs(node_dir, exist_ok=True)
        sample_info: Dict[str, Any] = {}

        # Branch-per-node: the selected parent's code state (checked out by the
        # loop BEFORE planning) is the base; the task patch is committed on top
        # of it with a ``task_<genid>`` ref. A rejected/absent patch aliases the
        # base itself — the per-node lineage never has a gap. (When code_repo is
        # off, fall back to patching the run copy.)
        source_root = self.code_root or self.repo_root
        apply_target = self.task_repo or self.code_root
        task_code_commit = None
        base_commit: Optional[str] = None
        code_ref_ok = True
        code_ref_mode = ""
        task_patch_files = []
        patch_applied = False
        task_patch_rejected = None
        if challenge_audit.get("triggered"):
            task_patch_rejected = "challenge response failed framework audit"
        if self.code_root and base:
            res = code_repo.apply_task_patch(
                apply_target, "planner", patch_str, genid,
                (parent.genid if parent is not None else "initial"), base,
                seat="plan")
            task_code_commit = res["code_commit"]
            base_commit = res["base_commit"]
            code_ref_ok = bool(res["ref_ok"])
            code_ref_mode = str(res["ref_mode"])
            patch_applied = bool(res["applied"])
            if patch_applied:
                task_patch_files = code_repo.changed_files(patch_str)
            # Only a PROPOSED patch with no new commit is a rejection --
            # "no patch proposed" must not masquerade as one.
            task_patch_rejected = (
                "challenge response failed framework audit"
                if challenge_audit.get("triggered") else
                task_patch_rejection(patch_str, task_code_commit, base_commit))
        elif (patch_str or "").strip() and apply_target:
            # legacy path (no base resolved): pre-branch time-line HEAD behaviour
            try:
                task_code_commit = code_repo.apply_code_patch(
                    apply_target, "planner", patch_str, f"task gen {genid}",
                    seat="plan")
                task_patch_files = code_repo.changed_files(patch_str)
                patch_applied = True
            except code_repo.PatchRejected as e:
                # The exception text quotes the planner's own artifacts
                # (comments/identifiers inside the rejected diff) and this field
                # rides _EVALUATOR_META_KEYS -- meta carries the SAME fixed
                # generic string as the main path; the full text stays on the
                # local audit log only.
                task_patch_rejected = TASK_PATCH_REJECTED_NO_COMMIT
                try:
                    with open(self.log_path, "a", encoding="utf-8") as lf:
                        lf.write(f"[genid {genid}] task patch rejected (legacy path): {e}\n")
                except OSError:
                    pass  # best-effort local audit line; the meta string is durable
            patch_str = ""  # applied to code_root (or rejected -> nothing to apply)

        # Split layout: re-materialize the composed view so the harness runs on
        # the just-applied task code (no-op in single layout / code_repo off).
        if self.rebuild_view is not None:
            self.rebuild_view()

        # Polyglot (Arch 2): the task child needs the whitelisted file-write
        # tool to produce its solution patch. Framework-injected capability
        # (analogous to task_brief): added before the heal, which keeps it --
        # the component is registered, so the slot heal never strips it.
        if self.domain == "polyglot":
            tools_slot = config.setdefault("tools", [])
            if "write_file" not in tools_slot:
                tools_slot.append("write_file")

        # Heal the design BEFORE it is persisted. The patch has
        # now either committed or rolled back to the parent's code, so
        # ``source_root`` is the authority for what this generation can assemble.
        # The design object is shared with the loop (``config_dict``), so healing
        # in place strips a dangling slot name from the persisted design file, the
        # node meta and the parent->child inheritance chain together; the stripped
        # names are recorded (event + meta) so the planner's next receipt shows
        # what was dropped and why.
        design_keys_stripped = tx.heal_design_keys(config, "task", source_root)
        design_stripped = tx.heal_design_slots(config, "task", source_root)
        design_stripped = design_keys_stripped + design_stripped
        if design_stripped:
            try:
                from gan.framework import paths as _paths
                from utils import trajectory_log as _tlog
                _tlog.append(_paths.events_path(self.output_dir), dict(
                    {"type": "design_dangling_stripped", "genid": str(genid),
                     "role": "task", "slot": "tools"}, names=design_stripped))
            except Exception as e:  # noqa: BLE001 -- audit is advisory; stderr covers
                print(f"[WARN] design_dangling_stripped event write failed: {e}")

        # Reserved cost keys (docs/10 §10, phase C): clamp the task design's
        # cost slot IN PLACE before the snapshot is persisted, so the persisted
        # design.json is canonical and the child-side extraction (harness — no
        # gan/ in the run dir) is a guarded pass-through that cannot drift from
        # this authority. Deviations are recorded (event + meta -> receipt).
        # Pre-clamp, the domain-configured per-question budget (if any) is
        # injected into the same reserved slot (operator governance: min with
        # any design-declared value) -- see apply_domain_token_budget.
        domain_budget = apply_domain_token_budget(config, self.domain_cfg)
        try:
            from gan.framework import cost_keys as _ck
            cost_clamps = _ck.clamp_cost_config(
                "task", config, ceiling_token_budget=self.token_budget_per_gen)
            cost_effective, _ = _ck.effective_all(
                "task", config, ceiling_token_budget=self.token_budget_per_gen)
        except Exception as e:  # noqa: BLE001 -- advisory, never fatal
            from utils.soft_fail import soft_fail
            soft_fail(f"cost key clamp failed for genid {genid}: {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="cost_keys_apply_failed", genid=str(genid))
            cost_clamps, cost_effective = [], {}
        self._event("cost_keys_applied", genid=str(genid), role="task",
                    keys=cost_effective, clamped=cost_clamps,
                    domain_budget_injected=domain_budget)

        # Framework: persist the design, prepare the minimal code copy, then
        # assemble sandbox-visible design/skills inside that copy.
        design_path = tx.persist_design(self.design_store, config, genid)
        model = self.default_model
        run_dir, patch_applied_copy = tx.prepare_run_dir(
            source_root, node_dir, patch_str, domain=self.domain,
        )
        env = tx.assemble_task_env(
            os.environ.copy(), design_path=design_path,
            run_dir=run_dir, config=config,
            code_root=self.code_root, task_brief=self.task_brief,
            response_contract=self.response_contract,
        )
        patch_applied = patch_applied or patch_applied_copy

        # Timeout plumbing: the per-question wall budget comes from the domain
        # registry (domains.yaml question_timeout_s; the harness defaults to
        # 300s when the env is absent) and the harness TOTAL budget scales
        # with it -- a fixed total silently breaks once the per-question
        # budget rises above total/N (observed: polyglot 2x1200s > 1800s).
        # The total is THE shared formula (task_execution.harness_total_timeout)
        # also used by the driver's broker registration -- the two launch
        # sites cannot disagree (they were two hand-copied formulae).
        question_timeout_s = int(self.domain_cfg.get("question_timeout_s") or 300)
        env["GAN_QUESTION_TIMEOUT_S"] = str(question_timeout_s)
        harness_timeout = tx.harness_total_timeout(self.domain_cfg, self.num_samples)
        # NOTE: the thinking-intensity knob is NOT an env variable -- it is
        # threaded as reasoning_effort= into each run_harness_and_report call
        # below and travels --reasoning_effort -> payload -> TaskAgent.

        # Surface the per-inner task toolset assembly report (written by
        # assemble_task_env into the runtime dir). Recorded as node meta (so the
        # planner's next-round receipt and the evaluator's meta view carry it)
        # and as a task_toolset_assembled event. Assembly gaps merely
        # degrade the child's capability — they must never silently vanish.
        toolset_report: Dict[str, Any] = {}
        toolset_report_path = os.path.join(run_dir, ".gan_runtime", "toolset_report.json")
        if os.path.isfile(toolset_report_path):
            try:
                with open(toolset_report_path, "r", encoding="utf-8") as f:
                    toolset_report = json.load(f)
            except Exception as e:  # noqa: BLE001 -- report is advisory meta
                toolset_report = {"error": f"unreadable toolset report: {e}"[:200]}
        if toolset_report:
            try:
                from gan.framework import paths as _paths
                from utils import trajectory_log as _tlog
                _tlog.append(_paths.events_path(self.output_dir), dict(
                    {"type": "task_toolset_assembled", "genid": str(genid)},
                    **toolset_report))
            except Exception as e:  # noqa: BLE001 -- audit write failure: stderr covers
                print(f"[WARN] task_toolset_assembled event write failed: {e}")

        run_id = f"gan_{genid}"
        report_path = os.path.join(run_dir, "outputs", run_id, "report.json")
        if os.path.exists(report_path):
            os.remove(report_path)

        output_path = os.path.dirname(report_path)
        os.makedirs(output_path, exist_ok=True)
        if self.domain == "polyglot":
            # Arch 2: the child's predictions are unified diffs; the parent
            # applies them inside the eval container and scores resolved/total.
            # Sampling mirrors the parent-scored CSV branch below: a
            # per-generation seed derived from the run-persistent base seed
            # rotates the question draw every generation (deterministic per
            # (base_seed, genid), so resume reproduces it); a None base seed
            # keeps the legacy prefix slice (explicit A/B baseline).
            per_gen_seed = (
                tx.derive_sample_seed(self.sample_seed_base, str(genid))
                if self.sample_seed_base is not None else None
            )
            if self.fixed_question_ids:
                # Fixed-root evaluation (docs/15 §12.5-d): the eval runner pins
                # the held-out id list -- exactly the same questions every
                # outer, no draw (mirror of the CSV branch's
                # prepare_questions_by_ids).
                questions_path, sample_info = tx.prepare_polyglot_questions_by_ids(
                    self.repo_root, run_dir, self.fixed_question_ids)
            else:
                questions_path, sample_info = tx.prepare_polyglot_questions(
                    self.repo_root, run_dir, self.domain_cfg, self.num_samples,
                    sample_seed=per_gen_seed, anchor_ids=self.anchor_ids)
            _t_harness = time.time()
            rc, _out = tx.run_harness_and_report(
                self.python, run_dir, self.domain, run_id, self.subset,
                self.num_samples, model, env, harness_timeout,
                questions_path=questions_path, scope_id=str(genid),
                log_path=self.log_path,
                reasoning_effort=self.reasoning_effort,
            )
            self._event("task_harness_done", genid=str(genid), rc=rc,
                        duration_s=round(time.time() - _t_harness, 1),
                        question_timeout_s=question_timeout_s,
                        harness_timeout_s=harness_timeout,
                        sample_mode=sample_info.get("mode"),
                        sample_seed=sample_info.get("sample_seed"),
                        sampled_ids=sample_info.get("sampled_ids"),
                        anchor_ids=sample_info.get("anchor_ids"),
                        challenge_ids=sample_info.get("challenge_ids"))
            report = tx.read_report(report_path) if rc == 0 else None
            if report is not None:
                self._event("task_eval_done", genid=str(genid),
                            eval_kind="docker_broker")
        elif tx.uses_parent_scoring(self.domain):
            per_gen_seed = (
                tx.derive_sample_seed(self.sample_seed_base, str(genid))
                if self.sample_seed_base is not None else None
            )
            if self.fixed_question_ids:
                questions_path, ground_truth_by_id, sample_info = (
                    tx.prepare_questions_by_ids(
                        self.repo_root, run_dir, self.domain, self.subset,
                        self.fixed_question_ids))
            else:
                questions_path, ground_truth_by_id, sample_info = tx.prepare_questions(
                    self.repo_root, run_dir, self.domain, self.subset, self.num_samples,
                    sample_seed=per_gen_seed, anchor_ids=self.anchor_ids,
                )
            # The child sees questions only; scoring remains in this parent.
            _t_harness = time.time()
            rc, _out = tx.run_harness_and_report(
                self.python, run_dir, self.domain, run_id, self.subset,
                self.num_samples, model, env, harness_timeout,
                questions_path=questions_path, scope_id=str(genid), log_path=self.log_path,
                reasoning_effort=self.reasoning_effort,
            )
            self._event("task_harness_done", genid=str(genid), rc=rc,
                        duration_s=round(time.time() - _t_harness, 1),
                        question_timeout_s=question_timeout_s,
                        harness_timeout_s=harness_timeout,
                        sample_mode=sample_info.get("mode"),
                        sample_seed=sample_info.get("sample_seed"),
                        sampled_ids=sample_info.get("sampled_ids"),
                        anchor_ids=sample_info.get("anchor_ids"),
                        challenge_ids=sample_info.get("challenge_ids"))
            predictions_path = os.path.join(output_path, "predictions.csv")
            report = None
            if os.path.exists(predictions_path):
                try:
                    _t_eval = time.time()
                    report = tx.write_parent_report(
                        predictions_path, report_path, self.domain, ground_truth_by_id,
                    )
                    self._event("task_eval_done", genid=str(genid),
                                duration_s=round(time.time() - _t_eval, 1),
                                eval_kind="parent")
                except Exception as e:
                    # A parent-scoring failure must not degrade
                    # silently; score reads None -> score_status="failed".
                    report = None
                    from utils.soft_fail import soft_fail
                    soft_fail(f"parent scoring failed for genid {genid}: {e}",
                              event_path=paths.events_path(self.output_dir),
                              event_type="parent_scoring_failed", genid=str(genid))
        else:
            # Preserve independent harness/evaluator domains.
            if self.fixed_question_ids:
                # Fixed-root evaluation must use the same explicit question
                # ids on every outer.  The broker receives this file through
                # the eval scope and must not resample the training subset.
                questions_path, _ground_truth, sample_info = (
                    tx.prepare_questions_by_ids(
                        self.repo_root, run_dir, self.domain, self.subset,
                        self.fixed_question_ids))
                rc, _out = tx.run_harness_and_report(
                    self.python, run_dir, self.domain, run_id, self.subset,
                    self.num_samples, model, env, self.timeout,
                    questions_path=questions_path, scope_id=str(genid),
                    log_path=self.log_path,
                    reasoning_effort=self.reasoning_effort,
                )
            else:
                if self.domain == "imo_proof":
                    # Materialize the selected questions in the framework so
                    # the actual train qids are auditable and can be bound to
                    # a blind challenge. The selection rule is unchanged:
                    # seeded runs use the existing per-generation seed and
                    # anchors; no-seed runs retain the legacy prefix.
                    per_gen_seed = (
                        tx.derive_sample_seed(self.sample_seed_base, str(genid))
                        if self.sample_seed_base is not None else None
                    )
                    questions_path, _ground_truth, sample_info = tx.prepare_questions(
                        self.repo_root, run_dir, self.domain, self.subset,
                        self.num_samples, sample_seed=per_gen_seed,
                        anchor_ids=self.anchor_ids,
                        challenge_ids=active_challenge_ids,
                    )
                    rc, _out = tx.run_harness_and_report(
                        self.python, run_dir, self.domain, run_id, self.subset,
                        self.num_samples, model, env, self.timeout,
                        questions_path=questions_path, scope_id=str(genid),
                        log_path=self.log_path,
                        reasoning_effort=self.reasoning_effort,
                    )
                else:
                    rc, _out = tx.run_harness_and_report(
                        self.python, run_dir, self.domain, self.subset,
                        self.num_samples, model, env, self.timeout,
                        dataset_root=self.repo_root, scope_id=str(genid), log_path=self.log_path,
                        reasoning_effort=self.reasoning_effort,
                    )
            if self.domain == "imo_proof":
                self._event("task_harness_done", genid=str(genid), rc=rc,
                            question_timeout_s=question_timeout_s,
                            harness_timeout_s=self.timeout,
                            sample_mode=sample_info.get("mode"),
                            sampled_ids=sample_info.get("sampled_ids"),
                            anchor_ids=sample_info.get("anchor_ids"),
                            challenge_ids=sample_info.get("challenge_ids"))
            report = None
            if self.domain == "imo_proof":
                # Proof grading is completed inside the privileged broker with
                # the same scoped proxy token; never run the legacy in-process
                # report path from the outer worker.
                report = tx.read_report(report_path) if rc == 0 else None
            elif rc == 0 or self.domain not in {"balrog_babyai", "balrog_babaisai"}:
                try:
                    report = tx.run_existing_domain_report(self.domain, output_path, model)
                except Exception as e:
                    # Same visibility contract as the parent-scoring path.
                    from utils.soft_fail import soft_fail
                    soft_fail(f"existing-domain report failed for genid {genid}: {e}",
                              event_path=paths.events_path(self.output_dir),
                              event_type="existing_domain_report_failed", genid=str(genid))
        # Context-management audit exit (docs/10 §11.5): fold the child's
        # seam events into events.jsonl on EVERY post-harness path (the child
        # may have run a seam session even when scoring later failed).
        self._ingest_cm_events(run_dir, run_id, genid)
        # Persist per-question failure diagnostics into runs/<genid>/: the work
        # dir is pruned at generation end, so anything left only under an
        # outputs/<run_id>/agent_evals directory dies before it can be read.
        evidence_dir = paths.runs_dir(self.output_dir, genid)
        # runs/<genid>/ may not exist yet at this point (the pre-existing
        # report/predictions copy below is what creates it) -- create it here
        # or the copy2 below dies with FileNotFoundError, silently swallowed.
        os.makedirs(evidence_dir, exist_ok=True)
        eval_sources = [(os.path.join(output_path, "agent_evals"), evidence_dir)]
        if self.domain == "imo_proof":
            eval_sources.append((
                os.path.join(run_dir, "outputs", f"{run_id}_proof_grading", "agent_evals"),
                os.path.join(evidence_dir, "proof_grading"),
            ))
        for evals_dir, archive_dir in eval_sources:
            if not os.path.isdir(evals_dir):
                continue
            for name in sorted(os.listdir(evals_dir)):
                if name == "eval_failures.jsonl" or (name.startswith("failure_")
                                                     and name.endswith(".log")):
                    try:
                        os.makedirs(archive_dir, exist_ok=True)
                        shutil.copy2(os.path.join(evals_dir, name),
                                     os.path.join(archive_dir, name))
                    except OSError:
                        pass  # evidence is best-effort; never fail the run
                    if name == "eval_failures.jsonl" and evals_dir == eval_sources[0][0]:
                        # Docs/19 §12.54-A: a generation where EVERY question
                        # was isolated by the same non-domain error is an
                        # infrastructure break, not a worse agent. ONE durable
                        # event carries the counts so the loop (and the
                        # offline reconcilers) can tell the two apart without
                        # opening the evidence tree.
                        try:
                            qf = summarize_question_failures(
                                os.path.join(evals_dir, name))
                            if qf:
                                self._event("question_failures",
                                            genid=str(genid), **qf)
                        except Exception as exc:  # noqa: BLE001 -- advisory
                            from utils.soft_fail import soft_fail
                            soft_fail(f"question-failure summary failed "
                                      f"(genid {genid}): {exc}",
                                      event_path=paths.events_path(self.output_dir),
                                      event_type="question_failures_fold_failed",
                                      genid=str(genid))
        # Merge the CHILD-reported load outcome into the toolset report (the
        # assembly stage can only report what it copied). Failures are classified
        # by owner: a component (or a role-owned always-on tool) is an agent-fixable
        # fact that rides into node meta -> the planner's next instruction; a
        # FROZEN always-on file is a framework bug, escalated and explicitly NOT
        # handed to an agent that cannot modify it.
        load_failed: List[Dict[str, Any]] = []
        load_report_path = os.path.join(run_dir, ".gan_runtime", "tools_load_report.json")
        if os.path.isfile(load_report_path):
            try:
                with open(load_report_path, "r", encoding="utf-8") as f:
                    load_report = json.load(f)
            except Exception as e:  # noqa: BLE001 -- advisory meta
                load_report = {"error": f"unreadable load report: {e}"[:200]}
            from gan.tools.assembly import load_failures
            load_failed = load_failures(load_report, "task",
                                        code_root=self.code_root)
            if toolset_report.get("skipped") is None:
                toolset_report["skipped"] = []
            toolset_report["load"] = {**load_report, "failed": load_failed}
            try:
                from gan.framework import paths as _paths
                from utils import trajectory_log as _tlog
                _tlog.append(_paths.events_path(self.output_dir), {
                    "type": "task_tools_loaded", "genid": str(genid),
                    "loaded": load_report.get("loaded") or [],
                    "failed": load_failed,
                    "framework_failed": [f for f in load_failed
                                         if f.get("kind") == "always_on_frozen"],
                    "schema_issues": load_report.get("schema_issues") or [],
                })
            except Exception as e:  # noqa: BLE001 -- audit write failure
                print(f"[WARN] task_tools_loaded event write failed: {e}")

        if report is None and os.path.isfile(report_path):
            from utils.soft_fail import soft_fail
            soft_fail(f"report.json exists but is unparseable for genid {genid} "
                      f"(score will read None/failed): {report_path}")
        score = tx.extract_score(report, self.score_key, self.domain)
        report_summary = tx.compute_report_summary(report, self.num_samples, self.output_contract)
        # polyglot patch-gate aggregate (counts only): one durable event per
        # generation so the repaired/rejected rates are greppable in
        # events.jsonl without opening report.json (the small_1028_0046
        # audit lesson: a gate failure visible ONLY inside docker logs is
        # invisible to the loop).
        if report_summary.get("patch_apply_summary"):
            try:
                self._event("question_patch_apply", genid=str(genid),
                            **report_summary["patch_apply_summary"])
            except Exception as e:  # noqa: BLE001 -- audit write failure
                print(f"[WARN] question_patch_apply event write failed: {e}")

        report_sha = scores.sha256_file(report_path)
        final_output_summary = None
        if self.domain == "imo_proof":
            final_output_summary = _final_output_summary(
                os.path.join(output_path, "predictions.csv"))
        # copy small evidence out of the ephemeral work dir (which is pruned)
        try:
            evid = paths.runs_dir(self.output_dir, genid)
            os.makedirs(evid, exist_ok=True)
            for name in ("report.json", "predictions.csv"):
                src = os.path.join(os.path.dirname(report_path), name)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(evid, name))
        except Exception as e:
            from utils.soft_fail import soft_fail
            soft_fail(f"evidence copy failed for genid {genid}: {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="evidence_copy_failed", genid=str(genid))

        # score status (unscored taxonomy; imputation is decided by the loop)
        if score is None:
            score_status = "failed"
            invalid_reason = None if report is not None else (
                "harness_failed" if rc != 0 else "no_report"
            )
        else:
            cov = report_summary.get("coverage")
            score_status = "partial" if (cov is not None and cov < 1.0) else "ok"
            invalid_reason = None

        parent_score = parent.value.score if parent is not None else None
        delta = (score - parent_score) if (score is not None and parent_score is not None) else None

        return Node(
            genid=genid,
            parent_genid=(parent.genid if parent is not None else None),
            curr_patch_files=[],
            value=NodeValue(score=score, score_status=score_status,
                            coverage=report_summary.get("coverage")),
            scores={self.domain: score},
            modify_depth=_modify_depth(records),
            meta={
                "run_dir": run_dir,
                "report_path": report_path,
                "report_sha": report_sha,
                "design_path": design_path,
                "harness_rc": rc,
                "model": model,
                "delta_vs_parent": delta,
                "patch_applied": patch_applied,
                "task_code_commit": task_code_commit,
                "code_commit": task_code_commit,
                "base_commit": base_commit,
                "code_ref_ok": code_ref_ok,
                "code_ref_mode": code_ref_mode,
                "task_patch_files": task_patch_files,
                "task_patch_rejected": task_patch_rejected,
                "challenge_audit": challenge_audit,
                "task_change": task_change,
                "records": records,
                "report_summary": report_summary,
                "score_status": score_status,
                "invalid_reason": invalid_reason,
                "toolset_report": toolset_report,
                "design_stripped": design_stripped or None,
                "final_output_summary": final_output_summary,
                "sample_mode": sample_info.get("mode"),
                "sample_seed": sample_info.get("sample_seed"),
                "sampled_ids": list(sample_info.get("sampled_ids") or []),
                "sample_anchor_ids": list(sample_info.get("anchor_ids") or []),
                "sample_challenge_ids": list(sample_info.get("challenge_ids") or []),
                # Reserved cost keys of this generation (docs/10 §10): the
                # effective values that governed the child + the clamp records
                # (the receipt projects the clamps to the planner).
                "cost_keys": {"effective": cost_effective, "clamped": cost_clamps},
            },
        )
