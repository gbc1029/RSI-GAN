"""Frozen protocol 3: source references, resolved evidence, and derived credit.

Source provenance and consistency are mechanical; mathematical validity is not.
Private resolved quotations never enter public settlement or role feedback.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

PROTOCOL_VERSION = 3
ACTIONABILITY_BASES = {"transferable_strategy", "instance_specific_content", "uncertain"}
MODIFICATION_SCOPES = {
    "transferable_strategy", "instance_specific_memory", "answer_leakage", "uncertain",
}
_STATUSES = {"satisfied", "violated", "uncertain"}
_ASSESSMENT_KEYS = {
    "obligations", "parent_consistency", "child_consistency", "actionable",
    "actionability_basis", "modification_scope", "scope_refs", "scope_reason",
}
_SOURCES = {"R": "repair_criterion", "P": "parent_answer", "C": "child_answer", "T": "task_change"}
_REF = re.compile(r"([RPCT])([0-9]{4,})(?:-([RPCT])([0-9]{4,}))?")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def numbered_inputs(inputs):
    """Number original physical lines; preserve contents, blank lines and endings."""
    result = dict(inputs)
    for prefix, key in _SOURCES.items():
        result[key] = "".join(f"[{prefix}{i:04d}] {line}"
                              for i, line in enumerate(inputs[key].splitlines(keepends=True), 1))
    return result


class _ReferenceFailure(ValueError):
    def __init__(self, code, field, prefix, count):
        super().__init__(code + "@" + field)
        self.prefix, self.count = prefix, count


def resolve_refs(refs, source, prefix, field):
    lines = source.splitlines(keepends=True) if source is not None else None
    count = len(lines) if lines is not None else None
    def fail(code, path):
        raise _ReferenceFailure(code, path, prefix, count)
    if not isinstance(refs, list):
        fail("invalid_evidence_refs", field)
    quotes = []
    for i, ref in enumerate(refs):
        path = f"{field}[{i}]"
        match = _REF.fullmatch(ref) if isinstance(ref, str) else None
        if not match:
            fail("invalid_evidence_ref", path)
        start_prefix, start, end_prefix, end = match.groups()
        start, end = int(start), int(end or start)
        if start_prefix != prefix or (end_prefix is not None and end_prefix != prefix):
            fail("evidence_ref_wrong_source", path)
        if start < 1 or end < start or (count is not None and end > count):
            fail("evidence_ref_out_of_range", path)
        if lines is not None:
            quote = "".join(lines[start - 1:end])
            if not _text(quote):
                fail("evidence_ref_empty", path)
            quotes.append(quote)
    return quotes


def _evidence(refs, quotes, inputs, prefix, field):
    source = inputs[_SOURCES[prefix]] if inputs is not None else None
    resolved = resolve_refs(refs, source, prefix, field)
    if (not isinstance(quotes, list) or len(quotes) != len(refs)
            or not all(_text(q) for q in quotes)):
        raise ValueError("invalid_resolved_evidence@" + field)
    if inputs is not None and quotes != resolved:
        raise ValueError("resolved_evidence_mismatch@" + field)


def _check(value, inputs, prefix, field):
    if not isinstance(value, dict) or set(value) != {"status", "refs", "quotes", "verification"}:
        raise ValueError("invalid_obligation_check@" + field)
    if value["status"] not in _STATUSES or not _text(value["verification"]):
        raise ValueError("invalid_check_status_or_verification@" + field)
    _evidence(value["refs"], value["quotes"], inputs, prefix, field + ".refs")


def validate_assessment(value, inputs=None):
    """Validate canonical stored evidence, including framework-resolved quotes."""
    if not isinstance(value, dict) or set(value) != _ASSESSMENT_KEYS | {"scope_quotes"}:
        raise ValueError("invalid_assessment_fields")
    items = value["obligations"]
    if not isinstance(items, list) or not items:
        raise ValueError("missing_repair_obligations")
    for index, item in enumerate(items):
        field = f"obligations[{index}]"
        if not isinstance(item, dict) or set(item) != {
                "criterion_refs", "criterion_quotes", "obligation", "parent", "child"}:
            raise ValueError("invalid_repair_obligation")
        if not _text(item["obligation"]) or not item["criterion_refs"]:
            raise ValueError("empty_repair_obligation")
        _evidence(item["criterion_refs"], item["criterion_quotes"], inputs, "R", field + ".criterion_refs")
        for side, prefix in (("parent", "P"), ("child", "C")):
            check = item[side]
            _check(check, inputs, prefix, field + "." + side)
            if check["status"] == "satisfied" and not check["refs"]:
                raise ValueError("satisfied_obligation_without_evidence@" + field + "." + side + ".refs")
    for side, prefix in (("parent", "P"), ("child", "C")):
        _check(value[f"{side}_consistency"], inputs, prefix, side + "_consistency")
    basis = value["actionability_basis"]
    if (type(value["actionable"]) is not bool or basis not in ACTIONABILITY_BASES
            or value["actionable"] != (basis == "transferable_strategy")):
        raise ValueError("invalid_actionability")
    if value["modification_scope"] not in MODIFICATION_SCOPES:
        raise ValueError("invalid_modification_scope@modification_scope")
    if not _text(value["scope_reason"]):
        raise ValueError("empty_scope_reason@scope_reason")
    _evidence(value["scope_refs"], value["scope_quotes"], inputs, "T", "scope_refs")
    if (value["modification_scope"] in {"instance_specific_memory", "answer_leakage"}
            and not value["scope_refs"]):
        raise ValueError("non_transferable_scope_without_evidence")
    return value


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def assessment_response_format(mode="json_schema"):
    """One wire schema for production, replay and capability negotiation."""
    if mode == "json_object":
        return {"type": mode}
    if mode != "json_schema":
        raise ValueError("invalid_response_format")
    text = {"type": "string", "minLength": 1}
    refs = {"type": "array", "items": text}
    check = _object({"status": {"type": "string", "enum": sorted(_STATUSES)},
                     "refs": refs, "verification": text})
    obligation = _object({"criterion_refs": {**refs, "minItems": 1}, "obligation": text,
                          "parent": check, "child": check})
    schema = _object({"obligations": {"type": "array", "items": obligation, "minItems": 1},
                      "parent_consistency": check, "child_consistency": check,
                      "actionable": {"type": "boolean"},
                      "actionability_basis": {"type": "string", "enum": sorted(ACTIONABILITY_BASES)},
                      "modification_scope": {"type": "string", "enum": sorted(MODIFICATION_SCOPES)},
                      "scope_refs": refs, "scope_reason": text})
    return {"type": mode, "json_schema": {"name": "imo_challenge_assessment_v3",
                                           "strict": True, "schema": schema}}


def _resolve_wire(value, inputs):
    # Reject old protocol and model-supplied quote fields before adding evidence.
    if not isinstance(value, dict) or set(value) != _ASSESSMENT_KEYS:
        raise ValueError("invalid_assessment_fields")
    value = copy.deepcopy(value)
    def attach(obj, refs_key, quotes_key, prefix, field):
        if not isinstance(obj, dict) or quotes_key in obj:
            raise ValueError("invalid_assessment_fields")
        obj[quotes_key] = resolve_refs(obj.get(refs_key), inputs[_SOURCES[prefix]], prefix, field)
    items = value.get("obligations")
    if not isinstance(items, list) or not items:
        raise ValueError("missing_repair_obligations")
    for i, item in enumerate(items):
        field = f"obligations[{i}]"
        attach(item, "criterion_refs", "criterion_quotes", "R", field + ".criterion_refs")
        for side, prefix in (("parent", "P"), ("child", "C")):
            attach(item.get(side), "refs", "quotes", prefix, field + "." + side + ".refs")
    for side, prefix in (("parent", "P"), ("child", "C")):
        attach(value.get(side + "_consistency"), "refs", "quotes", prefix, side + "_consistency.refs")
    attach(value, "scope_refs", "scope_quotes", "T", "scope_refs")
    return validate_assessment(value, inputs)

def presence(assessment, side):
    checks = [item[side] for item in assessment["obligations"]]
    checks.append(assessment[f"{side}_consistency"])
    if any(check["status"] == "violated" for check in checks):
        return True
    if any(check["status"] == "uncertain" for check in checks):
        return None
    return False


def proposes_repair(assessment):
    return (presence(assessment, "parent") is True
            and presence(assessment, "child") is False
            and assessment["actionable"] is True
            and assessment["modification_scope"] == "transferable_strategy")


def derived_verdict(assessment, confirmation):
    parent, child = presence(assessment, "parent"), presence(assessment, "child")
    status = "not_required"
    if proposes_repair(assessment):
        if confirmation is None:
            status, parent, child = "unavailable", None, None
        elif proposes_repair(confirmation):
            status = "confirmed"
        else:
            status, parent, child = "disagreed", None, None
    elif confirmation is not None:
        raise ValueError("unexpected_confirmation")
    return {"parent_present": parent, "child_present": child,
            "actionable": assessment["actionable"],
            "actionability_basis": assessment["actionability_basis"],
            "modification_scope": assessment["modification_scope"],
            "review_status": status,
            # Public settlement uses its own generic state reason. Never copy
            # proof quotations or the private mathematical analysis into it.
            "reason": f"structured_obligation_checks:{status}"}


def prompt_digest(prompt, review_instruction, format_instruction=""):
    return hashlib.sha256((prompt + "\n" + review_instruction + "\n"
                           + format_instruction).encode("utf-8")).hexdigest()


def make_verdict(assessment, confirmation, *, model, prompt_sha256, response_format="json_schema"):
    validate_assessment(assessment)
    if confirmation is not None:
        validate_assessment(confirmation)
    return {"protocol_version": PROTOCOL_VERSION, "assessment": assessment,
            "confirmation": confirmation, "model": model,
            "prompt_sha256": prompt_sha256, "response_format": response_format,
            **derived_verdict(assessment, confirmation)}


def validate_verdict(value, inputs=None):
    keys = {"protocol_version", "assessment", "confirmation", "model", "prompt_sha256",
            "parent_present", "child_present", "actionable", "actionability_basis",
            "modification_scope", "review_status", "reason", "response_format"}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value["protocol_version"]) is not int
            or value["protocol_version"] != PROTOCOL_VERSION):
        raise ValueError("invalid_verdict_protocol")
    if value["response_format"] not in {"json_schema", "json_object"}:
        raise ValueError("invalid_response_format")
    validate_assessment(value["assessment"], inputs)
    if value["confirmation"] is not None:
        validate_assessment(value["confirmation"], inputs)
    expected = derived_verdict(value["assessment"], value["confirmation"])
    if any(type(value[k]) is not type(v) or value[k] != v for k, v in expected.items()):
        raise ValueError("verdict_disagrees_with_evidence")
    if (not _text(value["model"]) or not isinstance(value["prompt_sha256"], str)
            or len(value["prompt_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in value["prompt_sha256"])):
        raise ValueError("missing_judge_provenance")
    return value



def parse_assessment(raw, inputs):
    return _resolve_wire(json.loads(str(raw or "").strip()), inputs)


def assessment_failure(exc):
    """Bounded, content-free diagnostics; never expose JSON/provider bodies."""
    if isinstance(exc, json.JSONDecodeError):
        categories = {"Expecting value": "expecting_value",
                      "Expecting property name enclosed in double quotes": "expecting_property",
                      "Expecting ',' delimiter": "expecting_comma",
                      "Expecting ':' delimiter": "expecting_colon",
                      "Extra data": "extra_data", "Invalid \\escape": "invalid_escape"}
        return {"code": "invalid_json", "field": "", "line": exc.lineno,
                "column": exc.colno, "position": exc.pos,
                "json_error": categories.get(exc.msg, "invalid_syntax")}
    message = str(exc)
    code, _, field = message.partition("@")
    allowed = {
        "invalid_assessment_fields", "missing_repair_obligations", "invalid_repair_obligation",
        "empty_repair_obligation", "invalid_obligation_check", "invalid_check_status_or_verification",
        "invalid_evidence_refs", "invalid_evidence_ref", "evidence_ref_wrong_source",
        "evidence_ref_out_of_range", "evidence_ref_empty", "invalid_resolved_evidence",
        "resolved_evidence_mismatch", "satisfied_obligation_without_evidence", "invalid_actionability",
        "invalid_modification_scope", "empty_scope_reason", "non_transferable_scope_without_evidence",
    }
    if code not in allowed or any(not (c.isascii() and (c.isalnum() or c in "_.[]")) for c in field):
        return {"code": "invalid_assessment", "field": ""}
    result = {"code": code, "field": field}
    if isinstance(exc, _ReferenceFailure):
        result["source_prefix"] = exc.prefix
        result["line_count"] = exc.count
    return result
