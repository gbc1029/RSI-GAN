import os
import json
import pandas as pd
from datasets import load_dataset


def download_dataset() -> pd.DataFrame:
    """
    Download the Search Arena dataset (test split) via Hugging Face Datasets,
    save the raw CSV for reference, and return it as a DataFrame.
    """
    ds = load_dataset("lmarena-ai/search-arena-v1-7k")
    test_ds = ds["test"]  # pyright: ignore
    df = pd.DataFrame(test_ds)
    os.makedirs("./domains/search_arena", exist_ok=True)
    df.to_csv("./domains/search_arena/dataset.csv", index=False)
    return df


def make_split(df: pd.DataFrame, id_list: list[int]) -> pd.DataFrame:
    """
    Return a DataFrame that contains only rows whose question_id is in id_list,
    ordered exactly as in id_list (stable for ties).
    """
    order_map = {int(qid): i for i, qid in enumerate(id_list)}
    out = df[df["question_id"].isin(order_map)].copy()
    out["__order__"] = out["question_id"].map(order_map)
    out = out.sort_values("__order__", kind="stable").drop(columns="__order__")
    return out


if __name__ == "__main__":
    # --- Load and filter dataset ---
    # (Always downloads/loads via HF and writes ./domains/search_arena/dataset.csv)
    df = download_dataset()

    # Filter: drop ties and non-English; shuffle for randomness (order won't matter later)
    df = df[(df["winner"] != "tie") & (df["winner"] != "tie (bothbad)")]
    df = df[df["language"] == "English"].copy()
    df = df.sample(frac=1, random_state=42).reset_index(drop=True)
    os.makedirs("./domains/search_arena", exist_ok=True)
    df.to_csv("./domains/search_arena/dataset_filtered.csv", index=False)
    print("Filtered dataset size:", len(df))

    # Ensure question_id is int
    df["question_id"] = df["question_id"].astype(int)

    # Universe of IDs after filtering
    all_ids = set(df["question_id"].unique())

    # --- Load question_id splits from JSON ---
    dataset_ids_path = "./domains/search_arena/dataset_filtered_100_ids.json"
    with open(dataset_ids_path, "r") as f:
        dataset_ids = json.load(f)

    # Basic validation on keys
    required_keys = {"train", "val", "test"}
    missing_keys = required_keys - set(dataset_ids.keys())
    if missing_keys:
        raise KeyError(f"Split file missing keys: {sorted(missing_keys)}")

    # Coerce to lists of int
    train_list = [int(x) for x in dataset_ids["train"]]
    val_list = [int(x) for x in dataset_ids["val"]]
    test_list = [int(x) for x in dataset_ids["test"]]

    # Overlap checks
    train_ids, val_ids, test_ids = set(train_list), set(val_list), set(test_list)
    assert not (train_ids & val_ids), "train/val overlap in question_id"
    assert not (train_ids & test_ids), "train/test overlap in question_id"
    assert not (val_ids & test_ids), "val/test overlap in question_id"

    # Ensure all requested IDs exist in filtered universe
    requested = train_ids | val_ids | test_ids
    missing_in_df = requested - all_ids
    if missing_in_df:
        example_missing = list(sorted(missing_in_df))[:10]
        raise ValueError(
            f"{len(missing_in_df)} split IDs not present after filtering: "
            f"(e.g., {example_missing}...)"
        )

    # --- Create split DataFrames in the exact JSON order ---
    train_df = make_split(df, train_list)
    val_df = make_split(df, val_list)
    test_df = make_split(df, test_list)

    # Optional: size sanity checks
    if len(train_df) != len(train_list):
        print(f"Warning: train_df rows ({len(train_df)}) != train_list length ({len(train_list)})")
    if len(val_df) != len(val_list):
        print(f"Warning: val_df rows ({len(val_df)}) != val_list length ({len(val_list)})")
    if len(test_df) != len(test_list):
        print(f"Warning: test_df rows ({len(test_df)}) != test_list length ({len(test_list)})")

    # --- Save ordered splits ---
    out_dir = "./domains/search_arena"
    os.makedirs(out_dir, exist_ok=True)

    train_path = os.path.join(out_dir, "dataset_filtered_100_train.csv")
    val_path = os.path.join(out_dir, "dataset_filtered_100_val.csv")
    test_path = os.path.join(out_dir, "dataset_filtered_100_test.csv")

    train_df.to_csv(train_path, index=False)
    val_df.to_csv(val_path, index=False)
    test_df.to_csv(test_path, index=False)

    print(
        "Saved splits:\n"
        f"  train -> {train_path} ({len(train_df)})\n"
        f"  val   -> {val_path}   ({len(val_df)})\n"
        f"  test  -> {test_path}  ({len(test_df)})"
    )
