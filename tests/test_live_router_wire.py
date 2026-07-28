"""Tests for LiveRouter failover integration in zai_proxy.py (Phase 1.2).

Verifies:
  1. LiveRouter is called when .enable_live_routing file exists AND both
     z.ai keys are exhausted (chosen is None after Phase 4 health check).
  2. LiveRouter is NOT called when the kill-switch file does not exist
     (normal routing unchanged — best_key returns None, old behavior).
  3. LiveRouter exception falls through to hardcoded failover (returns None).
  4. Normal best_key() path (ours/friend available) is COMPLETELY UNCHANGED
     — LiveRouter is never called when a key is available.

These tests import zai_proxy.py as a module and mock the LiveRouter and
helper functions to verify the wiring without making real HTTP calls.
"""
from __future__ import annotations

import os
import sys
import tempfile
import importlib
import importlib.util
from unittest.mock import patch, MagicMock

import pytest

# ── Path setup ───────────────────────────────────────────────────────────────
# zai_proxy.py lives in ~/.hermes/bot/ — outside the repo. We load it as a
# module so we can test best_key() in isolation.

_PROXY_PATH = os.path.expanduser("~/.hermes/bot/zai_proxy.py")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ensure merchant-routing-engine is on sys.path so `from src.live_router import
# LiveRouter` works inside zai_proxy.py
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _load_proxy_module():
    """Load zai_proxy.py as a module (fresh each call to avoid state leakage)."""
    spec = importlib.util.spec_from_file_location("zai_proxy_test", _PROXY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def proxy():
    """Load a fresh copy of zai_proxy module."""
    return _load_proxy_module()


@pytest.fixture
def enable_flag():
    """Create the kill-switch flag file and clean up after."""
    flag_path = os.path.expanduser("~/.hermes/bot/.enable_live_routing")
    # Ensure it doesn't exist before
    try:
        os.remove(flag_path)
    except FileNotFoundError:
        pass
    # Create it
    with open(flag_path, "w") as f:
        f.write("1")
    yield flag_path
    # Clean up
    try:
        os.remove(flag_path)
    except FileNotFoundError:
        pass


@pytest.fixture
def no_enable_flag():
    """Ensure the kill-switch flag file does NOT exist."""
    flag_path = os.path.expanduser("~/.hermes/bot/.enable_live_routing")
    try:
        os.remove(flag_path)
    except FileNotFoundError:
        pass
    yield flag_path
    try:
        os.remove(flag_path)
    except FileNotFoundError:
        pass


# ── Test 1: LiveRouter called when both keys exhausted + kill switch ON ─────


def test_live_router_called_when_both_keys_exhausted_and_flag_enabled(
    proxy, enable_flag
):
    """When both z.ai keys are dead AND .enable_live_routing exists,
    best_key() should call LiveRouter.select_failover and return its result."""
    # Mock _LIVE_ROUTER on the proxy module
    mock_router = MagicMock()
    mock_router.select_failover.return_value = ("ollama_cloud", "ppq")
    proxy._LIVE_ROUTER = mock_router

    # Mock health checks — both keys unhealthy (exhausted)
    proxy._is_key_healthy = MagicMock(return_value=False)

    # Mock quota snapshot helpers (they use quota_cache + lock internally)
    proxy._snapshot_quota = MagicMock(return_value={"ours": {"used_pct": 100}})
    proxy._snapshot_health = MagicMock(return_value={
        "ours": False, "friend": False, "ollama_cloud": True
    })
    proxy._is_peak_hour = MagicMock(return_value=False)

    # Mock _log_key_decision so it doesn't touch the DB
    proxy._log_key_decision = MagicMock()

    # Call best_key — should go through Phases 1-4, find both keys dead,
    # then hit the LiveRouter failover path
    result = proxy.best_key()

    # Verify LiveRouter was called
    assert mock_router.select_failover.called, \
        "LiveRouter.select_failover should be called when both keys exhausted"
    assert result == "ollama_cloud", \
        f"Expected 'ollama_cloud', got {result!r}"


# ── Test 2: LiveRouter NOT called when kill switch is OFF ────────────────────


def test_live_router_not_called_when_flag_disabled(proxy, no_enable_flag):
    """When .enable_live_routing does NOT exist, best_key() should NOT call
    LiveRouter even if both keys are exhausted. Returns None (old behavior)."""
    mock_router = MagicMock()
    mock_router.select_failover.return_value = ("ollama_cloud", "ppq")
    proxy._LIVE_ROUTER = mock_router

    # Both keys unhealthy
    proxy._is_key_healthy = MagicMock(return_value=False)
    proxy._snapshot_quota = MagicMock(return_value={})
    proxy._snapshot_health = MagicMock(return_value={})
    proxy._is_peak_hour = MagicMock(return_value=False)
    proxy._log_key_decision = MagicMock()

    result = proxy.best_key()

    # LiveRouter should NOT have been called
    assert not mock_router.select_failover.called, \
        "LiveRouter should NOT be called when kill switch is off"
    assert result is None, \
        f"Expected None (old behavior), got {result!r}"


# ── Test 3: LiveRouter exception falls through to hardcoded failover ─────────


def test_live_router_exception_falls_through(proxy, enable_flag):
    """If LiveRouter.select_failover raises, best_key() should catch it
    and return None so the hardcoded failover chain runs."""
    mock_router = MagicMock()
    mock_router.select_failover.side_effect = RuntimeError("boom")
    proxy._LIVE_ROUTER = mock_router

    proxy._is_key_healthy = MagicMock(return_value=False)
    proxy._snapshot_quota = MagicMock(return_value={})
    proxy._snapshot_health = MagicMock(return_value={})
    proxy._is_peak_hour = MagicMock(return_value=False)
    proxy._log_key_decision = MagicMock()

    result = proxy.best_key()

    # LiveRouter was called but raised — should fall through to None
    assert mock_router.select_failover.called, "LiveRouter should have been attempted"
    assert result is None, \
        f"Expected None (fallback to hardcoded chain), got {result!r}"


# ── Test 4: Normal routing path UNCHANGED ────────────────────────────────────


def test_normal_routing_unchanged_when_keys_available(proxy, enable_flag):
    """When at least one z.ai key is healthy, best_key() should return
    that key. LiveRouter should NOT be called at all — even with the kill
    switch enabled."""
    mock_router = MagicMock()
    proxy._LIVE_ROUTER = mock_router

    # Mock quota_cache so _best_unlocked / proactive path can work
    proxy.quota_cache = {
        "ours": ([{"window": "5-hour", "used_pct": 10}], 0.0),
        "friend": ([{"window": "5-hour", "used_pct": 20}], 0.0),
    }

    # Both keys healthy
    proxy._is_key_healthy = MagicMock(return_value=True)
    proxy._log_key_decision = MagicMock()

    # Mock the prediction helpers to return None (no exhaustion predicted)
    proxy._get_predictions = MagicMock(return_value={"exhausts_in_hours": None})
    proxy._will_exhaust = MagicMock(return_value=None)

    result = proxy.best_key()

    # Should return a z.ai key, NOT call LiveRouter
    assert result in ("ours", "friend"), \
        f"Expected 'ours' or 'friend', got {result!r}"
    assert not mock_router.select_failover.called, \
        "LiveRouter must NOT be called when keys are available"


def test_normal_routing_unchanged_friend_available(proxy, enable_flag):
    """When our key is unhealthy but friend is healthy, best_key() should
    return 'friend'. LiveRouter should NOT be called."""
    mock_router = MagicMock()
    proxy._LIVE_ROUTER = mock_router

    proxy.quota_cache = {
        "ours": ([{"window": "5-hour", "used_pct": 100}], 0.0),
        "friend": ([{"window": "5-hour", "used_pct": 30}], 0.0),
    }

    # Our key unhealthy, friend healthy
    def mock_healthy(name):
        return name == "friend"
    proxy._is_key_healthy = MagicMock(side_effect=mock_healthy)
    proxy._log_key_decision = MagicMock()
    proxy._get_predictions = MagicMock(return_value={"exhausts_in_hours": None})
    proxy._will_exhaust = MagicMock(return_value=None)

    result = proxy.best_key()

    assert result == "friend", f"Expected 'friend', got {result!r}"
    assert not mock_router.select_failover.called


# ── Test 5: _LIVE_ROUTER is None at startup when import fails ────────────────


def test_live_router_none_when_import_fails(proxy):
    """If the LiveRouter import fails at startup, _LIVE_ROUTER should be None.
    best_key() should still work normally (returns None when both dead)."""
    # _LIVE_ROUTER should be None if import failed (which it does in test
    # env because no DB path / converged rates)
    # We explicitly set it to None to simulate import failure
    proxy._LIVE_ROUTER = None
    proxy._is_key_healthy = MagicMock(return_value=False)
    proxy._log_key_decision = MagicMock()

    result = proxy.best_key()
    assert result is None, "Should return None when _LIVE_ROUTER is None"


# ── Test 6: LiveRouter returns None provider → falls through ─────────────────


def test_live_router_returns_none_provider_falls_through(proxy, enable_flag):
    """If LiveRouter.select_failover returns (None, None), best_key() should
    return None so the hardcoded failover chain runs."""
    mock_router = MagicMock()
    mock_router.select_failover.return_value = (None, None)
    proxy._LIVE_ROUTER = mock_router

    proxy._is_key_healthy = MagicMock(return_value=False)
    proxy._snapshot_quota = MagicMock(return_value={})
    proxy._snapshot_health = MagicMock(return_value={})
    proxy._is_peak_hour = MagicMock(return_value=False)
    proxy._log_key_decision = MagicMock()

    result = proxy.best_key()

    assert mock_router.select_failover.called
    assert result is None, \
        f"Expected None (LiveRouter had no provider), got {result!r}"


# ── Test 7: Kill switch file path is correct ─────────────────────────────────


def test_kill_switch_path_constant(proxy):
    """The kill switch path should be ~/.hermes/bot/.enable_live_routing"""
    # The proxy module should reference this path
    expected = os.path.expanduser("~/.hermes/bot/.enable_live_routing")
    # Check that the module has the constant or uses the right path
    # We verify by checking the source
    import inspect
    source = inspect.getsource(proxy.best_key)
    assert ".enable_live_routing" in source, \
        "best_key() source should reference .enable_live_routing"