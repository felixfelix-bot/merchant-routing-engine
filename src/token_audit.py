"""token_audit.py — billed-vs-actual token count audit (Phase 2.5.4 / 3.5).

Compares the tokens a provider *billed* (from its ``usage`` object) against an
estimate derived from the **actual completion text**, and flags a mismatch
beyond a threshold as a billing-fraud / silent-downgrade signal.

The mismatch flag is written to the ``provider_telemetry`` table (column
``token_mismatch``) by the production proxy's request-finally block, where it
feeds the :class:`~src.cpvo_calculator.CPVOCalculator` quality penalty and the
``get_quality_score`` report.

Phase 3.5 — false-positive fix
------------------------------
The original implementation estimated tokens as ``len(response_buffer) // 4``.
For a *streaming* (SSE) response the buffer is not the completion text — it is
a sequence of ``data: {...}`` frames whose JSON scaffolding (keys, indexes,
the embedded ``usage`` object, the trailing ``[DONE]``) is routinely 30–60×
the size of the real content. ``len(buf)//4`` therefore massively
over-estimated ``actual_tokens`` and tripped the >20 % gate on almost every
streaming request — a flood of false billing-mismatch alerts.

The estimate is now derived from the **extracted completion text**:

1. ``_extract_completion_text`` pulls the assistant content out of a single
   JSON body (``choices[].message.content``) or, for SSE, concatenates every
   ``choices[].delta.content`` across ``data:`` frames — mirroring the proxy's
   own ``_parse_usage`` scan.
2. ``_estimate_tokens`` counts those tokens with ``tiktoken`` when it is
   importable (precise, cl100k_base), falling back to a ``chars / 4`` heuristic
   on the extracted text otherwise.
3. The legacy ``len(buf)//4`` survives ONLY as a degenerate fallback for
   buffers that are non-empty but completely unparseable (e.g. a truncated /
   binary blob) — never for real responses, so it no longer produces false
   positives.

Design rules
------------
* **NEVER raises.** This runs inside the proxy's request-handling path.  Any
  error — bogus types, ``None`` buffer, overflow — is swallowed and yields a
  safe ``(0, False, 0.0)``.
* **Cheap.** Extraction is one or two JSON passes over a buffer that is
  already in memory; the heuristic path stays a single ``len()`` — well under
  the < 10 ms CPVO budget. ``tiktoken`` is optional and loaded lazily.
* **Threshold is the only knob.** Default 0.20 (20 %) per the PLAN.
* **Public API is unchanged** — ``(actual_tokens, mismatch, mismatch_rate)`` —
  so the production call site needs no edits.
"""
from __future__ import annotations

import json
from typing import Any

__all__ = ["audit_token_count", "TOKEN_MISMATCH_THRESHOLD"]

#: Default fraction above which a billed-vs-actual gap is flagged.
TOKEN_MISMATCH_THRESHOLD = 0.20

#: Rough chars-per-token estimate used by the dependency-free heuristic.
_CHARS_PER_TOKEN = 4

# ── Optional precise tokenizer (tiktoken) ────────────────────────────────────
# Loaded lazily on first use and cached at module scope. If tiktoken is absent
# (it is not a hard dependency) or its BPE data cannot be loaded, we silently
# fall back to the chars/4 heuristic. Never raises.
_TIKTOKEN_ENCODER = None
_TIKTOKEN_TRIED = False


def _get_tiktoken():
    """Return a cached cl100k_base encoder, or ``None`` if unavailable."""
    global _TIKTOKEN_ENCODER, _TIKTOKEN_TRIED
    if _TIKTOKEN_TRIED:
        return _TIKTOKEN_ENCODER
    _TIKTOKEN_TRIED = True
    try:
        import tiktoken  # type: ignore[import-not-found]

        _TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _TIKTOKEN_ENCODER = None
    return _TIKTOKEN_ENCODER


def _content_from_choice(choice: Any) -> str:
    """Pull the text out of one ``choices[]`` element across the common
    OpenAI/z.ai schemas (streaming delta, non-streaming message, legacy text)."""
    if not isinstance(choice, dict):
        return ""
    delta = choice.get("delta")
    if isinstance(delta, dict) and delta.get("content"):
        return str(delta["content"])
    message = choice.get("message")
    if isinstance(message, dict) and message.get("content"):
        return str(message["content"])
    if choice.get("text"):
        return str(choice["text"])
    return ""


def _extract_completion_text(buf: Any) -> tuple[str, bool]:
    """Best-effort extraction of the assistant completion text from a response
    buffer. Handles single-JSON bodies and SSE ``data: {...}`` streams. Mirrors
    the production proxy's ``_parse_usage`` frame scan. Never raises.

    Returns ``(text, saw_structure)`` where ``text`` is the concatenated
    completion content (possibly empty) and ``saw_structure`` is True when the
    buffer was recognisable as a JSON/SSE response — even if it carried no
    content (an error envelope, or a ``[DONE]``-only stream). The caller uses
    ``saw_structure`` to distinguish "content-less response" (→ 0 tokens, not a
    billing signal) from "raw unparseable blob" (→ coarse byte fallback)."""
    if buf is None:
        return ("", False)
    # Accept bytes / bytearray / str.
    if isinstance(buf, (bytes, bytearray)):
        text = buf.decode("utf-8", "ignore")
    elif isinstance(buf, str):
        text = buf
    else:
        return ("", False)
    if not text:
        return ("", False)

    saw_structure = False

    # 1) Non-streaming: the whole buffer is one JSON object.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            saw_structure = True
            choices = obj.get("choices")
            if isinstance(choices, list) and choices:
                parts = [_content_from_choice(c) for c in choices]
                joined = "".join(parts)
                if joined:
                    return (joined, True)
    except Exception:
        pass

    # 2) Streaming: scan each `data:` line and concatenate delta content.
    pieces: list[str] = []
    try:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            saw_structure = True  # any SSE framing ⇒ structured response
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            choices = obj.get("choices")
            if isinstance(choices, list):
                for c in choices:
                    piece = _content_from_choice(c)
                    if piece:
                        pieces.append(piece)
    except Exception:
        pass
    return ("".join(pieces), saw_structure)


def _estimate_tokens_from_text(text: str) -> int:
    """Estimate token count from completion *text*. Uses tiktoken when
    available, else the chars/4 heuristic."""
    if not text:
        return 0
    enc = _get_tiktoken()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass  # fall through to heuristic
    return len(text) // _CHARS_PER_TOKEN


def audit_token_count(
    billed_tokens: int,
    response_buffer: Any,
    threshold: float = TOKEN_MISMATCH_THRESHOLD,
) -> tuple[int, bool, float]:
    """Estimate actual tokens from the response and detect a billing mismatch.

    Parameters
    ----------
    billed_tokens:
        The provider's reported ``completion_tokens`` (NOT ``total_tokens`` —
        the estimate is derived from completion text only, so comparing against
        prompt+completion would itself manufacture a false mismatch).
    response_buffer:
        The raw response bytes (or bytearray/str). The completion text is
        extracted from a single JSON body or from SSE ``data:`` frames, and the
        token estimate is derived from that text — not the raw buffer length.
    threshold:
        Mismatch fraction above which a provider is flagged (default 0.20).

    Returns
    -------
    (actual_tokens, mismatch, mismatch_rate)
        ``actual_tokens``  — estimated tokens from the extracted completion text.
        ``mismatch``       — ``True`` iff ``mismatch_rate > threshold`` AND
                             ``billed_tokens`` is positive.
        ``mismatch_rate``  — ``abs(billed - actual) / max(billed, 1)``, or
                             ``0.0`` when billing is unavailable.

    Notes
    -----
    When ``billed_tokens <= 0`` there is nothing to compare against, so the
    function returns ``(actual_tokens, False, 0.0)`` — the audit cannot flag a
    provider it cannot bill.  A free (z.ai subscription) response with zero
    billed tokens is not fraud.  Likewise an empty/unparseable buffer means the
    request failed (captured separately via ``response_valid``) and is not a
    billing-fraud signal, so ``mismatch`` is ``False`` there too.
    """
    try:
        text, saw_structure = _extract_completion_text(response_buffer)
        if text:
            actual_tokens = _estimate_tokens_from_text(text)
        elif saw_structure:
            # Valid JSON/SSE response that carried no completion content (an
            # error envelope, or a [DONE]-only stream). No content ⇒ 0 tokens;
            # this is not a billing-fraud signal, so mismatch stays False.
            actual_tokens = 0
        else:
            # Degenerate fallback: a non-empty buffer that is completely
            # unparseable (truncated / binary blob). Fall back to a coarse byte
            # estimate so we still return *something*, but this path never
            # applies to a real SSE/JSON response and so cannot produce the
            # Phase 3.5 false positives.
            buf = response_buffer if response_buffer is not None else b""
            try:
                actual_tokens = int(len(buf) // _CHARS_PER_TOKEN)
            except TypeError:
                actual_tokens = 0
        billed = int(billed_tokens or 0)
        # We can only flag a billing mismatch when we have BOTH a billed count
        # AND a non-trivial content estimate.
        if billed <= 0 or actual_tokens <= 0:
            return (actual_tokens, False, 0.0)
        mismatch_rate = abs(billed - actual_tokens) / max(billed, 1)
        return (actual_tokens, mismatch_rate > threshold, mismatch_rate)
    except Exception:
        return (0, False, 0.0)
