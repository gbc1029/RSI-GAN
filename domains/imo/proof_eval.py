import argparse
import os
import pandas as pd
import json


MAX_POINTS = 7  # max possible score per question


def report_proof_grading(dname):
    # Get predictions
    path = os.path.join(dname, "predictions.csv")
    df = pd.read_csv(path, dtype=str)

    total = len(df)
    if total == 0:
        report = {
            "points_percentage": 0.0,
            "correct_percentage": 0.0,
            "total": 0,
            "label_distribution": {"prediction": {}},
            "question_ids_failed": [],
            "question_ids_passed": [],
        }
        print(json.dumps(report, indent=4))
        return

    # Safely convert to numeric; non-convertible values become 0.0
    df["prediction_points"] = (
        df["prediction"]
        .map({"incorrect": 0, "partial": 1, "almost": 6, "correct": 7})
        .fillna(0)
        .astype(float)
    )
    preds = df["prediction_points"]

    # Fraction of total possible points achieved
    points_percentage = preds.sum() / (MAX_POINTS * total)

    # Percentage of questions that achieved max score
    correct_mask = preds == MAX_POINTS
    correct_percentage = correct_mask.sum() / total

    # Normalized label distribution for predictions (as strings)
    label_distribution = {
        "prediction": (
            df["prediction"]
            .astype(str)
            .value_counts(normalize=True)
            .sort_index()
            .to_dict()
        )
    }

    question_ids_passed = df.loc[correct_mask, "Problem ID"].tolist()
    question_ids_failed = df.loc[~correct_mask, "Problem ID"].tolist()

    report = {
        "points_percentage": points_percentage,
        "correct_percentage": correct_percentage,
        "total": total,
        "label_distribution": label_distribution,
        "question_ids_failed": question_ids_failed,
        "question_ids_passed": question_ids_passed,
    }

# Save the report as a JSON file
    report_path = os.path.join(dname, "report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=4)

    return report, report_path


if __name__ == "__main__":
    # Parse command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--dname", required=True, help="Path to harness outputs")
    args = parser.parse_args()

    report_proof_grading(args.dname)
