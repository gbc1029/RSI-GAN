"""Task-trajectory archive (FRAMEWORK, frozen).

Per-generation task-agent trajectories are archived out of the run dir into the
per-instance JSONL layout, redacted, and indexed. Which generations a role may
read is decided by the loop and passed explicitly per session via
``AccessContext.trajectory_genids`` (planner = direct parent, evaluator =
current + parent, self-improvement = this outer's generations + the last
generation's direct parent).

Redaction is deliberately part of the frozen trust anchor: evolvable agents must
not be able to weaken it.

Layout (see ``gan/framework/paths.py``):
    trajectory/outer_<O>/<genid>/task.jsonl
"""
from __future__ import annotations

import glob
import json
import os
import time
from typing import Any, Dict, List, Tuple

from gan.framework import paths
from utils import trajectory_log

# Keys / fields that must never reach planner/evaluator (benchmark leakage).
_SENSITIVE = (
    "overall_accuracy",
    "random_guess_accuracy",
    "label_distribution",
    "report_summary",
    '"score"',
    "benchmark_score",
)
_REDACTION_MARK = "[REDACTED:sensitive]"

# Rendered-view caps for trajectory reads (docs/17 §15). The tool-level
# DEFAULT stays 6000; the seat-broker CEILING was a legacy hard 6000 that
# silently refused every larger request (field audit smoke_1030_204345: the
# evaluator asked for 60000 and received the same 6000-cut blob, then burned
# its blind phase reasoning about the cap). Raised to 120000 as a TEMPORARY
# MITIGATION -- the complete fix (offset/pagination over the RPC face +
# role-seat CM re-wiring) is documented in docs/17 §15 and remains open.
TRAJ_RENDER_DEFAULT_CHARS = 6000
TRAJ_RENDER_MAX_CHARS = 120_000


def redact(text: str) -> str:
    """Line-level redaction of benchmark/score material (best-effort)."""
    out: List[str] = []
    for line in (text or "").splitlines():
        if any(s in line for s in _SENSITIVE):
            out.append(_REDACTION_MARK)
        else:
            out.append(line)
    return "\n".join(out)


def _redact_value(v: Any) -> Any:
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, dict):
        return {k: _redact_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_redact_value(x) for x in v]
    return v


def redact_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(rec)
    for k in ("text", "input", "output", "reasoning"):
        if k in out:
            out[k] = _redact_value(out[k])
    out["redacted"] = True
    return out


def collect(run_dir: str, run_id: str, output_dir: str, outer: Any, genid: Any) -> str:
    """Archive + redact the task trajectories of one generation into JSONL.

    "No evidence" is a PIPELINE failure, not an empty archive: a missing
    ``agent_evals`` directory, no ``chat_history_*`` files or zero records all
    raise (the task run necessarily produced per-question chat histories, so an
    empty source means the run-dir handling/spotting drifted upstream and the
    evaluator would silently lose its evidence). The loop turns the raise into
    an explicit ``trajectory_archive_failed`` generation. The write is ATOMIC
    (tmp + ``os.replace``): a crash mid-write can no longer leave a partial
    file that looks like success.
    """
    evals = os.path.join(run_dir, "outputs", str(run_id), "agent_evals")
    dest = paths.session_traj_file(output_dir, outer, genid, "task")
    records: List[Dict[str, Any]] = []
    if os.path.isdir(evals):
        bases = set()
        for name in os.listdir(evals):
            if name.startswith("chat_history_"):
                bases.add(name.split(".jsonl")[0] + ".jsonl")
        for base in sorted(bases):
            qid = base[len("chat_history_"):].rsplit(".", 1)[0]
            text = trajectory_log.read_all(os.path.join(evals, base))
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    rec = {"kind": "text", "text": line}
                rec = redact_record(rec)
                rec["qid"] = qid
                records.append(rec)
    if not records:
        src = ("directory missing" if not os.path.isdir(evals)
               else ("no chat_history_* files" if not bases else "no records parsed"))
        raise RuntimeError(
            f"no task trajectory sources archived for genid {genid} "
            f"(run_dir={run_dir!r}, agent_evals={evals!r}: {src}) — the evaluator "
            f"would silently lose its evidence for this generation"
        )
    tmp = f"{dest}.collect-tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        os.replace(tmp, dest)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise

    # Sidecar archive: the per-call LLM records (agent/llm.py `llm_call` rows)
    # live NEXT TO each chat_history file inside the EPHEMERAL run dir
    # (work/<genid>/...), so without this step they would be pruned. They are
    # folded into `<dest>.llm_calls.jsonl` — a file NO agent read surface
    # resolves (same capability-by-existence argument as the seat sidecars).
    # Advisory-only: a sidecar failure must never fail the main archive, but
    # it must SPEAK (soft_fail event), never vanish silently.
    try:
        _collect_llm_calls_sidecar(evals, dest)
    except Exception as e:  # noqa: BLE001 -- audited, never fatal
        from utils.soft_fail import soft_fail
        soft_fail(f"task llm_calls sidecar archive failed for genid {genid}: {e}",
                  event_path=paths.events_path(output_dir),
                  event_type="task_llm_calls_sidecar_archive_failed",
                  genid=str(genid))
    return dest


def _collect_llm_calls_sidecar(evals: str, dest: str) -> None:
    """Fold the run dir's per-question llm_call sidecars into one archived file."""
    suffix = trajectory_log.LLM_CALLS_SIDECAR_SUFFIX
    sidecar_rows: List[Dict[str, Any]] = []
    if os.path.isdir(evals):
        for path in sorted(glob.glob(os.path.join(evals, f"*{suffix}"))):
            stem = os.path.basename(path)[:-len(suffix)]
            qid = stem[len("chat_history_"):].rsplit(".", 1)[0] \
                if stem.startswith("chat_history_") else stem
            for line in trajectory_log.read_all(path).splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn tail line must not poison the archive
                rec = redact_record(rec)
                rec["qid"] = qid
                sidecar_rows.append(rec)
    if not sidecar_rows:
        return
    sidecar_rows.sort(key=lambda r: (r.get("ts") is None, r.get("ts") or 0.0))
    side_dest = f"{dest}{suffix}"
    tmp = f"{side_dest}.collect-tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in sidecar_rows:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        os.replace(tmp, side_dest)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _session_file(output_dir: str, genid: Any, role: str) -> str:
    if role in ("planner", "evaluator"):
        seat = "plan" if role == "planner" else "evaluate"
        pat = os.path.join(paths.trajectory_root(output_dir), "outer_*", "roles",
                           role, seat, f"gen_{genid}.jsonl")
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    pat = os.path.join(paths.trajectory_root(output_dir), "outer_*", str(genid), f"{role}.jsonl")
    hits = sorted(glob.glob(pat))
    return hits[-1] if hits else ""


def _expose_reasoning() -> bool:
    """Decision-surface gate for reasoning text (loop.yaml, default OFF).

    SCOPE (see ``read``/``read_session``): the flag governs the TASK
    trajectory surface ONLY (``read_trajectory`` -> ``read(role="task")``).
    Role sessions (``read_session_trajectory``) NEVER expose reasoning
    regardless of this flag — planner/evaluator may see the task agent's CoT,
    never their own or each other's. The reasoning text IS recorded in the raw
    trajectory (audit surface, already line-redacted); the default mirrors the
    B29 artifact-vs-rationale rule (the task model's chain-of-thought is the
    strongest persuasion surface and may speculate about benchmark mechanics)."""
    try:
        from gan.framework.loader import load_gan_loop_config
        return bool(load_gan_loop_config().get("loop.trajectory_expose_reasoning", False))
    except Exception:
        return False


def _render(text: str, max_chars: int, expose_reasoning: Optional[bool] = None) -> str:
    """Render a JSONL trajectory blob for a decision surface.

    ``expose_reasoning=None`` keeps the legacy behavior (consult
    ``_expose_reasoning()`` per record); an explicit True/False pins the gate
    for this render — the scoping mechanism that keeps ROLE sessions
    reasoning-blind while the TASK surface follows the loop.yaml flag."""
    parts: List[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            parts.append(line)
            continue
        kind = rec.get("kind", "?")
        if kind == "tool_call":
            parts.append(f"tool_call {rec.get('tool')}: {rec.get('input')}")
        elif kind == "tool_output":
            parts.append(f"tool_output {rec.get('tool')}: {rec.get('output')}")
        else:
            out = f"{kind}: {rec.get('text', '')}"
            if kind == "output":
                # Reasoning-channel METADATA is always visible (counters carry
                # no isolation risk and are the diagnostic that explains empty
                # content / finish_reason=length on reasoning models).
                meta = [f"finish={rec['finish_reason']}" if rec.get("finish_reason") else None,
                        f"reasoning_tokens={rec['reasoning_tokens']}" if rec.get("reasoning_tokens") is not None else None]
                meta = [m for m in meta if m]
                if meta:
                    out += f" [{', '.join(meta)}]"
                rsn = rec.get("reasoning")
                if rsn:
                    expose = _expose_reasoning() if expose_reasoning is None else expose_reasoning
                    if expose:
                        out += f"\n[reasoning]\n{rsn}"
                    else:
                        out += f" [reasoning omitted: {len(rsn)} chars]"
            parts.append(out)
    rendered = "\n".join(parts)
    if len(rendered) > max_chars:
        # Mark VIEW truncation explicitly: a silently cut render is
        # indistinguishable from a short archive, and a downstream evaluator
        # has already misread the 6000-char default cut as truncated FILES
        # (field audit small_1030_095410: fabricated mid-function EOFs).
        # Redaction semantics are unchanged — this only labels the cap.
        rendered = rendered[:max_chars] + \
            f"\n...[render truncated at max_chars={max_chars}]"
    return rendered


def read(output_dir: str, genid: Any, max_chars: int = 6000, role: str = "task",
         expose_reasoning: Optional[bool] = None) -> str:
    """Return a redacted, truncated rendering of a generation's trajectory.

    Reasoning-exposure SCOPE rule, enforced here:
    - role="task" (the only surface the `read_trajectory` tool serves):
      follows ``loop.trajectory_expose_reasoning`` unless ``expose_reasoning``
      pins it explicitly;
    - any non-task role: agent-session content — NEVER exposed, regardless of
      the flag (session reads must go through ``read_session``, which pins it).
    """
    path = _session_file(output_dir, genid, role)
    if not path or not os.path.isfile(path):
        return ""
    if expose_reasoning is None:
        expose_reasoning = _expose_reasoning() if role == "task" else False
    return _render(trajectory_log.read_all(path), max_chars,
                   expose_reasoning=expose_reasoning)


def read_session(output_dir: str, outer: Any, genid: Any, role: str, max_chars: int = 6000,
                 *, seat: Any = None) -> str:
    """Read a role's own session trajectory for a given outer/genid.

    ``seat`` selects the file shape (self seat = outer-level files, plan/
    evaluate = per-generation files); ``None`` keeps the legacy inference for
    direct callers. A quarantined (unredactable) session is NEVER served: an
    explicit refusal replaces its content (fail-closed)."""
    if genid is not None:
        return _read_gen_session(output_dir, outer, genid, role, max_chars, seat=seat)
    return _read_outer_sessions(output_dir, outer, role, max_chars, seat=seat)


def _read_gen_session(output_dir: str, outer: Any, genid: Any, role: str,
                      max_chars: int, *, seat: Any = None) -> str:
    """Per-generation session read (genid-keyed files; attempt id irrelevant).

    Reasoning is PINNED OFF for role sessions regardless of the loop.yaml
    flag: planner/evaluator never read their own (or each other's) CoT."""
    seat = seat if seat is not None else _SEAT_FOR_ROLE.get(role)
    if seat == "self_improve":
        # The self seat writes OUTER-level files, so a genid address can only
        # name a file (``outer__<attempt>`` / ``outer``). Match by name, else
        # merge that outer's own sessions.
        wanted = str(genid)
        files = [row["path"] for row in seat_session_files(output_dir, outer, role,
                                                           seat=seat)
                 if not row.get("quarantined")
                 and os.path.basename(row["path"]).split(".jsonl", 1)[0] == wanted]
        if files:
            parts = [trajectory_log.read_all(p) for p in files]
            return _render("\n".join(t for t in parts if t.strip()), max_chars,
                           expose_reasoning=False)
        return _read_outer_sessions(output_dir, outer, role, max_chars, seat=seat)
    path = paths.session_traj_file(output_dir, outer, genid, role, seat=seat)
    if not os.path.isfile(path):
        # Legacy layout (pre-seat runs): the genid-keyed file may sit directly
        # under the outer dir. Quarantine detection must cover BOTH layouts --
        # a legacy .unredacted-* sibling is just as unservable.
        legacy = paths.session_traj_file(output_dir, outer, genid, role)
        quarantined = sorted(glob.glob(path + ".unredacted-*"))
        quarantined += sorted(glob.glob(legacy + ".unredacted-*"))
        if quarantined:
            return ("[unavailable] this session trajectory could not be redacted and was "
                    "quarantined; the audit trail is in events.jsonl "
                    "(session_redaction_failed).")
        if os.path.isfile(legacy):
            return _render(trajectory_log.read_all(legacy), max_chars,
                           expose_reasoning=False)
        # legacy fallback: a missing gen-session file falls back to the
        # generational archive. That may be a ROLE session file (role != task),
        # so the reasoning gate stays pinned off here too — only the
        # `read_trajectory` tool path (role="task") consults the flag.
        return read(output_dir, genid, max_chars, role, expose_reasoning=False)
    return _render(trajectory_log.read_all(path), max_chars, expose_reasoning=False)


def _outer_session_files(output_dir: str, outer: Any, role: str) -> List[str]:
    """OUTER-level trajectory files for a role, newest first.

    Loop-driven roles bind their sink to ``<role>__<attempt>.jsonl`` (one file
    per loop instantiation), while directly-constructed roles (legacy runs,
    offline smoke) use ``<role>.jsonl``. Readers accept every shape so an
    attempt-keyed file can never become invisible behind the canonical name;
    rotated parts (``.N``) and quarantine names are excluded by the glob."""
    seat_dir = paths.role_traj_dir(output_dir, outer, role, "self_improve")
    if os.path.isdir(seat_dir):
        cands = [os.path.join(seat_dir, "outer.jsonl")]
        cands += sorted(glob.glob(os.path.join(seat_dir, "outer__*.jsonl")))
        existing = [p for p in cands if os.path.isfile(p)]
        if existing:
            existing.sort(key=os.path.getmtime, reverse=True)
            return existing
    d = paths.outer_traj_dir(output_dir, outer)
    if not os.path.isdir(d):
        return []
    cands = [os.path.join(d, f"{role}.jsonl")]
    cands += sorted(glob.glob(os.path.join(d, f"{role}__*.jsonl")))
    existing = [p for p in cands if os.path.isfile(p)]
    existing.sort(key=os.path.getmtime, reverse=True)
    return existing


def _read_outer_sessions(output_dir: str, outer: Any, role: str,
                         max_chars: int, *, seat: Any = None) -> str:
    """Render a role's OUTER-level sessions: canonical + attempt files, newest
    first. Quarantined-only attempts are reported explicitly (fail-closed).
    Reasoning PINNED OFF: role sessions never expose CoT (see read_session)."""
    seat = seat if seat is not None else _SEAT_FOR_ROLE.get(role)
    rows = [row for row in seat_session_files(output_dir, outer, role, seat=seat)]
    files = [row["path"] for row in rows if not row.get("quarantined")]
    if seat != "self_improve":
        # plan/evaluate write PER-GENERATION files; their "own sessions" are
        # those, not the legacy outer-level role file (which never exists in a
        # seat-keyed run).
        files = sorted(files, key=os.path.getmtime, reverse=True)
    quarantined = [row["path"] for row in rows if row.get("quarantined")]
    if not files:
        if quarantined:
            return ("[unavailable] this session trajectory could not be redacted and was "
                    "quarantined; the audit trail is in events.jsonl "
                    "(session_redaction_failed).")
        return ""
    parts = [trajectory_log.read_all(p) for p in files]
    if quarantined:
        parts.append("[unavailable] %d attempt file(s) could not be redacted and were "
                     "quarantined (session_redaction_failed)." % len(quarantined))
    return _render("\n".join(t for t in parts if t.strip()), max_chars,
                   expose_reasoning=False)


_SEAT_FOR_ROLE = {"planner": "plan", "evaluator": "evaluate"}


def declared_session_outers(seat: Any, traj_outer: Any, outer: Any) -> Any:
    """ONE rule for the own-session outer set a seat may read (docs/19 §12.11).

    Consumers must not re-derive it: ``base_role`` stamps it onto the
    ``AccessContext`` and ``AccessBroker`` validates requests against it, so a
    seat can read exactly the outers the framework declared (never the agent's
    request). The self seat also reads the previous outer (L65); every other
    seat reads only its own."""
    if traj_outer is None:
        return None
    outers = {str(traj_outer)}
    if str(seat) == "self_improve" and isinstance(outer, int) and outer > 1:
        outers.add(str(outer - 1))
    return frozenset(outers)


def seat_session_files(output_dir: str, outer: Any, role: str, *,
                       seat: Any = None) -> List[Dict[str, Any]]:
    """The seat's OWN session files as rows ``{path, genid, name, bytes,
    quarantined}`` — the single source for BOTH the tool's fallback and the
    read tool's own-session face.

    Two shapes, because the two seats write different things:
    - ``self_improve``: OUTER-level files (``outer.jsonl`` / ``outer__<attempt>.jsonl``,
      one per loop instantiation — no genid).
    - ``plan`` / ``evaluate``: PER-GENERATION files (``gen_<genid>.jsonl``).
    The legacy layout (``outer_<O>/<genid>/<role>.jsonl``, pre-seat runs) is
    enumerated too and deduped, so an old run keeps reading.
    """
    seat = seat if seat is not None else _SEAT_FOR_ROLE.get(role)
    return _seat_session_files(output_dir, outer, role, seat)


def _seat_session_files(output_dir: str, outer: Any, role: str,
                        seat: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not seat:
        return rows

    def add(path: str, *, genid: Any, name: str, quarantined: bool = False) -> None:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        rows.append({"path": path, "genid": genid, "name": name,
                     "bytes": size, "quarantined": bool(quarantined)})

    if seat == "self_improve":
        seat_dir = paths.role_traj_dir(output_dir, outer, role, "self_improve")
        for p in _outer_session_files(output_dir, outer, role):
            add(p, genid=None, name=os.path.basename(p))
        seen_names = {row["name"] for row in rows}
        for q in sorted(glob.glob(os.path.join(seat_dir, "outer*.jsonl.unredacted-*"))):
            name = os.path.basename(q).split(".jsonl", 1)[0] + ".jsonl"
            if name not in seen_names:
                add(q, genid=None, name=name, quarantined=True)
        legacy_dir = paths.outer_traj_dir(output_dir, outer)
        for q in sorted(glob.glob(os.path.join(legacy_dir, f"{role}*.jsonl.unredacted-*"))):
            name = os.path.basename(q).split(".jsonl", 1)[0] + ".jsonl"
            if name not in seen_names:
                add(q, genid=None, name=name, quarantined=True)
        return rows

    # plan / evaluate: per-generation session files, seat layout first
    seat_dir = paths.role_traj_dir(output_dir, outer, role, seat)
    seen_genids: set = set()
    if os.path.isdir(seat_dir):
        for p in sorted(glob.glob(os.path.join(seat_dir, "gen_*.jsonl"))):
            genid = os.path.basename(p)[4:-6]
            seen_genids.add(genid)
            add(p, genid=genid, name=os.path.basename(p))
        for q in sorted(glob.glob(os.path.join(seat_dir, "gen_*.jsonl.unredacted-*"))):
            genid = os.path.basename(q).split(".jsonl", 1)[0][4:]
            if genid not in seen_genids:
                seen_genids.add(genid)
                add(q, genid=genid, name=f"gen_{genid}.jsonl", quarantined=True)
    legacy_dir = paths.outer_traj_dir(output_dir, outer)
    if os.path.isdir(legacy_dir):
        for entry in sorted(os.listdir(legacy_dir)):
            sub = os.path.join(legacy_dir, entry)
            if entry == "roles" or not os.path.isdir(sub):
                continue
            p = os.path.join(sub, f"{role}.jsonl")
            if os.path.isfile(p):
                if entry not in seen_genids:
                    seen_genids.add(entry)
                    add(p, genid=entry, name=f"{role}.jsonl")
            elif glob.glob(p + ".unredacted-*") and entry not in seen_genids:
                seen_genids.add(entry)
                add(p, genid=entry, name=f"{role}.jsonl", quarantined=True)
    return rows


def outer_session_index(output_dir: str, outer: Any, role: str, *,
                        seat: Any = None) -> List[Dict[str, Any]]:
    """List this outer's per-inner session files for a role (genid + size).

    Quarantined (unredactable) sessions are reported explicitly with
    ``quarantined: True`` instead of silently vanishing from the list."""
    return [
        {"genid": row["genid"], "name": row["name"], "bytes": row["bytes"],
         "quarantined": True}
        if row.get("quarantined") else
        {"genid": row["genid"], "name": row["name"], "bytes": row["bytes"]}
        if row.get("name") else
        {"genid": row["genid"], "bytes": row["bytes"]}
        for row in seat_session_files(output_dir, outer, role, seat=seat)
    ]


def session_label(current_outer: Any, outer: Any, genid: Any, filename: str) -> str:
    """The OWN-session address label for one file — the ONE place the label
    rule lives, shared by trajectory materialization and the
    read_session_trajectory fallback so both sides' address spaces agree.

    The current outer keeps the established genid-qualified form
    (``<genid>/<file>`` — zero regression for existing addresses); any OTHER
    outer gets an outer-prefixed form (``outer_<O>/<genid>/<file>``) so one
    session's address space stays unique across outers."""
    prefix = "" if str(outer) == str(current_outer) else f"outer_{outer}/"
    if genid is None:
        # OUTER-level session file (the self seat: one file per loop instance,
        # no genid). The file NAME is the address; the prefix keeps outers apart.
        return f"{prefix}{filename}"
    return f"{prefix}{genid}/{filename}"


def session_universe(output_dir: str, role: str, current_outer: Any,
                     extra_outers: Any = None, *, seat: Any = None) -> List[Tuple[str, str]]:
    """The seat's OWN-session visible universe as ``(label, path)`` pairs —
    the single definition served to BOTH consumers: trajectory materialization
    (``loop._traj_materialization_files``) and the read_session_trajectory
    fallback, so what a session can read is exactly what was declared.

    ``extra_outers`` (e.g. the previous outer for the self seat) widen the
    universe beyond the current outer; their entries carry outer-prefixed
    labels (see ``session_label``). Canonical files only (attempt siblings
    stay a fallback-side refinement, as before); quarantined (unredactable)
    sessions are EXCLUDED from the universe. Everything is advisory: missing
    files/dirs are skipped — the universe is what exists."""
    out: List[Tuple[str, str]] = []
    seen = set()
    for o in [current_outer] + list(extra_outers or []):
        if o is None:
            continue
        # The resolver owns BOTH shapes (seat-keyed and legacy). Rebuilding the
        # path here was the bug: it always produced the LEGACY
        # ``outer_<O>/<genid>/<role>.jsonl`` even when the run wrote the seat
        # layout, so the file never existed and every seat materialized 0
        # segments (docs/19 §12.12 F1).
        rows = seat_session_files(output_dir, o, role, seat=seat)
        rows.sort(key=lambda r: (0, int(str(r.get("genid"))))
                  if r.get("genid") is not None and str(r.get("genid")).isdigit()
                  else (1, str(r.get("genid") or "")))
        for row in rows:
            if row.get("quarantined"):
                continue
            path = row["path"]
            if not os.path.isfile(path):
                continue
            label = session_label(current_outer, o, row.get("genid"),
                                  os.path.basename(path))
            if label in seen:
                continue
            seen.add(label)
            out.append((label, path))
    return out


def redact_file(path: str) -> None:
    """Redact a JSONL trajectory file in place (parts merged, then removed).

    The replace is ATOMIC (tmp file + ``os.replace``) and the final write does
    not swallow ``OSError``: a failure raises so the caller can quarantine the
    unredacted file (fail-closed) instead of leaving it servable.
    """
    if not path or not os.path.exists(path):
        return
    text = trajectory_log.read_all(path)
    out: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            rec = {"kind": "text", "text": line}
        out.append(redact_record(rec))
    tmp = f"{path}.redact-tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in out:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        os.replace(tmp, path)
    except Exception:
        # never leave the truncated tmp behind; the ORIGINAL (unredacted) file is
        # untouched -- the caller owns the quarantine / visibility decision
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    # rotated parts were merged in; drop them only after the replace succeeded
    # (best-effort cleanup: leaving a part file cannot leak anything -- the
    # readers always resolve the main file first)
    for p in glob.glob(path + ".*"):
        if p[len(path) + 1:].isdigit():
            try:
                os.remove(p)
            except OSError:
                pass


def quarantine_unredacted(path: str, reason: str = "") -> str:
    """Move an UNREDACTED trajectory file out of every consumer's reach.

    All readers judge by exact file-name existence (``read_session``, ``outer_session_index``,
    ``_session_file``), so RENAMING is the fail-closed primitive: the raw file stays
    on disk for human forensics but no LLM-facing path can resolve it.

    Returns the quarantined file name.
    """
    if not path or not os.path.exists(path):
        return path
    dst = f"{path}.unredacted-{int(time.time() * 1000)}"
    try:
        os.replace(path, dst)
    except OSError:
        return path  # rename failed: the caller must NOT assume this is now closed
    return dst
