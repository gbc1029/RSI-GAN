import pandas as pd
from sklearn.model_selection import train_test_split  # NEW

from domains.imo.grading_utils import GROUND_TRUTH_KEY


def print_stats(name, df):
    print(f"== {name} ==")
    print(f"count: {len(df)}")
    print(
        "label distribution:",
        df[GROUND_TRUTH_KEY].value_counts(normalize=True).sort_index().round(3).to_dict()
    )


def curate_subsets_gradingbench():
    # Load GradingBench
    csv_path = "./domains/imo/gradingbench.csv"
    df = pd.read_csv(csv_path)
    print_stats("original", df)

    # Filter and shuffle
    df_filtered = df[df["Points"].isin([0, 1, 6, 7])].reset_index(drop=True)
    df_shuffled = df_filtered.sample(frac=1, random_state=42).reset_index(drop=True)
    base = csv_path.replace(".csv", "_filtered")
    df_shuffled.to_csv(f"{base}.csv", index=False)
    print_stats("filtered and shuffled", df_shuffled)

    # Create subset
    subset_size = 100
    n_total = subset_size * 3

    # Sample a fixed-size subset, then stratify-split it
    subset = df_shuffled.sample(n=n_total, random_state=42).reset_index(drop=True)

    train_df_subset, temp = train_test_split(
        subset,
        test_size=2 * subset_size,
        random_state=42,
        stratify=subset[GROUND_TRUTH_KEY],
    )
    val_df_subset, test_df_subset = train_test_split(
        temp,
        test_size=subset_size,
        random_state=42,
        stratify=temp[GROUND_TRUTH_KEY],
    )

    print(f"\n========== SUBSET {subset_size} ==========")
    print_stats("train", train_df_subset)
    print_stats("val", val_df_subset)
    print_stats("test", test_df_subset)

    base = csv_path.replace(".csv", f"_filtered_{subset_size}")
    train_df_subset.to_csv(f"{base}_train.csv", index=False)
    val_df_subset.to_csv(f"{base}_val.csv", index=False)
    test_df_subset.to_csv(f"{base}_test.csv", index=False)


if __name__ == "__main__":
    curate_subsets_gradingbench()
