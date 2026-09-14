import os
import math
import pandas as pd
from domains.harness import get_dataset

# ---------------- helpers ----------------

def _largest_remainder(target, sizes):
    total = sum(sizes)
    if total == 0:
        return [0] * len(sizes)
    raw = [target * (s / total) for s in sizes]
    floors = [int(math.floor(x)) for x in raw]
    remainder = target - sum(floors)
    fracs = sorted([(i, raw[i] - floors[i]) for i in range(len(sizes))],
                   key=lambda t: t[1], reverse=True)
    for i in range(remainder):
        floors[fracs[i][0]] += 1
    return floors

def _print_split_stats(name, df_split):
    total = len(df_split)
    accept_ct = (df_split['outcome'] == 'accept').sum()
    reject_ct = (df_split['outcome'] == 'reject').sum()
    accept_pct = (accept_ct / total * 100) if total else 0.0
    print(f"\n{name.upper()} dataset:")
    print(f"  Size: {total}")
    print(f"  Accept: {accept_ct} ({accept_pct:4.1f}%)")
    print(f"  Reject: {reject_ct} ({100 - accept_pct:4.1f}%)")
    print(f"  Unique question_ids: {df_split['question_id'].nunique()}")

def _leak_check(a_df, b_df, a_name, b_name):
    inter = set(a_df['question_id']) & set(b_df['question_id'])
    print(f"\nLeak check {a_name.upper()} vs {b_name.upper()}: overlap={len(inter)}")
    if inter:
        print(f"  Sample overlapping IDs (up to 10): {sorted(list(inter))[:10]}")

# ---------------- main builder ----------------

def make_balanced_splits(df, num_samples=100, shuffle_seed=42, prefix=""):
    df = df[df['outcome'].isin(['accept', 'reject'])].copy()
    df = df.sample(frac=1, random_state=shuffle_seed).reset_index(drop=True)

    accepted_df = df[df['outcome'] == 'accept'].copy()
    rejected_df = df[df['outcome'] == 'reject'].copy()

    # Max number of samples per split (each needs accept/reject = half of num_samples)
    per_class_per_split = num_samples // 2
    total_required_per_class = 3 * per_class_per_split

    min_count = min(len(accepted_df), len(rejected_df))
    if min_count < total_required_per_class:
        raise ValueError(f"Not enough data: need {total_required_per_class} accept/reject examples, but only have {min_count}")

    # Shuffle again for extra randomness
    accepted_df = accepted_df.sample(frac=1, random_state=shuffle_seed).reset_index(drop=True)
    rejected_df = rejected_df.sample(frac=1, random_state=shuffle_seed).reset_index(drop=True)

    # Split into train, val, test (equal sizes)
    a_counts = _largest_remainder(total_required_per_class, [1, 1, 1])
    r_counts = list(a_counts)  # symmetrical

    ptr_a, ptr_r = 0, 0
    splits = {}
    names = ["train", "val", "test"]

    for i, name in enumerate(names):
        a_part = accepted_df.iloc[ptr_a:ptr_a + a_counts[i]]
        r_part = rejected_df.iloc[ptr_r:ptr_r + r_counts[i]]
        split_df = pd.concat([a_part, r_part], ignore_index=True)
        split_df = split_df.sample(frac=1, random_state=shuffle_seed).reset_index(drop=True)
        splits[name] = split_df
        ptr_a += a_counts[i]
        ptr_r += r_counts[i]

    # Leak checks
    _leak_check(splits["train"], splits["val"], "train", "val")
    _leak_check(splits["train"], splits["test"], "train", "test")
    _leak_check(splits["val"], splits["test"], "val", "test")

    # Print stats
    for name in names:
        _print_split_stats(name, splits[name])

    # Save
    os.makedirs("./domains/paper_review", exist_ok=True)
    for name in names:
        splits[name].to_csv(f"./domains/paper_review/{prefix}dataset_filtered_{num_samples}_{name}.csv", index=False)

# ---------------- entrypoint ----------------

if __name__ == "__main__":
    df = get_dataset(domain="paper_review")
    df = df.sample(frac=1, random_state=42).reset_index(drop=True)

    make_balanced_splits(
        df,
        num_samples=100,   # each of train/val/test will have 100 rows (50 accept, 50 reject)
        shuffle_seed=42,
        prefix=""
    )
