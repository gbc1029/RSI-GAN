import backoff
import json
from typing import Tuple

import requests
import litellm
from dotenv import load_dotenv

load_dotenv()

MAX_TOKENS = 16384

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


# Retry on transient provider failures (rate limit / 5xx / timeout / conn drops).
# NOTE: there is NO fallback model — a model is chosen solely from
# gan/framework/models.yaml and passed in explicitly by the caller.
_BACKOFF_EXCEPTIONS = [requests.exceptions.RequestException, json.JSONDecodeError, KeyError]
for _name in ("RateLimitError", "ServiceUnavailableError", "Timeout", "APIConnectionError"):
    _exc = getattr(getattr(litellm, "exceptions", None), _name, None)
    if isinstance(_exc, type):
        _BACKOFF_EXCEPTIONS.append(_exc)
try:  # also cover raw openai SDK errors
    import openai as _openai
    for _name in ("APIError", "APIConnectionError", "APITimeoutError", "RateLimitError"):
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


def _completion_kwargs(model, messages, temperature, max_tokens):
    """Model-specific completion kwargs (GPT-5 / Claude-Haiku quirks)."""
    kw = {"model": model, "messages": messages}
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
    return kw


@backoff.on_exception(
    backoff.expo,
    _BACKOFF_EXCEPTIONS,
    max_time=600,
    max_value=60,
)
def get_response_from_llm(
    msg: str,
    model: str,
    temperature: float = 0.0,
    max_tokens: int = MAX_TOKENS,
    msg_history=None,
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
        **_completion_kwargs(model, new_msg_history, temperature, max_tokens)
    )
    _run_usage_hooks(model, response)
    response_text = response['choices'][0]['message']['content']  # pyright: ignore
    new_msg_history.append({"role": "assistant", "content": response['choices'][0]['message']['content']})

    # Convert content to text, compatible with MetaGen API
    new_msg_history = [
        {**msg, "text": msg.pop("content")} if "content" in msg else msg
        for msg in new_msg_history
    ]

    return response_text, new_msg_history, {}
