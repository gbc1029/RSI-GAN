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
    p.add_argument("--domains", default=None)
    p.add_argument("--task-domain", dest="task_domain", default="paper_review")
    p.add_argument("--subset", default="_filtered_100_train")
    p.add_argument("--num_samples", type=int, default=2)
    p.add_argument("--inner", type=int, default=None)
    args = p.parse_args()

    from gan.build import build_gan_loop

    cfg_overrides = None
    if args.inner is not None:
        cfg_overrides = {"loop": {"inner_max": args.inner}}

    loop = build_gan_loop(
        repo_root=args.repo_root,
        output_dir=args.output_dir,
        domains=(args.domains.split(",") if args.domains else None),
        task_domain=args.task_domain,
        subset=args.subset,
        num_samples=args.num_samples,
        cfg_overrides=cfg_overrides,
        code_repo=True,
    )
    loop.resume = True  # load trees/state produced by previous outers
    loop.run(only_outer=args.outer)


if __name__ == "__main__":
    main()
