"""Tests for token_audit.py — billed-vs-actual token count audit (Phase 2.5.4).

The audit compares the tokens a provider *billed* (from its usage object)
against a rough estimate derived from the response byte length (chars / 4).
A mismatch > 20% is a billing-fraud / silent-downgrade signal that feeds the
CPVO quality penalty.

These tests cover the pure audit function extracted from the production
proxy's request-finally block so it can be unit-tested without a live HTTP
server.  The function MUST NEVER raise — it runs inside request handling.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.token_audit import audit_token_count


class TestActualTokenEstimate:
    def test_actual_tokens_from_buffer_length(self):
        # 400 bytes / 4 = 100 tokens
        actual, _, _ = audit_token_count(100, b"x" * 400)
        assert actual == 100

    def test_bytearray_buffer_supported(self):
        actual, _, _ = audit_token_count(100, bytearray(b"x" * 200))
        assert actual == 50

    def test_empty_buffer(self):
        actual, _, _ = audit_token_count(100, b"")
        assert actual == 0


class TestMismatchDetection:
    def test_token_mismatch_detected(self):
        """billed=100, actual=50 → mismatch_rate=0.5 > 0.20 → mismatch=True."""
        # 200 bytes / 4 = 50 actual tokens
        actual, mismatch, rate = audit_token_count(100, b"x" * 200)
        assert actual == 50
        assert mismatch is True
        assert rate == pytest.approx(0.5)

    def test_no_mismatch_when_close(self):
        """billed=100, actual=95 → rate 0.05 < 0.20 → no mismatch."""
        # 380 bytes / 4 = 95
        actual, mismatch, _ = audit_token_count(100, b"x" * 380)
        assert actual == 95
        assert mismatch is False

    def test_threshold_boundary_not_flagged(self):
        """Exactly 20% mismatch is NOT flagged (strictly greater than)."""
        # billed=100, actual=80 → rate exactly 0.20 → not > threshold
        actual, mismatch, rate = audit_token_count(100, b"x" * 320)
        assert actual == 80
        assert rate == pytest.approx(0.20)
        assert mismatch is False

    def test_severe_overbilling_detected(self):
        """billed=1000, actual=100 → 90% mismatch → flagged."""
        actual, mismatch, rate = audit_token_count(1000, b"x" * 400)
        assert actual == 100
        assert mismatch is True
        assert rate == pytest.approx(0.9)


class TestNeverCrashes:
    def test_token_mismatch_no_crash_none_buffer(self):
        """None buffer never raises — returns zeros."""
        actual, mismatch, rate = audit_token_count(100, None)
        assert actual == 0
        assert mismatch is False
        assert rate == 0.0

    def test_zero_billed_no_crash(self):
        """billed=0 → cannot audit, no mismatch."""
        actual, mismatch, rate = audit_token_count(0, b"x" * 400)
        assert actual == 100
        assert mismatch is False
        assert rate == 0.0

    def test_negative_billed_no_crash(self):
        actual, mismatch, rate = audit_token_count(-50, b"x" * 400)
        assert mismatch is False

    def test_garbage_inputs_no_crash(self):
        """Bogus types must not raise."""
        actual, mismatch, rate = audit_token_count("notanint", None)  # type: ignore[arg-type]
        assert mismatch is False
        # An object whose __len__ works still yields an estimate
        actual, mismatch, rate = audit_token_count(100, 12345)  # type: ignore[arg-type]
        assert mismatch is False

    def test_custom_threshold(self):
        """A stricter 10% threshold flags what 20% would not."""
        # billed=100, actual=88 → rate 0.12
        _, mismatch_default, _ = audit_token_count(100, b"x" * 352)
        assert mismatch_default is False  # 0.12 < 0.20
        _, mismatch_strict, _ = audit_token_count(100, b"x" * 352, threshold=0.10)
        assert mismatch_strict is True  # 0.12 > 0.10


# ────────────────────────────────────────────────────────────────────────────
# Phase 3.5 — SSE / JSON false-positive regression
#
# The original `len(buffer)//4` heuristic counted the raw response BYTES, which
# for a streaming (SSE) response include `data: {...}` framing, JSON keys, the
# final `[DONE]` marker and the embedded `usage` object. That scaffolding is
# easily 30–60× the size of the actual completion text, so `actual_tokens` was
# massively inflated and almost every streaming request tripped the >20 %
# mismatch gate — a false-positive billing-fraud alert.
#
# These tests pin the FIXED behaviour: the estimate must be derived from the
# EXTRACTED completion text, not the raw buffer length.
# ────────────────────────────────────────────────────────────────────────────

# A realistic z.ai streaming completion. The actual assistant text is just
# "Hello there!" (≈3 tokens); the surrounding JSON/SSE framing is ~600 bytes.
_SSE_BUFFER = b"\n".join([
    b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}',
    b"",
    b'data: {"choices":[{"index":0,"delta":{"content":"Hello"}}]}',
    b"",
    b'data: {"choices":[{"index":0,"delta":{"content":" there!"}}]}',
    b"",
    b'data: {"choices":[{"index":0,"delta":{"content":""},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}',
    b"",
    b"data: [DONE]",
    b"",
])

# The same completion delivered as a single (non-streaming) JSON body.
_JSON_BUFFER = (
    b'{"id":"chatcmpl-1","object":"chat.completion","model":"glm-4.6",'
    b'"choices":[{"index":0,"message":{"role":"assistant","content":"Hello there!"},'
    b'"finish_reason":"stop"}],"usage":{"prompt_tokens":7,"completion_tokens":3,'
    b'"total_tokens":10}}'
)

# A bare API error envelope (no choices, no usable content).
_ERROR_BUFFER = (
    b'{"error":{"code":"rate_exceeded","message":"quota exceeded"}}'
)


class TestSSEStreamingFalsePositive:
    """Gate 1 (TDD) — this MUST fail on the old len(buffer)//4 code and pass
    after the Phase 3.5 fix."""

    def test_short_streaming_completion_not_flagged(self):
        """billed completion_tokens=3, real text "Hello there!" ≈3 tokens.
        Old code: len(_SSE_BUFFER)//4 ≫ 3 → spurious mismatch. Fixed code:
        extract text → ≈3 tokens → no mismatch."""
        actual, mismatch, rate = audit_token_count(3, _SSE_BUFFER)
        assert mismatch is False, (
            f"FALSE POSITIVE: estimated actual={actual} vs billed=3 "
            f"(rate={rate:.0%}) — SSE framing must not inflate the estimate"
        )

    def test_sse_actual_close_to_real_text_tokens(self):
        """The estimate should be in the ballpark of the true completion
        token count (≈3), NOT the raw buffer length (≈600 bytes ÷ 4 ≈ 150)."""
        actual, _, _ = audit_token_count(3, _SSE_BUFFER)
        # True count is 3; allow a generous band (1–8) but reject the old ≈150.
        assert 1 <= actual <= 8, f"actual={actual} looks like raw-buffer len//4"


class TestExtractionNonStreaming:
    """Single-JSON (non-streaming) bodies — content taken from
    choices[].message.content, not the raw byte length."""

    def test_non_streaming_completion_not_flagged(self):
        """billed=3, content 'Hello there!' (12 chars ≈ 3 tokens) → no
        mismatch. Old code would estimate from the ~190-byte JSON envelope."""
        actual, mismatch, rate = audit_token_count(3, _JSON_BUFFER)
        assert actual == 3, f"actual={actual} — expected text-derived estimate"
        assert mismatch is False
        assert rate == 0.0

    def test_non_streaming_real_mismatch_still_detected(self):
        """A genuine over-billing (billed=50 for ~3 real tokens) is still
        caught — the fix must not blind the audit, only kill false positives."""
        actual, mismatch, rate = audit_token_count(50, _JSON_BUFFER)
        assert actual == 3
        assert mismatch is True
        assert rate == pytest.approx((50 - 3) / 50)


class TestExtractionErrorAndEmpty:
    """Error responses and content-less buffers must never flag a mismatch."""

    def test_error_envelope_no_content_zero_tokens(self):
        """A quota-exceeded error has no completion content. With billed=0
        (no usage), mismatch must be False and actual should be 0 (not a
        byte-fallback of the error JSON length)."""
        actual, mismatch, rate = audit_token_count(0, _ERROR_BUFFER)
        assert mismatch is False
        assert rate == 0.0
        assert actual == 0, "error envelope must not yield a byte estimate"

    def test_error_envelope_with_spurious_billed_no_flag(self):
        """Even if a bogus billed count slips through, an error envelope has
        no content → actual=0 → billed<=0-or-actual<=0 guard ⇒ no mismatch."""
        actual, mismatch, _ = audit_token_count(999, _ERROR_BUFFER)
        assert actual == 0
        assert mismatch is False

    def test_done_only_stream_no_content(self):
        """A stream that is just the terminator frame carries no content."""
        buf = b"data: [DONE]\n\n"
        actual, mismatch, _ = audit_token_count(5, buf)
        assert actual == 0
        assert mismatch is False

    def test_truly_empty_buffer(self):
        actual, mismatch, rate = audit_token_count(100, b"")
        assert actual == 0
        assert mismatch is False
        assert rate == 0.0

    def test_none_buffer(self):
        actual, mismatch, rate = audit_token_count(100, None)
        assert actual == 0
        assert mismatch is False
        assert rate == 0.0

    def test_truncated_binary_blob_uses_byte_fallback(self):
        """A non-JSON / non-SSE blob (truncated response) falls back to the
        coarse byte estimate rather than crashing — and with no billing still
        reports mismatch=False."""
        blob = b"\x00\x01\x02garbage" * 40  # 400 bytes, unparseable
        actual, mismatch, _ = audit_token_count(0, blob)
        assert actual == 100  # 400 // 4 degenerate fallback
        assert mismatch is False


class TestStreamingMultiDeltaAggregation:
    """Content split across many deltas is concatenated before estimating."""

    def test_many_small_deltas_concatenated(self):
        # 5 deltas of "ab" each → "ababababab" (10 chars ≈ 2 tokens), billed 2.
        frames = []
        for _ in range(5):
            frames.append(b'data: {"choices":[{"delta":{"content":"ab"}}]}')
            frames.append(b"")
        frames.append(b"data: [DONE]")
        buf = b"\n".join(frames)
        actual, mismatch, _ = audit_token_count(2, buf)
        assert actual == 2, f"actual={actual} — deltas must be concatenated"
        assert mismatch is False
