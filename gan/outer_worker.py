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
    p.add_argument("--task-domain", dest="task_domain", default="paper_review")
    p.add_argument("--domains", default=None,
                   help="comma-separated meta domains; exactly one value is supported "
                        "(multi-domain evaluation is not implemented yet)")
    p.add_argument("--resume-boundary", dest="resume_boundary", default="latest",
                   choices=["latest", "outer"],
                   help="'outer' (P1): restore the latest OUTER-boundary checkpoint and "
                        "never resume from a crashed outer's partial inner state.")
    p.add_argument("--subset", default="_filtered_100_train")
    p.add_argument("--num_samples", type=int, default=2)
    args = p.parse_args()

    domains = None
    if args.domains:
        domains = [d.strip() for d in args.domains.split(",") if d.strip()]
        if len(domains) != 1:
            p.error(f"--domains accepts exactly one domain (got {domains}); "
                    f"multi-domain evaluation is not implemented yet")

    from gan.build import build_gan_loop

    loop = build_gan_loop(
        repo_root=args.repo_root,
        output_dir=args.output_dir,
        task_domain=args.task_domain,
        domains=domains,
        subset=args.subset,
        num_samples=args.num_samples,
        code_repo=True,
    )
    loop.resume = True  # load trees/state produced by previous outers
    loop.resume_boundary = args.resume_boundary
    loop.run(only_outer=args.outer)


if __name__ == "__main__":
    main()
