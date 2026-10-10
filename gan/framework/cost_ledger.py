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
            "reasoning_tokens", "total_tokens", "wallclock_s")
    out = {k: record.get(k) for k in keys if record.get(k) is not None}
    return out or None


# -- cost_sessions.jsonl (the loop's append-only per-session record) ---------
def append_session_cost(output_dir: str, record: Dict[str, Any]) -> None:
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
    line = f"- {label}: {tok} tokens, {_fmt_secs(rec.get('wallclock_s'))} wall"
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
                          max_chars: int = _STATEMENT_MAX_CHARS) -> str:
    """Planner plan-session statement: what the task child COST before you plan
    the next one (parent + this outer so far) + your own last plan session."""
    lines = ["## Cost statement (framework-measured; prior rounds only)"]
    if parent_task_cost is not None:
        tokens, wall = parent_task_cost
        lines.append(f"- task child, parent gen {parent_genid}: "
                     f"{_fmt_tokens(tokens)} tokens, {_fmt_secs(wall)} wall")
        if token_budget:
            try:
                pct = float(tokens) / float(token_budget) * 100.0
                lines.append(f"- budget reference: {_fmt_tokens(token_budget)} tokens/gen "
                             f"(parent used {pct:.1f}% of it)")
            except (TypeError, ValueError, ZeroDivisionError):
                pass
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
                              max_chars: int = _STATEMENT_MAX_CHARS) -> str:
    """Evaluator evaluate-session statement: THIS generation's task cost as a
    PROCESS fact (no scores — blind discipline holds) + your own last session."""
    lines = ["## Cost statement (framework-measured; process facts, no scores)"]
    if task_cost is not None:
        tokens, wall = task_cost
        lines.append(f"- task child, gen {genid}: "
                     f"{_fmt_tokens(tokens)} tokens, {_fmt_secs(wall)} wall")
    else:
        lines.append(f"- task child, gen {genid}: no cost recorded (proxy-less run?)")
    if own_last is not None:
        gid = own_last.get("genid")
        label = "your last evaluate session" + (f" (gen {gid})" if gid is not None else "")
        lines.append(_cost_line(label, own_last))
    return _cap("\n".join(lines), max_chars)


def render_self_statement(*, role: str, outer: Any = None,
                          own_sessions: Optional[List[Dict[str, Any]]] = None,
                          own_last: Optional[Dict[str, Any]] = None,
                          max_chars: int = _STATEMENT_MAX_CHARS) -> str:
    """Self-improvement statement: the role's OWN sessions this outer (the
    cost×quality reflection material; the OTHER role's costs are not here)."""
    lines = [f"## Cost statement (framework-measured; your own sessions, outer {outer})"]
    for rec in (own_sessions or [])[-6:]:
        gid = rec.get("genid")
        seat = rec.get("seat") or "session"
        label = f"{seat}" + (f" gen {gid}" if gid is not None else "")
        lines.append(_cost_line(label, rec))
    if not own_sessions:
        lines.append("- (no sessions recorded this outer yet)")
    if own_last is not None:
        lines.append(_cost_line("your last self-improve session", own_last))
    return _cap("\n".join(lines), max_chars)
