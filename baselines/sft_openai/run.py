#!/usr/bin/env python3
"""
Fine-tune GPT-4o (2024-08-06) + evaluate on data_test.jsonl
All outputs are saved under ./baselines/sft_openai/outputs/{domain}_{RUN_ID}/...

New:
  - Resume a stopped run with:  --resume baselines/sft_openai/outputs/{domain}_{2025...}/
  - Continues polling job status and streaming events into the same folder
  - Runs evaluation if not already present (or with --force-eval)

Usage example:
  python ft.py --domain search_arena
  python ft.py --domain paper_review --poll-secs 5
  python ft.py --resume baselines/sft_openai/outputs/search_arena_20250914_120000 --domain search_arena

Prereqs:
  pip install --upgrade openai
  export OPENAI_API_KEY=sk-...

Files (resolved from --domain):
  - ./baselines/sft_openai/data/{domain}_filtered_100_train.jsonl  (training)
  - ./baselines/sft_openai/data/{domain}_filtered_100_val.jsonl    (validation)
  - ./baselines/sft_openai/data/{domain}_filtered_100_test.jsonl   (evaluation)
"""

import os
import re
import time
import json
import argparse
import logging
from dotenv import load_dotenv
from difflib import SequenceMatcher
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple, Optional, Set
from openai import OpenAI

BASE_MODEL = "gpt-4o-2024-08-06"
# BASE_MODEL = "gpt-4o-mini-2024-07-18"
# BASE_MODEL = "gpt-4.1-2025-04-14"

# These are assigned dynamically from --domain in main()
DATASET_PATH: Optional[str] = None
VALIDATION_PATH: Optional[str] = None
TEST_PATH: Optional[str] = None
DOMAIN: Optional[str] = None

# Globals assigned in main()
logger: Optional[logging.Logger] = None
LOG_FILE: Optional[str] = None

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
def setup_logger(run_dir: Path):
    log_file = run_dir / "run.log"
    lg = logging.getLogger("ft")
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s")

    # Avoid duplicate handlers if rerun in same process
    if not any(isinstance(h, logging.FileHandler) and getattr(h, 'baseFilename', None) == str(log_file) for h in lg.handlers):
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        lg.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    lg.addHandler(ch)

    return lg, str(log_file)

def log_header(run_dir: Path, base_model: str, domain: str):
    logger.info("======== Fine-tune run started ========")
    logger.info(f"Run directory: {run_dir}")
    logger.info(f"Base model: {base_model}")
    logger.info(f"Domain: {domain}")
    logger.info(f"Training file path: {DATASET_PATH}")
    logger.info(f"Validation file path: {VALIDATION_PATH}")
    logger.info(f"Test file path: {TEST_PATH}")
    logger.info(f"Logs: {LOG_FILE}")

# -----------------------------------------------------------------------------
# Utils
# -----------------------------------------------------------------------------
def ensure_file_exists(path: str, name: str):
    if not Path(path).is_file():
        logger.error(f"{name} not found at {path}")
        raise SystemExit(1)

def upload_file(client: OpenAI, path: str) -> str:
    logger.info(f"Uploading file: {path}")
    with open(path, "rb") as f:
        resp = client.files.create(file=f, purpose="fine-tune")
    logger.info(f"Uploaded {path} -> id={resp.id}, status={resp.status}")
    return resp.id

def dump_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)

def dump_jsonl(path: Path, rows: List[Dict[str, Any]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

def norm_text(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def str_similarity(a: str, b: str) -> float:
    return float(SequenceMatcher(None, a, b).ratio())

def parse_json_maybe(s: str):
    try:
        return json.loads(s)
    except Exception:
        return s

def tool_calls_equal(exp_calls: List[Dict[str, Any]], pred_calls: List[Any]) -> Tuple[bool, bool]:
    """
    Compare tool names and JSON arguments (order-insensitive).
    Returns (names_match, args_match).
    """
    norm_pred = []
    for c in (pred_calls or []):
        if hasattr(c, "function"):
            fn = c.function
            name = getattr(fn, "name", None)
            args = getattr(fn, "arguments", None)
        else:
            name = c.get("function", {}).get("name")
            args = c.get("function", {}).get("arguments")
        norm_pred.append({"function": {"name": name, "arguments": args}})

    if len(exp_calls) != len(norm_pred):
        return False, False

    names_ok = True
    args_ok = True
    for e, p in zip(exp_calls, norm_pred):
        ename = e.get("function", {}).get("name")
        pname = p.get("function", {}).get("name")
        if ename != pname:
            names_ok = False
        eargs = parse_json_maybe(e.get("function", {}).get("arguments"))
        pargs = parse_json_maybe(p.get("function", {}).get("arguments"))
        if isinstance(eargs, dict) and isinstance(pargs, dict):
            if eargs != pargs:
                args_ok = False
        else:
            if str(eargs) != str(pargs):
                args_ok = False
    return names_ok, args_ok

# -----------------------------------------------------------------------------
# Event resume helpers
# -----------------------------------------------------------------------------
def load_event_state(events_path: Path) -> Tuple[Set[str], int]:
    """
    Reads existing events.jsonl (if any) and returns:
      - seen_ids: set of event 'id' values already recorded (when present)
      - last_created_at: max created_at seen (fallback for old entries)
    """
    seen_ids: Set[str] = set()
    last_created_at = 0
    if not events_path.exists():
        return seen_ids, last_created_at

    with open(events_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if "id" in obj and isinstance(obj["id"], str):
                    seen_ids.add(obj["id"])
                if "created_at" in obj:
                    last_created_at = max(last_created_at, int(obj["created_at"] or 0))
            except Exception:
                # ignore bad rows
                pass
    return seen_ids, last_created_at

# -----------------------------------------------------------------------------
# Evaluation
# -----------------------------------------------------------------------------
def evaluate_on_test(
    client: OpenAI,
    model: str,
    test_path: str,
    run_dir: Path
):
    """
    Runs the fine-tuned model on data_test.jsonl and computes basic metrics.
    Saves into run_dir:
      - eval_predictions.jsonl (per-example)
      - eval_summary.json      (aggregate)
    """
    logger.info("======== Evaluation started ========")
    ensure_file_exists(test_path, "Test file")

    # Load test rows
    rows = []
    with open(test_path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except Exception as ex:
                    logger.warning(f"Skipping bad JSONL line {i}: {ex}")

    preds_out = []
    total = 0
    em_hits = 0
    sim_sum = 0.0
    tool_name_hits = 0
    tool_args_hits = 0
    tool_examples = 0
    total_latency = 0.0
    total_prompt_tokens = 0
    total_completion_tokens = 0

    for idx, item in enumerate(rows):
        total += 1
        messages = item.get("messages", [])
        tools = item.get("tools")
        tool_choice = "auto" if tools else None

        expected_assistant = None
        send_messages = messages

        if messages and messages[-1].get("role") == "assistant":
            expected_assistant = messages[-1]
            send_messages = messages[:-1]

        try:
            start = time.time()
            kwargs = dict(model=model, messages=send_messages, temperature=0)
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = tool_choice

            resp = client.chat.completions.create(**kwargs)
            latency = time.time() - start
            total_latency += latency

            choice = resp.choices[0].message
            pred_content = (choice.content or "").strip()
            pred_tool_calls = getattr(choice, "tool_calls", None)

            usage = getattr(resp, "usage", None)
            if usage:
                total_prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                total_completion_tokens += getattr(usage, "completion_tokens", 0) or 0

            em = None
            sim = None
            names_match = None
            args_match = None

            if expected_assistant:
                if expected_assistant.get("tool_calls"):
                    tool_examples += 1
                    names_match, args_match = tool_calls_equal(
                        expected_assistant["tool_calls"], pred_tool_calls or []
                    )
                    if names_match:
                        tool_name_hits += 1
                    if args_match:
                        tool_args_hits += 1
                else:
                    exp_text = (expected_assistant.get("content") or "").strip()
                    if exp_text:
                        em = int(norm_text(exp_text) == norm_text(pred_content))
                        sim = str_similarity(exp_text, pred_content)
                        em_hits += (em or 0)
                        sim_sum += (sim or 0.0)

            preds_out.append(
                {
                    "index": idx,
                    "latency_sec": round(latency, 3),
                    "expected": expected_assistant,
                    "prediction": {
                        "content": pred_content,
                        "tool_calls": [
                            {
                                "function": {
                                    "name": getattr(tc.function, "name", None)
                                    if hasattr(tc, "function") else tc.get("function", {}).get("name"),
                                    "arguments": getattr(tc.function, "arguments", None)
                                    if hasattr(tc, "function") else tc.get("function", {}).get("arguments"),
                                }
                            }
                            for tc in (pred_tool_calls or [])
                        ],
                    },
                    "metrics": {
                        "exact_match": em,
                        "similarity": None if sim is None else round(sim, 4),
                        "tool_name_match": names_match,
                        "tool_args_match": args_match,
                    },
                }
            )

        except Exception as ex:
            logger.warning(f"[eval] example {idx} failed: {ex}")
            preds_out.append({"index": idx, "error": str(ex)})

    avg_latency = (total_latency / total) if total else None
    em_rate = (em_hits / max(1, sum(1 for r in preds_out if r.get("metrics", {}).get("exact_match") is not None))) if total else None
    avg_sim = (sim_sum / max(1, sum(1 for r in preds_out if r.get("metrics", {}).get("similarity") is not None))) if total else None
    tool_name_rate = (tool_name_hits / max(1, tool_examples)) if tool_examples else None
    tool_args_rate = (tool_args_hits / max(1, tool_examples)) if tool_examples else None

    preds_path = run_dir / "eval_predictions.jsonl"
    summary_path = run_dir / "eval_summary.json"

    dump_jsonl(preds_path, preds_out)
    dump_json(
        summary_path,
        {
            "model": model,
            "total_examples": total,
            "exact_match_rate": None if em_rate is None else round(em_rate, 4),
            "avg_similarity": None if avg_sim is None else round(avg_sim, 4),
            "tool_name_match_rate": None if tool_name_rate is None else round(tool_name_rate, 4),
            "tool_args_match_rate": None if tool_args_rate is None else round(tool_args_rate, 4),
            "avg_latency_sec": None if avg_latency is None else round(avg_latency, 3),
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "predictions_file": str(preds_path),
        },
    )

    logger.info("======== Evaluation finished ========")
    logger.info(f"  Predictions: {preds_path}")
    logger.info(f"  Summary:     {summary_path}")

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    global logger, LOG_FILE, DATASET_PATH, VALIDATION_PATH, TEST_PATH, DOMAIN

    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", type=str, required=True, choices=["search_arena", "paper_review"], help="Domain to finetune on")
    ap.add_argument("--resume", type=str, default=None, help="Path to baselines/sft_openai/outputs/<domain>_<RUN_ID> to resume a run")
    ap.add_argument("--poll-secs", type=int, default=10, help="Polling interval in seconds")
    ap.add_argument("--no-eval", action="store_true", help="Skip evaluation phase")
    ap.add_argument("--force-eval", action="store_true", help="Re-run evaluation even if eval_summary.json exists")
    ap.add_argument("--no-val", action="store_true", help="Do not use a validation file")
    args = ap.parse_args()

    # Resolve dataset paths from domain
    DOMAIN = args.domain
    base = Path("baselines") / "sft_openai" / "data"
    DATASET_PATH = str(base / f"{DOMAIN}_filtered_100_train.jsonl")
    VALIDATION_PATH = None if args.no_val else str(base / f"{DOMAIN}_filtered_100_val.jsonl")
    TEST_PATH = str(base / f"{DOMAIN}_filtered_100_test.jsonl")

    load_dotenv()  # loads environment variables from .env
    client = OpenAI()  # reads OPENAI_API_KEY from .env

    # Determine run dir (new or resume)
    if args.resume:
        run_dir = Path(args.resume).resolve()
        if not run_dir.is_dir():
            raise SystemExit(f"--resume path not found: {run_dir}")
        run_id = run_dir.name
        is_resume = True
    else:
        # timestamp id, then prepend domain for folder name
        timestamp_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = f"{DOMAIN}_{timestamp_id}"
        base_outputs = Path("baselines") / "sft_openai" / "outputs"
        run_dir = base_outputs / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        is_resume = False

    logger, LOG_FILE = setup_logger(run_dir)
    log_header(run_dir, BASE_MODEL, DOMAIN)
    poll_secs = max(1, args.poll_secs)

    # Common paths
    job_id_path = run_dir / "job_id.txt"
    events_jsonl = run_dir / "events.jsonl"
    summary_path = run_dir / "summary.json"

    # Load/create job
    if is_resume:
        if not job_id_path.exists():
            raise SystemExit(f"job_id.txt not found in {run_dir}. Cannot resume.")
        job_id = job_id_path.read_text(encoding="utf-8").strip()
        if not job_id:
            raise SystemExit("job_id.txt is empty.")
        logger.info(f"Resuming run {run_id} with job_id={job_id}")
    else:
        # Starting a fresh run: validate files and upload
        ensure_file_exists(DATASET_PATH, "Training file")
        if not args.no_val:
            ensure_file_exists(VALIDATION_PATH, "Validation file")
        ensure_file_exists(TEST_PATH, "Test file")

        train_file_id = upload_file(client, DATASET_PATH)
        val_file_id = None
        if not args.no_val:
            val_file_id = upload_file(client, VALIDATION_PATH)

        logger.info(f"Creating fine-tune job on base model: {BASE_MODEL}")
        job = client.fine_tuning.jobs.create(
            model=BASE_MODEL,
            training_file=train_file_id,
            validation_file=val_file_id if val_file_id else None,
            # hyperparameters={"n_epochs": 3, "batch_size": 1, "learning_rate_multiplier": 1.0},
        )
        job_id = job.id
        job_id_path.write_text(job_id, encoding="utf-8")
        dump_json(run_dir / "job_created.json", {"job_id": job_id, "status": job.status, "domain": DOMAIN})
        logger.info(f"Job created: id={job_id}, status={job.status}")
        logger.info(f"Run directory (all outputs): {run_dir}")

    # Resume state for events (dedupe by event id; fallback to created_at)
    seen_ids, last_seen_created_at = load_event_state(events_jsonl)

    # Poll loop
    while True:
        time.sleep(poll_secs)
        job = client.fine_tuning.jobs.retrieve(job_id)
        logger.info(f"[status] {job.status} | trained_tokens={job.trained_tokens}")

        # Append new events
        try:
            ev_resp = client.fine_tuning.jobs.list_events(job_id)
            events = list(getattr(ev_resp, "data", []))
            events.reverse()  # oldest -> newest
            with open(events_jsonl, "a", encoding="utf-8") as f:
                for e in events:
                    eid = getattr(e, "id", None)
                    created_at = int(getattr(e, "created_at", 0) or 0)
                    if (eid and eid in seen_ids) or (not eid and created_at <= last_seen_created_at):
                        continue  # already recorded
                    seen_ids.add(eid) if eid else None
                    last_seen_created_at = max(last_seen_created_at, created_at)
                    message = getattr(e, "message", "")
                    logger.info(f"[event] {message}")
                    json.dump(
                        {
                            "id": eid,
                            "created_at": created_at,
                            "level": getattr(e, "level", None),
                            "type": getattr(e, "type", None),
                            "message": message,
                        },
                        f,
                    )
                    f.write("\n")
        except Exception as ex:
            logger.warning(f"Failed to fetch events: {ex}")

        if job.status in ("succeeded", "failed", "cancelled"):
            break

    # Wrap-up + evaluation (idempotent)
    if job.status == "succeeded":
        ft_model = job.fine_tuned_model
        logger.info("✅ Fine-tuning complete.")
        logger.info(f"Fine-tuned model id: {ft_model}")

        # Decide whether to run eval
        eval_summary = run_dir / "eval_summary.json"
        should_eval = not args.no_eval and (args.force_eval or not eval_summary.exists())

        if should_eval:
            evaluate_on_test(client, ft_model, TEST_PATH, run_dir)
        else:
            if args.no_eval:
                logger.info("Evaluation skipped (--no-eval).")
            else:
                logger.info("Evaluation outputs already exist; skipping. Use --force-eval to re-run.")

    else:
        logger.error(f"❌ Job ended with status: {job.status}")
        if job.error:
            logger.error(f"Error: {job.error}")

    # Final summary snapshot (overwrite ok)
    final_summary = {
        "run_id": run_dir.name,  # e.g., search_arena_20250914_120000
        "run_dir": str(run_dir),
        "job_id": job.id,
        "status": job.status,
        "fine_tuned_model": getattr(job, "fine_tuned_model", None),
        "result_files": getattr(job, "result_files", None),
        "trained_tokens": getattr(job, "trained_tokens", None),
        "events_file": str(events_jsonl),
        "log_file": LOG_FILE,
        "domain": DOMAIN,
        "dataset_paths": {
            "train": DATASET_PATH,
            "val": VALIDATION_PATH,
            "test": TEST_PATH,
        },
    }
    dump_json(summary_path, final_summary)

    logger.info("Artifacts written to:")
    logger.info(f"  {run_dir}")
    logger.info("======== Fine-tune run finished ========")

if __name__ == "__main__":
    main()
