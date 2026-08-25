"""Tests for the Nostr kind-30315 Kalman pricing publisher in zai_proxy.py.

Tests cover:

* ``_build_kalman_pricing_json()`` — the JSON that gets published as the
  Nostr event ``content``.  Verifies required fields, availability logic,
  and the zero-price guard.

* ``_nostr_publish_kalman()`` — the background thread that signs + publishes
  the event via ``nak event``.  Verifies the nak command format (kind=30315,
  d=kalman-pricing tag, relays).

All z.ai API responses, quota caches, and subprocess calls are mocked.
No live Nostr relays or z.ai API connections are used.
"""
from __future__ import annotations

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

# zai_proxy is loaded in conftest.py (production/ dir on sys.path).
# Import at module level so patch.object targets the correct module object.
import zai_proxy  # noqa: E402

# ── Helpers ─────────────────────────────────────────────────────────────────


def _patch_all_pricing_deps(overrides: dict | None = None):
    """Context-manager stack that patches every module-level dependency of
    ``_build_kalman_pricing_json()``.

    ``overrides`` can selectively override any mock (e.g. health=True).
    Returns a dict of mock objects for assertions.
    """
    from contextlib import ExitStack

    defaults = dict(
        _is_peak_hour=lambda: False,
        _snapshot_quota=lambda: {"ppq": {"used_pct": 0.0}},
        _snapshot_health=lambda: {"ours": False, "friend": False},
        quota_cache={},
        is_key_locked=lambda key, wins: (False, None, 0.0, 0.0),
        _max_pct=lambda wins: 0.0,
        _converged_rates={"ours": 0.068, "friend": 0.082},
        _rpt_rate=lambda key: 0.068,
        _KEY_COST_MULTIPLIER={"ours": 1.0, "friend": 1.0},
        _get_cached_predictions=lambda key: {},
        _will_exhaust=lambda preds: None,
    )
    if overrides:
        defaults.update(overrides)

    stack = ExitStack()
    mocks: dict[str, MagicMock] = {}
    for attr, val in defaults.items():
        m = stack.enter_context(patch.object(zai_proxy, attr, val))
        mocks[attr] = m
    return stack, mocks


@pytest.fixture
def deps():
    """Default mock dependencies — all keys unavailable."""
    stack, mocks = _patch_all_pricing_deps()
    with stack:
        yield mocks


@pytest.fixture
def deps_available():
    """Mock dependencies with both z.ai keys healthy (available)."""
    stack, mocks = _patch_all_pricing_deps(
        overrides={
            "_snapshot_health": lambda: {"ours": True, "friend": True},
        }
    )
    with stack:
        yield mocks


# ── Event format ─────────────────────────────────────────────────────────────


class TestEventFormat:
    """Verify the published JSON has all required fields."""

    def test_has_zai_available(self, deps):
        result = zai_proxy._build_kalman_pricing_json()
        assert "zai_available" in result
        assert isinstance(result["zai_available"], bool)

    def test_has_effective_price(self, deps):
        result = zai_proxy._build_kalman_pricing_json()
        assert "zai_effective_price_usd_per_m" in result

    def test_has_timestamp(self, deps):
        before = int(time.time())
        result = zai_proxy._build_kalman_pricing_json()
        after = int(time.time())
        assert "timestamp" in result
        assert before <= result["timestamp"] <= after

    def test_has_source_field(self, deps):
        result = zai_proxy._build_kalman_pricing_json()
        assert result.get("source") == "T470"

    def test_has_providers_dict(self, deps):
        result = zai_proxy._build_kalman_pricing_json()
        providers = result.get("providers", {})
        assert "zai_ours" in providers
        assert "zai_friend" in providers
        assert "ppq" in providers

    def test_provider_has_required_fields(self, deps):
        result = zai_proxy._build_kalman_pricing_json()
        ours = result["providers"]["zai_ours"]
        for field in (
            "base_rate_usd_per_m",
            "effective_price_usd_per_m",
            "available",
            "locked",
            "quota_used_pct",
        ):
            assert field in ours, f"missing field {field} in zai_ours"

    def test_has_is_peak_hour(self, deps):
        result = zai_proxy._build_kalman_pricing_json()
        assert "is_peak_hour" in result
        assert result["is_peak_hour"] is False

    def test_json_is_serialisable(self, deps):
        """The content string must round-trip through json.dumps/loads."""
        result = zai_proxy._build_kalman_pricing_json()
        content = json.dumps(result, separators=(",", ":"))
        parsed = json.loads(content)
        assert parsed["zai_available"] == result["zai_available"]

    def test_has_locked_reason_when_unavailable(self, deps):
        result = zai_proxy._build_kalman_pricing_json()
        assert "zai_locked_reason" in result
        # Both keys unavailable → locked_reason should not be None/empty
        assert result["zai_locked_reason"] is not None


# ── Availability logic ───────────────────────────────────────────────────────


class TestAvailability:
    """Tests for the zai_available + effective price computation."""

    def test_unavailable_when_keys_unhealthy(self, deps):
        """When both z.ai keys are unhealthy → zai_available=False, price=None."""
        result = zai_proxy._build_kalman_pricing_json()
        assert result["zai_available"] is False
        assert result["zai_effective_price_usd_per_m"] is None

    def test_unavailable_when_one_key_unhealthy(self):
        """Only one key healthy → that key is available."""
        from contextlib import ExitStack

        stack, _ = _patch_all_pricing_deps(
            overrides={"_snapshot_health": lambda: {"ours": True, "friend": False}}
        )
        with stack:
            result = zai_proxy._build_kalman_pricing_json()
        assert result["zai_available"] is True
        assert result["zai_effective_price_usd_per_m"] is not None
        assert result["zai_effective_price_usd_per_m"] > 0

    def test_available_when_keys_healthy(self, deps_available):
        """Both keys healthy → zai_available=True with a real price."""
        result = zai_proxy._build_kalman_pricing_json()
        assert result["zai_available"] is True
        price = result["zai_effective_price_usd_per_m"]
        assert price is not None
        assert price > 0

    def test_available_price_matches_min_effective(self, deps_available):
        """Published price should be the cheapest available key."""
        result = zai_proxy._build_kalman_pricing_json()
        ours_price = result["providers"]["zai_ours"]["effective_price_usd_per_m"]
        friend_price = result["providers"]["zai_friend"]["effective_price_usd_per_m"]
        expected = min(ours_price, friend_price)
        assert result["zai_effective_price_usd_per_m"] == pytest.approx(expected, rel=1e-6)

    def test_unavailable_when_quota_data_unknown(self):
        """Sentinel windows (name='unknown') force availability to False."""
        from contextlib import ExitStack

        # Put 'unknown' sentinel windows in the cache
        unknown_win = [{"name": "unknown", "used_pct": 0}]
        stack, _ = _patch_all_pricing_deps(
            overrides={
                "_snapshot_health": lambda: {"ours": True, "friend": True},
                "quota_cache": {
                    "ours": (unknown_win, 0.0),
                    "friend": (unknown_win, 0.0),
                },
            }
        )
        with stack:
            result = zai_proxy._build_kalman_pricing_json()
        assert result["zai_available"] is False
        assert result["zai_effective_price_usd_per_m"] is None

    def test_locked_key_not_available(self):
        """A key that is_key_locked()=True should not appear in available_zai."""
        from contextlib import ExitStack

        stack, _ = _patch_all_pricing_deps(
            overrides={
                "_snapshot_health": lambda: {"ours": True, "friend": True},
                "is_key_locked": lambda key, wins: (
                    True,
                    "quota_5h",
                    95.0,
                    90.0,
                ),
            }
        )
        with stack:
            result = zai_proxy._build_kalman_pricing_json()
        assert result["zai_available"] is False
        assert result["zai_effective_price_usd_per_m"] is None

    def test_peak_hour_triples_price(self):
        """Peak-hour multiplier (3×) should increase the effective price."""
        from contextlib import ExitStack

        stack_off, _ = _patch_all_pricing_deps(
            overrides={
                "_snapshot_health": lambda: {"ours": True, "friend": True},
                "_is_peak_hour": lambda: False,
            }
        )
        with stack_off:
            off_peak = zai_proxy._build_kalman_pricing_json()

        stack_on, _ = _patch_all_pricing_deps(
            overrides={
                "_snapshot_health": lambda: {"ours": True, "friend": True},
                "_is_peak_hour": lambda: True,
            }
        )
        with stack_on:
            on_peak = zai_proxy._build_kalman_pricing_json()

        off_p = off_peak["zai_effective_price_usd_per_m"]
        on_p = on_peak["zai_effective_price_usd_per_m"]
        assert on_p == pytest.approx(3.0 * off_p, rel=1e-6)

    def test_unhealthy_key_has_health_multiplier_10(self, deps):
        """Unhealthy keys get health_mult=10 (visible in provider fields)."""
        result = zai_proxy._build_kalman_pricing_json()
        assert result["providers"]["zai_ours"]["health_multiplier"] == 10.0


# ── Zero-price guard ─────────────────────────────────────────────────────────


class TestZeroPriceGuard:
    """The publisher must NOT publish a zero-effective-price as a number.

    When ``zai_eff_price`` evaluates to zero (falsy), the JSON serialiser
    emits ``None`` instead of ``0.0`` — because the code does:

        "zai_effective_price_usd_per_m": round(zai_eff_price, 6) if zai_eff_price else None

    This means the hook side will see ``price is None`` and treat it as
    invalid → disable zai-coding.  This is the publisher-level zero-price guard.
    """

    def test_zero_base_rate_yields_none_price(self):
        """When base rate is 0 → effective price 0 → published as None."""
        from contextlib import ExitStack

        stack, _ = _patch_all_pricing_deps(
            overrides={
                "_snapshot_health": lambda: {"ours": True, "friend": True},
                "_converged_rates": {"ours": 0.0, "friend": 0.0},
                "_rpt_rate": lambda key: 0.0,
            }
        )
        with stack:
            result = zai_proxy._build_kalman_pricing_json()
        # The individual provider's effective price is 0.0 ...
        assert result["providers"]["zai_ours"]["effective_price_usd_per_m"] == 0.0
        # ... but published zai_effective_price is None (falsy guard)
        assert result["zai_effective_price_usd_per_m"] is None


# ── nak event command construction ──────────────────────────────────────────


class TestNakCommand:
    """Verify the nak event command format in _nostr_publish_kalman()."""

    def _run_publisher_once(self, pricing_json: dict) -> list[list[str]]:
        """Run _nostr_publish_kalman for exactly one iteration.

        Mocks subprocess.run, os.path.exists (nak binary), _load_nostr_sec,
        _build_kalman_pricing_json, and time.sleep (raises SystemExit to
        break the infinite while loop).  Returns the list of captured
        subprocess.run call args.
        """
        captured: list[list[str]] = []

        def _fake_run(cmd, *a, **kw):
            captured.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = '{"id":"evt123","pubkey":"abc"}'
            m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=_fake_run), \
             patch("os.path.exists", return_value=True), \
             patch.object(zai_proxy, "_load_nostr_sec", return_value="a" * 64), \
             patch.object(zai_proxy, "_build_kalman_pricing_json", return_value=pricing_json), \
             patch.object(zai_proxy, "_NOSTR_PUBLISH_INTERVAL", 30), \
             patch.object(zai_proxy.time, "sleep", side_effect=SystemExit(0)):
            with pytest.raises(SystemExit):
                zai_proxy._nostr_publish_kalman()

        return captured

    def test_command_has_kind_30315(self):
        captured = self._run_publisher_once(
            {"zai_available": True, "zai_effective_price_usd_per_m": 0.068,
             "timestamp": 1700000000}
        )
        event_cmds = [c for c in captured if "event" in c and "--kind" in c]
        assert len(event_cmds) == 1
        cmd = event_cmds[0]
        idx = cmd.index("--kind")
        assert cmd[idx + 1] == "30315"

    def test_command_has_d_kalman_pricing_tag(self):
        captured = self._run_publisher_once(
            {"zai_available": True, "zai_effective_price_usd_per_m": 0.068,
             "timestamp": 1700000000}
        )
        event_cmds = [c for c in captured if "event" in c]
        cmd_str = " ".join(event_cmds[0])
        assert "d=kalman-pricing" in cmd_str

    def test_command_has_routstr_tag(self):
        captured = self._run_publisher_once(
            {"zai_available": False, "zai_effective_price_usd_per_m": None,
             "timestamp": 1700000000}
        )
        event_cmds = [c for c in captured if "event" in c]
        cmd_str = " ".join(event_cmds[0])
        assert "t=routstr" in cmd_str

    def test_command_content_is_json_with_required_fields(self):
        pricing = {
            "zai_available": True,
            "zai_effective_price_usd_per_m": 0.068,
            "timestamp": 1700000000,
            "source": "T470",
        }
        captured = self._run_publisher_once(pricing)
        event_cmds = [c for c in captured if "event" in c and "--content" in c]
        assert len(event_cmds) == 1
        idx = event_cmds[0].index("--content")
        content_str = event_cmds[0][idx + 1]
        parsed = json.loads(content_str)
        assert "zai_available" in parsed
        assert "zai_effective_price_usd_per_m" in parsed
        assert "timestamp" in parsed

    def test_command_includes_all_relays(self):
        relay_count = len(zai_proxy._NOSTR_RELAYS)
        captured = self._run_publisher_once(
            {"zai_available": False, "zai_effective_price_usd_per_m": None,
             "timestamp": 1700000000}
        )
        event_cmds = [c for c in captured if "event" in c]
        # The relays are appended after --content
        cmd = event_cmds[0]
        content_idx = cmd.index("--content")
        # relays start 2 positions after --content (after the content value)
        relay_args = cmd[content_idx + 2:]
        assert len(relay_args) == relay_count

    def test_env_has_nostr_secret_key(self):
        """The NOSTR_SECRET_KEY env var must be set (not passed as --sec arg)."""
        captured_env: list[dict] = []

        def _fake_run(cmd, *a, **kw):
            captured_env.append(kw.get("env", {}))
            m = MagicMock()
            m.returncode = 0
            m.stdout = '{"id":"evt123"}'
            m.stderr = ""
            return m

        pricing = {"zai_available": True, "zai_effective_price_usd_per_m": 0.068,
                   "timestamp": 1700000000}
        with patch("subprocess.run", side_effect=_fake_run), \
             patch("os.path.exists", return_value=True), \
             patch.object(zai_proxy, "_load_nostr_sec", return_value="b" * 64), \
             patch.object(zai_proxy, "_build_kalman_pricing_json", return_value=pricing), \
             patch.object(zai_proxy, "_NOSTR_PUBLISH_INTERVAL", 30), \
             patch.object(zai_proxy.time, "sleep", side_effect=SystemExit(0)):
            with pytest.raises(SystemExit):
                zai_proxy._nostr_publish_kalman()

        # Skip the "which" calls (they don't pass env kw)
        envs_with_secret = [e for e in captured_env if "NOSTR_SECRET_KEY" in e]
        assert len(envs_with_secret) >= 1
        assert envs_with_secret[0]["NOSTR_SECRET_KEY"] == "b" * 64

    def test_publisher_without_sec_key_returns(self):
        """When the private key file is missing, the publisher returns immediately."""
        with patch.object(zai_proxy, "_load_nostr_sec", return_value=None):
            # Should return without error (and without entering the loop)
            zai_proxy._nostr_publish_kalman()
        # If we reach this assertion, the function returned cleanly


# ── Load-sec helper ──────────────────────────────────────────────────────────


class TestLoadNostrSec:
    def test_returns_none_when_file_missing(self):
        with patch.object(zai_proxy, "_NOSTR_SEC_PATH") as mock_path:
            mock_path.exists.return_value = False
            assert zai_proxy._load_nostr_sec() is None

    def test_returns_sec_when_file_present(self):
        fake_sec = "a" * 64
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = fake_sec
        with patch.object(zai_proxy, "_NOSTR_SEC_PATH", mock_path):
            assert zai_proxy._load_nostr_sec() == fake_sec

    def test_returns_none_on_wrong_length(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = "tooshort"
        with patch.object(zai_proxy, "_NOSTR_SEC_PATH", mock_path):
            assert zai_proxy._load_nostr_sec() is None
