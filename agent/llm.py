import backoff
import os
from typing import Tuple
import requests
import litellm
from dotenv import load_dotenv
import json

load_dotenv()

MAX_TOKENS = 16384

CLAUDE_MODEL = "anthropic/claude-sonnet-4-5-20250929"
CLAUDE_HAIKU_MODEL = "anthropic/claude-3-haiku-20240307"
CLAUDE_35NEW_MODEL = "anthropic/claude-3-5-sonnet-20241022"
OPENAI_MODEL = "openai/gpt-4o"
OPENAI_MINI_MODEL = "openai/gpt-4o-mini"
OPENAI_O3_MODEL = "openai/o3"
OPENAI_O3MINI_MODEL = "openai/o3-mini"
OPENAI_O4MINI_MODEL = "openai/o4-mini"
OPENAI_GPT52_MODEL = "openai/gpt-5.2"
OPENAI_GPT5_MODEL = "openai/gpt-5"
OPENAI_GPT5MINI_MODEL = "openai/gpt-5-mini"
GEMINI_3_MODEL = "gemini/gemini-3-pro-preview"
GEMINI_MODEL = "gemini/gemini-2.5-pro"
GEMINI_FLASH_MODEL = "gemini/gemini-2.5-flash"

# --- GAN extensions -------------------------------------------------------
# Allow overriding the default model via env (e.g. GAN_MODEL_DEFAULT=openai/glm-4.7-flash).
OPENAI_MODEL = os.environ.get("GAN_MODEL_DEFAULT", OPENAI_MODEL)

# Usage/cost hooks: registered callbacks receive (model, usage_dict) after every
# successful LLM call. The GAN reward layer uses this to attach token cost to
# the RewardPacket.
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
except Exception:
    pass
_BACKOFF_EXCEPTIONS = tuple(_BACKOFF_EXCEPTIONS)

litellm.drop_params=True


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
    model: str = OPENAI_MODEL,
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

    # Primary call; on failure fall back once to GAN_MODEL_FALLBACK (if configured).
    effective_model = model
    try:
        response = litellm.completion(
            **_completion_kwargs(model, new_msg_history, temperature, max_tokens)
        )
    except Exception:
        fallback = os.environ.get("GAN_MODEL_FALLBACK")
        if not fallback or fallback == model:
            raise
        effective_model = fallback
        response = litellm.completion(
            **_completion_kwargs(fallback, new_msg_history, temperature, max_tokens)
        )
    _run_usage_hooks(effective_model, response)
    response_text = response['choices'][0]['message']['content']  # pyright: ignore
    new_msg_history.append({"role": "assistant", "content": response['choices'][0]['message']['content']})

    # Convert content to text, compatible with MetaGen API
    new_msg_history = [
        {**msg, "text": msg.pop("content")} if "content" in msg else msg
        for msg in new_msg_history
    ]

    return response_text, new_msg_history, {}


if __name__ == "__main__":
    msg = 'Hello there!'
    models = [
        ("CLAUDE_MODEL", CLAUDE_MODEL),
        ("CLAUDE_HAIKU_MODEL", CLAUDE_HAIKU_MODEL),
        ("CLAUDE_35NEW_MODEL", CLAUDE_35NEW_MODEL),
        ("OPENAI_MODEL", OPENAI_MODEL),
        ("OPENAI_MINI_MODEL", OPENAI_MINI_MODEL),
        ("OPENAI_O3_MODEL", OPENAI_O3_MODEL),
        ("OPENAI_O3MINI_MODEL", OPENAI_O3MINI_MODEL),
        ("OPENAI_O4MINI_MODEL", OPENAI_O4MINI_MODEL),
        ("OPENAI_GPT52_MODEL", OPENAI_GPT52_MODEL),
        ("OPENAI_GPT5_MODEL", OPENAI_GPT5_MODEL),
        ("OPENAI_GPT5MINI_MODEL", OPENAI_GPT5MINI_MODEL),
        ("GEMINI_3_MODEL", GEMINI_3_MODEL),
        ("GEMINI_MODEL", GEMINI_MODEL),
        ("GEMINI_FLASH_MODEL", GEMINI_FLASH_MODEL),
    ]
    for name, model in models:
        print(f"\n{'='*50}")
        print(f"Testing {name}: {model}")
        print('='*50)
        try:
            output_msg, msg_history, info = get_response_from_llm(msg, model=model)
            print(f"OK: {output_msg[:100]}...")
        except Exception as e:
            print(f"FAIL: {str(e)[:200]}")
