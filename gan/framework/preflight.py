"""Model availability preflight (FRAMEWORK, frozen).

Optional startup probe: for every configured role key, make ONE minimal call and
report availability. It NEVER substitutes a fallback model — a failure is
reported (and callers may choose to abort), consistent with the no-fallback
model policy.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from gan.framework import models as registry


def preflight(keys: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    """Probe each configured model key with a 1-token request.

    Returns ``{key: {"ok": bool, "model": str|None, "error": str|None}}``.
    """
    from agent.llm import get_response_from_llm

    chosen = list(keys) if keys is not None else list(registry.describe().keys())
    results: Dict[str, Dict[str, Any]] = {}
    for key in chosen:
        try:
            model = registry.resolve(key)
        except Exception as e:  # unresolved key
            results[key] = {"ok": False, "model": None, "error": f"unresolved: {e}"[:200]}
            continue
        try:
            get_response_from_llm("ping", model=model, temperature=0.0, max_tokens=1)
            results[key] = {"ok": True, "model": model, "error": None}
        except Exception as e:
            results[key] = {
                "ok": False, "model": model,
                "error": f"{type(e).__name__}: {e}"[:200],
            }
    return results


def all_ok(results: Dict[str, Dict[str, Any]]) -> bool:
    return all(bool(r.get("ok")) for r in results.values())
