"""Task-trajectory archive (FRAMEWORK, frozen).

Per-generation task-agent trajectories are archived out of the run dir into the
per-instance JSONL layout, redacted, and indexed. Which generations a role may
read is decided by the loop and passed explicitly per session via
``AccessContext.trajectory_genids`` (v4.20; planner = direct parent, evaluator =
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
from typing import Any, Dict, List

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
    for k in ("text", "input", "output"):
        if k in out:
            out[k] = _redact_value(out[k])
    out["redacted"] = True
    return out


def collect(run_dir: str, run_id: str, output_dir: str, outer: Any, genid: Any) -> str:
    """Archive + redact the task trajectories of one generation into JSONL."""
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
    if records:
        with open(dest, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return dest


def _session_file(output_dir: str, genid: Any, role: str) -> str:
    pat = os.path.join(paths.trajectory_root(output_dir), "outer_*", str(genid), f"{role}.jsonl")
    hits = sorted(glob.glob(pat))
    return hits[-1] if hits else ""


def _render(text: str, max_chars: int) -> str:
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
            parts.append(f"{kind}: {rec.get('text', '')}")
    return "\n".join(parts)[:max_chars]


def read(output_dir: str, genid: Any, max_chars: int = 6000, role: str = "task") -> str:
    """Return a redacted, truncated rendering of a generation's trajectory."""
    path = _session_file(output_dir, genid, role)
    if not path or not os.path.isfile(path):
        return ""
    return _render(trajectory_log.read_all(path), max_chars)


def read_session(output_dir: str, outer: Any, genid: Any, role: str, max_chars: int = 6000) -> str:
    """Read a role's own session trajectory for a given outer/genid."""
    path = paths.session_traj_file(output_dir, outer, genid, role)
    if not os.path.isfile(path):
        return read(output_dir, genid, max_chars, role)
    return _render(trajectory_log.read_all(path), max_chars)


def outer_session_index(output_dir: str, outer: Any, role: str) -> List[Dict[str, Any]]:
    """List this outer's per-inner session files for a role (genid + size)."""
    d = paths.outer_traj_dir(output_dir, outer)
    out: List[Dict[str, Any]] = []
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            sub = os.path.join(d, name)
            if os.path.isdir(sub):
                p = os.path.join(sub, f"{role}.jsonl")
                if os.path.isfile(p):
                    out.append({"genid": name, "bytes": os.path.getsize(p)})
    return out


def redact_file(path: str) -> None:
    """Redact a JSONL trajectory file in place (parts merged, then removed)."""
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
    for p in glob.glob(path + ".*"):
        if p[len(path) + 1:].isdigit():
            try:
                os.remove(p)
            except OSError:
                pass
    try:
        with open(path, "w", encoding="utf-8") as f:
            for rec in out:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass
