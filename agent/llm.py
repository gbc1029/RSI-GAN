import backoff
import json
import os
from typing import Tuple

import requests
import litellm
from dotenv import load_dotenv

load_dotenv()

# Output budget for agent calls. Reasoning models (glm-5.3-flash observed:
# 16377 reasoning tokens on an imo_grading question, finish_reason=length,
# content="") spend most of it on internal thinking BEFORE any visible
# content -- 16k leaves nothing for the answer on hard domains.
MAX_TOKENS = 32768

# Per-call client budget, EXPLICIT (litellm's implicit default is silent and
# untuned). Ordering contract with the other layers: client <= proxy upstream
# timeout (gan/framework/loop.yaml llm_proxy_timeout_s, parent-side) < the
# per-question wall budget -- the outer wall must be the ONLY thing that can
# cut a healthy in-flight call (observed: an implicit-600s client waiting on a
# 120s proxy that already replied 502, burning the whole question budget on
# doomed retries).
LLM_TIMEOUT_S = float(os.environ.get("GAN_LLM_TIMEOUT_S", "600"))

# Thinking-intensity knob for reasoning models, forwarded to the gateway as the
# OpenAI-compatible ``reasoning_effort`` body field (verified: litellm forwards
# it verbatim under drop_params=True; the GAN proxy forwards the body verbatim;
# the gateway accepts low | high | max). Values are the GATEWAY contract, not
# the OpenAI enum -- unknown values fail loudly here instead of mid-run.
# Transport is EXPLICIT ONLY (function argument threaded from
# gan/framework/models.yaml through the roles / the task harness payload);
# there is deliberately no environment fallback.
REASONING_EFFORTS = ("low", "high", "max")

# Usage/cost hooks: registered callbacks receive (model, usage_dict) after every
# successful LLM call. The GAN reward layer uses this to attach token cost.
USAGE_HOOKS: list = []


def register_usage_hook(fn):
    """Register fn(model: str, usage: dict | None)."""
    USAGE_HOOKS.append(fn)


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


def _completion_kwargs(model, messages, temperature, max_tokens, reasoning_effort=None):
    """Model-specific completion kwargs (GPT-5 / Claude-Haiku quirks)."""
    kw = {"model": model, "messages": messages, "timeout": LLM_TIMEOUT_S}
    # GPT-5 and GPT-5-mini only support default temperature (1); GPT-5.2 does.
    if model not in ["openai/gpt-5", "openai/gpt-5-mini"]:
        kw["temperature"] = temperature
    # GPT-5 models require max_completion_tokens instead of max_tokens.
    if "gpt-5" in model:
        kw["max_completion_tokens"] = max_tokens
    elif "claude-3-haiku" in model:
        kw["max_tokens"] = min(max_tokens, 4096)
    else:
        kw["max_tokens"] = max_tokens
    effort = _reasoning_effort(reasoning_effort)
    if effort is not None:
        kw["reasoning_effort"] = effort
    return kw


@backoff.on_exception(
    backoff.expo,
    _BACKOFF_EXCEPTIONS,
    max_time=120,
    max_value=60,
)
def get_response_from_llm(
    msg: str,
    model: str,
    temperature: float = 0.0,
    max_tokens: int = MAX_TOKENS,
    msg_history=None,
    reasoning_effort: str | None = None,
) -> Tuple[str, list, dict]:
    if msg_history is None:
        msg_history = []

    # Convert text to content, compatible with LITELLM API
    msg_history = [
        {**msg, "content": msg.pop("text")} if "text" in msg else msg
        for msg in msg_history
    ]

    new_msg_history = msg_history + [{"role": "user", "content": msg}]

    response = litellm.completion(
        **_completion_kwargs(model, new_msg_history, temperature, max_tokens,
                             reasoning_effort=reasoning_effort)
    )
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
                                            "reasoning_tokens": reasoning_tokens}
