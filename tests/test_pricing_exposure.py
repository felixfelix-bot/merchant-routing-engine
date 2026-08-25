"""Tests for src/pricing_exposure.py — CG-2 price exposure (plan v2.1 §0.5/§2.1).

Covers the /v1/pricing payload builder:

- entitlement + realized baselines (v2.1 §0.5: fee ÷ entitlement, NOT $0 floor)
- denominator rule max(smoothed capacity estimate, trailing-30d usage)
- per-window usage fractions (u_5h, u_week, u_month) from proxy window dicts
- pressure superposition applied on z.ai rows and NOT flat tiers
- peak flag {active, mult}
- staleness marker > 15 min
- forecast (+5/+15/+60 min + ?horizon_min=) matches closed-form pressure at the
  projected usage fraction, gated on kalman-convergence-check green
- exhaustion (u ≥ 1.0 → price None + exhausted flag, never a floored number)
- fixture price history feeding CG-1 (evaluate_cost_gate) end to end
- providers.yaml fee loader + price_observations persistence round-trip
- realtime_pricing._measure_zai_amortized switched to the entitlement denominator
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.pricing_engine import (
    ZAI_QUOTA_PRESSURE_ASYMPTOTE,
    ZAI_QUOTA_PRESSURE_ONSET,
    peak_multiplier,
    quota_pressure_factor,
)
from src.pricing_exposure import (
    DEFAULT_ENTITLEMENT_TOKENS_MO,
    FEE_UNCONFIGURED_ERROR,
    FORECAST_HORIZONS_MIN,
    STALENESS_THRESHOLD_S,
    build_flat_row,
    build_pricing_payload,
    build_zai_pricing_row,
    entitlement_baseline_usd_per_m,
    entitlement_denominator,
    entitlement_utilization_pct,
    insert_price_observation,
    is_stale,
    kalman_convergence_green,
    latest_observation_ts,
    load_zai_fees,
    projected_usage_fraction,
    realized_baseline_usd_per_m,
    trailing_usage_tokens,
    usage_fractions,
    zai_pressure_mult,
)
from src.cost_gate import ALLOW, DENY, evaluate_cost_gate

# §0.5 reference numbers -------------------------------------------------------

FRIEND_FEE = 80.0
OURS_FEE = 155.0
ENTITLEMENT = 18.45e9  # friend monthly entitlement estimate, tokens


def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


# ── §0.5 baselines ───────────────────────────────────────────────────────────


class TestEntitlementBaselines:
    def test_friend_baseline_matches_plan_reference(self):
        # $80/mo ÷ 18.45B tokens → ≈ $0.0043/M
        rate = entitlement_baseline_usd_per_m(FRIEND_FEE, ENTITLEMENT)
        assert rate is not None
        assert round(rate, 4) == 0.0043

    def test_ours_baseline(self):
        rate = entitlement_baseline_usd_per_m(OURS_FEE, ENTITLEMENT)
        assert rate is not None
        assert _close(rate, 155.0 / (ENTITLEMENT / 1e6))

    def test_fee_zero_is_none_not_floor(self):
        # v2.1: fee=0 must NEVER yield the $0.001 floor (free-tier artifact)
        assert entitlement_baseline_usd_per_m(0.0, ENTITLEMENT) is None
        assert realized_baseline_usd_per_m(0.0, ENTITLEMENT) is None

    def test_zero_denominator_is_none(self):
        assert entitlement_baseline_usd_per_m(FRIEND_FEE, 0.0) is None


class TestRealizedBaseline:
    def test_trailing_30d_amortization(self):
        # $80 ÷ 983.9M tokens → ≈ $0.0813/M (Aug-to-date reference)
        rate = realized_baseline_usd_per_m(FRIEND_FEE, 983.9e6)
        assert rate is not None
        assert round(rate, 4) == 0.0813

    def test_insufficient_sample_is_none(self):
        assert realized_baseline_usd_per_m(FRIEND_FEE, 1_000) is None


class TestDenominatorRule:
    def test_max_picks_capacity_when_established(self):
        tokens, source = entitlement_denominator(18.45e9, 983.9e6)
        assert _close(tokens, 18.45e9)
        assert source == "capacity_estimate"

    def test_falls_back_to_trailing_when_capacity_missing(self):
        tokens, source = entitlement_denominator(None, 5.0e9)
        assert _close(tokens, 5.0e9)
        assert source == "trailing_30d_usage"

    def test_picks_larger_when_both_present(self):
        tokens, source = entitlement_denominator(1.0e9, 5.0e9)
        assert _close(tokens, 5.0e9)
        assert source == "trailing_30d_usage"

    def test_default_estimate_when_both_missing(self):
        tokens, source = entitlement_denominator(None, None)
        assert _close(tokens, DEFAULT_ENTITLEMENT_TOKENS_MO)
        assert source == "default_estimate"

    def test_entitlement_utilization(self):
        assert _close(entitlement_utilization_pct(983.9e6, ENTITLEMENT), 983.9e6 / ENTITLEMENT * 100)
        assert entitlement_utilization_pct(0, ENTITLEMENT) == 0.0


# ── window mapping + pressure ────────────────────────────────────────────────


class TestUsageFractions:
    def test_maps_by_window_hours(self):
        # proxy /quota shape: {name, used_pct, resets_at, window_hours}
        wins = [
            {"name": "5-hour", "used_pct": 62, "resets_at": 1, "window_hours": 5},
            {"name": "7d", "used_pct": 41, "resets_at": 2, "window_hours": 168},
            {"name": "monthly", "used_pct": 33, "resets_at": 3, "window_hours": 720},
        ]
        fr = usage_fractions(wins)
        assert _close(fr["u_5h"], 0.62)
        assert _close(fr["u_week"], 0.41)
        assert _close(fr["u_month"], 0.33)

    def test_missing_windows_are_none(self):
        fr = usage_fractions([{"name": "unknown", "used_pct": 0, "window_hours": 0}])
        assert fr["u_5h"] is None
        assert fr["u_week"] is None
        assert fr["u_month"] is None


class TestPressureSuperposition:
    def test_matches_engine_closed_form(self):
        # pricing_exposure must delegate to pricing_engine's z.ai curve,
        # not re-implement it
        expected = quota_pressure_factor(
            0.95, 0.5, 0.3,
            onset=ZAI_QUOTA_PRESSURE_ONSET,
            asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE,
            hard_limit=True,
        )
        assert _close(zai_pressure_mult(0.95, 0.5, 0.3), expected)

    def test_below_onset_is_one(self):
        assert _close(zai_pressure_mult(0.1, 0.1, 0.1), 1.0)

    def test_exhausted_is_inf(self):
        assert zai_pressure_mult(1.0, 0.5, 0.3) == math.inf


# ── z.ai pricing row (v2.1 payload) ──────────────────────────────────────────


def _win(hours: int, pct: int) -> dict:
    return {"name": "w", "used_pct": pct, "resets_at": 0, "window_hours": hours}


class TestZaiPricingRow:
    def _row(self, **over):
        kw = dict(
            provider="friend",
            monthly_fee_usd=FRIEND_FEE,
            entitlement_tokens_mo=ENTITLEMENT,
            capacity_estimate_tokens=ENTITLEMENT,
            trailing_30d_tokens=983.9e6,
            windows=[_win(5, 62), _win(168, 41), _win(720, 33)],
            projections=None,
            last_obs_ts=None,
            now_ts=1_800_000_000.0,
            hour_utc=12,
        )
        kw.update(over)
        return build_zai_pricing_row(**kw)

    def test_v21_field_names(self):
        row = self._row()
        for field in (
            "baseline_entitlement_usd_per_m",
            "baseline_realized_usd_per_m",
            "entitlement_utilization_pct",
            "windows",
            "pressure_mult",
            "peak",
            "effective_price_usd_per_m",
            "forecast",
        ):
            assert field in row, f"missing v2.1 field: {field}"
        assert row["kind"] == "subscription"

    def test_windows_block_fields(self):
        row = self._row()
        w = row["windows"]
        for field in ("u_5h", "u_week", "u_month", "estimated_capacity_tokens", "confidence"):
            assert field in w, f"missing windows field: {field}"

    def test_price_composition_baseline_times_pressure_times_peak(self):
        row = self._row()  # hour 12 → no peak
        baseline = FRIEND_FEE / (ENTITLEMENT / 1e6)
        pressure = zai_pressure_mult(0.62, 0.41, 0.33)
        assert _close(row["effective_price_usd_per_m"], baseline * pressure)
        assert _close(row["pressure_mult"], pressure)
        assert row["peak"] == {"active": False, "mult": 1.0}

    def test_peak_flag_active(self):
        row = self._row(hour_utc=7)  # z.ai peak window {6..9}
        assert row["peak"] == {"active": True, "mult": 3.0}
        baseline = FRIEND_FEE / (ENTITLEMENT / 1e6)
        pressure = zai_pressure_mult(0.62, 0.41, 0.33)
        assert _close(row["effective_price_usd_per_m"], baseline * pressure * 3.0)

    def test_pressure_below_onset_no_penalty(self):
        # u=0.1 everywhere → pressure 1.0, price == baseline
        row = self._row(windows=[_win(5, 10), _win(168, 10), _win(720, 10)])
        assert _close(row["pressure_mult"], 1.0)
        assert _close(
            row["effective_price_usd_per_m"],
            FRIEND_FEE / (ENTITLEMENT / 1e6),
        )

    def test_fee_zero_row_flags_error_no_floor(self):
        row = self._row(monthly_fee_usd=0.0)
        assert row["error"] == FEE_UNCONFIGURED_ERROR
        assert row["baseline_entitlement_usd_per_m"] is None
        assert row["effective_price_usd_per_m"] is None
        # explicit: the $0.001 floor must not come back via any field
        assert row["effective_price_usd_per_m"] != 0.001

    def test_exhausted_window_price_none_exhausted_flag(self):
        row = self._row(windows=[_win(5, 100), _win(168, 41), _win(720, 33)])
        assert row["exhausted"] is True
        assert row["effective_price_usd_per_m"] is None

    def test_staleness_marker(self):
        now = 1_800_000_000.0
        row = self._row(now_ts=now, last_obs_ts=now - 20 * 60)
        assert row["staleness"]["stale"] is True  # 20 min > 15 min threshold
        row_fresh = self._row(now_ts=now, last_obs_ts=now - 5 * 60)
        assert row_fresh["staleness"]["stale"] is False
        row_missing = self._row(now_ts=now, last_obs_ts=None)
        assert row_missing["staleness"]["stale"] is True

    def test_denominator_fields_exposed(self):
        row = self._row()
        assert _close(row["denominator"]["tokens"], ENTITLEMENT)
        assert row["denominator"]["source"] == "capacity_estimate"


# ── forecast (projected_total_pct → pressure at projected u) ─────────────────


class TestProjection:
    def test_linear_interpolation_to_window_end(self):
        # u_now=0.95, projected_total_pct=98 at 1h left, horizon 30 min
        u = projected_usage_fraction(0.95, 98.0, hours_left=1.0, horizon_min=30)
        assert _close(u, 0.965)

    def test_clamped_at_projection(self):
        # horizon beyond window end clamps to the projected total
        u = projected_usage_fraction(0.95, 98.0, hours_left=1.0, horizon_min=120)
        assert _close(u, 0.98)

    def test_no_projection_stays_flat(self):
        u = projected_usage_fraction(0.95, None, hours_left=1.0, horizon_min=30)
        assert _close(u, 0.95)

    def test_exhaustion_clamps_to_one(self):
        u = projected_usage_fraction(0.99, 105.0, hours_left=1.0, horizon_min=60)
        assert _close(u, 1.0)


class TestForecast:
    """Projections shaped like burn_predictor.predict_exhaustion rows:

    {"window": "5h"|"7d"|"30d"..., "projected_total_pct": float,
     "exhausts_in_hours": float, "estimated_capacity_tokens": int, ...}
    """

    PROJ = [
        {"window": "5h", "projected_total_pct": 98.0, "exhausts_in_hours": 1.0,
         "estimated_capacity_tokens": 900_000, "burn_rate_tph": 5000},
        {"window": "7d", "projected_total_pct": 55.0, "exhausts_in_hours": 40.0,
         "estimated_capacity_tokens": 12_000_000_000, "burn_rate_tph": 5000},
        {"window": "30d", "projected_total_pct": 40.0, "exhausts_in_hours": 300.0,
         "estimated_capacity_tokens": ENTITLEMENT, "burn_rate_tph": 5000},
    ]

    def _row(self, verdict="healthy", **over):
        kw = dict(
            provider="friend",
            monthly_fee_usd=FRIEND_FEE,
            entitlement_tokens_mo=ENTITLEMENT,
            capacity_estimate_tokens=ENTITLEMENT,
            trailing_30d_tokens=983.9e6,
            windows=[_win(5, 95), _win(168, 50), _win(720, 30)],
            projections=[dict(p) for p in self.PROJ],
            last_obs_ts=1_799_999_995.0,
            now_ts=1_800_000_000.0,
            hour_utc=12,
            kalman_verdict=verdict,
        )
        kw.update(over)
        return build_zai_pricing_row(**kw)

    def test_default_horizons_present(self):
        fc = self._row()["forecast"]
        assert fc["kalman_convergence"] == "green"
        got = [h["horizon_min"] for h in fc["at_horizon"]]
        assert sorted(got) == sorted(FORECAST_HORIZONS_MIN)

    def test_extra_horizon_min_appended(self):
        fc = self._row(extra_horizons_min=(90,))["forecast"]
        got = {h["horizon_min"] for h in fc["at_horizon"]}
        assert got == set(FORECAST_HORIZONS_MIN) | {90}

    def test_forecast_matches_closed_form_pressure_at_projected_u(self):
        row = self._row()
        baseline = FRIEND_FEE / (ENTITLEMENT / 1e6)
        fc = row["forecast"]
        for h in fc["at_horizon"]:
            # closed form: interpolate each window to the horizon, then apply
            # the engine pressure curve directly
            u5 = projected_usage_fraction(0.95, 98.0, 1.0, h["horizon_min"])
            uw = projected_usage_fraction(0.50, 55.0, 40.0, h["horizon_min"])
            um = projected_usage_fraction(0.30, 40.0, 300.0, h["horizon_min"])
            expected_mult = quota_pressure_factor(
                u5, uw, um,
                onset=ZAI_QUOTA_PRESSURE_ONSET,
                asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE,
                hard_limit=True,
            )
            expected_price = baseline * expected_mult  # hour 12 → no peak
            assert _close(h["pressure_mult"], expected_mult, tol=1e-6), h
            assert _close(h["effective_price_usd_per_m"], expected_price, tol=1e-6), h
            assert h["stale"] is False

    def test_forecast_peak_composition(self):
        row = self._row(hour_utc=7)
        baseline = FRIEND_FEE / (ENTITLEMENT / 1e6)
        for h in row["forecast"]["at_horizon"]:
            # whole horizon inside the peak window → mult 3.0 applies
            assert h["peak_mult"] in (3.0,)
            assert _close(
                h["effective_price_usd_per_m"],
                baseline * h["pressure_mult"] * 3.0,
                tol=1e-6,
            )

    def test_kalman_not_green_falls_back_to_current_price_with_stale_flag(self):
        row = self._row(verdict="degraded")
        fc = row["forecast"]
        assert fc["kalman_convergence"] != "green"
        current = row["effective_price_usd_per_m"]
        for h in fc["at_horizon"]:
            assert h["stale"] is True
            assert _close(h["effective_price_usd_per_m"], current)

    def test_green_verdicts_strict(self):
        assert kalman_convergence_green("healthy") is True
        # anything else — including "improving" — is NOT green (conservative)
        assert kalman_convergence_green("improving") is False
        assert kalman_convergence_green("degraded") is False
        assert kalman_convergence_green("unhealthy") is False
        assert kalman_convergence_green("") is False

    def test_no_projections_forecast_is_current_plus_note(self):
        row = self._row(projections=None)
        fc = row["forecast"]
        assert fc["kalman_convergence"] == "green"
        for h in fc["at_horizon"]:
            assert h["stale"] is True  # nothing to project from
            assert _close(h["effective_price_usd_per_m"], row["effective_price_usd_per_m"])


# ── flat tiers (no pressure, no peak, no forecast windows) ───────────────────


class TestFlatRows:
    def test_ollama_cloud_tracker_amortized_no_pressure(self):
        now = 1_800_000_000.0
        row = build_flat_row(
            provider="ollama_cloud",
            catalog_price_usd_per_m=0.0155,
            measured=True,
            last_obs_ts=now - 60,
            now_ts=now,
        )
        assert row["kind"] == "flat_subscription"
        assert _close(row["effective_price_usd_per_m"], 0.0155)
        # explicit: NO pressure/peak machinery on flat tiers
        assert "pressure_mult" not in row
        assert "peak" not in row
        assert "forecast" not in row
        assert row["staleness"]["stale"] is False
        assert row["measured"] is True

    def test_flat_staleness(self):
        now = 1_800_000_000.0
        row = build_flat_row(
            provider="ollama_cloud",
            catalog_price_usd_per_m=0.0155,
            measured=True,
            last_obs_ts=now - 30 * 60,
            now_ts=now,
        )
        assert row["staleness"]["stale"] is True


class TestCatalogRows:
    def test_routstrd_catalog_row(self):
        now = 1_800_000_000.0
        row = build_flat_row(
            provider="routstrd",
            catalog_price_usd_per_m=1.0,
            measured=False,
            last_obs_ts=None,
            now_ts=now,
        )
        assert row["kind"] == "pay_per_use"
        assert _close(row["effective_price_usd_per_m"], 1.0)
        assert "pressure_mult" not in row
        assert row["staleness"]["stale"] is True


# ── payload envelope ─────────────────────────────────────────────────────────


class TestPayloadEnvelope:
    def test_envelope_shape(self):
        now = 1_800_000_000.0
        zai = build_zai_pricing_row(
            provider="friend",
            monthly_fee_usd=FRIEND_FEE,
            entitlement_tokens_mo=ENTITLEMENT,
            capacity_estimate_tokens=ENTITLEMENT,
            trailing_30d_tokens=983.9e6,
            windows=[_win(5, 62), _win(168, 41), _win(720, 33)],
            projections=None,
            last_obs_ts=now - 60,
            now_ts=now,
            hour_utc=12,
        )
        flat = build_flat_row(
            provider="ollama_cloud",
            catalog_price_usd_per_m=0.0155,
            measured=True,
            last_obs_ts=now - 60,
            now_ts=now,
        )
        payload = build_pricing_payload(
            rows={"friend": zai, "ollama_cloud": flat},
            kalman_verdict="healthy",
            model="glm-5.3",
            horizon_min=45,
            now_ts=now,
        )
        for field in ("generated_ts", "model", "horizon_min", "kalman_convergence", "providers"):
            assert field in payload
        assert payload["model"] == "glm-5.3"
        assert payload["horizon_min"] == 45
        assert payload["kalman_convergence"]["green"] is True
        assert set(payload["providers"]) == {"friend", "ollama_cloud"}
        assert json.dumps(payload)  # payload must be JSON-serializable


# ── I/O helpers (tmp sqlite + providers.yaml fixture) ────────────────────────


YAML_FIXTURE = """\
zai:
  keys:
    ours:
      monthly_fee_usd: 155
    friend:
      monthly_fee_usd: 80
      entitlement_tokens_mo: 20000000000
"""


@pytest.fixture()
def fee_yaml(tmp_path):
    p = tmp_path / "providers.yaml"
    p.write_text(YAML_FIXTURE, encoding="utf-8")
    return str(p)


@pytest.fixture()
def usage_db(tmp_path):
    p = tmp_path / "usage.db"
    conn = sqlite3.connect(str(p))
    conn.execute(
        "CREATE TABLE api_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts REAL NOT NULL, key_name TEXT, total_tokens INTEGER)"
    )
    return str(p)


class TestFeeLoader:
    def test_loads_fees_and_override(self, fee_yaml):
        inv = load_zai_fees(fee_yaml)
        assert _close(inv["friend"]["monthly_fee_usd"], 80.0)
        assert _close(inv["ours"]["monthly_fee_usd"], 155.0)
        assert _close(inv["friend"]["entitlement_tokens_mo"], 20.0e9)
        assert _close(inv["ours"]["entitlement_tokens_mo"], DEFAULT_ENTITLEMENT_TOKENS_MO)

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_zai_fees(str(tmp_path / "nope.yaml")) == {}

    def test_fee_zero_survives_loader_no_default_injection(self, tmp_path):
        p = tmp_path / "zero.yaml"
        p.write_text("zai:\n  keys:\n    friend:\n      monthly_fee_usd: 0\n")
        inv = load_zai_fees(str(p))
        assert _close(inv["friend"]["monthly_fee_usd"], 0.0)


class TestUsageQueries:
    def test_trailing_30d_usage(self, usage_db):
        now = time.time()
        conn = sqlite3.connect(usage_db)
        conn.executemany(
            "INSERT INTO api_calls (ts, key_name, total_tokens) VALUES (?, ?, ?)",
            [
                (now - 10 * 86400, "friend", 400_000_000),
                (now - 20 * 86400, "friend", 300_000_000),
                (now - 45 * 86400, "friend", 999_999_999),  # outside 30d
                (now - 5 * 86400, "ours", 250_000_000),
            ],
        )
        conn.commit()
        conn.close()
        assert trailing_usage_tokens(usage_db, "friend", days=30) == 700_000_000
        assert trailing_usage_tokens(usage_db, "ours", days=30) == 250_000_000
        assert trailing_usage_tokens(usage_db, "ghost", days=30) == 0


class TestObservationPersistence:
    def test_insert_and_latest_ts_roundtrip(self, usage_db):
        now = time.time()
        assert latest_observation_ts(usage_db, "friend") is None
        assert insert_price_observation(
            usage_db, provider="friend", rate_usd_per_m=0.0043,
            source="cg2_entitlement", is_measured=False, confidence=0.6,
            sample_tokens=int(983.9e6), sample_cost_usd=80.0,
            note={"estimated_capacity_tokens": int(ENTITLEMENT)},
        )
        assert insert_price_observation(
            usage_db, provider="ollama_cloud", rate_usd_per_m=0.0155,
            source="tracker_amortized", is_measured=True, confidence=0.9,
        )
        ts = latest_observation_ts(usage_db, "friend")
        assert ts is not None and abs(ts - now) < 30
        # measured flag round-trips
        conn = sqlite3.connect(usage_db)
        rows = conn.execute(
            "SELECT provider, rate_per_m, is_measured, note FROM price_observations "
            "ORDER BY provider"
        ).fetchall()
        conn.close()
        assert rows[0][0] == "friend" and rows[0][2] == 0
        assert json.loads(rows[0][3])["estimated_capacity_tokens"] == int(ENTITLEMENT)
        assert rows[1][0] == "ollama_cloud" and rows[1][2] == 1


# ── CG-1 integration: fixture price history → evaluate_cost_gate ─────────────


class TestCG1Integration:
    def test_history_fixture_feeds_cost_gate_allow(self, usage_db):
        # 60 hourly entitlement-baseline observations (cheap band)
        now = time.time()
        for i in range(60):
            assert insert_price_observation(
                usage_db, provider="friend",
                rate_usd_per_m=0.0043 + (0.0001 if i % 2 else 0),
                source="cg2_entitlement", is_measured=False, confidence=0.6,
                ts=now - (60 - i) * 3600,
            )
        # 30 recent rows above the cheap band; newest lands at `now` so the
        # staleness gate (>15 min) does not trip before the percentile gate.
        for i in range(30):
            insert_price_observation(
                usage_db, provider="friend", rate_usd_per_m=0.0090,
                source="cg2_entitlement", is_measured=False, confidence=0.6,
                ts=now - (29 - i) * 3600,
            )
        conn = sqlite3.connect(usage_db)
        history = [
            r[0] for r in conn.execute(
                "SELECT rate_per_m FROM price_observations "
                "WHERE provider='friend' ORDER BY ts"
            )
        ]
        last_ts = conn.execute(
            "SELECT MAX(ts) FROM price_observations WHERE provider='friend'"
        ).fetchone()[0]
        conn.close()
        assert len(history) == 90  # ≥ MIN_HISTORY_SAMPLES

        verdict = evaluate_cost_gate(
            effective_price_usd_per_m=0.0043,
            price_source="cg2_entitlement",
            price_age_min=(time.time() - last_ts) / 60.0,
            price_history=history,
            rolling_paid_spend_usd=1.0,
            budget_cap_usd=15.0,
            route_tier="subscription",
            deferrable=True,
        )
        assert verdict["decision"] == ALLOW
        assert verdict["provenance"]["price_source"] == "cg2_entitlement"

    def test_history_fixture_stale_price_denies(self, usage_db):
        now = time.time()
        for i in range(60):
            insert_price_observation(
                usage_db, provider="friend", rate_usd_per_m=0.0043,
                source="cg2_entitlement", is_measured=False, confidence=0.6,
                ts=now - 86400 - (60 - i) * 3600,  # newest is ~24h old
            )
        ts = latest_observation_ts(usage_db, "friend")
        verdict = evaluate_cost_gate(
            effective_price_usd_per_m=0.0043,
            price_age_min=(now - ts) / 60.0,
            price_history=[0.0043] * 60,
            rolling_paid_spend_usd=0.0,
            budget_cap_usd=15.0,
            deferrable=True,
        )
        assert verdict["decision"] == DENY
        assert verdict["reason_code"] == "infra_down"


# ── realtime_pricing: entitlement denominator switch ─────────────────────────


class TestMeasureZaiAmortizedSwitch:
    def _make_db(self, tmp_path, tokens_per_key):
        p = str(tmp_path / "zai.db")
        conn = sqlite3.connect(p)
        conn.execute(
            "CREATE TABLE api_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts REAL NOT NULL, key_name TEXT, total_tokens INTEGER)"
        )
        now = time.time()
        for key, toks in tokens_per_key.items():
            # spread over the trailing 30d so the monthly usage is realistic
            for i in range(10):
                conn.execute(
                    "INSERT INTO api_calls (ts, key_name, total_tokens) "
                    "VALUES (?, ?, ?)",
                    (now - i * 2 * 86400, key, toks // 10),
                )
        conn.commit()
        conn.close()
        return p

    def test_denominator_is_max_of_capacity_and_trailing_30d(self, tmp_path, fee_yaml):
        from src.realtime_pricing import RealtimePricing

        db = self._make_db(tmp_path, {"friend": 2_000_000_000})  # 2B trailing
        rp = RealtimePricing(
            zai_db_path=db, burn_db_path=str(tmp_path / "burn.db"),
            providers_yaml=fee_yaml,
        )
        rp._capacity_estimates = {"friend": 18.45e9}  # smoothed capacity estimate
        obs = rp._measure_zai_amortized()
        row = obs[("friend", None)]
        # fee 80 ÷ max(18.45e9, 2e9) = entitlement denominator wins
        assert _close(row.rate_per_m, 80.0 / (18.45e9 / 1e6), tol=1e-6)
        assert row.source == "zai_amortized"

    def test_trailing_wins_when_capacity_missing(self, tmp_path, fee_yaml):
        from src.realtime_pricing import RealtimePricing

        db = self._make_db(tmp_path, {"friend": 2_000_000_000})
        rp = RealtimePricing(
            zai_db_path=db, burn_db_path=str(tmp_path / "burn.db"),
            providers_yaml=fee_yaml,
        )
        rp._capacity_estimates = {}
        obs = rp._measure_zai_amortized()
        row = obs[("friend", None)]
        # fee 80 ÷ max(None, 2e9) → trailing-30d denominator
        assert _close(row.rate_per_m, 80.0 / (2.0e9 / 1e6), tol=1e-6)

    def test_fee_zero_yields_cold_start_not_floor(self, tmp_path):
        from src.realtime_pricing import RealtimePricing

        p = tmp_path / "zero.yaml"
        p.write_text("zai:\n  keys:\n    friend:\n      monthly_fee_usd: 0\n")
        db = self._make_db(tmp_path, {"friend": 2_000_000_000})
        rp = RealtimePricing(
            zai_db_path=db, burn_db_path=str(tmp_path / "burn.db"),
            providers_yaml=str(p),
        )
        rp._capacity_estimates = {"friend": 18.45e9}
        obs = rp._measure_zai_amortized()
        row = obs[("friend", None)]
        # fee=0 → flagged cold start, NEVER the $0.001 floor via amortization
        assert row.source != "zai_amortized"


# ── latest_observation_rate (endpoint ollama_cloud tracker rate) ─────────────


class TestLatestObservationRate:
    def test_roundtrip_latest_rate_and_ts(self, usage_db):
        now = time.time()
        insert_price_observation(
            usage_db, provider="ollama_cloud", rate_usd_per_m=0.50,
            source="ollama_billing", is_measured=True, ts=now - 7200,
        )
        insert_price_observation(
            usage_db, provider="ollama_cloud", rate_usd_per_m=0.55,
            source="ollama_billing", is_measured=True, ts=now,
        )
        from src.pricing_exposure import latest_observation_rate
        rate, ts, measured = latest_observation_rate(usage_db, "ollama_cloud")
        assert _close(rate, 0.55)
        assert _close(ts, now, tol=1e-3)
        assert measured is True

    def test_absent_provider_is_none(self, usage_db):
        from src.pricing_exposure import latest_observation_rate
        assert latest_observation_rate(usage_db, "nobody") == (None, None, False)


# ── collector: scripts/collect_price_observations.py ─────────────────────────

_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
_COLLECTOR_PATH = os.path.join(_SCRIPTS_DIR, "collect_price_observations.py")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_collector():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "collect_price_observations_test", _COLLECTOR_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def collector():
    return _load_collector()


class TestCapacityDerivation:
    def test_capacity_from_month_fraction(self, collector):
        # 9e9 trailing tokens at u_month=0.5 → 18e9 implied monthly capacity
        cap = collector.derive_capacity_estimate(9.0e9, 0.5)
        assert _close(cap, 18.0e9, tol=1e-6)

    def test_no_month_data_is_none(self, collector):
        assert collector.derive_capacity_estimate(9.0e9, None) is None

    def test_tiny_fraction_is_none(self, collector):
        # u_month below MIN_MONTH_FRACTION is too noisy to imply capacity
        assert collector.derive_capacity_estimate(1e6, 0.001) is None


class TestPersistObservations:
    def _payload(self, now=None):
        now = now or time.time()
        zai_row = build_zai_pricing_row(
            provider="friend",
            monthly_fee_usd=80.0,
            entitlement_tokens_mo=18.45e9,
            capacity_estimate_tokens=18.45e9,
            trailing_30d_tokens=9.0e9,
            windows=[
                {"name": "5h", "used_pct": 62, "window_hours": 5},
                {"name": "7d", "used_pct": 41, "window_hours": 168},
                {"name": "30d", "used_pct": 50, "window_hours": 720},
            ],
            now_ts=now,
        )
        flat_row = build_flat_row(
            provider="routstrd", catalog_price_usd_per_m=1.10,
            measured=False, now_ts=now,
        )
        return build_pricing_payload(
            rows={"friend": zai_row, "routstrd": flat_row},
            kalman_verdict="improving", now_ts=now,
        )

    def test_persists_zai_and_flat_rows(self, collector, usage_db):
        payload = self._payload()
        n = collector.persist_observations(usage_db, payload)
        assert n == 2
        conn = sqlite3.connect(usage_db)
        rows = conn.execute(
            "SELECT provider, source, is_measured, rate_per_m, note "
            "FROM price_observations ORDER BY provider"
        ).fetchall()
        conn.close()
        by_prov = {r[0]: r for r in rows}
        # zai row: derived effective price, source marks the exposure pipeline
        fr = by_prov["friend"]
        assert fr[1] == "pricing_exposure"
        assert fr[2] == 0  # derived, not measured
        assert fr[3] is not None and fr[3] > 0
        note = json.loads(fr[4])
        assert note["windows"]["estimated_capacity_tokens"] == 18.45e9
        # flat row: catalog rate, measured flag preserved
        rr = by_prov["routstrd"]
        assert rr[1] == "catalog:routstrd"
        assert rr[2] == 0
        assert _close(rr[3], 1.10)

    def test_skips_null_price_rows(self, collector, usage_db):
        payload = self._payload()
        # fee=0 artifact → effective price None → row skipped, never $0 floor
        payload["providers"]["friend"]["effective_price_usd_per_m"] = None
        n = collector.persist_observations(usage_db, payload)
        assert n == 1

    def test_measured_flat_row_keeps_flag(self, collector, usage_db):
        payload = self._payload()
        payload["providers"]["routstrd"]["measured"] = True
        collector.persist_observations(usage_db, payload)
        conn = sqlite3.connect(usage_db)
        (measured,) = conn.execute(
            "SELECT is_measured FROM price_observations WHERE provider='routstrd'"
        ).fetchone()
        conn.close()
        assert measured == 1


class TestCollectorMain:
    def test_once_run_writes_state_and_observations(
        self, collector, tmp_path, monkeypatch
    ):
        db = str(tmp_path / "usage.db")
        state_path = str(tmp_path / "state.json")
        payload = TestPersistObservations()._payload()

        monkeypatch.setattr(
            collector, "fetch_pricing_snapshot", lambda base: payload
        )
        rc = collector.main([
            "--db", db, "--state", state_path, "--base", "http://127.0.0.1:9099",
        ])
        assert rc == 0
        state = json.loads(open(state_path).read())
        assert state["kalman_verdict"] in ("healthy", "improving", "degraded",
                                           "unhealthy", "unverified")
        assert "generated_ts" in state
        # capacity estimate was derived from the payload month window + usage
        assert state["capacity_estimates"]["friend"] > 0
        conn = sqlite3.connect(db)
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()
        conn.close()
        assert n == 2

    def test_fixture_mode_seeds_history(self, collector, tmp_path):
        db = str(tmp_path / "usage.db")
        rc = collector.main(["--db", db, "--fixture", "--hours", "24"])
        assert rc == 0
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT provider, COUNT(*) FROM price_observations "
            "GROUP BY provider"
        ).fetchall()
        conn.close()
        counts = {r[0]: r[1] for r in rows}
        assert counts.get("friend") == 24
        assert counts.get("ours") == 24


# ── proxy GET /v1/pricing endpoint contract ──────────────────────────────────

_PROXY_PATH = os.path.expanduser("~/.hermes/bot/zai_proxy.py")


def _load_proxy_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("zai_proxy_pricing_test", _PROXY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def proxy():
    return _load_proxy_module()


@pytest.fixture()
def pricing_env(proxy, tmp_path, monkeypatch):
    """Deterministic endpoint environment: state file, usage DB, caches."""
    state_path = tmp_path / "pricing_state.json"
    state_path.write_text(json.dumps({
        "generated_ts": time.time(),
        "kalman_verdict": "healthy",
        "capacity_estimates": {"friend": 18.45e9},
    }))
    usage_db = tmp_path / "usage.db"
    conn = sqlite3.connect(str(usage_db))
    conn.execute(
        "CREATE TABLE api_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts REAL NOT NULL, key_name TEXT, total_tokens INTEGER)"
    )
    conn.close()
    monkeypatch.setattr(proxy, "_PRICING_EXPOSURE_STATE", str(state_path))
    monkeypatch.setattr(proxy, "USAGE_DB", usage_db)
    monkeypatch.setattr(proxy, "PROVIDERS_YAML",
                        os.path.join(_REPO_ROOT, "config", "providers.yaml"))
    now = time.time()
    wins = [
        {"name": "5h", "used_pct": 62, "resets_at": now + 3600, "window_hours": 5},
        {"name": "7d", "used_pct": 41, "resets_at": now + 86400, "window_hours": 168},
        {"name": "30d", "used_pct": 50, "resets_at": now + 86400 * 15, "window_hours": 720},
    ]
    monkeypatch.setattr(proxy, "quota_cache", {"friend": (wins, now)})
    monkeypatch.setattr(proxy, "KEYS", ["friend"])
    monkeypatch.setattr(
        proxy, "_get_cached_predictions",
        lambda key: [{
            "key": key, "window": "5h", "used_pct": 62,
            "projected_total_pct": 75.0, "exhausts_in_hours": 4.0,
            "will_exhaust": False, "note": "",
        }],
    )
    monkeypatch.setattr(
        proxy, "_get_routstrd_rates",
        lambda: {"glm-4.6": 1.10, "glm-5.2": 1.30},
    )
    monkeypatch.setattr(proxy, "_get_routstr_rates", lambda: {})
    return proxy


class TestV1PricingEndpoint:
    def test_zai_row_v21_shape(self, pricing_env):
        proxy = pricing_env
        payload = proxy._build_v1_pricing(model=None, horizon_min=None)
        assert payload["kalman_convergence"]["green"] is True
        row = payload["providers"]["friend"]
        assert row["kind"] == "subscription"
        # 80 / 18.45e9 * 1e6 = 0.00434 $/M entitlement baseline
        assert _close(row["baseline_entitlement_usd_per_m"], 80.0 / 18450.0, tol=1e-6)
        assert row["windows"]["estimated_capacity_tokens"] == 18.45e9
        assert row["windows"]["confidence"] == "high"
        assert row["pressure_mult"] > 1.0  # u_5h=0.62 above onset
        assert row["forecast"] is not None

    def test_routstrd_catalog_row_no_pressure(self, pricing_env):
        proxy = pricing_env
        payload = proxy._build_v1_pricing(model=None, horizon_min=None)
        rr = payload["providers"]["routstrd"]
        assert rr["kind"] == "pay_per_use"
        assert _close(rr["catalog_price_usd_per_m"], 1.10)  # catalog min
        assert "pressure_mult" not in rr or rr.get("pressure_mult") in (None, 1.0)

    def test_model_filter_picks_model_rate(self, pricing_env):
        proxy = pricing_env
        payload = proxy._build_v1_pricing(model="glm-5.2", horizon_min=45)
        assert payload["model"] == "glm-5.2"
        assert payload["horizon_min"] == 45
        rr = payload["providers"]["routstrd"]
        assert _close(rr["catalog_price_usd_per_m"], 1.30)
        row = payload["providers"]["friend"]
        assert 45 in row["forecast"]["horizons_min"]

    def test_not_green_forecast_falls_back(self, pricing_env, tmp_path, monkeypatch):
        proxy = pricing_env
        state_path = tmp_path / "state2.json"
        state_path.write_text(json.dumps({
            "generated_ts": time.time(),
            "kalman_verdict": "improving",
            "capacity_estimates": {},
        }))
        monkeypatch.setattr(proxy, "_PRICING_EXPOSURE_STATE", str(state_path))
        payload = proxy._build_v1_pricing(model=None, horizon_min=None)
        assert payload["kalman_convergence"]["green"] is False
        row = payload["providers"]["friend"]
        for h in row["forecast"]["at_horizon"]:
            assert h["stale"] is True
            assert _close(h["effective_price_usd_per_m"],
                          row["effective_price_usd_per_m"], tol=1e-9)

    def test_missing_state_file_forecast_fallback(self, pricing_env, monkeypatch, tmp_path):
        proxy = pricing_env
        monkeypatch.setattr(proxy, "_PRICING_EXPOSURE_STATE",
                            str(tmp_path / "missing.json"))
        payload = proxy._build_v1_pricing(model=None, horizon_min=None)
        assert payload["kalman_convergence"]["green"] is False
        assert payload["kalman_convergence"]["verdict"] == "unverified"

    def test_do_get_routes_v1_pricing(self, pricing_env):
        proxy = pricing_env
        import io

        class _Resp:
            def __init__(self):
                self.buf = io.BytesIO()

            def write(self, b):
                self.buf.write(b)

        h = object.__new__(proxy.Handler)
        h.path = "/v1/pricing?model=glm-5.2"
        h.wfile = _Resp()
        h.close_connection = True
        sent = {}
        h.send_response = lambda code, **kw: sent.setdefault("code", code)
        h.send_header = lambda k, v: None
        h.end_headers = lambda: None
        payload = proxy._build_v1_pricing(model="glm-5.2", horizon_min=None)
        called = {}

        def _fake_build(model, horizon_min):
            called["model"] = model
            return payload

        import unittest.mock as mock

        with mock.patch.object(proxy, "_build_v1_pricing", _fake_build):
            proxy.Handler.do_GET(h)
        assert sent["code"] == 200
        body = json.loads(h.wfile.buf.getvalue())
        assert body["model"] == "glm-5.2"
        assert called["model"] == "glm-5.2"
        assert "friend" in body["providers"]
