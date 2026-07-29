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
    # Mock _LIVE_ROUTER on the proxy module.
    # NOTE: select_failover returns ((provider, model), (fallback, model)) —
    # a tuple of tuples (P3.4 corrected contract; the old gate wrongly treated
    # the inner (provider, model) tuple as the provider string).
    mock_router = MagicMock()
    mock_router.select_failover.return_value = (
        ("ollama_cloud", "glm-5.2"), ("ppq", None))
    mock_router.last_pace_mults = {"ollama_cloud": 1.0}
    proxy._LIVE_ROUTER = mock_router

    # Mock health checks — both keys unhealthy (exhausted)
    proxy._is_key_healthy = MagicMock(return_value=False)

    # Mock quota snapshot helpers (they use quota_cache + lock internally)
    proxy._snapshot_quota = MagicMock(return_value={"ours": {"used_pct": 100}})
    proxy._snapshot_health = MagicMock(return_value={
        "ours": False, "friend": False, "ollama_cloud": True
    })
    proxy._is_peak_hour = MagicMock(return_value=False)

    # Mock decision logging so tests never touch the real usage DB
    proxy._log_key_decision = MagicMock()
    proxy._log_live_decision = MagicMock()

    # Call best_key — should go through Phases 1-4, find both keys dead,
    # then hit the LiveRouter failover path (via _consult_live_router)
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
    """If LiveRouter.select_failover returns ((None, None), (None, None)),
    best_key() should return None so the hardcoded failover chain runs."""
    mock_router = MagicMock()
    mock_router.select_failover.return_value = ((None, None), (None, None))
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
    """The kill switch path ~/.hermes/bot/.enable_live_routing must be
    referenced by the LiveRouter consultation logic. P3.4 centralised the
    kill-switch check into _consult_live_router (shared by the best_key()
    gate AND the retry-loop terminal fallback), so we inspect that helper."""
    expected = os.path.expanduser("~/.hermes/bot/.enable_live_routing")
    import inspect
    # _consult_live_router is the single LiveRouter entry point (P3.4).
    source = inspect.getsource(proxy._consult_live_router)
    assert ".enable_live_routing" in source, \
        "_consult_live_router should reference .enable_live_routing"
    assert proxy._LIVE_ROUTING_FLAG == expected, \
        f"Expected kill-switch path {expected}, got {proxy._LIVE_ROUTING_FLAG!r}"


# ── P3.4 Fix 1 (retry-loop bypass) + Fix 2 (routing_live_decisions) ─────────
#
# _consult_live_router() is the single entry point that BOTH the best_key()
# Phase 5 gate AND the request-handler retry-loop terminal fallback call. The
# retry loop is the real PRODUCTION dual-exhaustion path: best_key()'s health
# cache lags the actual 429, so it returns a key that 429s mid-request, the
# loop exhausts both keys, and (pre-P3.4) fell straight to the hardcoded
# ollama->external chain — bypassing LiveRouter entirely (841 events/2h,
# 0 live events). These tests pin the wiring fix + the new decisions table.

import json
import sqlite3


@pytest.fixture
def consult_env(proxy, tmp_path):
    """Wire a mock LiveRouter + isolated kill-switch flag + isolated usage DB.

    Patches proxy._LIVE_ROUTING_FLAG to a temp path (so the real production
    flag is NEVER touched) and proxy._usage_db to a temp SQLite file so the
    routing_live_decisions writes are isolated. Yields (proxy, mock_router,
    conn, flag_path).
    """
    mock_router = MagicMock()
    mock_router.select_failover.return_value = (
        ("deepinfra", "deepseek-ai/DeepSeek-V4-Pro"),
        ("ppq", "deepseek/deepseek-v4-pro"),
    )
    mock_router.last_pace_mults = {"deepinfra": 1.0, "ppq": 1.2}
    proxy._LIVE_ROUTER = mock_router

    # Isolated kill-switch flag (default ON)
    flag = tmp_path / ".enable_live_routing"
    flag.write_text("1")
    proxy._LIVE_ROUTING_FLAG = str(flag)

    # Deterministic snapshots (values don't matter — select is mocked)
    proxy._snapshot_quota = MagicMock(return_value={"ours": {"used_pct": 100.0}})
    proxy._snapshot_health = MagicMock(
        return_value={"ours": False, "friend": False})
    proxy._is_peak_hour = MagicMock(return_value=False)
    proxy._pace_windows = {}

    # Isolated usage DB so _log_live_decision writes land in a temp file.
    # NOTE: _log_live_decision is left REAL so Fix 2 (table + row) is exercised.
    db_path = tmp_path / "usage.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None,
                           check_same_thread=False)
    # Pre-create the live-decisions table so count assertions work even when
    # _consult_live_router returns early (no pick) and never logs.
    conn.execute(proxy._ROUTING_LIVE_DECISIONS_SQL)
    proxy._usage_db = lambda: conn
    yield proxy, mock_router, conn, flag
    conn.close()


def test_consult_picks_provider_on_dual_exhaustion(consult_env):
    """(a) The retry-loop terminal fallback calls _consult_live_router and gets
    back a real provider to route to (previously bypassed)."""
    proxy, mock_router, _, _ = consult_env
    pick, model, fb, fb_model = proxy._consult_live_router()
    assert pick == "deepinfra"
    assert model == "deepseek-ai/DeepSeek-V4-Pro"
    assert fb == "ppq"
    mock_router.select_failover.assert_called_once()


def test_consult_pick_is_string_not_tuple(consult_env):
    """Regression for the latent tuple-unpack bug: the provider must be a bare
    STRING, not a (provider, model) tuple (the old gate returned the tuple)."""
    proxy, _, _, _ = consult_env
    pick, *_ = proxy._consult_live_router()
    assert isinstance(pick, str)


def test_consult_logs_live_decision_row(consult_env):
    """Fix 2: each live engagement writes a row to routing_live_decisions
    (the new table) with the pace multipliers LiveRouter used."""
    proxy, _, conn, _ = consult_env
    proxy._consult_live_router()
    row = conn.execute(
        "SELECT live_provider, live_model, shadow_provider, pace_mults "
        "FROM routing_live_decisions ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row[0] == "deepinfra"
    assert row[1] == "deepseek-ai/DeepSeek-V4-Pro"
    assert row[2] == "ppq"          # fallback provider (shadow_provider col)
    assert row[3] is not None       # pace_mults JSON
    assert "deepinfra" in json.loads(row[3])


def test_consult_no_pick_no_log(consult_env):
    """When LiveRouter finds no viable provider, nothing is logged."""
    proxy, mock_router, conn, _ = consult_env
    mock_router.select_failover.return_value = ((None, None), (None, None))
    before = conn.execute(
        "SELECT COUNT(*) FROM routing_live_decisions").fetchone()[0]
    pick, *_ = proxy._consult_live_router()
    assert pick is None
    after = conn.execute(
        "SELECT COUNT(*) FROM routing_live_decisions").fetchone()[0]
    assert after == before


def test_consult_kill_switch_off_disables(consult_env):
    """(b) Flag absent -> no consultation, no logging, instant revert to the
    hardcoded chain. Acceptance criterion 3."""
    proxy, mock_router, conn, flag = consult_env
    flag.unlink()  # remove the kill-switch flag
    pick, model, fb, fb_model = proxy._consult_live_router()
    assert pick is None and model is None
    mock_router.select_failover.assert_not_called()


def test_consult_exception_safe_fallthrough(consult_env):
    """(c) Any LiveRouter failure degrades to (None,...) — the caller then
    falls through to the hardcoded chain. LiveRouter failures must NEVER break
    routing. Acceptance criterion 4."""
    proxy, mock_router, _, _ = consult_env
    mock_router.select_failover.side_effect = RuntimeError("boom")
    pick, model, fb, fb_model = proxy._consult_live_router()
    assert pick is None and model is None


def test_consult_router_none_disables(proxy):
    """_LIVE_ROUTER is None (import failed at startup) -> safe no-op."""
    proxy._LIVE_ROUTER = None
    pick, *_ = proxy._consult_live_router()
    assert pick is None


def test_retry_loop_terminal_calls_consult(proxy):
    """The retry-loop terminal fallback source must call _consult_live_router
    (the previously-bypassed path). Source-level check pins the wiring."""
    import inspect
    # _proxy is the request handler; find the retry-loop terminal block.
    src = inspect.getsource(proxy.Handler._proxy)
    assert "_consult_live_router()" in src, \
        "retry-loop terminal fallback must consult LiveRouter (P3.4 Fix 1)"
    # The LiveRouter pick must be routed via the external handlers, honouring
    # LiveRouter's choice via the `preferred` kwarg (not silently overridden).
    assert "preferred=_pick" in src, \
        "LiveRouter pick must be routed with preferred= (honour the choice)"