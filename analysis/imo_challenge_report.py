#!/usr/bin/env python3
"""Print the per-outer IMO challenge and paired-validation metrics.

Offline only: reads ``ckpt/supernodes/outer_<N>.json`` and never opens task
reports, answer files, or the held-out question set.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, List


def _supernodes(run_dir: str) -> List[Dict[str, Any]]:
    root = os.path.join(run_dir, "ckpt", "supernodes")
    rows = []
    if not os.path.isdir(root):
        return rows
    for name in os.listdir(root):
        if not name.startswith("outer_") or not name.endswith(".json"):
            continue
        if "_" in name[6:-5]:
            continue
        try:
            with open(os.path.join(root, name), "r", encoding="utf-8") as stream:
                row = json.load(stream)
            if isinstance(row, dict):
                rows.append(row)
        except (OSError, ValueError, TypeError):
            continue
    return sorted(rows, key=lambda row: int(row.get("outer", 0)))


def _row(sn: Dict[str, Any]) -> Dict[str, Any]:
    benchmark = sn.get("benchmark") or {}
    planner = (sn.get("planner_stats") or {}).get("challenge") or {}
    evaluator = (sn.get("evaluator_stats") or {}).get("challenge") or {}
    challenge_fitness = sn.get("challenge_fitness") or {}
    return {
        "outer": sn.get("outer"),
        "state_counts": evaluator.get("state_counts", planner.get("state_counts", {})),
        "supported_challenges": evaluator.get("supported_challenges",
                                               planner.get("supported_challenges", 0)),
        "undecidable_challenges": evaluator.get("undecidable_challenges",
                                                  planner.get("undecidable_challenges", 0)),
        "evaluator_adversarial_score": evaluator.get("evaluator_adversarial_score"),
        "challenge_precision_smoothed": evaluator.get("challenge_precision_smoothed"),
        "planner_adversarial_score": planner.get("planner_adversarial_score"),
        "repair_rate_smoothed": planner.get("repair_rate_smoothed"),
        "planner_root_val_mean": benchmark.get("planner_root_val_mean",
                                                 benchmark.get("val_mean")),
        "task_val_mean": benchmark.get("task_val_mean"),
        "task_parent_val_mean": benchmark.get("task_parent_val_mean"),
        "task_val_delta": benchmark.get("task_val_delta"),
        "task_val_noop_abs_q75": benchmark.get("task_val_noop_abs_q75"),
        "challenge_fitness_active": challenge_fitness.get("active", False),
        "generalized_repair": challenge_fitness.get("generalized_repair", False),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit one JSON object per outer")
    args = parser.parse_args(argv)
    rows = [_row(sn) for sn in _supernodes(args.run_dir)]
    if args.as_json:
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        return 0
    print("outer\tsupported\tundecidable\tevaluator_adv\tplanner_adv\t"
          "planner_root_val\ttask_val\ttask_parent_val\ttask_delta\t"
          "noop_q75\tfitness_active\tgeneralized_repair\tstate_counts")
    for row in rows:
        print("\t".join(str(row[key]) for key in (
            "outer", "supported_challenges", "undecidable_challenges",
            "evaluator_adversarial_score", "planner_adversarial_score",
            "planner_root_val_mean", "task_val_mean", "task_parent_val_mean",
            "task_val_delta", "task_val_noop_abs_q75",
            "challenge_fitness_active", "generalized_repair", "state_counts")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
