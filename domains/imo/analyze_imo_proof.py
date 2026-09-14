import os
import pandas as pd

# eval_path = "./outputs/generate_20251230_160325_797106/gen_20/imo_proof_eval"
eval_path = "./outputs/generate_20251230_160325_797106_testevals/gen_20_gemini25pro"
MAX_POINTS = 7  # max possible score per question

# Load predictions
predictions_path = os.path.join(eval_path, "gradings/predictions.csv")

if os.path.exists(predictions_path):
    df = pd.read_csv(predictions_path)

    # Convert predictions to points
    df["prediction_points"] = (
        df["prediction"]
        .map({"incorrect": 0, "partial": 1, "almost": 6, "correct": 7})
        .fillna(0)
        .astype(float)
    )

    # Extract category from Problem ID (e.g., "PB-Basic-001" -> "Basic")
    df["question_type"] = df["Problem ID"].str.split("-").str[1]

    # Calculate points_percentage for basic questions
    basic_df = df[df["question_type"] == "Basic"]
    if len(basic_df) > 0:
        basic_points_pct = basic_df["prediction_points"].sum() / (MAX_POINTS * len(basic_df))
        print(f"\nBasic questions ({len(basic_df)} questions):")
        print(f"  points_percentage: {basic_points_pct:.4f}")
    else:
        print("\nNo basic questions found")

    # Calculate points_percentage for advanced questions
    advanced_df = df[df["question_type"] == "Advanced"]
    if len(advanced_df) > 0:
        advanced_points_pct = advanced_df["prediction_points"].sum() / (MAX_POINTS * len(advanced_df))
        print(f"\nAdvanced questions ({len(advanced_df)} questions):")
        print(f"  points_percentage: {advanced_points_pct:.4f}")

        # Break down by source
        print()
        # Novel Problems
        source_df = advanced_df[advanced_df["Source"] == "Novel Problem"]
        if len(source_df) > 0:
            source_points_pct = source_df["prediction_points"].sum() / (MAX_POINTS * len(source_df))
            print(f"  Novel Problem ({len(source_df)} questions): {source_points_pct:.4f}")

        # IMO 2024 (all modified problems)
        source_df = advanced_df[advanced_df["Source"].str.contains("IMO 2024", na=False)]
        if len(source_df) > 0:
            source_points_pct = source_df["prediction_points"].sum() / (MAX_POINTS * len(source_df))
            print(f"  IMO 2024 ({len(source_df)} questions): {source_points_pct:.4f}")

        # USAMO 2025
        source_df = advanced_df[advanced_df["Source"] == "USAMO 2025"]
        if len(source_df) > 0:
            source_points_pct = source_df["prediction_points"].sum() / (MAX_POINTS * len(source_df))
            print(f"  USAMO 2025 ({len(source_df)} questions): {source_points_pct:.4f}")
    else:
        print("No advanced questions found")
else:
    print(f"Predictions file not found: {predictions_path}")
