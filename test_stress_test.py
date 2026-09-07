"""TDD tests for the routstr stress-test harness (T-C, ADR-007 Gate 3).

Red phase: these tests must FAIL when run against an empty/absent
stress_test implementation, then go GREEN after stress_test.py lands.

Scenario under test (plan T-C): z.ai at ~90% quota pressure with mixed
internal + sold load. Under that pressure the router must:
  * NEVER block internal requests (no 429).
  * Degrade SOLD requests via 429 + Retry-After once predicted
    exhaustion is within the safety window.
  * Delist an exhausted provider < 5 minutes after it crosses the
    90% threshold.
  * Write ~/.hermes/bot/stress_test_results.json with "status": "pass"
    when every assertion is green over the run.
"""
import json
import os
import tempfile

import stress_test as st


# ── simulated request decisions ──────────────────────────────────────────────

class TestInternalNeverBlocked:
    """Under imminent quota pressure, internal requests must ALWAYS route."""

    def test_internal_routes_under_imminent_pressure(self):
        predictions = [
            {"key": "ours", "window": "weekly", "will_exhaust": True,
             "exhausts_in_hours": 0.5, "used_pct": 92.0},
        ]
        decision = st.simulate_request(caller_class="internal", predictions=predictions)
        assert decision["blocked"] is False, \
            "internal must never be blocked (no 429) under quota pressure"

    def test_internal_never_429_even_at_95pct(self):
        predictions = [
            {"key": "ours", "window": "weekly", "will_exhaust": True,
             "exhausts_in_hours": 0.1, "used_pct": 95.0},
        ]
        decision = st.simulate_request(caller_class="internal", predictions=predictions)
        assert decision["status"] == "routed", \
            f"internal must route, got {decision['status']}"

    def test_internal_not_affected_by_sold_gate(self):
        # Same predictions that WOULD gate sold must leave internal alone.
        predictions = [
            {"key": "routstr", "window": "monthly", "will_exhaust": True,
             "exhausts_in_hours": 1.0, "used_pct": 90.0},
        ]
        assert st.simulate_request("internal", predictions)["blocked"] is False
        assert st.simulate_request("sold", predictions)["blocked"] is True


class TestSoldDegrade429RetryAfter:
    """Sold requests degrade via 429 + Retry-After when exhaustion imminent."""

    def test_sold_gated_when_exhaustion_imminent(self):
        predictions = [
            {"key": "routstr", "window": "monthly", "will_exhaust": True,
             "exhausts_in_hours": 1.0, "used_pct": 90.0},
        ]
        decision = st.simulate_request(caller_class="sold", predictions=predictions)
        assert decision["status"] == "429", \
            f"sold must be gated with 429, got {decision['status']}"
        assert decision["retry_after"] is not None, "429 must carry Retry-After"
        assert decision["retry_after"] > 0

    def test_sold_served_when_exhaustion_far(self):
        predictions = [
            {"key": "routstr", "window": "monthly", "will_exhaust": False,
             "exhausts_in_hours": 24.0, "used_pct": 40.0},
        ]
        decision = st.simulate_request(caller_class="sold", predictions=predictions)
        assert decision["status"] == "routed", \
            "sold must still be served when exhaustion is far out"

    def test_sold_fail_open_when_no_predictions(self):
        decision = st.simulate_request(caller_class="sold", predictions=None)
        assert decision["status"] == "routed", \
            "sold must fail open (route) when the predictor is down"

    def test_sold_uses_safety_window(self):
        predictions = [
            {"key": "routstr", "window": "monthly", "will_exhaust": True,
             "exhausts_in_hours": 1.7, "used_pct": 90.0},
        ]
        # default 2h safety → gated
        assert st.simulate_request("sold", predictions)["blocked"] is True
        # tighter 1h safety → NOT gated (1.7h > 1h)
        assert st.simulate_request(
            "sold", predictions, safety_hours=1.0)["blocked"] is False


# ── delist < 5 min after threshold ───────────────────────────────────────────

class TestDelistTriggersUnder5Min:
    """An exhausted provider must be delisted < 5 min after crossing 90%."""

    def test_delist_decides_true_once_threshold_crossed(self):
        tracker = st.DelistTracker(threshold_pct=90.0, max_delay_s=300)
        t0 = 1_000_000.0
        tracker.record_health(used_pct=89.0, ts=t0)   # below threshold
        assert tracker.should_delist(ts=t0) is False, \
            "no delist while under the 90% threshold"

    def test_delist_fires_within_5min_of_crossing(self):
        tracker = st.DelistTracker(threshold_pct=90.0, max_delay_s=300)
        t0 = 1_000_000.0
        tracker.record_health(used_pct=90.0, ts=t0)   # crosses threshold NOW
        # 4 minutes later → delist must already have been triggered (< 5 min)
        t4 = t0 + 4 * 60
        assert tracker.should_delist(ts=t4) is True, \
            "delist must trigger < 5 min after crossing the threshold"
        # the recorded trigger latency is under the 300s cap
        assert tracker.trigger_latency_s <= 300

    def test_delist_not_before_grace_elapses(self):
        tracker = st.DelistTracker(threshold_pct=90.0, max_delay_s=300)
        t0 = 1_000_000.0
        tracker.record_health(used_pct=91.0, ts=t0)
        t_plus_1min = t0 + 60
        # At 1 min the provider is clearly over threshold and 1min < 5min, so
        # the delist must already have decided True. Key guarantee: < 5 min.
        assert tracker.should_delist(ts=t_plus_1min) is True

    def test_delist_resets_when_quota_recovers(self):
        tracker = st.DelistTracker(threshold_pct=90.0, max_delay_s=300)
        t0 = 1_000_000.0
        tracker.record_health(used_pct=91.0, ts=t0)
        assert tracker.should_delist(ts=t0 + 10) is True
        # Quota resets well below the threshold → no longer over-budget
        tracker.record_health(used_pct=30.0, ts=t0 + 20)
        assert tracker.should_delist(ts=t0 + 25) is False, \
            "recovered quota must clear the delist pending state"


# ── results JSON ────────────────────────────────────────────────────────────

class TestResultsJSON:
    """stress_test must write a pass/fail results JSON at completion."""

    def test_write_results_writes_pass_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "stress_test_results.json")
            summary = {
                "internal_requests": 1000,
                "internal_blocked": 0,
                "sold_requests": 2000,
                "sold_429": 1000,
                "sold_served": 1000,
                "delist_latency_max_s": 180,
                "delist_cap_violation": False,
                "green": True,
                "duration_s": 60,
            }
            st.write_results(summary, out_path=out)
            with open(out) as f:
                data = json.load(f)
            assert data["status"] == "pass"
            assert data["internal_never_blocked"] is True
            assert data["sold_degrade_429_retry_after"] is True
            assert data["delist_triggers_under_5min"] is True

    def test_write_results_writes_fail_marker_when_not_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "stress_test_results.json")
            summary = {
                "internal_requests": 100,
                "internal_blocked": 5,   # violation!
                "sold_requests": 100,
                "sold_429": 100,
                "sold_served": 0,
                "delist_latency_max_s": 180,
                "delist_cap_violation": False,
                "green": False,
                "duration_s": 60,
            }
            st.write_results(summary, out_path=out)
            with open(out) as f:
                data = json.load(f)
            assert data["status"] == "fail"
            assert data["internal_never_blocked"] is False


# ── soak runner smoke (fast) ────────────────────────────────────────────────

class TestSoakRunner:
    def test_run_stress_short_returns_green_summary(self):
        summary = st.run_stress(
            duration_s=0.5,
            pressure_predictions=[
                {"key": "routstr", "window": "monthly", "will_exhaust": True,
                 "exhausts_in_hours": 0.8, "used_pct": 92.0},
            ],
            delist_threshold_pct=90.0,
            request_interval_s=0.0001,
        )
        assert summary["green"] is True, "fast soak run must pass green"
        assert summary["internal_blocked"] == 0
        assert summary["sold_429"] > 0, "pressure phase must gate some sold"
        assert summary["sold_served"] > 0, "healthy phase must serve some sold"
        assert summary["delist_latency_max_s"] <= 300
        assert summary["delist_cap_violation"] is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])


# ── cap enforcement + wall-clock latency (added after cold review) ───────────

class TestDelistCapEnforcement:
    """The <5min cap must be genuinely enforced, not vacuously satisfied."""

    def test_delist_fires_with_real_latency_under_cap(self):
        tracker = st.DelistTracker(threshold_pct=90.0, max_delay_s=300)
        crossed = 100.0
        tracker.record_health(used_pct=91.0, ts=crossed)
        # 4 minutes of real elapsed time later → delist decided, within cap.
        assert tracker.should_delist(ts=crossed + 4 * 60) is True
        assert tracker.trigger_latency_s == 4 * 60
        assert tracker.within_cap is True

    def test_cap_violation_when_latency_exceeds_max(self):
        tracker = st.DelistTracker(threshold_pct=90.0, max_delay_s=300)
        crossed = 100.0
        tracker.record_health(used_pct=91.0, ts=crossed)
        # 6 minutes later — past the 5min cap → violation must be flagged.
        assert tracker.should_delist(ts=crossed + 6 * 60) is True
        assert tracker.cap_violation is True
        assert tracker.within_cap is False

    def test_too_early_decision_within_cap_not_a_violation(self):
        tracker = st.DelistTracker(threshold_pct=90.0, max_delay_s=300)
        crossed = 100.0
        tracker.record_health(used_pct=91.0, ts=crossed)
        assert tracker.should_delist(ts=crossed) is True   # 0s → fine
        assert tracker.cap_violation is False
        assert tracker.trigger_latency_s == 0.0
