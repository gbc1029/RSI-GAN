import argparse
import os
import json

from utils.common import load_json_file


def get_all_performance(run_keyword, results_dir='./outputs', expected_num_tasks=None):
    """
    Retrieve performance results for all runs based on the provided keyword.

    Args:
        run_keyword (str): A keyword used to identify the target runs' evaluation results.

    Returns:
        list: A list of dictionaries, each containing performance results for a matching run.
    """
    # Find all JSON files in eval_results_dir matching the keyword
    matching_files = [
        f for f in os.listdir(results_dir)
        if f.endswith('.json') and run_keyword in f
    ]
    
    # Return an empty list if no matches are found
    if not matching_files:
        print(f"No evaluation files found matching the keyword '{run_keyword}'.")
        return None, None
    
    # Process each matching file
    performance_results = []
    total_resolved_instances = 0
    total_submitted_instances = 0
    total_unresolved_ids = []
    total_resolved_ids = []
    total_emptypatch_ids = []
    for file_name in matching_files:
        eval_agent_path = os.path.join(results_dir, file_name)
        eval_results = load_json_file(eval_agent_path)
        resolved_instances = eval_results.get('resolved_instances', 0)
        submitted_instances = eval_results.get('submitted_instances', 0)
        total_resolved_instances += resolved_instances
        total_submitted_instances += submitted_instances
        accuracy_score = resolved_instances / submitted_instances if submitted_instances > 0 else 0
        performance_results.append({'file': file_name, 'accuracy_score': accuracy_score, **eval_results})
        total_unresolved_ids.extend(eval_results.get('unresolved_ids', []))
        total_emptypatch_ids.extend(eval_results.get('empty_patch_ids', []))
        total_resolved_ids.extend(eval_results.get('resolved_ids', []))

    # Calculate the overall accuracy score
    overall_performance = {}
    total_instances = expected_num_tasks if expected_num_tasks is not None else total_submitted_instances
    overall_performance['accuracy_score'] = total_resolved_instances / total_instances if total_instances > 0 else 0
    overall_performance['total_resolved_instances'] = total_resolved_instances
    overall_performance['total_submitted_instances'] = total_submitted_instances
    overall_performance['total_instances'] = total_instances
    overall_performance['files'] = matching_files
    overall_performance['total_unresolved_ids'] = total_unresolved_ids
    overall_performance['total_emptypatch_ids'] = total_emptypatch_ids
    overall_performance['total_resolved_ids'] = total_resolved_ids
    
    return performance_results, overall_performance

def report(output_dir, run_keyword, expected_num_tasks=None):
    # Get performance results
    performances, overall_performance = get_all_performance(run_keyword, results_dir=output_dir, expected_num_tasks=expected_num_tasks)

    # Write report
    report_file = os.path.join(output_dir, "report.json")
    with open(report_file, "w") as f:
        print(json.dumps(overall_performance, indent=4), file=f)
    print(f"Report written to {report_file}")


if __name__ == "__main__":
    # Parse command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--output_dir", type=str, default="./outputs/initial_polyglot_0", help="Output directory")
    parser.add_argument("--model_name_or_path", type=str, default="eval_run", help="Dataset subset")
    args = parser.parse_args()

    report(args.output_dir, args.model_name_or_path)
