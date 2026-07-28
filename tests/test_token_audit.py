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
