import argparse
from datetime import datetime

from domains.harness import harness
from domains.report import report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run AI reviewer")
    parser.add_argument("--run_id", type=str, default=None, help="Run ID")
    parser.add_argument("--num_samples", type=int, default=-1, help="Number of samples to evaluate, -1 for all")
    args = parser.parse_args()

    domain = "paper_review"
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f") if args.run_id is None else args.run_id
    num_samples = args.num_samples

    output_folder = harness(
        agent_path="./baselines/ai_reviewer/agent.py",
        output_dir="./baselines/ai_reviewer/outputs",
        run_id=run_id,
        domain=domain,
        num_samples=num_samples,
        subset="_filtered_100_test",
    )

    report(dname=output_folder, domain=domain)
