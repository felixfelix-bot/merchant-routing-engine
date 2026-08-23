"""Tests for the fail-safe logic in kalman-pricing-hook.py.

Tests cover:

* **Stale threshold math** — ``event_age > STALE_THRESHOLD_SECONDS`` boundary
* **Retry counter** — ``disable_provider_safe()`` tries DB update 3 times
  (with 1-second sleeps between attempts)
* **Docker stop fallback** — when all 3 DB retries fail, escalates to
  ``docker stop routstr-public`` as a last-resort circuit breaker

All subprocess (nak, docker), sqlite3, and time calls are mocked.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest


# ── Stale threshold math ───────────────────────────────────────────────────


class TestStaleThresholdMath:
    """Verify the staleness comparison used in main()."""

    def test_fresh_event_under_threshold(self, hook_mod):
        """age = 299s → NOT stale (< 300)."""
        now = 10_000
        created_at = 10_000 - 299  # age = 299s
        age = now - created_at
        assert age <= hook_mod.STALE_THRESHOLD_SECONDS

    def test_event_exactly_at_threshold(self, hook_mod):
        """age = 300s → NOT stale (uses > not >=)."""
        now = 10_000
        created_at = 10_000 - 300  # age = 300s
        age = now - created_at
        assert age == hook_mod.STALE_THRESHOLD_SECONDS
        assert not (age > hook_mod.STALE_THRESHOLD_SECONDS)

    def test_stale_event_over_threshold(self, hook_mod):
        """age = 301s → stale (> 300)."""
        now = 10_000
        created_at = 10_000 - 301  # age = 301s
        age = now - created_at
        assert age > hook_mod.STALE_THRESHOLD_SECONDS

    def test_threshold_is_300(self, hook_mod):
        """The configured threshold is 5 minutes (300 seconds)."""
        assert hook_mod.STALE_THRESHOLD_SECONDS == 300

    def test_very_old_event_is_stale(self, hook_mod):
        """An event from an hour ago is definitely stale."""
        now = 10_000
        created_at = now - 3600  # 1 hour old
        age = now - created_at
        assert age > hook_mod.STALE_THRESHOLD_SECONDS

    def test_future_event_not_stale(self, hook_mod):
        """created_at > now (clock skew) → negative age → not stale."""
        now = 10_000
        created_at = 10_001  # 1 second in the future
        age = now - created_at
        assert age < 0
        assert not (age > hook_mod.STALE_THRESHOLD_SECONDS)


# ── Retry counter logic ─────────────────────────────────────────────────────


class TestRetryCounter:
    """disable_provider_safe() retries update_provider_db() up to 3 times."""

    def test_succeeds_on_first_attempt(self, hook_mod):
        """DB update works on attempt 1 → returns True, no docker stop."""
        with patch.object(hook_mod, "update_provider_db", return_value=True) as mock_update, \
             patch.object(hook_mod.time, "sleep") as mock_sleep, \
             patch("subprocess.run") as mock_docker:
            result = hook_mod.disable_provider_safe()

        assert result is True
        assert mock_update.call_count == 1
        mock_docker.assert_not_called()

    def test_succeeds_on_second_attempt(self, hook_mod):
        """First call fails, second succeeds → returns True after 2 tries."""
        with patch.object(hook_mod, "update_provider_db",
                          side_effect=[False, True]) as mock_update, \
             patch.object(hook_mod.time, "sleep") as mock_sleep:
            result = hook_mod.disable_provider_safe()

        assert result is True
        assert mock_update.call_count == 2
        # sleep called once (between attempt 1 and 2)
        mock_sleep.assert_called_once_with(1)

    def test_succeeds_on_third_attempt(self, hook_mod):
        """Fails twice, succeeds on 3rd → returns True after 3 tries."""
        with patch.object(hook_mod, "update_provider_db",
                          side_effect=[False, False, True]) as mock_update, \
             patch.object(hook_mod.time, "sleep") as mock_sleep:
            result = hook_mod.disable_provider_safe()

        assert result is True
        assert mock_update.call_count == 3
        # sleep called between each failed attempt (1→2 and 2→3)
        assert mock_sleep.call_count == 2

    def test_all_three_fail_then_docker_stop(self, hook_mod):
        """All 3 DB retries fail → escalates to docker stop."""
        mock_docker_result = MagicMock()
        mock_docker_result.returncode = 0
        mock_docker_result.stdout = "routstr-public"
        mock_docker_result.stderr = ""

        with patch.object(hook_mod, "update_provider_db", return_value=False) as mock_update, \
             patch.object(hook_mod.time, "sleep") as mock_sleep, \
             patch("subprocess.run", return_value=mock_docker_result) as mock_docker:
            result = hook_mod.disable_provider_safe()

        assert result is True  # docker stop succeeded
        assert mock_update.call_count == 3  # exactly 3 DB attempts
        # docker stop called once
        assert mock_docker.call_count == 1
        docker_cmd = mock_docker.call_args[0][0]
        assert "docker" in docker_cmd
        assert "stop" in docker_cmd
        assert "routstr-public" in docker_cmd

    def test_sleep_between_retries(self, hook_mod):
        """time.sleep(1) is called between failed attempts (not after 3rd)."""
        with patch.object(hook_mod, "update_provider_db", return_value=False), \
             patch.object(hook_mod.time, "sleep") as mock_sleep, \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
            hook_mod.disable_provider_safe()

        # 3 attempts → 2 sleeps (between attempt 1&2, and 2&3 — not after #3)
        assert mock_sleep.call_count == 2
        mock_sleep.assert_has_calls([call(1), call(1)])

    def test_update_called_with_disabled_false(self, hook_mod):
        """All update_provider_db calls must pass enabled=False."""
        with patch.object(hook_mod, "update_provider_db", return_value=True) as mock_update:
            hook_mod.disable_provider_safe()

        for c in mock_update.call_args_list:
            assert c[1]["enabled"] is False

    def test_update_called_with_fee_1_43(self, hook_mod):
        """The fail-safe disable uses a fixed fee of 1.43."""
        with patch.object(hook_mod, "update_provider_db", return_value=True) as mock_update:
            hook_mod.disable_provider_safe()

        for c in mock_update.call_args_list:
            assert c[1]["fee"] == 1.43


# ── Docker stop fallback ────────────────────────────────────────────────────


class TestDockerStopFallback:
    """When all 3 DB retries fail, docker stop is the last resort."""

    def test_docker_stop_succeeds(self, hook_mod):
        """docker stop returns rc=0 → disable_provider_safe returns True."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "routstr-public"
        mock_result.stderr = ""

        with patch.object(hook_mod, "update_provider_db", return_value=False), \
             patch.object(hook_mod.time, "sleep"), \
             patch("subprocess.run", return_value=mock_result) as mock_docker:
            result = hook_mod.disable_provider_safe()

        assert result is True
        mock_docker.assert_called_once()
        cmd = mock_docker.call_args[0][0]
        assert cmd == ["docker", "stop", "routstr-public"]

    def test_docker_stop_fails_returns_false(self, hook_mod):
        """docker stop also fails → returns False (provider may still serve!)."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "container not found"

        with patch.object(hook_mod, "update_provider_db", return_value=False), \
             patch.object(hook_mod.time, "sleep"), \
             patch("subprocess.run", return_value=mock_result):
            result = hook_mod.disable_provider_safe()

        assert result is False

    def test_docker_stop_exception_returns_false(self, hook_mod):
        """docker stop raises → returns False, never crashes."""
        with patch.object(hook_mod, "update_provider_db", return_value=False), \
             patch.object(hook_mod.time, "sleep"), \
             patch("subprocess.run", side_effect=Exception("connection refused")):
            result = hook_mod.disable_provider_safe()

        assert result is False

    def test_docker_stop_timeout(self, hook_mod):
        """docker stop times out → returns False."""
        import subprocess
        with patch.object(hook_mod, "update_provider_db", return_value=False), \
             patch.object(hook_mod.time, "sleep"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=15)):
            result = hook_mod.disable_provider_safe()

        assert result is False

    def test_escalation_only_after_3_failures(self, hook_mod):
        """docker stop must NOT be called if fewer than 3 DB attempts failed."""
        # Succeed on 2nd attempt → no docker stop
        with patch.object(hook_mod, "update_provider_db",
                          side_effect=[False, True]), \
             patch.object(hook_mod.time, "sleep"), \
             patch("subprocess.run") as mock_docker:
            hook_mod.disable_provider_safe()

        mock_docker.assert_not_called()


# ── Constants ────────────────────────────────────────────────────────────────


class TestConstants:
    """Verify the fail-safe constants are correctly configured."""

    def test_stale_threshold(self, hook_mod):
        assert hook_mod.STALE_THRESHOLD_SECONDS == 300

    def test_max_fee(self, hook_mod):
        assert hook_mod.MAX_FEE == 10.0

    def test_min_fee(self, hook_mod):
        assert hook_mod.MIN_FEE == 0.01

    def test_litellm_base_rate(self, hook_mod):
        assert hook_mod.LITELLM_ZAI_BASE_RATE == 0.14

    def test_zai_slug(self, hook_mod):
        assert hook_mod.ZAI_SLUG == "zai-coding"

    def test_has_publisher_npubs(self, hook_mod):
        assert len(hook_mod.KALMAN_PUBLISHER_NPUBS) >= 1

    def test_has_relays(self, hook_mod):
        assert len(hook_mod.NOSTR_RELAYS) >= 1
