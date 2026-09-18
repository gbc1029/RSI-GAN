"""CLI entry for the GAN dual-loop.

Example (paper_review, minimal):
    python scripts/run_gan.py --repo_root . --output_dir outputs/gan_paper_review \
        --task-domain paper_review --subset _filtered_100_train --num_samples 2 \
        --outer 1 --inner 2
"""
from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def main():
    p = argparse.ArgumentParser(description="Run the GAN dual-loop (planner/evaluator/task).")
    p.add_argument("--repo_root", default=".")
    p.add_argument("--output_dir", default="./outputs/gan_run")
    p.add_argument("--domains", default=None, help="comma-separated meta domains (default: task domain)")
    p.add_argument("--task-domain", dest="task_domain", default="paper_review")
    p.add_argument("--subset", default="_filtered_100_train")
    p.add_argument("--num_samples", type=int, default=2)
    p.add_argument("--outer", type=int, default=None)
    p.add_argument("--inner", type=int, default=None)
    args = p.parse_args()

    from gan.build import build_gan_loop

    overrides = {}
    if args.outer is not None:
        overrides.setdefault("loop", {})["outer_generations"] = args.outer
    if args.inner is not None:
        overrides.setdefault("loop", {})["inner_max"] = args.inner

    loop = build_gan_loop(
        repo_root=args.repo_root,
        output_dir=args.output_dir,
        domains=(args.domains.split(",") if args.domains else None),
        task_domain=args.task_domain,
        subset=args.subset,
        num_samples=args.num_samples,
        cfg_overrides=overrides or None,
    )
    tree = loop.run()
    print(f"GAN loop done. task tree size={len(tree)}; output_dir={os.path.abspath(args.output_dir)}")


if __name__ == "__main__":
    main()
