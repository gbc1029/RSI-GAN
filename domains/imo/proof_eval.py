import argparse
import os
import pandas as pd
import json


MAX_POINTS = 7  # max possible score per question


def report_proof_grading(dname):
    # Get predictions
    path = os.path.join(dname, "predictions.csv")
    df = pd.read_csv(path, dtype=str, keep_default_na=False)

    # Numeric 0-7 preserves the full IMO scale. Four legacy labels remain
    # readable for old installed proofgrader packages during deployment.
    requested_total = len(df)
    normalized = df["prediction"].astype(str).str.strip().str.lower()
    numeric = pd.to_numeric(normalized, errors="coerce")
    legacy = normalized.map({"incorrect": 0, "partial": 1, "almost": 6, "correct": 7})
    points = numeric.where(numeric.notna(), legacy)
    valid = points.isin(range(MAX_POINTS + 1)) & (points % 1 == 0)
    question_ids_ungraded = df.loc[~valid, "Problem ID"].astype(str).tolist()
    df = df.loc[valid].copy()
    df["prediction_points"] = points.loc[valid].astype(int)
    df["prediction"] = df["prediction_points"].astype(str)

    total = len(df)
    preds = df["prediction_points"]

    # Fraction of total possible points achieved
    points_percentage = float(preds.sum() / (MAX_POINTS * total)) if total else 0.0

    # Percentage of questions that achieved max score
    correct_mask = preds == MAX_POINTS
    correct_percentage = float(correct_mask.sum() / total) if total else 0.0

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
    question_points = {
        str(qid): int(points)
        for qid, points in zip(df["Problem ID"], df["prediction_points"])
    }

    report = {
        "points_percentage": points_percentage,
        "correct_percentage": correct_percentage,
        "requested_total": requested_total,
        "total": total,
        "coverage": (total / requested_total) if requested_total else None,
        "grading_scale": "imo_0_to_7",
        "label_distribution": label_distribution,
        "question_points": question_points,
        "question_ids_ungraded": question_ids_ungraded,
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
