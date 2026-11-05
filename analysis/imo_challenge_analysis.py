#!/usr/bin/env python3
"""Compact offline analysis for an IMO proof challenge run.

Reads only durable run artifacts and writes a small, decision-oriented bundle
under ``<run>/score_analysis``.  It deliberately keeps train proof scores,
paired task validation, challenge adjudication, and runtime health separate.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List


def _json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, TypeError, ValueError):
        return default


def _jsonl(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        pass
    return rows


def _write_csv(path: str, rows: Iterable[Dict[str, Any]], fields: List[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def _num(value: Any) -> Any:
    return value if isinstance(value, (int, float)) else ""


def _prediction_ids(run: str, genid: Any) -> set[str]:
    path = os.path.join(run, "runs", str(genid), "predictions.csv")
    ids: set[str] = set()
    try:
        with open(path, encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                qid = str(row.get("Problem ID") or "").strip()
                if qid:
                    ids.add(qid)
    except OSError:
        pass
    return ids


def _event_index(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    index: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for event in events:
        index[str(event.get("type", ""))][str(event.get("genid", ""))].append(event)
    return index


def _load_challenges(run: str) -> List[Dict[str, Any]]:
    ledger = _json(os.path.join(run, "ckpt", "challenges", "ledger.json"), {})
    result: List[Dict[str, Any]] = []
    for raw in (ledger.get("challenges", {}) or {}).values():
        if not isinstance(raw, dict):
            continue
        parent = str(raw.get("producer_node", ""))
        child = str(raw.get("consumed_child_node", ""))
        qid = str(raw.get("qid", ""))
        parent_ids = _prediction_ids(run, parent) if parent != "" else set()
        child_ids = _prediction_ids(run, child) if child not in ("", "None") else set()
        adjudication = raw.get("adjudication") or {}
        result.append({
            "challenge_id": raw.get("challenge_id"),
            "outer": raw.get("outer"),
            "qid": qid,
            "defect_type": raw.get("defect_type"),
            "producer_node": parent,
            "consumer_node": child if child != "None" else "",
            "status": raw.get("status"),
            "state": adjudication.get("state", ""),
            "reason": adjudication.get("reason", ""),
            "actionable": adjudication.get("actionable", ""),
            "parent_has_qid": qid in parent_ids,
            "child_has_qid": qid in child_ids,
            "proof_complete": bool(qid in parent_ids and qid in child_ids),
        })
    return result


def _load_generations(run: str, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    scores = _jsonl(os.path.join(run, "scores", "scores.jsonl"))
    by_gen: Dict[str, Dict[str, Any]] = {}
    index = _event_index(events)
    for score in scores:
        genid = str(score.get("genid", ""))
        harness = (index["task_harness_done"].get(genid) or [{}])[-1]
        inner = (index["inner_done"].get(genid) or [{}])[-1]
        invalid = (index["inner_invalid"].get(genid) or [{}])[-1]
        sample_ids = harness.get("sampled_ids") or []
        challenge_ids = harness.get("challenge_ids") or []
        by_gen[genid] = {
            "genid": genid,
            "outer": inner.get("outer", score.get("outer", "")),
            "inner": inner.get("inner", ""),
            "parent_genid": inner.get("parent_genid", ""),
            "score": _num(score.get("score")),
            "coverage": _num(score.get("coverage")),
            "score_status": score.get("score_status", ""),
            "valid_parent": score.get("valid_parent", ""),
            "sample_count": len(sample_ids),
            "challenge_sample_count": len(challenge_ids),
            "harness_rc": harness.get("rc", ""),
            "harness_duration_s": harness.get("duration_s", ""),
            "inner_duration_s": inner.get("duration_s", ""),
            "invalid_reason": invalid.get("reason", ""),
        }
    return sorted(by_gen.values(), key=lambda row: int(row["genid"]) if str(row["genid"]).isdigit() else str(row["genid"]))


def _load_outers(run: str, events: List[Dict[str, Any]], challenges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    index = _event_index(events)
    paths = []
    for path in sorted(os.listdir(os.path.join(run, "ckpt", "supernodes"))):
        if re.fullmatch(r"outer_\d+\.json", path):
            paths.append(os.path.join(run, "ckpt", "supernodes", path))
    rows: List[Dict[str, Any]] = []
    for path in paths:
        data = _json(path, {})
        outer = str(data.get("outer", ""))
        benchmark = data.get("benchmark", {}) or {}
        planner = data.get("planner_stats", {}).get("challenge", {}) or {}
        evaluator = data.get("evaluator_stats", {}).get("challenge", {}) or {}
        done = (index["outer_done"].get(outer) or [{}])[-1]
        invalid = [e for e in events if str(e.get("outer")) == outer and e.get("type") == "inner_invalid"]
        inner_done = [e for e in events if str(e.get("outer")) == outer and e.get("type") == "inner_done"]
        states = planner.get("state_counts") or {}
        rows.append({
            "outer": outer,
            "train_n_valid": benchmark.get("n_valid", ""),
            "task_val_mean": benchmark.get("task_val_mean", ""),
            "task_parent_val_mean": benchmark.get("task_parent_val_mean", ""),
            "task_val_delta": benchmark.get("task_val_delta", ""),
            "task_val_n_valid": benchmark.get("task_val_n_valid", ""),
            "supported_challenges": planner.get("supported_challenges", ""),
            "undecidable_challenges": planner.get("undecidable_challenges", ""),
            "verified_repair": states.get("VERIFIED_REPAIR", 0),
            "verified_unresolved": states.get("VERIFIED_UNRESOLVED", 0),
            "unsupported_challenge": states.get("UNSUPPORTED_CHALLENGE", 0),
            "regression_after_false": states.get("REGRESSION_AFTER_FALSE_CHALLENGE", 0),
            "challenge_precision_smoothed": evaluator.get("challenge_precision_smoothed", ""),
            "inner_done": len(inner_done),
            "inner_invalid": len(invalid),
            "duration_s": done.get("duration_s", ""),
        })
    return sorted(rows, key=lambda row: int(row["outer"]) if str(row["outer"]).isdigit() else str(row["outer"]))


def _plots(outdir: str, generations: List[Dict[str, Any]], outers: List[Dict[str, Any]]) -> List[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    made: List[str] = []
    x = np.arange(len(generations))
    scores = [float(row["score"]) if isinstance(row["score"], (int, float)) else np.nan for row in generations]
    coverage = [float(row["coverage"]) if isinstance(row["coverage"], (int, float)) else np.nan for row in generations]
    invalid = [i for i, row in enumerate(generations) if row["invalid_reason"]]
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    axes[0].plot(x, scores, "o-", label="train proof score")
    if invalid:
        axes[0].scatter(invalid, [0] * len(invalid), marker="x", color="crimson", label="inner invalid")
    axes[0].set_ylabel("score (0..1)")
    axes[0].legend()
    axes[0].grid(alpha=0.3)
    axes[1].plot(x, coverage, "s-", color="darkorange", label="proof coverage")
    axes[1].set_ylim(-0.05, 1.05)
    axes[1].set_ylabel("coverage")
    axes[1].set_xlabel("generation order")
    axes[1].legend()
    axes[1].grid(alpha=0.3)
    fig.suptitle("IMO proof generation health")
    fig.tight_layout()
    path = os.path.join(outdir, "generation_progress.png")
    fig.savefig(path, dpi=140)
    plt.close(fig)
    made.append(path)

    ox = np.arange(len(outers))
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    states = [
        ("verified_repair", "VERIFIED_REPAIR"),
        ("verified_unresolved", "VERIFIED_UNRESOLVED"),
        ("unsupported_challenge", "UNSUPPORTED_CHALLENGE"),
        ("regression_after_false", "REGRESSION_AFTER_FALSE"),
        ("undecidable_challenges", "UNDECIDABLE"),
    ]
    bottom = np.zeros(len(outers))
    for field, label in states:
        values = np.asarray([float(row.get(field) or 0) for row in outers])
        axes[0].bar(ox, values, bottom=bottom, label=label)
        bottom += values
    axes[0].set_ylabel("challenge count")
    axes[0].legend(ncol=3, fontsize=8)
    axes[0].grid(alpha=0.3, axis="y")
    candidate = [float(row["task_val_mean"]) if isinstance(row["task_val_mean"], (int, float)) else np.nan for row in outers]
    parent = [float(row["task_parent_val_mean"]) if isinstance(row["task_parent_val_mean"], (int, float)) else np.nan for row in outers]
    delta = [float(row["task_val_delta"]) if isinstance(row["task_val_delta"], (int, float)) else np.nan for row in outers]
    axes[1].plot(ox, candidate, "o-", label="task candidate val")
    axes[1].plot(ox, parent, "s--", label="direct parent val")
    axes[1].plot(ox, delta, "^:", label="val delta")
    axes[1].axhline(0, color="gray", lw=0.8)
    axes[1].set_ylabel("val / delta")
    axes[1].set_xlabel("outer")
    axes[1].set_xticks(ox, [str(row["outer"]) for row in outers])
    axes[1].legend(fontsize=8)
    axes[1].grid(alpha=0.3)
    fig.suptitle("Challenge states and paired task validation")
    fig.tight_layout()
    path = os.path.join(outdir, "challenge_states.png")
    fig.savefig(path, dpi=140)
    plt.close(fig)
    made.append(path)

    fig, ax = plt.subplots(figsize=(11, 4))
    ax.plot(ox, candidate, "o-", label="candidate val")
    ax.plot(ox, parent, "s--", label="parent val")
    ax.bar(ox, delta, alpha=0.25, label="delta")
    ax.axhline(0, color="gray", lw=0.8)
    ax.set_xticks(ox, [str(row["outer"]) for row in outers])
    ax.set_xlabel("outer")
    ax.set_ylabel("task val")
    ax.set_title("Paired task validation by outer")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    path = os.path.join(outdir, "val_progress.png")
    fig.savefig(path, dpi=140)
    plt.close(fig)
    made.append(path)
    return made


def analyze(run: str) -> str:
    outdir = os.path.join(run, "score_analysis")
    os.makedirs(outdir, exist_ok=True)
    events = _jsonl(os.path.join(run, "logs", "events.jsonl"))
    challenges = _load_challenges(run)
    generations = _load_generations(run, events)
    outers = _load_outers(run, events, challenges)
    _write_csv(os.path.join(outdir, "challenge_summary.csv"), challenges, [
        "challenge_id", "outer", "qid", "defect_type", "producer_node",
        "consumer_node", "status", "state", "reason", "actionable",
        "parent_has_qid", "child_has_qid", "proof_complete",
    ])
    _write_csv(os.path.join(outdir, "generation_metrics.csv"), generations, list(generations[0]) if generations else ["genid"])
    _write_csv(os.path.join(outdir, "outer_metrics.csv"), outers, list(outers[0]) if outers else ["outer"])
    made = _plots(outdir, generations, outers) if outers else []

    states = Counter(str(row.get("state") or "<none>") for row in challenges)
    missing = [row for row in challenges if not row["proof_complete"] and row["consumer_node"]]
    invalid = [row for row in generations if row["invalid_reason"]]
    lines = [f"# IMO challenge analysis: `{os.path.basename(os.path.normpath(run))}`", "",
             f"- generations: {len(generations)}; outers: {len(outers)}",
             f"- challenges: {len(challenges)}; states: {dict(states)}",
             f"- consumed challenges with missing parent/child qid: {len(missing)}",
             f"- invalid inner generations: {len(invalid)}",
             "", "## Interpretation", "",
             "- `progress.png` is the legacy proof score/coverage plot per generation; it is not a task-val or challenge plot.",
             "- `challenge_states.png` and `val_progress.png` are outer-level because paired val is run at outer boundaries.",
             "- Missing child qids are evidence of incomplete proof output; they are reported as UNDECIDABLE, not treated as repair or failure.",
             "", "## Files", ""]
    for name in ["challenge_summary.csv", "generation_metrics.csv", "outer_metrics.csv", "generation_progress.png", "challenge_states.png", "val_progress.png"]:
        if os.path.exists(os.path.join(outdir, name)):
            lines.append(f"- `{name}`")
    report = os.path.join(outdir, "challenge_report.md")
    with open(report, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    print(json.dumps({"run": run, "outdir": outdir, "report": report, "plots": made,
                      "challenge_states": dict(states), "missing_proof": len(missing),
                      "invalid_inner": len(invalid)}, ensure_ascii=False, indent=2))
    return outdir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run")
    analyze(os.path.abspath(parser.parse_args().run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
