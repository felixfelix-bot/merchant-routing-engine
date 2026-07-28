"""token_audit.py — billed-vs-actual token count audit (Phase 2.5.4).

Compares the tokens a provider *billed* (from its ``usage`` object) against a
rough estimate derived from the response byte length (chars / 4).  A mismatch
beyond a threshold is a billing-fraud / silent-downgrade signal.

The mismatch flag is written to the ``provider_telemetry`` table (column
``token_mismatch``) by the production proxy's request-finally block, where it
feeds the :class:`~src.cpvo_calculator.CPVOCalculator` quality penalty and the
``get_quality_score`` report.

Design rules
------------
* **NEVER raises.** This runs inside the proxy's request-handling path.  Any
  error — bogus types, ``None`` buffer, overflow — is swallowed and yields a
  safe ``(0, False, 0.0)``.
* **Cheap.** A single ``len()`` and a couple of arithmetic ops — well under the
  < 10 ms CPVO budget.
* **Threshold is the only knob.** Default 0.20 (20 %) per the PLAN.
"""
from __future__ import annotations

from typing import Any

__all__ = ["audit_token_count", "TOKEN_MISMATCH_THRESHOLD"]

#: Default fraction above which a billed-vs-actual gap is flagged.
TOKEN_MISMATCH_THRESHOLD = 0.20

#: Rough chars-per-token estimate used to derive ``actual_tokens``.
_CHARS_PER_TOKEN = 4


def audit_token_count(
    billed_tokens: int,
    response_buffer: Any,
    threshold: float = TOKEN_MISMATCH_THRESHOLD,
) -> tuple[int, bool, float]:
    """Estimate actual tokens from the response and detect a billing mismatch.

    Parameters
    ----------
    billed_tokens:
        ``total_tokens`` reported by the provider's ``usage`` object.
    response_buffer:
        The raw response bytes (or bytearray, or any object with ``__len__``).
        ``len(buffer) // 4`` is the rough actual-token estimate.
    threshold:
        Mismatch fraction above which a provider is flagged (default 0.20).

    Returns
    -------
    (actual_tokens, mismatch, mismatch_rate)
        ``actual_tokens``  — estimated tokens from the response length.
        ``mismatch``       — ``True`` iff ``mismatch_rate > threshold`` AND
                             ``billed_tokens`` is positive.
        ``mismatch_rate``  — ``abs(billed - actual) / max(billed, 1)``, or
                             ``0.0`` when billing is unavailable.

    Notes
    -----
    When ``billed_tokens <= 0`` there is nothing to compare against, so the
    function returns ``(actual_tokens, False, 0.0)`` — the audit cannot flag a
    provider it cannot bill.  This is intentional: a free (z.ai subscription)
    response with zero billed tokens is not fraud.
    """
    try:
        buf = response_buffer if response_buffer is not None else b""
        actual_tokens = int(len(buf) // _CHARS_PER_TOKEN)
        billed = int(billed_tokens or 0)
        # We can only flag a billing mismatch when we have BOTH a billed count
        # AND a non-trivial content estimate.  An empty buffer means the
        # request failed (captured separately via response_valid) — it is not a
        # billing-fraud signal, so we return mismatch=False.  This mirrors the
        # original production guard (``_actual > 0``).
        if billed <= 0 or actual_tokens <= 0:
            return (actual_tokens, False, 0.0)
        mismatch_rate = abs(billed - actual_tokens) / max(billed, 1)
        return (actual_tokens, mismatch_rate > threshold, mismatch_rate)
    except Exception:
        return (0, False, 0.0)
