import backoff
import json
import os
import time
import uuid
from contextvars import ContextVar
from typing import Tuple

import requests
import litellm
from dotenv import load_dotenv

if (os.environ.get("GAN_SECURE_MODE") != "1" and
        os.environ.get("PYTHON_DOTENV_DISABLED") != "1"):
    load_dotenv()

# Output budget for agent calls. Reasoning models (glm-5.3-flash observed:
# 16377 reasoning tokens on an imo_grading question, finish_reason=length,
# content="") spend most of it on internal thinking BEFORE any visible
# content -- 16k leaves nothing for the answer on hard domains.
MAX_TOKENS = 32768

# Per-call client budget, EXPLICIT (litellm's implicit default is silent and
# untuned). Ordering contract with the other layers: proxy upstream timeout
# (gan/framework/loop.yaml llm_proxy_timeout_s, parent-side) < client timeout <
# per-question wall budget. The proxy must finish first; otherwise a client
# retry overlaps the still-running upstream request and its eventual response
# hits a closed socket.
LLM_TIMEOUT_S = float(os.environ.get("GAN_LLM_TIMEOUT_S", "600"))

# Thinking-intensity knob for reasoning models, forwarded to the gateway as the
# OpenAI-compatible ``reasoning_effort`` body field through ``extra_body`` so
# LiteLLM does not drop it under drop_params=True. Values are the GATEWAY
# contract, not the OpenAI enum -- unknown values fail loudly here instead of
# mid-run.
# Transport is EXPLICIT ONLY (function argument threaded from
# gan/framework/models.yaml through the roles / the task harness payload);
# there is deliberately no environment fallback.
# NOTE: the constant itself lives in agent/llm_params.py (stdlib only) --
# importing THIS module pulls litellm, whose import-time load_dotenv crashes
# restricted-uid processes that can see but not read the root-owned .env
# (observed: the sandboxed task harness died on a validation-only import).
from agent.llm_params import REASONING_EFFORTS

# Usage/cost hooks: registered callbacks receive (model, usage_dict) after every
# successful LLM call. The GAN reward layer uses this to attach token cost.
USAGE_HOOKS: list = []


def register_usage_hook(fn):
    """Register fn(model: str, usage: dict | None)."""
    USAGE_HOOKS.append(fn)


# --- per-call LLM records (llm_call rows) ------------------------------------
# Every get_response_from_llm call — success AND failure — produces ONE
# content-free record (call_id / timing / attempts / retries / usage /
# finish_reason / gateway request id). Routing is a ContextVar SINK set by the
# session layer (agent/llm_withtools registers one pointing at the trajectory's
# .llm_calls.jsonl SIDECAR — never the main trajectory, so no agent read
# surface can resolve it). A ContextVar (not a global hook list) is required
# because parallel role sessions share the process: a broadcast list would
# misroute records into other sessions' sidecars. No sink (e.g. preflight)
# records nothing.
class _CallContext:
    """Mutable per-call recording state (thread/task local via ContextVar)."""

    __slots__ = ("call_id", "t_start", "t_start_mono", "model", "reasoning_effort",
                 "timeout_s", "retries", "pending_failure")

    def __init__(self, model, reasoning_effort, timeout_s):
        self.call_id = uuid.uuid4().hex[:12]
        self.t_start = time.time()
        self.t_start_mono = time.monotonic()
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_s = timeout_s
        self.retries = []            # one entry per failed attempt
        self.pending_failure = None  # {error_class, error} of the latest failure


_LLM_CALL_SINK: ContextVar = ContextVar("gan_llm_call_sink", default=None)
_LLM_CALL_CTX: ContextVar = ContextVar("gan_llm_call_ctx", default=None)

_SINK_WARNED: set = set()


def set_llm_call_sink(fn):
    """Register the session's llm_call sink; returns a reset token."""
    return _LLM_CALL_SINK.set(fn)


def reset_llm_call_sink(token):
    """Reset the sink binding (must accompany every set, see chat_with_agent)."""
    _LLM_CALL_SINK.reset(token)


def _emit_llm_call_record(record: dict) -> None:
    """Hand one record to the session sink. Fail-open, warn once per sink."""
    sink = _LLM_CALL_SINK.get()
    if sink is None:
        return
    try:
        sink(record)
    except Exception as e:  # noqa: BLE001 -- a broken sink must never fail the call
        if id(sink) not in _SINK_WARNED:
            _SINK_WARNED.add(id(sink))
            print(f"[WARN] llm_call sink failed ({type(e).__name__}: {str(e)[:120]}); "
                  f"this session's per-call LLM records will not be written")


def _run_usage_hooks(model, response):
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is not None and hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    for fn in USAGE_HOOKS:
        try:
            fn(model, usage)
        except Exception:
            pass


def _usage_dict(response) -> dict:
    """Whitelisted per-call usage counters for the session trajectory.

    Cost-attribution source (gan/framework/cost_ledger.py sums these over a
    session's ``output`` rows; the proxy audit is the independent
    reconciliation source). Unavailable counters stay ``None`` -- an
    unrecorded cost must never be recorded as 0.
    """
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        try:
            usage = usage.model_dump()
        except Exception:
            usage = None
    if not isinstance(usage, dict):
        return {}

    def _get(key):
        val = usage.get(key)
        return val if isinstance(val, (int, float)) else None

    return {"prompt_tokens": _get("prompt_tokens"),
            "completion_tokens": _get("completion_tokens"),
            "total_tokens": _get("total_tokens")}


def _detail_get(details, key):
    """Read one key from a usage-details object (dict or attribute style)."""
    if isinstance(details, dict):
        return details.get(key)
    return getattr(details, key, None)


def _usage_full(response) -> dict:
    """Whitelisted usage counters for the `llm_call` record.

    Superset of `_usage_dict`: adds the gateway-provided details counters
    (probe-verified: prompt_tokens_details.cached_tokens and
    completion_tokens_details.reasoning_tokens). Absent counters stay ABSENT,
    never 0.
    """
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        try:
            usage = usage.model_dump()
        except Exception:  # noqa: BLE001
            usage = None
    if not isinstance(usage, dict):
        return {}

    def _num(val):
        return val if isinstance(val, (int, float)) and val == val else None

    out = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = _num(usage.get(key))
        if val is not None:
            out[key] = val
    prompt_details = usage.get("prompt_tokens_details")
    cached = _num(_detail_get(prompt_details, "cached_tokens")) if prompt_details else None
    if cached is not None:
        out["cached_tokens"] = cached
    completion_details = usage.get("completion_tokens_details")
    reasoning = _num(_detail_get(completion_details, "reasoning_tokens")) if completion_details else None
    if reasoning is not None:
        out["reasoning_tokens"] = reasoning
    return out


def _finish_reason(response):
    """Top-choice finish_reason of a raw response (None when unavailable)."""
    try:
        choice = response["choices"][0]
    except (TypeError, KeyError, IndexError):
        return None
    if hasattr(choice, "get"):
        return choice.get("finish_reason")
    return getattr(choice, "finish_reason", None)


def _gateway_request_id(response):
    """Gateway correlation id (= X-Request-Id response header, probe-verified)."""
    rid = getattr(response, "id", None)
    if rid is None and isinstance(response, dict):
        rid = response.get("id")
    return str(rid)[:64] if rid else None


# Retry on TRANSIENT provider failures only (rate limit / timeout / conn drops).
# NOTE: there is NO fallback model — a model is chosen solely from
# gan/framework/models.yaml and passed in explicitly by the caller.
#
# The catch-all ``openai.APIError`` is deliberately NOT in this list: it is the
# parent of deterministic configuration errors (AuthenticationError, NotFoundError,
# ServiceUnavailableError "no available channel", BadRequestError), and retrying
# those burns ``max_time=600`` per call for a result that can never change
# (observed: the sandboxed missing-credential hang; the gpt-6-luna no-channel
# probe). Genuinely transient failures remain covered by the narrow subclasses.
_BACKOFF_EXCEPTIONS = [requests.exceptions.RequestException, json.JSONDecodeError, KeyError]
for _name in ("RateLimitError", "Timeout", "APIConnectionError"):
    _exc = getattr(getattr(litellm, "exceptions", None), _name, None)
    if isinstance(_exc, type):
        _BACKOFF_EXCEPTIONS.append(_exc)
try:  # also cover raw openai SDK errors
    import openai as _openai
    for _name in ("APIConnectionError", "APITimeoutError", "RateLimitError"):
        _exc = getattr(_openai, _name, None)
        if isinstance(_exc, type) and _exc not in _BACKOFF_EXCEPTIONS:
            _BACKOFF_EXCEPTIONS.append(_exc)
except Exception as _openai_err:
    # A-level sweep: the fallback silently shrinks the transient-retry class
    # set; say so once instead of zero signal.
    print(f"[WARN] openai SDK exception classes unavailable "
          f"({type(_openai_err).__name__}: {str(_openai_err)[:120]}); "
          f"transient-error retry coverage reduced")
    _BACKOFF_EXCEPTIONS = tuple(_BACKOFF_EXCEPTIONS)
else:
    _BACKOFF_EXCEPTIONS = tuple(_BACKOFF_EXCEPTIONS)


class TransientUpstreamError(RuntimeError):
    """A RETRYABLE 5xx from the gateway.

    litellm maps unknown upstream 5xx (e.g. "upstream proxy failure" from the
    local proxy when the TLS handshake to the provider times out) to the
    catch-all ``APIError``, which is deliberately NOT retried (it is the parent
    of deterministic config errors). This wrapper re-classifies ONLY the
    genuinely transient ones so a single blip does not kill a whole role
    session (observed: one 502 lost an entire self-improve session).
    """


def _is_transient_upstream(exc: BaseException) -> bool:
    text = str(exc).lower()
    # Deterministic gateway answers: retrying can never change the outcome.
    if ("no available channel" in text or "model_not_found" in text
            or "authentication" in text or "invalid api key" in text):
        return False
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and 500 <= code <= 599:
        return True
    return any(marker in text for marker in (
        "upstream proxy failure", "bad gateway", "gateway timeout",
        "internal server error", "connection reset", "handshake operation timed out",
    ))


_BACKOFF_EXCEPTIONS = tuple(_BACKOFF_EXCEPTIONS) + (TransientUpstreamError,)


def _backoff_record_failure(exc: BaseException) -> None:
    """Stash the latest attempt failure into the call context.

    backoff's ``on_backoff`` handler does NOT receive the exception object
    (only tries/wait), so the attempt function itself records the failure
    here; the handlers below attach the try number / wait and move it into
    ``ctx.retries``.
    """
    ctx = _LLM_CALL_CTX.get()
    if ctx is not None:
        ctx.pending_failure = {"error_class": type(exc).__name__,
                               "error": str(exc)[:300]}


def _on_backoff(details: dict) -> None:
    """One failed attempt that WILL be retried."""
    ctx = _LLM_CALL_CTX.get()
    if ctx is None:
        return
    entry = dict(ctx.pending_failure or {"error_class": "Unknown", "error": ""})
    entry["try"] = details.get("tries")
    entry["waited_s"] = details.get("wait")
    ctx.retries.append(entry)
    ctx.pending_failure = None


def _on_giveup(details: dict, exception=None) -> None:
    """The FINAL failed attempt (no further retry; raise_on_giveup follows)."""
    ctx = _LLM_CALL_CTX.get()
    if ctx is None:
        return
    if ctx.pending_failure is not None:
        entry = dict(ctx.pending_failure)
    elif exception is not None:
        entry = {"error_class": type(exception).__name__, "error": str(exception)[:300]}
    else:
        entry = {"error_class": "Unknown", "error": ""}
    entry["try"] = details.get("tries")
    entry["waited_s"] = None
    entry["gave_up"] = True
    ctx.retries.append(entry)
    ctx.pending_failure = None

litellm.drop_params = True


def _reasoning_effort(explicit):
    """Validate the explicit knob; None keeps the gateway default."""
    if explicit is None:
        return None
    effort = str(explicit).strip().lower()
    if effort not in REASONING_EFFORTS:
        raise ValueError(
            f"reasoning_effort {effort!r} not in {list(REASONING_EFFORTS)}")
    return effort


def _completion_kwargs(model, messages, temperature, max_tokens,
                       reasoning_effort=None, api_key=None, call_id=None):
    """Model-specific completion kwargs (GPT-5 / Claude-Haiku quirks)."""
    kw = {
        "model": model, 
        "messages": messages, 
        "timeout": LLM_TIMEOUT_S, 
        "num_retries": 0,}
    # Explicit per-caller credential (GAN roles under the shared proxy: each
    # role carries its own scoped token; threads must NEVER rely on the
    # process-global OPENAI_API_KEY, which a concurrent session could swap).
    if api_key:
        kw["api_key"] = api_key
    # Correlation id for the parent-side proxy audit (litellm forwards
    # extra_headers to the transport; both relay implementations only rewrite
    # Authorization, so the header reaches ParentLLMProxy untouched. A direct
    # gateway simply ignores the unknown header.)
    if call_id:
        kw["extra_headers"] = {"X-Gan-Call-Id": str(call_id)[:64]}
    # GPT-5 and GPT-5-mini only support default temperature (1); GPT-5.2 does.
    if model not in ["openai/gpt-5", "openai/gpt-5-mini"]:
        kw["temperature"] = temperature
    # GPT-5 models require max_completion_tokens instead of max_tokens.
    if "gpt-5" in model:
        kw["max_completion_tokens"] = max_tokens
    elif model in {"openai/gpt-4o", "openai/gpt-4o-mini"}:
        kw["max_tokens"] = min(max_tokens, 16384)
    elif "claude-3-haiku" in model:
        kw["max_tokens"] = min(max_tokens, 4096)
    else:
        kw["max_tokens"] = max_tokens
    effort = _reasoning_effort(reasoning_effort)
    if effort is not None:
        kw["extra_body"] = {"reasoning_effort": effort}
    return kw


def _attempt_completion(model, messages, temperature, max_tokens,
                        reasoning_effort=None, api_key=None, call_id=None):
    """One network attempt (transient 5xx re-classified, then backoff retries)."""
    try:
        return litellm.completion(
            **_completion_kwargs(model, messages, temperature, max_tokens,
                                 reasoning_effort=reasoning_effort, api_key=api_key,
                                 call_id=call_id)
        )
    except BaseException as _exc:  # noqa: BLE001 -- re-raise unchanged by default
        _backoff_record_failure(_exc)
        if _is_transient_upstream(_exc):
            raise TransientUpstreamError(str(_exc)[:300]) from _exc
        raise


_completion_with_backoff = backoff.on_exception(
    backoff.expo,
    _BACKOFF_EXCEPTIONS,
    max_time=120,
    max_value=60,
    on_backoff=_on_backoff,
    on_giveup=_on_giveup,
)(_attempt_completion)


def _llm_call_record(ctx: _CallContext, outcome: str, *,
                     response=None, exc: BaseException | None = None) -> dict:
    """Build one content-free `llm_call` record (sidecar row)."""
    rec = {"kind": "llm_call", "call_id": ctx.call_id, "model": ctx.model,
           "reasoning_effort": ctx.reasoning_effort, "timeout_s": ctx.timeout_s,
           "t_start": round(ctx.t_start, 3),
           "duration_s": round(time.monotonic() - ctx.t_start_mono, 3),
           "outcome": outcome}
    if outcome == "ok":
        rec["attempts"] = len(ctx.retries) + 1
        rec["finish_reason"] = _finish_reason(response)
        gw = _gateway_request_id(response)
        if gw:
            rec["gateway_request_id"] = gw
        usage = _usage_full(response)
        if usage:
            rec["usage"] = usage
    else:
        # attempts = the try number recorded by the giveup handler (falls back
        # to the retry count when no giveup entry exists, e.g. re-raise paths)
        last_try = ctx.retries[-1].get("try") if ctx.retries else None
        rec["attempts"] = last_try if isinstance(last_try, int) and last_try > 0 \
            else len(ctx.retries) + 1
        rec["error_class"] = type(exc).__name__ if exc is not None else "Unknown"
        cause = getattr(exc, "__cause__", None) if exc is not None else None
        if isinstance(exc, TransientUpstreamError) and cause is not None:
            # the backoff-visible error is the re-classified wrapper; keep the
            # ORIGINAL transport exception class for forensics
            rec["original_error_class"] = type(cause).__name__
        rec["error"] = str(exc)[:300] if exc is not None else ""
    if ctx.retries:
        rec["retries"] = [dict(r) for r in ctx.retries]
    return rec


def get_response_from_llm(
    msg: str,
    model: str,
    temperature: float = 0.0,
    max_tokens: int = MAX_TOKENS,
    msg_history=None,
    reasoning_effort: str | None = None,
    api_key: str | None = None,
) -> Tuple[str, list, dict]:
    if msg_history is None:
        msg_history = []

    # Convert text to content, compatible with LITELLM API
    msg_history = [
        {**msg, "content": msg.pop("text")} if "text" in msg else msg
        for msg in msg_history
    ]

    new_msg_history = msg_history + [{"role": "user", "content": msg}]

    # Recording lifecycle: one `llm_call` record per call, success AND failure
    # (finally semantics — a timeout/dead gateway still leaves its record).
    ctx = _CallContext(model, reasoning_effort, LLM_TIMEOUT_S)
    ctx_tok = _LLM_CALL_CTX.set(ctx)
    try:
        try:
            response = _completion_with_backoff(
                model, new_msg_history, temperature, max_tokens,
                reasoning_effort=reasoning_effort, api_key=api_key,
                call_id=ctx.call_id)
        except BaseException as _exc:
            _emit_llm_call_record(_llm_call_record(ctx, "error", exc=_exc))
            raise
        _emit_llm_call_record(_llm_call_record(ctx, "ok", response=response))
    finally:
        _LLM_CALL_CTX.reset(ctx_tok)

    _run_usage_hooks(model, response)
    _choice = response['choices'][0]
    _msg = _choice['message']
    response_text = _msg['content']  # pyright: ignore
    new_msg_history.append({"role": "assistant", "content": _msg['content']})

    # Reasoning capture (separate channel, NOT part of content/history): glm /
    # deepseek style ``reasoning_content``, o-series style ``reasoning``. The
    # text rides OUT in the info dict for trajectory recording; it never enters
    # msg_history, so the model does not see its own reasoning in later turns.
    def _msg_get(m, key):
        try:
            return m.get(key)
        except AttributeError:
            return getattr(m, key, None)
    reasoning = _msg_get(_msg, "reasoning_content") or _msg_get(_msg, "reasoning") or None
    finish_reason = _choice.get("finish_reason") if hasattr(_choice, "get") else getattr(_choice, "finish_reason", None)
    reasoning_tokens = None
    try:
        reasoning_tokens = response.usage.completion_tokens_details.reasoning_tokens  # pyright: ignore
    except AttributeError:
        pass

    # Convert content to text, compatible with MetaGen API
    new_msg_history = [
        {**msg, "text": msg.pop("content")} if "content" in msg else msg
        for msg in new_msg_history
    ]

    return response_text, new_msg_history, {"reasoning": reasoning, "finish_reason": finish_reason,
                                            "reasoning_tokens": reasoning_tokens,
                                            "usage": _usage_dict(response),
                                            # per-call record join keys (agent/llm.py
                                            # recording lifecycle; consumers optional)
                                            "call_id": ctx.call_id,
                                            "duration_s": round(time.monotonic() - ctx.t_start_mono, 3),
                                            "gateway_request_id": _gateway_request_id(response)}
