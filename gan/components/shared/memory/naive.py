"""Seed memory strategy: naive (keep the last N messages)."""
from __future__ import annotations


def info():
    return {"name": "naive", "description": "Keep the last N messages."}


def build_context(msg_history, max_items=10):
    try:
        max_items = int(max_items)
    except Exception:
        max_items = 10
    return list(msg_history or [])[-max_items:]
