"""Tests for src/quota_window_extractor.py — parse z.ai quota API into pace_factor inputs.

The extractor converts the proxy's quota_cache structure:
    quota_cache[key_name] = (windows_list, timestamp)
    windows_list = [{"name", "type", "used_pct", "resets_at", "window_hours"}, ...]

into pace_factor input tuples:
    [(quota_used, quota_total, time_elapsed_pct, burn_rate, window_duration_hours), ...]

ADR-008: deterministic multipliers outside Kalman — the pace_factor function
consumes these tuples to compute the predictive quota-pacing multiplier.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.quota_window_extractor import extract_quota_windows


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_window(name: str, used_pct: int, resets_at: int, window_hours: int) -> dict:
    """Create a window dict matching the proxy's _parse_limit_entry output."""
    return {
        "name": name,
        "type": "TOKENS_LIMIT",
        "used_pct": used_pct,
        "resets_at": resets_at,
        "window_hours": window_hours,
    }


def _make_cache_entry(windows: list[dict], ts: float | None = None) -> tuple[list[dict], float]:
    """Create a quota_cache entry: (windows, timestamp)."""
    return (windows, ts if ts is not None else time.time())


# ── 5h window extraction ─────────────────────────────────────────────────────


class TestExtract5hWindow:
    def test_extract_5h_window_correctly(self):
        """5h window at 80% usage, 4h elapsed (80% of 5h)."""
        now = int(time.time())
        window_start = now - 4 * 3600  # 4 hours ago → 80% elapsed
        resets_at = window_start + 5 * 3600

        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 80, resets_at, 5),
        ])}

        result = extract_quota_windows(cache, burn_rate=200.0, quota_total=1_000_000)

        assert len(result) == 1
        used, total, elapsed_pct, burn, duration_hours = result[0]
        assert used == pytest.approx(800_000, rel=1e-3)  # 80% of 1M
        assert total == 1_000_000
        assert elapsed_pct == pytest.approx(0.8, abs=0.01)
        assert burn == 200.0
        assert duration_hours == 5.0

    def test_5h_window_at_start(self):
        """5h window just started → elapsed_pct ≈ 0."""
        now = int(time.time())
        window_start = now  # just started
        resets_at = window_start + 5 * 3600

        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 0, resets_at, 5),
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)

        assert len(result) == 1
        used, total, elapsed_pct, burn, duration_hours = result[0]
        assert elapsed_pct == pytest.approx(0.0, abs=0.01)
        assert used == 0

    def test_5h_window_near_end(self):
        """5h window almost expired → elapsed_pct ≈ 1.0."""
        now = int(time.time())
        window_start = now - 4 * 3600 - 50 * 60  # 4h50m ago
        resets_at = window_start + 5 * 3600

        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 95, resets_at, 5),
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)

        assert len(result) == 1
        used, total, elapsed_pct, burn, duration_hours = result[0]
        assert elapsed_pct == pytest.approx(0.966, abs=0.01)
        assert used == pytest.approx(950_000, rel=1e-3)


# ── Weekly window extraction ──────────────────────────────────────────────────


class TestExtractWeeklyWindow:
    def test_extract_weekly_window_correctly(self):
        """Weekly window at 50% usage, 84h elapsed (50% of 168h)."""
        now = int(time.time())
        window_start = now - 84 * 3600  # 84 hours ago → 50% of 168h
        resets_at = window_start + 168 * 3600

        cache = {"ours": _make_cache_entry([
            _make_window("weekly", 50, resets_at, 168),
        ])}

        result = extract_quota_windows(cache, burn_rate=6000.0, quota_total=2_000_000)

        assert len(result) == 1
        used, total, elapsed_pct, burn, duration_hours = result[0]
        assert used == pytest.approx(1_000_000, rel=1e-3)  # 50% of 2M
        assert total == 2_000_000
        assert elapsed_pct == pytest.approx(0.5, abs=0.01)
        assert burn == 6000.0
        assert duration_hours == 168.0

    def test_weekly_window_at_start(self):
        """Weekly window just started → elapsed_pct ≈ 0."""
        now = int(time.time())
        resets_at = now + 168 * 3600

        cache = {"ours": _make_cache_entry([
            _make_window("weekly", 0, resets_at, 168),
        ])}

        result = extract_quota_windows(cache, burn_rate=5000.0, quota_total=2_000_000)

        assert len(result) == 1
        _, _, elapsed_pct, _, _ = result[0]
        assert elapsed_pct == pytest.approx(0.0, abs=0.01)

    def test_weekly_window_near_end(self):
        """Weekly window almost expired → elapsed_pct ≈ 1.0."""
        now = int(time.time())
        window_start = now - 167 * 3600  # 167h ago → 167/168 ≈ 99.4%
        resets_at = window_start + 168 * 3600

        cache = {"ours": _make_cache_entry([
            _make_window("weekly", 99, resets_at, 168),
        ])}

        result = extract_quota_windows(cache, burn_rate=5000.0, quota_total=2_000_000)

        assert len(result) == 1
        _, _, elapsed_pct, _, _ = result[0]
        assert elapsed_pct == pytest.approx(0.994, abs=0.01)


# ── Multiple windows ─────────────────────────────────────────────────────────


class TestMultipleWindows:
    def test_multiple_windows_returned_as_list(self):
        """Both 5h and weekly windows → list of 2 tuples."""
        now = int(time.time())

        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 60, now - 3 * 3600 + 5 * 3600, 5),
            _make_window("weekly", 40, now - 50 * 3600 + 168 * 3600, 168),
        ])}

        result = extract_quota_windows(cache, burn_rate=200.0, quota_total=1_000_000)

        assert len(result) == 2
        durations = [r[4] for r in result]
        assert 5.0 in durations
        assert 168.0 in durations

    def test_multiple_keys_returned_as_list(self):
        """Two keys, each with one window → list of 2 tuples."""
        now = int(time.time())
        resets_at_5h = now - 2 * 3600 + 5 * 3600

        cache = {
            "ours": _make_cache_entry([
                _make_window("5-hour", 30, resets_at_5h, 5),
            ]),
            "friend": _make_cache_entry([
                _make_window("5-hour", 60, resets_at_5h, 5),
            ]),
        }

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)

        assert len(result) == 2
        used_values = [r[0] for r in result]
        assert 300_000 in [pytest.approx(u, rel=1e-3) for u in used_values]
        assert 600_000 in [pytest.approx(u, rel=1e-3) for u in used_values]

    def test_all_windows_from_all_keys(self):
        """Two keys, each with 5h+weekly → list of 4 tuples."""
        now = int(time.time())

        cache = {
            "ours": _make_cache_entry([
                _make_window("5-hour", 50, now - 2 * 3600 + 5 * 3600, 5),
                _make_window("weekly", 30, now - 50 * 3600 + 168 * 3600, 168),
            ]),
            "friend": _make_cache_entry([
                _make_window("5-hour", 70, now - 3 * 3600 + 5 * 3600, 5),
                _make_window("weekly", 45, now - 80 * 3600 + 168 * 3600, 168),
            ]),
        }

        result = extract_quota_windows(cache, burn_rate=200.0, quota_total=2_000_000)
        assert len(result) == 4


# ── Skip missing / malformed ─────────────────────────────────────────────────


class TestSkipMalformed:
    def test_skip_missing_5h_window(self):
        """If only a weekly window exists, return only that (no 5h)."""
        now = int(time.time())
        cache = {"ours": _make_cache_entry([
            _make_window("weekly", 50, now + 100 * 3600, 168),
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 1
        assert result[0][4] == 168.0

    def test_skip_missing_weekly_window(self):
        """If only a 5h window exists, return only that (no weekly)."""
        now = int(time.time())
        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 50, now + 3 * 3600, 5),
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 1
        assert result[0][4] == 5.0

    def test_skip_malformed_window_missing_name(self):
        """Window dict missing 'name' key → skip it."""
        now = int(time.time())
        cache = {"ours": _make_cache_entry([
            {"type": "TOKENS_LIMIT", "used_pct": 50, "resets_at": now + 3 * 3600, "window_hours": 5},
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 0

    def test_skip_malformed_window_missing_resets_at(self):
        """Window dict missing 'resets_at' → skip it."""
        cache = {"ours": _make_cache_entry([
            {"name": "5-hour", "type": "TOKENS_LIMIT", "used_pct": 50, "window_hours": 5},
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 0

    def test_skip_malformed_window_missing_used_pct(self):
        """Window dict missing 'used_pct' → skip it."""
        now = int(time.time())
        cache = {"ours": _make_cache_entry([
            {"name": "5-hour", "type": "TOKENS_LIMIT", "resets_at": now + 3 * 3600, "window_hours": 5},
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 0

    def test_skip_malformed_window_missing_window_hours(self):
        """Window dict missing 'window_hours' → skip it."""
        now = int(time.time())
        cache = {"ours": _make_cache_entry([
            {"name": "5-hour", "type": "TOKENS_LIMIT", "used_pct": 50, "resets_at": now + 3 * 3600},
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 0

    def test_skip_window_with_zero_window_hours(self):
        """window_hours == 0 → can't compute elapsed → skip."""
        now = int(time.time())
        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 50, now + 3 * 3600, 0),
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 0

    def test_skip_unknown_window_name(self):
        """Window with an unrecognized name (not 5h/weekly/monthly) → skip."""
        now = int(time.time())
        cache = {"ours": _make_cache_entry([
            _make_window("unknown", 50, now + 3 * 3600, 5),
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 0

    def test_skip_error_sentinel_window(self):
        """The proxy sends sentinel windows with used_pct=999 on error → skip."""
        cache = {"ours": _make_cache_entry([
            {"name": "error", "type": "TOKENS_LIMIT", "used_pct": 999, "resets_at": 0, "window_hours": 0},
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 0

    def test_skip_window_with_resets_at_zero(self):
        """resets_at == 0 means the API failed → skip."""
        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 50, 0, 5),
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 0

    def test_mixed_valid_and_invalid(self):
        """One valid, one invalid → only the valid one returned."""
        now = int(time.time())
        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 50, now + 3 * 3600, 5),
            {"name": "broken", "used_pct": 50},  # missing fields
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 1
        assert result[0][4] == 5.0


# ── Zero / edge cases ────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_handle_zero_total_quota(self):
        """quota_total = 0 → used should still be 0, tuple still returned."""
        now = int(time.time())
        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 50, now + 3 * 3600, 5),
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=0)
        assert len(result) == 1
        used, total, elapsed_pct, burn, duration_hours = result[0]
        assert used == 0
        assert total == 0
        assert burn == 100.0
        assert duration_hours == 5.0

    def test_zero_burn_rate(self):
        """burn_rate = 0 → tuple still returned, burn_rate is 0."""
        now = int(time.time())
        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 50, now + 3 * 3600, 5),
        ])}

        result = extract_quota_windows(cache, burn_rate=0.0, quota_total=1_000_000)
        assert len(result) == 1
        assert result[0][3] == 0.0

    def test_empty_cache(self):
        """Empty quota_cache → empty list."""
        result = extract_quota_windows({}, burn_rate=100.0, quota_total=1_000_000)
        assert result == []

    def test_empty_windows_list(self):
        """Key with empty windows list → no tuples for that key."""
        cache = {"ours": _make_cache_entry([])}
        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert result == []

    def test_elapsed_clamped_at_zero(self):
        """If now is before window_start → elapsed_pct should be 0, not negative."""
        now = int(time.time())
        # resets_at is in the future, but window_start is also in the future
        window_start = now + 3600  # 1h in the future
        resets_at = window_start + 5 * 3600

        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 0, resets_at, 5),
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 1
        _, _, elapsed_pct, _, _ = result[0]
        assert elapsed_pct == 0.0

    def test_elapsed_clamped_at_one(self):
        """If now is after resets_at → elapsed_pct should be 1.0, not >1."""
        now = int(time.time())
        resets_at = now - 3600  # already expired 1h ago

        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 100, resets_at, 5),
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 1
        _, _, elapsed_pct, _, _ = result[0]
        assert elapsed_pct == 1.0

    def test_default_quota_total(self):
        """If quota_total not provided, default to 2_000_000 (proxy default)."""
        now = int(time.time())
        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 50, now + 3 * 3600, 5),
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0)
        assert len(result) == 1
        _, total, _, _, _ = result[0]
        assert total == 2_000_000

    def test_used_pct_100(self):
        """used_pct = 100 → used = total."""
        now = int(time.time())
        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 100, now + 1 * 3600, 5),
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 1
        used, total, _, _, _ = result[0]
        assert used == 1_000_000
        assert total == 1_000_000

    def test_used_pct_zero(self):
        """used_pct = 0 → used = 0."""
        now = int(time.time())
        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 0, now + 5 * 3600, 5),
        ])}

        result = extract_quota_windows(cache, burn_rate=100.0, quota_total=1_000_000)
        assert len(result) == 1
        used, _, _, _, _ = result[0]
        assert used == 0

    def test_monthly_window_included(self):
        """Monthly window (720h) should also be extracted."""
        now = int(time.time())
        resets_at = now + 600 * 3600  # 600h remaining out of 720h

        cache = {"ours": _make_cache_entry([
            _make_window("monthly", 16, resets_at, 720),
        ])}

        result = extract_quota_windows(cache, burn_rate=500.0, quota_total=5_000_000)
        assert len(result) == 1
        used, total, elapsed_pct, burn, duration_hours = result[0]
        assert duration_hours == 720.0
        assert used == pytest.approx(800_000, rel=1e-3)  # 16% of 5M


# ── Integration with pace_factor_multi ────────────────────────────────────────


class TestIntegrationWithPaceFactor:
    def test_extracted_tuples_feed_pace_factor_multi(self):
        """The tuples from extract_quota_windows should be directly usable
        in pace_factor_multi."""
        from src.pricing_engine import pace_factor_multi

        now = int(time.time())
        cache = {"ours": _make_cache_entry([
            _make_window("5-hour", 80, now - 4 * 3600 + 5 * 3600, 5),
            _make_window("weekly", 50, now - 84 * 3600 + 168 * 3600, 168),
        ])}

        windows = extract_quota_windows(
            cache, burn_rate=200.0, quota_total=1_000_000
        )
        assert len(windows) == 2

        # Should not raise — tuples are in the correct format
        pf = pace_factor_multi(windows=windows)
        assert 0.5 <= pf <= 3.0

    def test_per_key_quota_total(self):
        """Different quota_total per key should produce different used values."""
        now = int(time.time())
        resets_at = now - 2 * 3600 + 5 * 3600

        cache = {
            "ours": _make_cache_entry([_make_window("5-hour", 50, resets_at, 5)]),
            "friend": _make_cache_entry([_make_window("5-hour", 50, resets_at, 5)]),
        }

        result = extract_quota_windows(
            cache,
            burn_rate=100.0,
            quota_total={"ours": 1_000_000, "friend": 500_000},
        )
        assert len(result) == 2
        used_values = sorted(r[0] for r in result)
        assert used_values[0] == pytest.approx(250_000, rel=1e-3)  # 50% of 500k
        assert used_values[1] == pytest.approx(500_000, rel=1e-3)  # 50% of 1M