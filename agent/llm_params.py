"""LLM call-parameter CONTRACT (stdlib only, safe to import anywhere).

This module exists because ``agent/llm.py`` imports litellm at module load,
and litellm's import-time ``load_dotenv()`` tries to read the repo-root
``.env`` file -- a fatal ``PermissionError`` for any restricted-uid process
that can SEE the file but cannot read it (e.g. the sandboxed task harness
under setpriv --reuid). Anything that must be importable from such processes
(harness CLI validation, registry resolution) takes its constants from HERE;
``agent/llm.py`` re-exports them so existing importers keep working.
"""

# Thinking-intensity values accepted by the current OpenAI-compatible gateway
# for its reasoning models (NOT the OpenAI enum: low/high/max). The value is
# forwarded to the gateway as the OpenAI-compatible ``reasoning_effort`` body
# field. Update here (single source) when the gateway contract changes.
REASONING_EFFORTS = ("low", "high", "max")
