"""Cost ledger (FRAMEWORK, frozen): per-session cost aggregation + reconciliation
+ cost-statement rendering (docs/10 §2.2/§2.3, phases A/B).

Two independent sources, reconciled — never merged silently:

1. **Session trajectories** (the FINE, per-session source): ``output`` rows
   carry per-call usage counters (prompt/completion/total tokens) written by
   the frozen chat loop (``agent/llm.py`` → ``agent/llm_withtools.py``).
   Sessions are keyed by ``(outer, genid, role)`` (plan/evaluate/task) or
   ``(outer, role, attempt)`` (self-improvement), so per-session attribution
   needs no proxy re-issuance. Redaction preserves usage fields
   (``trajectory.redact_record`` only rewrites content values).

2. **Proxy audit** (``logs/events.jsonl`` ``llm_call``; the COARSE source):
   authoritative per ``(role, outer)`` for role sessions and per ``genid`` for
   the task child (``_llm_cost_for_gen`` semantics, moved here so there is one
   implementation). It cannot separate the parallel self-improvement threads,
   so it RECONCILES the fine source instead of replacing it.

Conventions:
- **None ≠ 0**: a session with no recorded usage reports ``None`` totals, never
  a fabricated 0 (a fake 0 would poison every downstream ratio).
- **Reconciliation is advisory**: a mismatch is surfaced as data
  (``cost_reconciliation`` event, emitted by the loop) — a bookkeeping bug to
  investigate, not a run-killer.
- **Statements are aggregate-only** and length-capped: the statement rides the
  FIRST message of the session it opens and persists in history for every later
  turn, so it must stay small (docs/10 §2.3: the statement itself is under
  context management).
"""
from __future__ import annotations

import glob
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from gan.framework import paths
from utils import trajectory_log

_STATEMENT_MAX_CHARS = 1500
_RECONCILE_MISMATCH_CAP = 20


# -- row helpers -------------------------------------------------------------
def _read_rows(path: str) -> List[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return []
    rows: List[Dict[str, Any]] = []
    for line in trajectory_log.read_all(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def _sum_usage(rows: List[Dict[str, Any]]) -> Tuple[Optional[int], Optional[int],
                                                   Optional[int], Optional[int]]:
    """Sum usage over ``output`` rows. Any counter with no observations is
    ``None`` (None ≠ 0): old-format trajectories (pre-usage rows) must not
    read as a zero-cost session."""
    acc: Dict[str, int] = {}
    for rec in rows:
        if rec.get("kind") != "output":
            continue
        for key in ("prompt_tokens", "completion_tokens",
                    "total_tokens", "reasoning_tokens"):
            val = rec.get(key)
            if isinstance(val, (int, float)):
                acc[key] = acc.get(key, 0) + int(val)
    if not acc:
        return None, None, None, None
    return (acc.get("prompt_tokens"), acc.get("completion_tokens"),
            acc.get("total_tokens"), acc.get("reasoning_tokens"))


def _count(rows: List[Dict[str, Any]], kind: str) -> int:
    return sum(1 for r in rows if r.get("kind") == kind)


def _llm_sidecar_metrics(path: str) -> Optional[Dict[str, Any]]:
    """Per-call LLM metrics from the trajectory's SIDEcar (agent/llm.py rows).

    Returns ``None`` when the sidecar does not exist or carries no ``llm_call``
    rows (None ≠ 0 — a session recorded before this mechanism must not read as
    a zero-failure one). Token totals deliberately stay with the ``output``-row
    sums above (实付口径); these metrics add the FAILURE/latency dimension the
    main trajectory cannot see (failed calls write no output row).
    """
    if not path:
        return None
    sidecar = trajectory_log.llm_calls_sidecar(path)
    if not os.path.isfile(sidecar):
        return None
    calls = [r for r in _read_rows(sidecar) if r.get("kind") == "llm_call"]
    if not calls:
        return None
    total_s = 0.0
    for r in calls:
        d = r.get("duration_s")
        if isinstance(d, (int, float)):
            total_s += float(d)
    return {"llm_calls_failed": sum(1 for r in calls if r.get("outcome") == "error"),
            "llm_retry_total": sum(len(r["retries"]) for r in calls
                                   if isinstance(r.get("retries"), list)),
            "llm_time_s": round(total_s, 3)}


def _cm_summary(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Aggregate the session's ``cm_*`` trajectory rows (docs/10 §11 D12).

    Returns None when the session had no CM rows (None ≠ 0 — a seam-free
    session must not read as a zero-ops one). Content-free: the rows already
    are; this only counts."""
    if not any(str(r.get("kind", "")).startswith("cm_") for r in rows):
        return None
    return {
        "cm_view_turns": _count(rows, "cm_view"),
        "cm_violations": _count(rows, "cm_violation"),
        "cm_policy_failures": (_count(rows, "cm_policy_failed")
                               + _count(rows, "cm_policy_disabled")
                               + _count(rows, "cm_policy_overrun")),
        "cm_assembly_failures": (_count(rows, "cm_assembly_failed")
                                 + _count(rows, "cm_assembly_disabled")
                                 + _count(rows, "cm_assembly_overrun")),
        "cm_budget_warnings": _count(rows, "cm_budget_checkpoint"),
        "est_chars_saved": sum(int(r.get("stats", {}).get("saved_chars") or 0)
                               for r in rows if r.get("kind") == "cm_view"),
    }


def cm_summary_from_events(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Aggregate CM events (the sidecar/ingested form) into a cost-record
    ``cm`` summary — same shape as :func:`_cm_summary` but keyed on the event
    ``type`` field: the task child's seam events arrive via events.jsonl
    (role-attributed by task_runner's ingest), not the trajectory. Returns
    None when there are no cm events (None ≠ 0)."""
    if not events:
        return None

    def _count(kind: str) -> int:
        return sum(1 for e in events if e.get("type") == kind)

    return {
        "cm_view_turns": _count("cm_view"),
        "cm_violations": _count("cm_violation"),
        "cm_policy_failures": (_count("cm_policy_failed")
                               + _count("cm_policy_disabled")
                               + _count("cm_policy_overrun")),
        "cm_assembly_failures": (_count("cm_assembly_failed")
                                 + _count("cm_assembly_disabled")
                                 + _count("cm_assembly_overrun")),
        "cm_budget_warnings": _count("cm_budget_checkpoint"),
        "est_chars_saved": sum(int((e.get("stats") or {}).get("saved_chars") or 0)
                               for e in events if e.get("type") == "cm_view"),
    }


def task_cm_summary(output_dir: str, genid: Any) -> Optional[Dict[str, Any]]:
    """The task child's CM summary for one generation, from its INGESTED
    events (task_runner folds the sidecar into events.jsonl with role=task
    before the cost record is written — docs/10 §11.5)."""
    ev_path = paths.events_path(output_dir)
    if not os.path.exists(ev_path):
        return None
    events: List[Dict[str, Any]] = []
    for line in trajectory_log.read_all(ev_path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict) or not str(e.get("type", "")).startswith("cm_"):
            continue
        if e.get("role") != "task" or str(e.get("genid")) != str(genid):
            continue
        events.append(e)
    return cm_summary_from_events(events)


def _wallclock(rows: List[Dict[str, Any]]) -> Optional[float]:
    ts = [r.get("ts") for r in rows if isinstance(r.get("ts"), (int, float))]
    if len(ts) < 2:
        return None
    return round(max(ts) - min(ts), 1)


# -- session aggregation (FINE source) ---------------------------------------
def session_cost_from_file(path: str, *, role: str, seat: str, outer: Any,
                           genid: Any = None, attempt: Optional[str] = None,
                           output_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """One session's cost record from its trajectory file.

    ``None`` when the file does not exist (nothing to attribute — the caller
    decides whether that is an error; the loop only records settled sessions).
    """
    if not path or not os.path.isfile(path):
        return None
    rows = _read_rows(path)
    prompt, completion, total, reasoning = _sum_usage(rows)
    cm_summary = _cm_summary(rows)
    rec: Dict[str, Any] = {
        "role": role, "seat": seat, "outer": outer,
        "genid": (str(genid) if genid is not None else None),
        "attempt": attempt,
        "source": "trajectory",
        "records": len(rows),
        "llm_calls": _count(rows, "output"),
        "tool_calls": _count(rows, "tool_call"),
        "tool_outputs": _count(rows, "tool_output"),
        "prompt_tokens": prompt, "completion_tokens": completion,
        "reasoning_tokens": reasoning, "total_tokens": total,
        "wallclock_s": _wallclock(rows),
        "ts": time.time(),
    }
    if cm_summary is not None:
        # Context-management session summary (docs/10 §11 D12): ops counts and
        # the ESTIMATED chars saved — a derived indicator, never billing; the
        # ledger's tokens stay 实付 (proxy usage sums above).
        rec["cm"] = cm_summary
    llm_metrics = _llm_sidecar_metrics(path)
    if llm_metrics is not None:
        rec.update(llm_metrics)
    if output_dir:
        try:
            rec["path"] = os.path.relpath(path, output_dir).replace(os.sep, "/")
        except ValueError:
            rec["path"] = path
    return rec


def gen_session_costs(output_dir: str, outer: Any, genid: Any) -> Dict[str, Optional[Dict[str, Any]]]:
    """The two role sessions of one generation (plan / evaluate), by trajectory."""
    out: Dict[str, Optional[Dict[str, Any]]] = {}
    for role, seat in (("planner", "plan"), ("evaluator", "evaluate")):
        out[f"{role}_{seat}"] = session_cost_from_file(
            paths.session_traj_file(output_dir, outer, genid, role),
            role=role, seat=seat, outer=outer, genid=genid, output_dir=output_dir)
    return out


def self_session_costs(output_dir: str, outer: Any, role: str) -> List[Dict[str, Any]]:
    """A role's OUTER-level self-improvement sessions (canonical + attempt files,
    newest first) — each file is one loop instantiation's session."""
    d = paths.outer_traj_dir(output_dir, outer)
    if not os.path.isdir(d):
        return []
    files = [os.path.join(d, f"{role}.jsonl")]
    files += sorted(glob.glob(os.path.join(d, f"{role}__*.jsonl")))
    out: List[Dict[str, Any]] = []
    for p in files:
        if not os.path.isfile(p):
            continue
        attempt = None
        stem = os.path.basename(p)[: -len(".jsonl")]
        if "__" in stem:
            attempt = stem.split("__", 1)[1]
        rec = session_cost_from_file(p, role=role, seat="self_improve", outer=outer,
                                     attempt=attempt, output_dir=output_dir)
        if rec is not None:
            out.append(rec)
    out.sort(key=lambda r: str(r.get("path") or ""), reverse=True)
    return out


def task_trajectory_cost(output_dir: str, outer: Any, genid: Any) -> Optional[Dict[str, Any]]:
    """Task child session cost from its (redacted) trajectory — reconciliation
    source for the proxy-audit task cost, never a replacement."""
    return session_cost_from_file(
        paths.session_traj_file(output_dir, outer, genid, "task"),
        role="task", seat="task", outer=outer, genid=genid, output_dir=output_dir)


# -- task cost (COARSE source; moved from loop._llm_cost_for_gen) ------------
def task_cost_for_gen(output_dir: str, genid: Any) -> Optional[Tuple[float, float]]:
    """Task-child cost for one generation, from the proxy audit (single source).

    Returns ``(cost_tokens, cost_wallclock_s)`` for the generation's task
    child — llm tokens (prompt+completion) summed over the child's proxy
    calls, wall clock from the harness duration (falls back to summed call
    latency). ``None`` when the generation produced no llm calls (offline
    smoke / planner-failed generations): an unrecorded cost must stay None,
    not a fake 0 — ``cost_penalty`` and packet.json treat None as absent.
    """
    ev_path = paths.events_path(output_dir)
    if not os.path.exists(ev_path):
        return None
    calls, harness_s = [], None
    for line in trajectory_log.read_all(ev_path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "llm_call" and e.get("scope_type") == "genid" \
                and str(e.get("scope_id")) == str(genid):
            calls.append(e)
        elif e.get("type") == "task_harness_done" and str(e.get("genid")) == str(genid):
            harness_s = e.get("duration_s")
    if not calls:
        return None
    tokens = float(sum(e.get("total_tokens") or 0 for e in calls))
    wall = float(harness_s) if harness_s is not None else \
        round(sum(e.get("latency_ms") or 0 for e in calls) / 1000.0, 3)
    return tokens, wall


def task_costs_for_gens(output_dir: str, genids: List[Any]) -> Dict[str, Tuple[float, float]]:
    """Bulk form of :func:`task_cost_for_gen` — ONE pass over the audit for a
    list of genids (the plan statement's this-outer history line). Missing
    genids are absent from the result (None ≠ 0 at the caller)."""
    wanted = {str(g) for g in (genids or [])}
    out: Dict[str, Tuple[float, float]] = {}
    if not wanted:
        return out
    ev_path = paths.events_path(output_dir)
    if not os.path.exists(ev_path):
        return out
    per_gen: Dict[str, List[float]] = {}
    harness: Dict[str, float] = {}
    for line in trajectory_log.read_all(ev_path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "llm_call" and e.get("scope_type") == "genid" \
                and str(e.get("scope_id")) in wanted:
            tokens = e.get("total_tokens")
            if isinstance(tokens, (int, float)):
                per_gen.setdefault(str(e.get("scope_id")), []).append(float(tokens))
        elif e.get("type") == "task_harness_done" and str(e.get("genid")) in wanted:
            if isinstance(e.get("duration_s"), (int, float)):
                harness[str(e.get("genid"))] = float(e["duration_s"])
    for gid, calls in per_gen.items():
        if not calls:
            continue
        tokens = float(sum(calls))
        wall = harness[gid] if gid in harness else None
        if wall is None:
            # wall-clock fallback needs the latency of the SAME gen's calls;
            # the single pass above did not keep them, so re-derive cheaply
            # only when a harness duration is missing (rare: offline smokes).
            single = task_cost_for_gen(output_dir, gid)
            if single is not None:
                out[gid] = single
            continue
        out[gid] = (tokens, round(wall, 3))
    return out


def compact(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Whitelisted projection of a session-cost record (statement / receipt /
    recent-payload surfaces ride THIS, never the raw record)."""
    if not isinstance(record, dict):
        return None
    keys = ("role", "seat", "outer", "genid", "attempt", "llm_calls",
            "tool_calls", "tool_outputs", "prompt_tokens", "completion_tokens",
            "reasoning_tokens", "total_tokens", "wallclock_s",
            "llm_calls_failed", "llm_retry_total", "llm_time_s")
    out = {k: record.get(k) for k in keys if record.get(k) is not None}
    return out or None


# -- cost_sessions.jsonl (the loop's append-only per-session record) ---------
def append_session_cost(output_dir: str, record: Dict[str, Any]) -> None:
    """Append one session's cost record, refusing duplicate keys.

    The ledger is append-only; a second write for the same
    (role, seat, outer, genid, attempt) would silently double-count the
    session in aggregates (reconcile / cost statements). Fail loud instead.
    """
    key = (record.get("role"), record.get("seat"),
           str(record.get("outer")), str(record.get("genid")), record.get("attempt"))
    for old in read_session_costs(output_dir):
        old_key = (old.get("role"), old.get("seat"),
                   str(old.get("outer")), str(old.get("genid")), old.get("attempt"))
        if old_key == key:
            raise ValueError(f"duplicate session-cost record {key!r} refused")
    trajectory_log.append(paths.cost_sessions_path(output_dir), record)


def read_session_costs(output_dir: str) -> List[Dict[str, Any]]:
    path = paths.cost_sessions_path(output_dir)
    if not os.path.isfile(path):
        return []
    out: List[Dict[str, Any]] = []
    for line in trajectory_log.read_all(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def latest_session_cost(output_dir: str, role: str, seat: str,
                        exclude_genid: Any = None) -> Optional[Dict[str, Any]]:
    """Newest recorded session for (role, seat) — the statement's "your last
    session" line. Records are appended at settle, so the newest match is the
    PREVIOUS session of that seat (the current one is not yet recorded)."""
    recs = read_session_costs(output_dir)
    for rec in reversed(recs):
        if rec.get("role") != role or rec.get("seat") != seat:
            continue
        if exclude_genid is not None and str(rec.get("genid")) == str(exclude_genid):
            continue
        return rec
    return None


# -- reconciliation (COARSE vs FINE; advisory) --------------------------------
def reconcile(output_dir: str) -> Dict[str, Any]:
    """Proxy audit vs session-trajectory sums, per attribution key.

    Keys: ``role:<role>:outer_<O>`` (role sessions: plan+evaluate from genid
    dirs + self from outer-level files) and ``task:gen_<g>`` (the task child).
    A key is comparable only when BOTH sides recorded numbers (either side
    ``None`` → ``unattributed``, reported but never a mismatch: old-format
    trajectories and proxy-less smokes are legitimate).
    """
    ev_path = paths.events_path(output_dir)
    audit: Dict[str, List[int]] = {}
    if os.path.exists(ev_path):
        for line in trajectory_log.read_all(ev_path).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") != "llm_call" or e.get("allow") is False:
                continue
            tokens = e.get("total_tokens")
            if not isinstance(tokens, (int, float)):
                continue
            if e.get("scope_type") == "genid":
                key = f"task:gen_{e.get('scope_id')}"
            elif e.get("scope_type") == "outer" and e.get("role") in ("planner", "evaluator"):
                key = f"role:{e.get('role')}:outer_{e.get('scope_id')}"
            else:
                continue
            audit.setdefault(key, []).append(int(tokens))

    fine: Dict[str, List[int]] = {}
    unattributed = 0
    traj_root = paths.trajectory_root(output_dir)
    if os.path.isdir(traj_root):
        for outer_name in sorted(os.listdir(traj_root)):
            outer_dir = os.path.join(traj_root, outer_name)
            if not os.path.isdir(outer_dir) or not outer_name.startswith("outer_"):
                continue
            try:
                outer = int(outer_name[len("outer_"):])
            except ValueError:
                continue
            for name in sorted(os.listdir(outer_dir)):
                sub = os.path.join(outer_dir, name)
                if os.path.isdir(sub):
                    for role in ("planner", "evaluator"):
                        rec = session_cost_from_file(
                            os.path.join(sub, f"{role}.jsonl"),
                            role=role, seat="plan", outer=outer, genid=name)
                        if rec:
                            key = f"role:{role}:{outer_name}"
                            fine.setdefault(key, [])
                            if rec.get("total_tokens") is not None:
                                fine[key].append(int(rec["total_tokens"]))
                            else:
                                unattributed += 1
                    rec = session_cost_from_file(
                        os.path.join(sub, "task.jsonl"),
                        role="task", seat="task", outer=outer, genid=name)
                    if rec:
                        key = f"task:gen_{name}"
                        fine.setdefault(key, [])
                        if rec.get("total_tokens") is not None:
                            fine[key].append(int(rec["total_tokens"]))
                        else:
                            unattributed += 1
                elif name.endswith(".jsonl"):
                    stem = name[: -len(".jsonl")]
                    role = stem.split("__", 1)[0]
                    if role not in ("planner", "evaluator"):
                        continue
                    rec = session_cost_from_file(
                        os.path.join(outer_dir, name),
                        role=role, seat="self_improve", outer=outer)
                    if rec:
                        key = f"role:{role}:{outer_name}"
                        fine.setdefault(key, [])
                        if rec.get("total_tokens") is not None:
                            fine[key].append(int(rec["total_tokens"]))
                        else:
                            unattributed += 1

    mismatches: List[Dict[str, Any]] = []
    checked = 0
    for key in sorted(set(audit) | set(fine)):
        a = sum(audit[key]) if key in audit else None
        f = sum(fine[key]) if key in fine else None
        if a is None or f is None:
            continue
        checked += 1
        if a != f:
            mismatches.append({"key": key, "audit": a, "trajectory": f, "delta": a - f})
            if len(mismatches) >= _RECONCILE_MISMATCH_CAP:
                break
    return {"checked": checked, "mismatches": mismatches,
            "unattributed_sessions": unattributed}


# -- cost statements (B; aggregate-only, length-capped) -----------------------
def _fmt_tokens(n: Any) -> str:
    return f"{int(n):,}" if isinstance(n, (int, float)) else "n/a"


def _fmt_secs(s: Any) -> str:
    return f"{float(s):.1f}s" if isinstance(s, (int, float)) else "n/a"


def _fmt_calls(n: Any) -> str:
    return f"{int(n)}" if isinstance(n, (int, float)) else "n/a"


def _cost_line(label: str, rec: Optional[Dict[str, Any]], *, with_calls: bool = True) -> str:
    if not rec:
        return f"- {label}: no record"
    tokens = rec.get("total_tokens")
    if tokens is None and rec.get("prompt_tokens") is None and rec.get("completion_tokens") is None:
        tok = "n/a (no usage recorded)"
    else:
        tok = _fmt_tokens(tokens)
    line = f"- {label}: {tok} tokens"
    # Reasoning split (docs/10 §4: 注入面必须保留这一列 — reasoning is the
    # cost majority on reasoning models; shown only when recorded, None≠0).
    rt = rec.get("reasoning_tokens")
    if isinstance(rt, (int, float)):
        line += f" (reasoning {_fmt_tokens(rt)})"
    line += f", {_fmt_secs(rec.get('wallclock_s'))} wall"
    if with_calls:
        line += f", {_fmt_calls(rec.get('tool_calls'))} tool calls"
    return line


def _cap(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars - len("\n…[truncated]")] + "\n…[truncated]"


def render_plan_statement(*, parent_genid: Any = None,
                          parent_task_cost: Optional[Tuple[float, float]] = None,
                          history_costs: Optional[List[Dict[str, Any]]] = None,
                          own_last: Optional[Dict[str, Any]] = None,
                          token_budget: Any = None,
                          task_budgets: Optional[Dict[str, Any]] = None,
                          own_budget: Any = None,
                          max_chars: int = _STATEMENT_MAX_CHARS) -> str:
    """Planner plan-session statement: what the task child COST before you plan
    the next one (parent + this outer so far) + your own last plan session.

    Phase C (docs/10 §10): ``task_budgets`` = the effective task-side reserved
    keys of the design the child will inherit (本代预算生效值, §4);
    ``own_budget`` = the planner design's own ``cost.plan_token_budget``.
    """
    lines = ["## Cost statement (framework-measured; prior rounds only)"]
    if parent_task_cost is not None:
        tokens, wall = parent_task_cost
        lines.append(f"- task child, parent gen {parent_genid}: "
                     f"{_fmt_tokens(tokens)} tokens, {_fmt_secs(wall)} wall")
        if token_budget:
            try:
                pct = float(tokens) / float(token_budget) * 100.0
                lines.append(f"- budget reference: {_fmt_tokens(token_budget)} tokens/gen "
                             f"(parent used {pct:.1f}% of it; reference only, not enforced)")
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    if task_budgets:
        parts = []
        tqb = task_budgets.get("per_question_token_budget")
        if isinstance(tqb, (int, float)):
            parts.append(f"{_fmt_tokens(tqb)} tokens/question")
        tqm = task_budgets.get("per_question_max_tool_calls")
        if isinstance(tqm, (int, float)):
            parts.append(f"{_fmt_calls(tqm)} tool calls/question")
        eff = task_budgets.get("reasoning_effort")
        if eff:
            parts.append(f"reasoning_effort={eff}")
        if parts:
            lines.append("- task budget keys in effect (inherited design): " + "; ".join(parts))
    if isinstance(own_budget, (int, float)):
        line = f"- your plan session budget: {_fmt_tokens(own_budget)} tokens (your design key)"
        own_used = (own_last or {}).get("total_tokens")
        if isinstance(own_used, (int, float)):
            try:
                line += f" -- your last session used {float(own_used) / float(own_budget) * 100.0:.1f}% of it"
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        lines.append(line)
    else:
        # Unset must SPEAK (no-silent-fail rule): a silently-absent budget
        # line reads as "a budget exists somewhere" — it does not; only
        # tool-call caps and the time walls constrain this session.
        lines.append("- your plan session token budget: unset (no token soft "
                     "stop armed; tool-call caps and time walls still apply)")
    hist = [h for h in (history_costs or []) if h]
    if hist:
        parts = []
        for h in hist[-4:]:
            parts.append(f"gen {h.get('genid')}: {_fmt_tokens(h.get('cost_tokens'))} tok")
        lines.append("- task cost this outer so far: " + "; ".join(parts))
    if own_last is not None:
        gid = own_last.get("genid")
        label = f"your last plan session" + (f" (gen {gid})" if gid is not None else "")
        lines.append(_cost_line(label, own_last))
    lines.append("Plan the next generation with these costs in mind: a cheaper "
                 "design that holds score wins; cost is measured, not asserted.")
    return _cap("\n".join(lines), max_chars)


def render_evaluate_statement(*, genid: Any = None,
                              task_cost: Optional[Tuple[float, float]] = None,
                              own_last: Optional[Dict[str, Any]] = None,
                              own_budget: Any = None,
                              token_budget: Any = None,
                              max_chars: int = _STATEMENT_MAX_CHARS) -> str:
    """Evaluator evaluate-session statement: THIS generation's task cost as a
    PROCESS fact (no scores — blind discipline holds) + your own last session.

    Phase C: ``own_budget`` = the evaluator design's
    ``cost.evaluate_token_budget`` (effective, clamped by the frozen table).
    ``token_budget`` = loop.yaml ``cost.token_budget_per_gen`` — the reference
    line the task cost is shown against (docs/10 §4: the evaluate statement's
    task-cost line gains its budget anchor)."""
    lines = ["## Cost statement (framework-measured; process facts, no scores)"]
    if task_cost is not None:
        tokens, wall = task_cost
        lines.append(f"- task child, gen {genid}: "
                     f"{_fmt_tokens(tokens)} tokens, {_fmt_secs(wall)} wall")
        if isinstance(token_budget, (int, float)) and token_budget > 0:
            try:
                pct = float(tokens) / float(token_budget) * 100.0
                lines.append(f"- budget reference: {_fmt_tokens(token_budget)} tokens/gen "
                             f"(task used {pct:.1f}% of it; reference only, not enforced)")
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    else:
        lines.append(f"- task child, gen {genid}: no cost recorded (proxy-less run?)")
    if isinstance(own_budget, (int, float)):
        line = f"- your evaluate session budget: {_fmt_tokens(own_budget)} tokens (your design key)"
        own_used = (own_last or {}).get("total_tokens")
        if isinstance(own_used, (int, float)):
            try:
                line += f" -- your last session used {float(own_used) / float(own_budget) * 100.0:.1f}% of it"
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        lines.append(line)
    else:
        lines.append("- your evaluate session token budget: unset (no token soft "
                     "stop armed; tool-call caps and time walls still apply)")
    if own_last is not None:
        gid = own_last.get("genid")
        label = "your last evaluate session" + (f" (gen {gid})" if gid is not None else "")
        lines.append(_cost_line(label, own_last))
    return _cap("\n".join(lines), max_chars)


def render_self_statement(*, role: str, outer: Any = None,
                          own_sessions: Optional[List[Dict[str, Any]]] = None,
                          own_last: Optional[Dict[str, Any]] = None,
                          scores_by_genid: Optional[Dict[str, Any]] = None,
                          max_chars: int = _STATEMENT_MAX_CHARS) -> str:
    """Self-improvement statement: the role's OWN sessions this outer (the
    cost×quality reflection material; the OTHER role's costs are not here).

    When ``scores_by_genid`` is provided, each session line gains a
    ``score`` column aligning that round's cost with its measured result
    (per-round cost×score alignment; missing scores show as n/a)."""
    lines = [f"## Cost statement (framework-measured; your own sessions, outer {outer})"]
    for rec in (own_sessions or [])[-6:]:
        gid = rec.get("genid")
        seat = rec.get("seat") or "session"
        label = f"{seat}" + (f" gen {gid}" if gid is not None else "")
        line = _cost_line(label, rec)
        if scores_by_genid and gid is not None and str(gid) in scores_by_genid:
            line += f", score {scores_by_genid[str(gid)]}"
        lines.append(line)
    if not own_sessions:
        lines.append("- (no sessions recorded this outer yet)")
    if own_last is not None:
        lines.append(_cost_line("your last self-improve session", own_last))
    return _cap("\n".join(lines), max_chars)
