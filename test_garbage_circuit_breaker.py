"""Garbage circuit-breaker tests — per-(provider, model) demotion (t_b7725426).

Covers the detector + breaker helpers in zai_proxy and the candidate filter in
flat_router.select_provider():
  * trips on degenerate oversized completions (ceiling / tokens-per-sec)
  * demotes ONLY the (provider, model) pair — sibling lanes unaffected
  * select_provider excludes a demoted pair, keeps provider for other models
  * exactly ONE alert per trip; auto-re-arm after expiry
  * cold-start optimistic (never blocks routing); enable-flag off disables
"""

import os
import sys
import time
from unittest.mock import patch

import pytest

# ── Path setup (repo copy: derived from this file's location) ───────────────
# Works both in a repo checkout (this file at repo root, zai_proxy.py in
# production/, Kalman modules in src/) and in the deployed-host layout
# (~/.hermes/bot/ siblings, Kalman modules from ~/merchant-routing-engine/src).
# Entries added later take import priority, so the repo layout wins here.
_HERE = os.path.dirname(os.path.abspath(__file__))
for p in [
    os.path.expanduser("~/.hermes/bot"),
    os.path.expanduser("~/merchant-routing-engine"),
    os.path.join(os.path.expanduser("~/merchant-routing-engine"), "src"),
    _HERE,
    os.path.join(_HERE, "src"),
    os.path.join(_HERE, "production"),
]:
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

# ── Pin the zai_proxy under test (same recipe as test_flat_router) ──────────
# flat_router.py's path bootstrap inserts production/ at sys.path[0], so a bare
# `import zai_proxy` would resolve to whatever production copy is present. Load
# it by explicit path and register in sys.modules BEFORE importing flat_router,
# so flat_router's lazy _resolve gets the same module instance the tests mutate.
import importlib.util as _ilu
_ZAI_PROXY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "production", "zai_proxy.py")
if not os.path.exists(_ZAI_PROXY_FILE):
    # deployed-host layout fallback
    _ZAI_PROXY_FILE = os.path.join(os.path.expanduser("~/.hermes/bot"),
                                   "zai_proxy.py")
_zai_proxy_spec = _ilu.spec_from_file_location("zai_proxy", _ZAI_PROXY_FILE)
_zai_proxy = _ilu.module_from_spec(_zai_proxy_spec)
sys.modules["zai_proxy"] = _zai_proxy
_zai_proxy_spec.loader.exec_module(_zai_proxy)

zg = sys.modules["zai_proxy"]  # alias to the module under test

from flat_router import select_provider, PROVIDER_MODELS  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_breaker_state():
    """Reset breaker state + force-parse env on each test for determinism.

    Also restores GARBAGE_* env defaults so tests don't inherit outer config.
    """
    old_env = {
        k: os.environ.get(k)
        for k in ("GARBAGE_CB_ENABLED", "GARBAGE_MAX_COMPLETION_TOKENS",
                  "GARBAGE_MAX_TOKENS_PER_SEC", "GARBAGE_CB_TTL_SECONDS")
    }
    os.environ["GARBAGE_CB_ENABLED"] = "1"
    os.environ["GARBAGE_MAX_COMPLETION_TOKENS"] = "32000"
    os.environ["GARBAGE_MAX_TOKENS_PER_SEC"] = "0"
    os.environ["GARBAGE_CB_TTL_SECONDS"] = "86400"
    # Re-read the module-level config from env so the fixture takes effect.
    zg._GARBAGE_CB_ENABLED = True
    zg._GARBAGE_MAX_COMPLETION_TOKENS = 32000
    zg._GARBAGE_MAX_TOKENS_PER_SEC = 0.0
    zg.GARBAGE_CB_TTL_SECONDS = 86400.0
    zg._garbage_cb.clear()
    zg._garbage_cb_alerted.clear()
    zg._garbage_cb_last.clear()
    yield
    zg._garbage_cb.clear()
    zg._garbage_cb_alerted.clear()
    zg._garbage_cb_last.clear()
    for k, v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ── Detector: oversized completion trips the breaker ────────────────────────

class TestDetectorTripsOnCeiling:
    def test_oversized_completion_demotes_pair(self):
        # A degenerate 77k-token completion (incident-scale) must trip the breaker.
        trip = zg._check_garbage_cb(
            "neuralwatt", "glm-5.3", completion_tokens=77_366,
            duration_ms=727_000)
        assert trip is True
        assert zg._garbage_cb_pair_demoted("neuralwatt", "glm-5.3") is True

    def test_small_completion_does_not_trip(self):
        trip = zg._check_garbage_cb(
            "neuralwatt", "glm-5.3", completion_tokens=1_000,
            duration_ms=5_000)
        assert trip is False
        assert zg._garbage_cb_pair_demoted("neuralwatt", "glm-5.3") is False

    def test_zero_or_missing_tokens_never_trips(self):
        assert zg._check_garbage_cb("neuralwatt", "glm-5.3",
                                    completion_tokens=0) is False
        assert zg._check_garbage_cb("neuralwatt", "glm-5.3",
                                    completion_tokens=-5) is False


# ── Sibling lanes on the same provider stay healthy ─────────────────────────

class TestSiblingLanesUnaffected:
    def test_sibling_models_remain_eligible(self):
        zg._check_garbage_cb("neuralwatt", "glm-5.3", completion_tokens=77_366)
        # Only the (neuralwatt, glm-5.3) pair is demoted.
        assert zg._garbage_cb_pair_demoted("neuralwatt", "glm-5.3") is True
        assert zg._garbage_cb_pair_demoted("neuralwatt", "glm-5.2") is False
        assert zg._garbage_cb_pair_demoted("neuralwatt",
                                           "deepseek/deepseek-v4-flash") is False
        assert zg._garbage_cb_pair_demoted("neuralwatt", "kimi-k3") is False
        # Unrelated providers untouched.
        assert zg._garbage_cb_pair_demoted("deepinfra", "glm-5.3") is False
        assert zg._garbage_cb_pair_demoted("ollama_cloud", "glm-5.3") is False


# ── select_provider excludes the demoted pair only ──────────────────────────

class TestSelectProviderExcludesDemotedPair:
    def test_glm52_neuralwatt_removed_while_provider_kept_for_others(self):
        # NOTE: patch A already removed glm-5.3 from neuralwatt's PROVIDER_MODELS
        # (it no longer lists glm-5.3 at all). To exercise the general breaker,
        # demote the still-served (neuralwatt, glm-5.2) pair.
        zg._check_garbage_cb("neuralwatt", "glm-5.2", completion_tokens=77_366)
        names_for_glm52 = [c.name for c in select_provider(model="glm-5.2")]
        assert "neuralwatt" not in names_for_glm52, \
            "neuralwatt must be removed from glm-5.2 rotation when (nw, glm-5.2) demoted"
        # The same provider still serves its OTHER models (kimi-k3 is also on
        # neuralwatt; and it was never demoted).
        names_for_kimi = [c.name for c in select_provider(model="kimi-k3")]
        assert "neuralwatt" in names_for_kimi, \
            "neuralwatt must stay eligible for kimi-k3 after glm-5.2 demotion"

    def test_no_demotion_keeps_normal_candidates(self):
        names = [c.name for c in select_provider(model="glm-5.2")]
        assert "neuralwatt" in names


# ── Exactly one alert per trip; re-arm after expiry ─────────────────────────

class TestAlertOnceAndRearm:
    def test_alert_fires_once_per_trip(self):
        with patch.object(zg, "_log_anomaly") as mock_alert:
            zg._check_garbage_cb("neuralwatt", "glm-5.3", 77_366)
            zg._check_garbage_cb("neuralwatt", "glm-5.3", 77_366)  # repeat, still demoted
            zg._check_garbage_cb("neuralwatt", "glm-5.3", 90_000)  # bigger, still demoted
        garb_alerts = [
            c for c in mock_alert.call_args_list
            if c.args and len(c.args) > 1 and c.args[1] == "GARBAGE_CIRCUIT_BREAKER"
        ]
        assert len(garb_alerts) == 1, \
            f"expected exactly one alert per trip, got {len(garb_alerts)}"

    def test_rearm_after_expiry_fires_again(self):
        zg.GARBAGE_CB_TTL_SECONDS = 0.001  # 1ms — expires almost immediately
        with patch.object(zg, "_log_anomaly") as mock_alert:
            zg._check_garbage_cb("neuralwatt", "glm-5.3", 77_366)
            time.sleep(0.01)
            # Expired → pair no longer demoted.
            assert zg._garbage_cb_pair_demoted("neuralwatt", "glm-5.3") is False
            zg._check_garbage_cb("neuralwatt", "glm-5.3", 77_366)  # new trip
        garb_alerts = [
            c for c in mock_alert.call_args_list
            if c.args and len(c.args) > 1 and c.args[1] == "GARBAGE_CIRCUIT_BREAKER"
        ]
        assert len(garb_alerts) == 2, \
            "a trip after expiry is a NEW trip and must re-alert"


# ── Tokens-per-second trigger ───────────────────────────────────────────────

class TestTokensPerSecTrigger:
    def test_pathological_rate_trips_when_configured(self):
        zg._GARBAGE_MAX_TOKENS_PER_SEC = 1000.0
        # 200k tokens in 60s = 3333 tok/s > 1000 → trip.
        trip = zg._check_garbage_cb(
            "neuralwatt", "glm-5.3", completion_tokens=200_000,
            duration_ms=60_000)
        assert trip is True
        assert zg._garbage_cb_pair_demoted("neuralwatt", "glm-5.3") is True

    def test_rate_trigger_disabled_by_default(self):
        # Default GARBAGE_MAX_TOKENS_PER_SEC=0 → the t/s clause is inert; only
        # the absolute ceiling applies.
        zg._GARBAGE_MAX_TOKENS_PER_SEC = 0.0
        zg._GARBAGE_MAX_COMPLETION_TOKENS = 32000
        # 10k tokens over 1s (fast but well under ceiling) → NOT tripped.
        assert zg._check_garbage_cb(
            "neuralwatt", "glm-5.3", completion_tokens=10_000,
            duration_ms=1_000) is False


# ── Enable flag + cold-start optimism ───────────────────────────────────────

class TestGuardrails:
    def test_disabled_flag_never_demotes(self):
        zg._GARBAGE_CB_ENABLED = False
        assert zg._check_garbage_cb("neuralwatt", "glm-5.3", 77_366) is False
        assert zg._garbage_cb_pair_demoted("neuralwatt", "glm-5.3") is False

    def test_cold_start_is_optimistic(self):
        # Fresh (empty) state never blocks routing.
        assert zg._garbage_cb_pair_demoted("neuralwatt", "glm-5.2") is False
        names = [c.name for c in select_provider(model="glm-5.2")]
        assert "neuralwatt" in names

    def test_demotion_is_timer_based_not_permanent(self):
        zg.GARBAGE_CB_TTL_SECONDS = 1.0
        zg._check_garbage_cb("neuralwatt", "glm-5.3", 77_366)
        assert zg._garbage_cb_pair_demoted("neuralwatt", "glm-5.3") is True
        time.sleep(1.1)
        assert zg._garbage_cb_pair_demoted("neuralwatt", "glm-5.3") is False
