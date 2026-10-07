import re
import json
import os


def read_file(file_path):
    with open(file_path, "r") as file:
        content = file.read()
    return content

def _repair_json_escapes(s):
    """Escape lone backslashes that do not start a valid JSON escape sequence.

    Models routinely embed LaTeX in JSON strings (``\\( p_h \\)``, ``\\alpha``);
    ``\\(`` etc. are invalid JSON escapes and strict parsing rejects the whole
    object. Doubling the backslash preserves the author-intended literal text.
    """
    return re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)

def _parse_json_candidate(s):
    """Parse one candidate string through the repair ladder.

    Returns ``(ok, obj, tier)``; ``ok`` disambiguates a parsed JSON ``null``
    from failure. Tiers: strict -> control-char tolerant -> escape-repaired.
    """
    repaired = _repair_json_escapes(s)
    attempts = (
        ("strict", lambda t: json.loads(t)),
        ("strict=False", lambda t: json.loads(t, strict=False)),
        ("escape-repair", lambda t: json.loads(repaired)),
        ("escape-repair+strict=False", lambda t: json.loads(repaired, strict=False)),
    )
    failures = []
    for tier, fn in attempts:
        try:
            obj = fn(s)
            return True, obj, tier
        except (json.JSONDecodeError, ValueError) as e:
            failures.append(f"{tier}: {e}")
    return False, None, "; ".join(failures)

def _scan_top_level_objects(s):
    """Yield top-level balanced ``{...}`` substrings (string/escape aware).

    Recovers bare JSON objects that carry no ``<json>`` tags and no code
    fences, including objects embedded in surrounding prose.
    """
    objs, depth, start, in_str, esc = [], 0, -1, False, False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    objs.append(s[start:i + 1])
                    start = -1
    return objs

def extract_jsons(response, logging=None):
    """
    Extracts all JSON objects from the given response string.

    Candidate sources, strict-first (legacy order preserved so callers that
    take ``[-1]`` see no drift for previously-parseable outputs):
      1. ``<json>...</json>`` tagged blocks
      2. ```json fenced blocks
    Fallback-only groups (used ONLY when 1+2 yield nothing parsed):
      3. generic fenced blocks whose content starts with ``{``/``[``
      4. bare top-level ``{...}`` objects recovered from prose

    Every candidate runs through a repair ladder (see _parse_json_candidate),
    so LaTeX-style invalid escapes no longer silently kill the whole answer.

    Returns the parsed objects, or None when nothing parsed. Never raises;
    per-candidate failures go to ``logging`` when provided.
    """
    log = logging if logging is not None else (lambda *a, **k: None)

    def _parse_group(name, candidates):
        parsed, failures = [], []
        for c in candidates:
            ok, obj, tier = _parse_json_candidate(c.strip())
            if ok:
                parsed.append(obj)
            else:
                failures.append(tier if len(tier) < 200 else tier[:200] + "...")
        if failures:
            log(f"json_extract[{name}]: {len(failures)}/{len(candidates)} candidate(s) "
                f"failed to parse: {failures[0]}")
        return parsed

    extracted = []
    extracted += _parse_group("tagged", re.findall(r'<json>(.*?)</json>', response, re.DOTALL))
    extracted += _parse_group("fenced-json", re.findall(r'```json(.*?)```', response, re.DOTALL))

    if not extracted:
        # Fallback-only: keep legacy ordering semantics for tagged/fenced hits.
        generic = [c for c in re.findall(r'```(.*?)```', response, re.DOTALL)
                   if c.strip()[:1] in ("{", "[")]
        extracted += _parse_group("fenced-generic", generic)
        extracted += _parse_group("bare-object", _scan_top_level_objects(response))

    if not extracted:
        log(f"json_extract: no parseable JSON in {len(response)} chars "
            f"(output tail: ...{response[-300:]!r})")
    return extracted if extracted else None

def summarize_error(e, limit=800):
    """One-record error summary that keeps the CAUSE, not the noise.

    ``subprocess.TimeoutExpired``/``CalledProcessError`` render the FULL argv
    in ``str(e)``, which alone can consume any truncation budget (the recorded
    tail then holds the command head while the reason is cut off). Here:
    argv is folded to head+tail, and when the exception carries captured
    stderr (as the subprocess exceptions do) the stderr TAIL wins.
    """
    etype = type(e).__name__[:60]
    cmd = getattr(e, "cmd", None)
    stderr = getattr(e, "stderr", None)
    if isinstance(stderr, (bytes, bytearray)):
        stderr = bytes(stderr).decode("utf-8", "replace")
    tail_budget = max(120, limit - 260)
    if isinstance(stderr, str) and stderr.strip():
        reason = "stderr tail: " + stderr.strip()[-tail_budget:]
    else:
        reason = "detail tail: " + str(e)[-tail_budget:]
    parts = [etype]
    if cmd is not None:
        if isinstance(cmd, (list, tuple)):
            argv = [str(x) for x in cmd]
            if len(argv) > 8:
                fold = (" ".join(argv[:3]) + f" ... <{len(argv) - 6} args omitted> ... "
                        + " ".join(argv[-3:]))
                parts.append("cmd: " + fold[:220])
            else:
                parts.append("cmd: " + " ".join(argv)[:220])
        else:
            parts.append("cmd: " + str(cmd)[:220])
    parts.append(reason)
    out = " | ".join(parts)
    return out if len(out) <= limit else out[-limit:]

def file_exist_and_not_empty(file_path):
    return os.path.exists(file_path) and os.path.getsize(file_path) > 0

def load_json_file(file_path):
    """
    Load a JSON file and return its contents as a dictionary.
    """
    with open(file_path, 'r') as file:
        return json.load(file)
