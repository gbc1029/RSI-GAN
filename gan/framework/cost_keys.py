"""Reserved cost-key family (FRAMEWORK, frozen) — docs/10 §10, phase C.

The per-role ``cost`` design slot is a RESERVED key family: framework-declared,
framework-consumed, clamped at the consumption point. It is deliberately NOT a
dynamic key (it lives in ``ROLE_SCHEMAS``, hence in schema ``_RESERVED`` —
``add_config_key`` can neither shadow nor re-declare it) and its authority is
THIS module, in the frozen framework layer — never the agent-editable
``gan/design/schema_ext/<role>.json`` and never the free-form ``params`` slot.

Single source (L1 口径归一, docs/10 §2.2): EVERY consumer derives effective
values through this module — the loop (plan/evaluate seats), the task runner
(task design snapshot), schema validation (``gan.design.schema.validate_config``)
and the ``set_config`` operator. One range table, one clamp implementation.

Semantics (docs/10 §10.1):
- effective = clamp(requested design value) when one is present; the static
  range is framework property, not evolution surface;
- absent key → the caller's fallback (e.g. the ``loop.yaml`` value for the
  plan/evaluate seats) or the spec default — token budgets default to ``None``
  (= the clamp is NOT active; today's behaviour);
- every deviation of effective from requested produces a
  ``{key, requested, effective}`` record the callers emit as
  ``cost_keys_applied`` (audit) and, for the task side, ride the receipt's cost
  node so the planner sees its own clamped decision (责任内化, docs/10 §6);
- ``cost.token_budget_per_gen`` (loop.yaml) is the CEILING for the task
  ``per_question_token_budget``: one question exceeding the whole generation's
  budget is meaningless (docs/10 §10.0.3).

``None ≠ 0`` holds here too: an absent key is absence, never a fabricated 0.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from agent.llm_params import REASONING_EFFORTS

COST_SLOT = "cost"

# role -> key -> spec. Keys are the RESERVED subkeys of the design ``cost``
# dict; "default" is the no-design-value behaviour (must equal the pre-C
# behaviour so a default design is byte-for-byte unchanged); "consumer" names
# the enforcement point (documentation + audit vocabulary).
_SPECS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "task": {
        # Soft per-question token budget (chat-level stop; docs/10 §10.2 C8).
        "per_question_token_budget": {
            "type": "int", "min": 2_000, "max": 5_000_000, "default": None,
            "consumer": "harness payload -> agent.llm_withtools max_total_tokens",
        },
        # B21: the task child's tool-call cap finally rides the explicit chain
        # (40 was the silent chat default before C).
        "per_question_max_tool_calls": {
            "type": "int", "min": 1, "max": 200, "default": 40,
            "consumer": "harness payload -> chat_with_agent max_tool_calls",
        },
        "knowledge_max_file_chars": {
            "type": "int", "min": 1_000, "max": 100_000, "default": 20_000,
            "consumer": "task_execution.assemble_task_env (per-note cap)",
        },
        "knowledge_max_total_chars": {
            "type": "int", "min": 5_000, "max": 1_000_000, "default": 200_000,
            "consumer": "task_execution.assemble_task_env (per-generation cap)",
        },
        # Overrides the models.yaml-derived effort for the task child ONLY
        # (docs/10 §10.0.2); the model itself stays models.yaml-owned.
        "reasoning_effort": {
            "type": "str", "enum": tuple(REASONING_EFFORTS), "default": None,
            "consumer": "harness payload reasoning_effort (falls back to CLI/models)",
        },
    },
    "planner": {
        "plan_token_budget": {
            "type": "int", "min": 2_000, "max": 5_000_000, "default": None,
            "consumer": "loop stamp Role.session_token_budget (plan seat)",
        },
        "plan_max_tool_calls": {
            "type": "int", "min": 1, "max": 200, "default": None,
            "consumer": "loop -> planner.plan(max_tool_calls) (fallback: loop.plan_max_tool_calls)",
        },
        "patch_retry_k": {
            "type": "int", "min": 0, "max": 5, "default": None,
            "consumer": "loop -> planner.plan(patch_retry_k) (fallback: loop.patch_retry_k)",
        },
    },
    "evaluator": {
        "evaluate_token_budget": {
            "type": "int", "min": 2_000, "max": 5_000_000, "default": None,
            "consumer": "loop stamp Role.session_token_budget (evaluate seat)",
        },
        # Blind/reveal phases stay SEPARATE (docs/10 §6: never starve the
        # mandatory eval-points; the reveal phase is a finalization pass).
        "evaluate_blind_max_tool_calls": {
            "type": "int", "min": 4, "max": 200, "default": None,
            "consumer": "loop -> evaluator.evaluate(max_tool_calls) (fallback: loop.evaluate_max_tool_calls)",
        },
        "evaluate_reveal_max_tool_calls": {
            "type": "int", "min": 2, "max": 100, "default": None,
            "consumer": "loop -> evaluator.evaluate(reveal_max_tool_calls) (fallback: max(6, blind//3))",
        },
    },
}

# Context-management keys (docs/10 §5.2.3, §11): SAME reserved family and
# authority as above — declared here, clamped at the consumption point (the
# seam / the slice front-end). The mode enum is the CM activation state:
# ``passthrough`` is the safe default (identity view = pre-D behaviour), the
# other modes switch on the slice/compact behaviours. The per-role key names
# are identical (whose keys live in whose design, docs/10 §4); p/e consumption
# is front-end + fixed pre-segment only in the first increment (§11.0.4).
_CM_KEYS: Dict[str, Dict[str, Any]] = {
    "cm_mode": {
        "type": "str", "enum": ("passthrough", "slice", "compact_conservative", "custom"),
        "default": "passthrough",
        "consumer": "cm seam (view assembly) + read_trajectory slice front-end",
    },
    "cm_window_k": {
        "type": "int", "min": 1, "max": 50, "default": 4,
        "consumer": "cm seam (default pin window; framework floor is the min)",
    },
    "cm_drop_floor_chars": {
        "type": "int", "min": 0, "max": 100_000, "default": 400,
        "consumer": "cm seam invariant (blocks smaller than this are never dropped/compacted)",
    },
    "cm_max_ops_per_turn": {
        "type": "int", "min": 1, "max": 200, "default": 32,
        "consumer": "cm seam (policy op budget per turn; excess ops are refused)",
    },
    "cm_policy_exec_ms": {
        "type": "int", "min": 10, "max": 10_000, "default": 200,
        "consumer": "cm seam (policy execution budget; overruns count toward the wedge breaker)",
    },
    "cm_note_cap_chars": {
        "type": "int", "min": 200, "max": 100_000, "default": 8_000,
        "consumer": "cm note primitive (scratchpad per-note cap)",
    },
}
for _role in ("task", "planner", "evaluator"):
    _SPECS[_role].update(_CM_KEYS)

_CEILING_KEY = "per_question_token_budget"


def specs(role: str) -> Dict[str, Dict[str, Any]]:
    """The role's reserved-key spec table (a copy; callers must not mutate)."""
    return dict(_SPECS.get(role, {}))


def spec_keys(role: str) -> List[str]:
    return list(_SPECS.get(role, {}))


def is_reserved_slot(key: str) -> bool:
    return key == COST_SLOT


def cost_config(config: Any) -> Dict[str, Any]:
    """The normalized ``cost`` dict of a design config ({} when absent/invalid)."""
    if not isinstance(config, dict):
        return {}
    value = config.get(COST_SLOT)
    return dict(value) if isinstance(value, dict) else {}


def _fallback(spec: Dict[str, Any], fallbacks: Optional[Dict[str, Any]],
              key: str) -> Any:
    if fallbacks and key in fallbacks:
        return fallbacks[key]
    return spec.get("default")


def _clamp_int(spec: Dict[str, Any], requested: Any) -> Tuple[Any, Optional[str]]:
    """Clamp an int request into [min, max]; (value, reason-or-None)."""
    if type(requested) is not int:  # bool is not an int here (type check, not isinstance)
        return None, f"type {type(requested).__name__}, expected int"
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and requested < lo:
        return int(lo), f"below minimum {lo}"
    if hi is not None and requested > hi:
        return int(hi), f"above maximum {hi}"
    return requested, None


def _validate_value(spec: Dict[str, Any], value: Any) -> Optional[str]:
    if spec.get("type") == "int":
        if type(value) is not int:
            return f"must be an integer (got {type(value).__name__})"
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and value < lo:
            return f"must be >= {lo}"
        if hi is not None and value > hi:
            return f"must be <= {hi}"
        return None
    if spec.get("type") == "str":
        if not isinstance(value, str):
            return f"must be a string (got {type(value).__name__})"
        enum = spec.get("enum")
        if enum is not None and value not in enum:
            return f"must be one of {list(enum)}"
        return None
    return "unsupported spec type"


def validate_cost_config(role: str, value: Any) -> List[str]:
    """Validate a whole ``cost`` slot value (set_config / schema.validate_config).

    The write path REFUSES invalid slots outright (design untouched); this is
    the same authority the consumption-side clamp falls back on.
    """
    specs_ = _SPECS.get(role)
    if specs_ is None:
        return [f"unknown role '{role}'"]
    if not isinstance(value, dict):
        return ["cost slot must be an object (dict of reserved keys)"]
    problems: List[str] = []
    for key, sub in value.items():
        spec = specs_.get(str(key))
        if spec is None:
            problems.append(f"unknown cost key '{key}' for role '{role}'")
            continue
        if sub is None:
            # None = explicitly unset (the key falls back to default/fallback
            # semantics; the clamp-in-place pass removes it from the snapshot).
            continue
        err = _validate_value(spec, sub)
        if err:
            problems.append(f"cost key '{key}' {err}")
    return problems


def effective_all(role: str, config: Any,
                  fallbacks: Optional[Dict[str, Any]] = None,
                  ceiling_token_budget: Any = None,
                  ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Effective values for EVERY reserved key of the role + clamp records.

    Returns ``(values, clamps)``: ``values`` maps every spec key to its
    effective value (never raises on a bad design — a bad value degrades to the
    fallback/default WITH a record); ``clamps`` lists only the keys whose
    effective value deviates from what the design requested.
    """
    specs_ = _SPECS.get(role, {})
    requested_map = cost_config(config)
    values: Dict[str, Any] = {}
    clamps: List[Dict[str, Any]] = []
    for key, spec in specs_.items():
        requested = requested_map.get(key)
        if requested is None:
            # Absent (None ≠ 0): the fallback (loop.yaml value / models value)
            # or the spec default governs; nothing was requested, no record.
            values[key] = _fallback(spec, fallbacks, key)
            continue
        if spec.get("type") == "int":
            eff, reason = _clamp_int(spec, requested)
        else:
            reason = _validate_value(spec, requested)
            eff = None if reason else requested
        if eff is None:
            eff = _fallback(spec, fallbacks, key)
        else:
            # Ceiling (per-question token budget <= per-generation budget).
            if key == _CEILING_KEY:
                try:
                    ceiling = int(ceiling_token_budget) if ceiling_token_budget else None
                except (TypeError, ValueError):
                    ceiling = None
                if ceiling and ceiling > 0 and eff > ceiling:
                    eff = ceiling
                    reason = f"above token_budget_per_gen ceiling {ceiling}"
        values[key] = eff
        if eff != requested or reason:
            rec = {"key": key, "requested": requested, "effective": eff}
            if reason:
                rec["reason"] = reason
            clamps.append(rec)
    return values, clamps


def clamp_cost_config(role: str, config: Any,
                      ceiling_token_budget: Any = None) -> List[Dict[str, Any]]:
    """Rewrite the design's ``cost`` slot IN PLACE to its effective values.

    Used by the task runner BEFORE the design snapshot is persisted: the
    persisted ``design.json`` is then canonical (clamped), so the child-side
    extraction (harness, no ``gan/`` in the run dir) is a type-guarded
    pass-through and can never drift from the parent-side authority. Unknown
    subkeys are removed (inert, but confusing); the slot itself is kept when
    the result is non-empty. Returns the deviation records for the caller's
    event/meta. Mutates ``config`` in place like the key heal does.
    """
    specs_ = _SPECS.get(role, {})
    if not isinstance(config, dict) or not specs_:
        return []
    slot = config.get(COST_SLOT)
    if not isinstance(slot, dict):
        if slot is not None:
            # A non-dict slot cannot be clamped; leave it for the key heal
            # (which strips it) and record nothing here.
            return [{"key": COST_SLOT, "requested": slot, "effective": None,
                     "reason": "cost slot must be an object"}]
        return []
    values, clamps = effective_all(role, config,
                                   ceiling_token_budget=ceiling_token_budget)
    records: List[Dict[str, Any]] = list(clamps)
    new_slot: Dict[str, Any] = {}
    for key in list(slot):
        if key in specs_:
            new_slot[key] = values.get(key)
        else:
            records.append({"key": str(key), "requested": slot.get(key),
                            "effective": None, "reason": "unknown cost key removed"})
    # Drop keys whose effective value is None-and-absent semantics: keep only
    # keys the design actually requested (a clamp record preserves the fact).
    for key in list(new_slot):
        if new_slot[key] is None and slot.get(key) is None:
            del new_slot[key]
    if new_slot:
        config[COST_SLOT] = new_slot
    else:
        config.pop(COST_SLOT, None)
    return records
