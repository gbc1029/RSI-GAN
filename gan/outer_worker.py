"""Per-outer worker (glue): run exactly one outer generation from the code tree.

Launched by ``gan.driver`` with ``cwd=code_root`` and ``PYTHONPATH=code_root`` so
that ``import gan.*`` resolves to the *evolved* code for this outer. Role code
changes therefore take effect for the next outer only.
"""
from __future__ import annotations

import argparse


def main() -> None:
    p = argparse.ArgumentParser(description="GAN single-outer worker")
    p.add_argument("--repo_root", required=True)
    p.add_argument("--output_dir", required=True)
    p.add_argument("--outer", type=int, required=True)
    p.add_argument("--domains", required=True,
                   help="task domain; exactly one value (required, no default — the "
                        "only default lives in scripts/run_gan.py; empty and "
                        "multi-value inputs are rejected here before any work).")
    p.add_argument("--resume-boundary", dest="resume_boundary", default="latest",
                   choices=["latest", "outer"],
                   help="'outer' (P1): restore the latest OUTER-boundary checkpoint and "
                        "never resume from a crashed outer's partial inner state.")
    p.add_argument("--subset", default="_filtered_100_train")
    p.add_argument("--num_samples", type=int, default=2)
    p.add_argument("--inner", type=int, default=None)
    args = p.parse_args()

    # Entry validation before building/running anything: empty and multi-value
    # fail fast; multi-domain evaluation is not implemented yet.
    domains = [d.strip() for d in (args.domains or "").split(",") if d.strip()]
    if not domains:
        p.error("--domains 解析为空；请提供至少一个域名")
    if len(domains) != 1:
        p.error(f"--domains 目前仅支持单个域（收到 {len(domains)} 个：{domains}）；"
                f"多域评测尚未实现，本次不执行任务")

    from gan.build import build_gan_loop

    cfg_overrides = None
    if args.inner is not None:
        cfg_overrides = {"loop": {"inner_max": args.inner}}

    loop = build_gan_loop(
        repo_root=args.repo_root,
        output_dir=args.output_dir,
        domains=domains,
        subset=args.subset,
        num_samples=args.num_samples,
        cfg_overrides=cfg_overrides,
        code_repo=True,
    )
    loop.resume = True  # load trees/state produced by previous outers
    loop.resume_boundary = args.resume_boundary
    loop.run(only_outer=args.outer)


if __name__ == "__main__":
    main()
