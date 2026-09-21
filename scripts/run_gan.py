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
    p.add_argument("--task-domain", dest="task_domain", default="paper_review")
    p.add_argument("--domains", default=None,
                   help="Meta domain(s) as comma-separated values. Currently exactly ONE "
                        "domain is supported (multi-domain not implemented yet); a single "
                        "value is authoritative and overrides --task-domain (warning on "
                        "mismatch).")
    p.add_argument("--subset", default="_filtered_100_train")
    p.add_argument("--num_samples", type=int, default=2)
    p.add_argument("--outer", type=int, default=None)
    p.add_argument("--inner", type=int, default=None)
    p.add_argument("--resume", action="store_true",
                   help="Continue an interrupted run: skip completed outers and resume at "
                        "the next one (restores the last OUTER-boundary checkpoint; never "
                        "resumes from a crashed outer's partial state).")
    p.add_argument("--force", action="store_true",
                   help="Re-run from outer 1 on an output_dir that already has completed "
                        "outers (legacy duplicate-prone behaviour; explicit escape hatch).")
    p.add_argument("--preflight", action="store_true",
                   help="Probe configured models once before running (fail-fast).")
    p.add_argument("--in-process", action="store_true",
                   help="Run all outers in one process WITHOUT the per-run code repo (legacy).")
    args = p.parse_args()

    # --domains: single-value passthrough (authoritative); multi-domain not supported yet.
    domains = None
    if args.domains:
        domains = [d.strip() for d in args.domains.split(",") if d.strip()]
        if not domains:
            p.error("--domains解析为空；请提供至少一个域名")
        if len(domains) != 1:
            p.error(f"--domains 目前仅支持单个域（收到 {len(domains)} 个：{domains}）；"
                    f"多域评测尚未实现")
        if args.task_domain != domains[0]:
            print(f"[warn] --domains {domains[0]} 与 --task-domain {args.task_domain} 不一致："
                  f"以 --domains 为准（task_domain -> {domains[0]}）")
            args.task_domain = domains[0]

    overrides = {}
    if args.outer is not None:
        overrides.setdefault("loop", {})["outer_generations"] = args.outer
    if args.inner is not None:
        overrides.setdefault("loop", {})["inner_max"] = args.inner

    if args.resume and args.force:
        p.error("--resume and --force are mutually exclusive")

    if args.in_process:
        from gan.build import build_gan_loop
        loop = build_gan_loop(
            repo_root=args.repo_root,
            output_dir=args.output_dir,
            domains=domains,
            task_domain=args.task_domain,
            subset=args.subset,
            num_samples=args.num_samples,
            cfg_overrides=overrides or None,
            preflight=args.preflight,
            code_repo=False,
        )
        tree = loop.run()
        print(f"GAN loop done (in-process). task tree size={len(tree)}; "
              f"output_dir={os.path.abspath(args.output_dir)}")
    else:
        from gan.driver import run_gan_driver
        code_root = run_gan_driver(
            repo_root=args.repo_root,
            output_dir=args.output_dir,
            task_domain=args.task_domain,
            domains=domains,
            subset=args.subset,
            num_samples=args.num_samples,
            cfg_overrides=overrides or None,
            preflight=args.preflight,
            resume=args.resume,
            force=args.force,
        )
        print(f"GAN driver done. code_root={code_root}; "
              f"output_dir={os.path.abspath(args.output_dir)}")


if __name__ == "__main__":
    main()
