import argparse
import importlib
import json
import os
from pathlib import Path
from typing import Optional
import pandas as pd
import tiktoken


def get_dataset(domain, subset=""):
    df = pd.read_csv(f'./domains/{domain}/dataset{subset}.csv')
    return df

def process_to_sft_jsonl(
    domain: str,
    subset: str = "",
) -> Path:
    """
    Load dataset using get_dataset(domain, subset), then write SFT JSONL.

    Each line:
      {"messages":[
         {"role":"user","content":"<stringified format_input_dict(row)>"},
         {"role":"assistant","content":"<row[GROUND_TRUTH_KEY]>"}
      ]}
    """
    # Dynamically import utils for the chosen domain
    utils_module = importlib.import_module(f"domains.{domain}.utils")
    GROUND_TRUTH_KEY = getattr(utils_module, "GROUND_TRUTH_KEY")
    format_input_dict = getattr(utils_module, "format_input_dict")

    _ENC = tiktoken.get_encoding("cl100k_base")
    def _count_tokens(s: str) -> int:
        return len(_ENC.encode(s))
    def _truncate_to_tokens(s: str, max_tokens: int) -> str:
        if max_tokens <= 0:
            return ""
        toks = _ENC.encode(s)
        if len(toks) <= max_tokens:
            return s
        return _ENC.decode(toks[:max_tokens])

    MAX_CONTEXT_TOKENS = 65000   # stay under 65536 context
    SAFETY_TOKENS = 1024         # role/JSON overhead

    # Load dataset following harness pattern
    df = get_dataset(domain=domain, subset=subset)

    # Derive default output path if not provided
    out_dir = Path("./baselines/sft_openai/data")
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{domain}{subset}.jsonl"

    # Write JSONL
    written = 0
    with output_path.open("w", encoding="utf-8") as out_f:
        # Iterate as dicts for compatibility with format_input_dict(row['col'])
        for idx, row in enumerate(df.to_dict(orient="records"), start=1):
            # Build assistant content from ground truth
            assistant_content = str(row.get(GROUND_TRUTH_KEY, "") or "")

            # Build user content using domain-specific formatter, then stringify
            user_payload = format_input_dict(row)
            user_content = json.dumps(user_payload, ensure_ascii=False)

            # truncate user content to fit within context budget
            assistant_tokens = _count_tokens(assistant_content)
            user_budget = MAX_CONTEXT_TOKENS - SAFETY_TOKENS - assistant_tokens
            if user_budget < 0:
                # Assistant alone exceeds budget; warn and empty user content
                print(f"[warn] Example {idx}: assistant tokens ({assistant_tokens}) exceed budget; user content set to empty.")
                user_budget = 0

            user_tokens = _count_tokens(user_content)
            if user_tokens > user_budget:
                truncated = _truncate_to_tokens(user_content, user_budget)
                new_user_tokens = _count_tokens(truncated)
                print(f"[truncate] Example {idx}: user {user_tokens} -> {new_user_tokens} tokens (budget={user_budget})")
                user_content = truncated

            rec = {
                "messages": [
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": assistant_content},
                ]
            }
            out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1

    print(f"Wrote {written} JSONL lines to {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Format domain dataset into SFT JSONL.")
    parser.add_argument("--domain", type=str, required=True, choices=["search_arena", "paper_review"], help="Domain whose dataset to convert",)
    args = parser.parse_args()

    for subset in ["_filtered_100_train", "_filtered_100_val", "_filtered_100_test"]:
        output_path = process_to_sft_jsonl(domain=args.domain, subset=subset)

if __name__ == "__main__":
    main()
