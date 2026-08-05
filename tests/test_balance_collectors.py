"""Tests for src/balance_collectors.py — DeepInfra billing API balance collector.

Covers:
  - happy path: single-month spend + checklist balance
  - multi-month lifetime aggregation (initial_month -> current)
  - cost unit conversion: cents -> USD (verified 1.30/M DeepSeek-V4-Pro rate)
  - /payment/checklist field mapping: stripe_balance sign semantics
    (negative = credit, positive = owed), suspended, reason, recent
  - remaining = starting_balance - total_spent_usd
  - never raises: no key, network failure, HTTP 4xx/5xx, malformed JSON,
    bogus types, empty/missing fields
  - pure helpers: _cents_to_usd, _iter_months (year wrap), _next_period,
    _resolve_stripe_balance, _coerce_float (NaN/inf/negative), _valid_period
  - the collector uses month-by-month period queries (never the 31-day range cap)
"""
from __future__ import annotations

import json

import pytest

from src.balance_collectors import (
    COST_FIELD_UNIT,
    DeepInfraBalance,
    _cents_to_usd,
    _coerce_float,
    _current_period,
    _iter_months,
    _next_period,
    _parse_checklist,
    _parse_usage_month,
    _resolve_stripe_balance,
    _valid_period,
    collect_deepinfra_balance,
)


# ── fixtures / helpers ───────────────────────────────────────────────────────


def _usage_body(items, period="2026.07", initial_month="2026.07"):
    """Build a /payment/usage response body from a list of (units, rate, cost)."""
    return json.dumps({
        "months": [{"period": period,
                    "items": [{"model": {"model_name": "m"}, "units": u,
                               "rate": r, "cost": c, "pricing_type": "input_tokens"}
                              for (u, r, c) in items]}],
        "initial_month": initial_month,
    })


def _period_of(url):
    """Extract the from=YYYY.MM period from a usage query URL."""
    import urllib.parse as up
    qs = up.parse_qs(up.urlsplit(url).query)
    return qs.get("from", [None])[0]


class FakeBilling:
    """A scripted HTTP seam matching URL substrings.

    ``responses`` maps a URL substring -> (status, body). Requests not matched
    return (404, '{"detail":"Not Found"}'). Call log recorded in ``calls``.
    """

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        for needle, resp in self.responses.items():
            if needle in url:
                return resp
        return (404, '{"detail":"Not Found"}')

    @property
    def urls_called(self):
        return self.calls


def _single_month_router(spend_items, checklist_body, *, initial_month=None):
    """A period-aware http_get seam for single-month tests.

    Returns the spend body for the *current* month only (with initial_month set
    to current so no forward walk happens), and an empty body for any other
    period. checklist_body is returned for /payment/checklist.
    """
    current = _current_period()
    im = initial_month or current

    def router(url, headers, timeout):
        if "/payment/checklist" in url:
            return (200, checklist_body)
        period = _period_of(url)
        if period == current:
            return (200, _usage_body(spend_items, period=current, initial_month=im))
        return (200, json.dumps({"months": [], "initial_month": im}))

    return router


# ── pure helper unit tests ───────────────────────────────────────────────────


class TestCentsToUsd:
    def test_basic(self):
        assert _cents_to_usd(2549) == pytest.approx(25.49)

    def test_zero(self):
        assert _cents_to_usd(0) == 0.0

    def test_unit_constant(self):
        assert COST_FIELD_UNIT == 100.0


class TestCoerceFloat:
    def test_int(self):
        assert _coerce_float(5) == 5.0

    def test_float(self):
        assert _coerce_float(1.5) == 1.5

    def test_numeric_string(self):
        assert _coerce_float("3.14") == 3.14

    def test_none(self):
        assert _coerce_float(None) is None

    def test_garbage(self):
        assert _coerce_float("abc") is None

    def test_nan(self):
        assert _coerce_float(float("nan")) is None

    def test_inf(self):
        assert _coerce_float(float("inf")) is None
        assert _coerce_float(float("-inf")) is None

    def test_negative_ok(self):
        assert _coerce_float(-2.5) == -2.5


class TestValidPeriod:
    def test_good(self):
        assert _valid_period("2026.07") is True

    def test_january_and_december(self):
        assert _valid_period("2026.01") is True
        assert _valid_period("2026.12") is True

    def test_bad_month(self):
        assert _valid_period("2026.13") is False
        assert _valid_period("2026.00") is False

    def test_bad_year(self):
        assert _valid_period("1899.07") is False

    def test_garbage(self):
        assert _valid_period("nope") is False
        assert _valid_period("2026-07") is False
        assert _valid_period(None) is False
        assert _valid_period(2026) is False

    def test_three_parts(self):
        assert _valid_period("2026.07.01") is False


class TestNextPeriod:
    def test_mid_year(self):
        assert _next_period("2026.07") == "2026.08"

    def test_december_wrap(self):
        assert _next_period("2026.12") == "2027.01"

    def test_garbage(self):
        assert _next_period("garbage") is None
        assert _next_period("2026-07") is None

    def test_bad_month(self):
        assert _next_period("2026.13") is None
        assert _next_period("2026.00") is None


class TestIterMonths:
    def test_single(self):
        assert list(_iter_months("2026.07", "2026.07", 24)) == ["2026.07"]

    def test_two_months(self):
        assert list(_iter_months("2026.07", "2026.08", 24)) == ["2026.07", "2026.08"]

    def test_year_wrap(self):
        assert list(_iter_months("2026.11", "2027.02", 24)) == [
            "2026.11", "2026.12", "2027.01", "2027.02",
        ]

    def test_limit_caps(self):
        out = list(_iter_months("2026.01", "2026.12", 3))
        assert out == ["2026.01", "2026.02", "2026.03"]

    def test_zero_limit(self):
        assert list(_iter_months("2026.07", "2026.08", 0)) == []

    def test_garbage_initial(self):
        assert list(_iter_months("nope", "2026.08", 24)) == []


class TestResolveStripeBalance:
    def test_negative_is_credit(self):
        rts, owed = _resolve_stripe_balance(-5.0)
        assert rts == 5.0 and owed == 0.0

    def test_positive_is_owed(self):
        rts, owed = _resolve_stripe_balance(0.49)
        assert rts == 0.0 and owed == 0.49

    def test_zero(self):
        rts, owed = _resolve_stripe_balance(0.0)
        assert rts in (0.0, -0.0) and owed == 0.0

    def test_none(self):
        assert _resolve_stripe_balance(None) == (None, None)


class TestParseUsageMonth:
    def test_sums_costs(self):
        body = _usage_body([(1595024, 1.3e-4, 207), (100, 1e-4, 0), (50, 1e-3, 5)])
        cents, im = _parse_usage_month(body)
        assert cents == pytest.approx(212.0)
        assert im == "2026.07"

    def test_missing_cost_item(self):
        body = json.dumps({"months": [{"items": [{"units": 10}, {"cost": 8}]}],
                           "initial_month": "2026.07"})
        cents, im = _parse_usage_month(body)
        assert cents == 8.0 and im == "2026.07"

    def test_no_months(self):
        cents, im = _parse_usage_month(json.dumps({"months": [], "initial_month": None}))
        assert cents == 0.0 and im is None

    def test_malformed_json(self):
        cents, im = _parse_usage_month("not json{{{")
        assert cents == 0.0 and im is None

    def test_empty_body(self):
        cents, im = _parse_usage_month("")
        assert cents == 0.0 and im is None

    def test_non_dict(self):
        cents, im = _parse_usage_month(json.dumps([1, 2, 3]))
        assert cents == 0.0 and im is None

    def test_cost_non_numeric_ignored(self):
        body = json.dumps({"months": [{"items": [{"cost": "oops"}, {"cost": 3}]}]})
        cents, _ = _parse_usage_month(body)
        assert cents == 3.0


class TestParseChecklist:
    def test_full_mapping(self):
        body = json.dumps({
            "stripe_balance": 0.49, "recent": 1.23, "limit": 100.0,
            "suspended": True, "suspend_reason": "payment-method",
            "billing_type": "balance",
        })
        cl = _parse_checklist(body)
        assert cl["stripe_balance"] == 0.49
        assert cl["recent"] == 1.23
        assert cl["limit"] == 100.0
        assert cl["suspended"] is True
        assert cl["suspend_reason"] == "payment-method"
        assert cl["billing_type"] == "balance"

    def test_negative_balance(self):
        cl = _parse_checklist(json.dumps({"stripe_balance": -7.5}))
        assert cl["stripe_balance"] == -7.5

    def test_malformed(self):
        assert _parse_checklist("nope") == {}

    def test_non_dict(self):
        assert _parse_checklist(json.dumps([1])) == {}

    def test_missing_fields_none(self):
        cl = _parse_checklist(json.dumps({}))
        assert cl["stripe_balance"] is None
        assert cl["suspended"] is None
        assert cl["suspend_reason"] is None


# ── collector integration (via http_get seam) ───────────────────────────────


class TestCollectHappyPath:
    def test_single_month_spend_and_balance(self):
        """Current month only: spend aggregated, checklist mapped, remaining computed."""
        # cost 207+1039 cents = 1246 cents = $12.46 spent
        router = _single_month_router(
            [(1595024, 1.3e-4, 207), (57724032, 1.8e-5, 1039)],
            json.dumps({"stripe_balance": -7.5, "recent": 2.0, "suspended": False,
                        "suspend_reason": None, "billing_type": "balance"}),
        )
        res = collect_deepinfra_balance("key", starting_balance=20.0, http_get=router)
        assert res.ok
        assert res.total_spent_usd == pytest.approx(12.46)
        assert res.remaining_usd == pytest.approx(20.0 - 12.46)
        assert res.stripe_balance == -7.5
        assert res.ready_to_spend_usd == 7.5
        assert res.money_owed_usd == 0.0
        assert res.recent_usd == 2.0
        assert res.suspended is False
        assert res.billing_type == "balance"
        assert res.error is None

    def test_unit_conversion_matches_real_deepseek_rate(self):
        """DeepSeek-V4-Pro input at 1.300e-4 cents/token == $1.30/M (codebase seed).

        1,000,000 tokens * 1.300e-4 cents/token = 130 cents = $1.30.
        """
        router = _single_month_router(
            [(1_000_000, 1.300e-4, 130)],
            json.dumps({"stripe_balance": -5.0}),
        )
        res = collect_deepinfra_balance("key", starting_balance=10.0, http_get=router)
        assert res.total_spent_usd == pytest.approx(1.30)
        assert res.remaining_usd == pytest.approx(8.70)

    def test_checklist_positive_balance_is_owed(self):
        router = _single_month_router(
            [(100, 1e-4, 1)],
            json.dumps({"stripe_balance": 0.49, "suspended": True,
                        "suspend_reason": "payment-method", "billing_type": "balance"}),
        )
        res = collect_deepinfra_balance("key", http_get=router)
        assert res.stripe_balance == 0.49
        assert res.ready_to_spend_usd == 0.0
        assert res.money_owed_usd == 0.49
        assert res.suspended is True
        assert res.suspend_reason == "payment-method"


class TestCollectNeverRaises:
    def test_no_api_key(self):
        res = collect_deepinfra_balance(None)
        assert res.error == "no api key"
        assert res.total_spent_usd is None
        assert res.remaining_usd is None
        assert res.ok is False

    def test_empty_api_key(self):
        res = collect_deepinfra_balance("")
        assert res.error == "no api key"

    def test_network_failure_on_first_call(self):
        fb = FakeBilling({"/payment/usage": (None, "")})  # status None = transport fail
        res = collect_deepinfra_balance("key", http_get=fb)
        assert res.ok is False
        assert "network" in (res.error or "").lower()
        assert res.total_spent_usd is None

    def test_http_500_on_usage(self):
        fb = FakeBilling({
            "/payment/usage": (500, '{"detail":"boom"}'),
            "/payment/checklist": (200, json.dumps({"stripe_balance": -1.0})),
        })
        res = collect_deepinfra_balance("key", http_get=fb)
        # spend failed but checklist succeeded -> partial result
        assert res.total_spent_usd is None
        assert res.remaining_usd is None
        assert res.stripe_balance == -1.0
        assert res.ready_to_spend_usd == 1.0
        assert res.ok is False
        assert "500" in (res.error or "")

    def test_http_500_on_both(self):
        fb = FakeBilling({
            "/payment/usage": (500, ""),
            "/payment/checklist": (503, ""),
        })
        res = collect_deepinfra_balance("key", http_get=fb)
        assert res.ok is False
        assert res.total_spent_usd is None
        assert res.stripe_balance is None

    def test_malformed_json_does_not_raise(self):
        fb = FakeBilling({
            "/payment/usage": (200, "<<<not json>>>"),
            "/payment/checklist": (200, "{broken"),
        })
        res = collect_deepinfra_balance("key", http_get=fb)
        # JSON parse failed but HTTP 200 -> spend 0, balance None, no exception
        assert res.total_spent_usd == pytest.approx(0.0)
        assert res.stripe_balance is None

    def test_non_dict_response_body(self):
        fb = FakeBilling({
            "/payment/usage": (200, json.dumps([1, 2, 3])),
            "/payment/checklist": (200, json.dumps("a string")),
        })
        res = collect_deepinfra_balance("key", http_get=fb)
        assert res.total_spent_usd == pytest.approx(0.0)
        assert res.stripe_balance is None


class TestCollectMultiMonth:
    def test_aggregates_across_months(self):
        """initial_month two months before current: all months summed."""
        current = _current_period()
        # compute two months before current
        y, m = (int(x) for x in current.split("."))
        for _ in range(2):
            m -= 1
            if m < 1:
                m, y = 12, y - 1
        two_back = "%04d.%02d" % (y, m)
        one_back_m = m + 1 if m < 12 else 1
        one_back_y = y if m < 12 else y + 1
        one_back = "%04d.%02d" % (one_back_y, one_back_m)

        def route(url, headers, timeout):
            if "/payment/checklist" in url:
                return (200, json.dumps({"stripe_balance": -10.0}))
            period = _period_of(url)
            if period == current:
                return (200, _usage_body([(100, 1e-4, 50)], period=current,
                                         initial_month=two_back))
            if period == two_back:
                return (200, _usage_body([(100, 1e-4, 100)], period=two_back,
                                         initial_month=two_back))
            if period == one_back:
                return (200, _usage_body([(100, 1e-4, 200)], period=one_back,
                                         initial_month=two_back))
            return (200, json.dumps({"months": [], "initial_month": two_back}))

        res = collect_deepinfra_balance("key", starting_balance=100.0, http_get=route)
        assert res.ok
        # 50 + 100 + 200 = 350 cents = $3.50
        assert res.total_spent_usd == pytest.approx(3.50)
        assert res.remaining_usd == pytest.approx(96.50)
        assert res.initial_month == two_back
        assert two_back in res.months_covered
        assert one_back in res.months_covered
        assert current in res.months_covered
        assert len(res.months_covered) == 3

    def test_months_limit_caps_total_queries(self):
        """months_limit caps the TOTAL usage GETs (current + forward)."""
        current = _current_period()
        y, m = (int(x) for x in current.split("."))
        m -= 6
        if m < 1:
            m, y = m + 12, y - 1
        far = "%04d.%02d" % (y, m)

        usage_gets = {"n": 0}

        def route(url, headers, timeout):
            if "/payment/checklist" in url:
                return (200, json.dumps({"stripe_balance": -1.0}))
            usage_gets["n"] += 1
            period = _period_of(url)
            if period == current:
                return (200, _usage_body([(1, 1e-4, 1)], period=current, initial_month=far))
            return (200, _usage_body([(1, 1e-4, 1)], period=period, initial_month=far))

        res = collect_deepinfra_balance("key", months_limit=3, http_get=route)
        assert res.ok
        # current + at most (months_limit-1) forward = at most months_limit total
        assert usage_gets["n"] <= 3

    def test_partial_month_failure_keeps_others(self):
        """A 500 on one historical month is skipped; other months still summed."""
        current = _current_period()
        y, m = (int(x) for x in current.split("."))
        m -= 1
        if m < 1:
            m, y = 12, y - 1
        prev = "%04d.%02d" % (y, m)

        def route(url, headers, timeout):
            if "/payment/checklist" in url:
                return (200, json.dumps({"stripe_balance": -1.0}))
            period = _period_of(url)
            if period == prev:
                return (500, '{"detail":"boom"}')  # this month fails
            if period == current:
                return (200, _usage_body([(100, 1e-4, 50)], period=current, initial_month=prev))
            return (200, json.dumps({"months": [], "initial_month": prev}))

        res = collect_deepinfra_balance("key", http_get=route)
        assert res.total_spent_usd == pytest.approx(0.50)  # only current month counted
        # error notes a partial failure
        assert res.error is not None
        assert "partial" in (res.error or "").lower() or "failed" in (res.error or "").lower()


class TestCollectorUsesMonthQueries:
    def test_uses_period_string_not_range(self):
        """The collector must query by YYYY.MM period (from=), never the
        31-day-capped from+to range, so lifetime spend isn't truncated."""
        fb = FakeBilling({
            "/payment/usage": (200, _usage_body([(100, 1e-4, 1)], initial_month="1900.01")),
            "/payment/checklist": (200, json.dumps({"stripe_balance": -1.0})),
        })
        collect_deepinfra_balance("key", http_get=fb)
        usage_calls = [u for u in fb.urls_called if "/payment/usage" in u]
        assert len(usage_calls) >= 1
        for u in usage_calls:
            assert "from=" in u
            assert "to=" not in u

    def test_authorization_header_sent(self):
        seen = {}
        fb = FakeBilling({
            "/payment/usage": (200, _usage_body([(1, 1e-4, 1)])),
            "/payment/checklist": (200, json.dumps({})),
        })

        def capturing(url, headers, timeout):
            seen["auth"] = headers.get("Authorization")
            return fb(url, headers, timeout)

        collect_deepinfra_balance("secret-key", http_get=capturing)
        assert seen["auth"] == "Bearer secret-key"


class TestDeepInfraBalanceDataclass:
    def test_default_fields_none(self):
        b = DeepInfraBalance()
        assert b.total_spent_usd is None
        assert b.remaining_usd is None
        assert b.stripe_balance is None
        assert b.ok is False
        assert b.error is None

    def test_ok_property_true_when_spend_present(self):
        b = DeepInfraBalance(total_spent_usd=1.0, remaining_usd=4.0)
        assert b.ok is True

    def test_ok_false_when_error_set(self):
        b = DeepInfraBalance(total_spent_usd=1.0, error="partial")
        assert b.ok is False
