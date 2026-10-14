"""Guarded-artifact writer tokens (FRAMEWORK, frozen).

Step 1 of the B46 ledger-integrity narrow handle (docs/7 appendix 26). The
decision-grade artifacts -- ``scores/scores.jsonl``, the design configs and the
checkpoints -- are written only through a handful of framework entry points.
Each of them now requires a per-run :class:`WriterToken` that is minted once by
``gan.build.build_gan_loop`` and threaded explicitly through the framework call
chain (mirroring how role/seat travel: supplied by framework code, never by the
agent). A bare ``import gan.framework.scores`` no longer reaches a callable
write; the agent-facing one-line forgery face is closed.

This is NOT a security boundary against arbitrary in-process Python: object
surgery (``object.__new__``, attribute rewriting) can mint a look-alike, and the
companion static gate (``ast_checker`` ``observed_ledger_writer``) refuses
agent-authored modules that import the guarded writer modules on the registry
and commit paths. What remains open -- deliberate evasion plus out-of-band file
tampering -- is exactly what the deferred step 2 (hash chain + manifest, same
trigger conditions as the original B46 ruling) detects; the token is the natural
stamping point for that chain.
"""
from __future__ import annotations

from typing import Any, Optional


class LedgerAccessError(TypeError):
    """Raised when a guarded-artifact write is attempted without a run token."""


class WriterToken:
    """Per-run capability object for guarded-artifact writes.

    Not agent-constructible by the normal API (``_MINT_KEY`` is module-private);
    ``open_run`` is the only minting entry and is called by framework assembly
    code only. Deliberately minimal: identity, not secrecy.
    """

    __slots__ = ("run_dir",)

    _MINT_KEY = object()

    def __init__(self, run_dir: str, *, _key: Any = None) -> None:
        if _key is not WriterToken._MINT_KEY:
            raise LedgerAccessError(
                "WriterToken is minted by gan.framework.ledger.open_run(); it is "
                "not constructible by agent-owned code (B46 step-1 gate). If you "
                "are framework plumbing, thread the run token instead of minting "
                "a new one.")
        self.run_dir = str(run_dir)


def open_run(run_dir: str) -> WriterToken:
    """Mint the per-run writer token (called once by ``build_gan_loop``)."""
    return WriterToken(run_dir, _key=WriterToken._MINT_KEY)


def guard_writer(token: Optional[WriterToken], artifact: str) -> None:
    """Validate the token of a guarded-artifact write; raise when absent.

    Fail-loud by design: a missing token at one of the four guarded write points
    is a framework wiring bug (or a forgery attempt) -- it must never degrade to
    an unguarded write (fail-open would be fail-silent here).
    """
    if not isinstance(token, WriterToken):
        raise LedgerAccessError(
            f"write to {artifact} requires the per-run ledger WriterToken "
            f"(gan.framework.ledger.open_run); B46 step-1 gate refuses "
            f"unguarded framework-artifact writes (got "
            f"{type(token).__name__ if token is not None else 'None'}).")
