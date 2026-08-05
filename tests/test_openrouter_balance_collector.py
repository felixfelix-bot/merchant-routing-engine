"""Tests for src/openrouter_balance_collector.py — OpenRouter balance collector.

Covers:
  * parsing every documented response shape (full envelope + bare data dict)
  * usage_fraction derivation for all edge cases the gate requires:
      - unlimited via null limit (docs)        -> 0.0
      - unlimited via limit=-1 (task body)      -> 0.0
      - unlimited via limit<=0 (defensive)      -> 0.0
      - exhausted via limit_remaining<=0        -> 1.0
      - exhausted via usage>=limit              -> 1.0
      - normal via limit_remaining              -> 1 - rem/limit
      - normal via usage fallback               -> usage/limit
      - clamping to [0, 1]
  * the NEVER-RAISES invariant against garbage inputs
  * HTTP collection (monkeypatched urllib): 200/parse, 401, network error,
    missing key, non-200, bad JSON
  * SQLite round-trip: store -> get_latest, idempotent table, time-series,
    None handling
  * used_pct property (live_router compat), default_db_path, cron main()
"""
from __future__ import annotations

import io
import json
import time
from unittest.mock import patch

import pytest

from src.openrouter_balance_collector import (
    OPENROUTER_DEFAULT_TIMEOUT,
    OPENROUTER_KEY_ENDPOINT,
    OpenRouterBalance,
    _as_float,
    _compute_usage_fraction,
    _is_unlimited_limit,
    collect_and_store_openrouter,
    collect_openrouter_balance,
    default_db_path,
    get_latest_openrouter_balance,
    main,
    parse_openrouter_key,
    store_openrouter_balance,
)


# ── canned response builder ──────────────────────────────────────────────────
def _resp(usage=2.0, limit: float | None = 10.0,
          limit_remaining: float | None = 8.0, **extra):
    """A full {data: {...}} OpenRouter /api/v1/key envelope."""
    data = {
        "label": "prod-key",
        "limit": limit,
        "limit_reset": None,
        "limit_remaining": limit_remaining,
        "usage": usage,
        "is_free_tier": False,
    }
    data.update(extra)
    return {"data": data}


# ════════════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════════════
class TestAsFloat:
    def test_int_float_string(self):
        assert _as_float(3) == 3.0
        assert _as_float(2.5) == 2.5
        assert _as_float("1.25") == 1.25

    def test_none(self):
        assert _as_float(None) is None

    def test_bool_rejected(self):
        assert _as_float(True) is None
        assert _as_float(False) is None

    def test_nan_inf(self):
        assert _as_float(float("nan")) is None
        assert _as_float(float("inf")) is None
        assert _as_float(float("-inf")) is None

    def test_garbage(self):
        assert _as_float("not a number") is None
        assert _as_float([1, 2]) is None

    def test_negative_allowed(self):
        assert _as_float(-0.5) == -0.5


class TestIsUnlimited:
    def test_none_is_unlimited(self):
        assert _is_unlimited_limit(None) is True

    def test_nonpositive_is_unlimited(self):
        assert _is_unlimited_limit(-1.0) is True
        assert _is_unlimited_limit(-0.01) is True
        assert _is_unlimited_limit(0.0) is True

    def test_positive_is_not_unlimited(self):
        assert _is_unlimited_limit(10.0) is False


# ════════════════════════════════════════════════════════════════════════════
# usage_fraction derivation
# ════════════════════════════════════════════════════════════════════════════
class TestUsageFraction:
    def test_normal_via_limit_remaining(self):
        assert _compute_usage_fraction(2.0, 10.0, 8.0) == pytest.approx(0.2)

    def test_normal_via_usage_fallback(self):
        assert _compute_usage_fraction(3.0, 10.0, None) == pytest.approx(0.3)

    def test_unlimited_null_limit(self):
        assert _compute_usage_fraction(5.0, None, None) == 0.0

    def test_unlimited_minus_one(self):
        assert _compute_usage_fraction(999.0, -1.0, None) == 0.0

    def test_unlimited_zero_limit(self):
        assert _compute_usage_fraction(1.0, 0.0, None) == 0.0

    def test_exhausted_remaining_zero(self):
        assert _compute_usage_fraction(10.0, 10.0, 0.0) == 1.0

    def test_exhausted_remaining_negative(self):
        assert _compute_usage_fraction(10.5, 10.0, -0.5) == 1.0

    def test_exhausted_via_usage_ge_limit(self):
        assert _compute_usage_fraction(10.0, 10.0, None) == 1.0
        assert _compute_usage_fraction(11.0, 10.0, None) == 1.0

    def test_fresh_key_no_usage_no_remaining(self):
        assert _compute_usage_fraction(None, 10.0, None) == 0.0

    def test_clamp_high_remaining(self):
        assert _compute_usage_fraction(0.0, 10.0, 15.0) == 0.0

    def test_half(self):
        assert _compute_usage_fraction(5.0, 10.0, 5.0) == pytest.approx(0.5)

    def test_within_unit_interval(self):
        for u, lim, rem in [(1.0, 10.0, 9.0), (9.0, 10.0, 1.0), (9.9, 10.0, 0.1)]:
            frac = _compute_usage_fraction(u, lim, rem)
            assert 0.0 <= frac <= 1.0


# ════════════════════════════════════════════════════════════════════════════
# parse_openrouter_key
# ════════════════════════════════════════════════════════════════════════════
class TestParse:
    def test_full_envelope(self):
        b = parse_openrouter_key(_resp(usage=2.0, limit=10.0, limit_remaining=8.0))
        assert b is not None
        assert b.usage == 2.0
        assert b.limit == 10.0
        assert b.limit_remaining == 8.0
        assert b.usage_fraction == pytest.approx(0.2)
        assert b.is_unlimited is False
        assert b.is_free_tier is False
        assert b.label == "prod-key"
        assert b.used_pct == pytest.approx(20.0)

    def test_bare_data_dict(self):
        b = parse_openrouter_key(
            {"usage": 5.0, "limit": 10.0, "limit_remaining": 5.0,
             "is_free_tier": True}
        )
        assert b is not None
        assert b.usage_fraction == pytest.approx(0.5)
        assert b.is_free_tier is True

    def test_unlimited_null(self):
        b = parse_openrouter_key(_resp(usage=5.0, limit=None, limit_remaining=None))
        assert b is not None
        assert b.is_unlimited is True
        assert b.usage_fraction == 0.0
        assert b.used_pct == 0.0

    def test_unlimited_minus_one(self):
        b = parse_openrouter_key(_resp(usage=5.0, limit=-1, limit_remaining=None))
        assert b is not None
        assert b.is_unlimited is True
        assert b.usage_fraction == 0.0

    def test_exhausted(self):
        b = parse_openrouter_key(_resp(usage=10.0, limit=10.0, limit_remaining=0.0))
        assert b is not None
        assert b.is_unlimited is False
        assert b.usage_fraction == 1.0
        assert b.is_exhausted is True
        assert b.used_pct == pytest.approx(100.0)

    def test_limit_reset_and_label(self):
        b = parse_openrouter_key(_resp(limit_reset="monthly", label="ci-key"))
        assert b is not None
        assert b.limit_reset == "monthly"
        assert b.label == "ci-key"

    def test_remaining_alias(self):
        b = parse_openrouter_key(_resp(limit_remaining=3.3))
        assert b is not None
        assert b.remaining == 3.3
        assert b.remaining == b.limit_remaining

    def test_is_exhausted_unlimited_false(self):
        b = parse_openrouter_key(_resp(limit=None))
        assert b is not None
        assert b.is_exhausted is False


# ════════════════════════════════════════════════════════════════════════════
# never-raises invariant
# ════════════════════════════════════════════════════════════════════════════
class TestNeverRaises:
    @pytest.mark.parametrize("junk", [
        None, [], "not a dict", 42, 3.14, {},
        {"data": None}, {"data": "wrong"}, {"data": []}, {"data": {}},
        {"nope": {"usage": "x"}},
        {"data": {"usage": float("nan"), "limit": float("inf")}},
        {"data": {"usage": "abc", "limit": [1]}},
        {"data": {"limit": True, "usage": False}},
        {"data": {"is_free_tier": "yes"}},
    ])
    def test_does_not_raise(self, junk):
        result = parse_openrouter_key(junk)
        assert result is None or isinstance(result, OpenRouterBalance)

    def test_empty_data_yields_unlimited(self):
        b = parse_openrouter_key({"data": {}})
        assert b is not None
        assert b.is_unlimited is True
        assert b.usage_fraction == 0.0
        assert b.usage is None


# ════════════════════════════════════════════════════════════════════════════
# collect_openrouter_balance (HTTP, monkeypatched)
# ════════════════════════════════════════════════════════════════════════════
class _FakeResponse:
    def __init__(self, body, status=200):
        self._body = body if isinstance(body, bytes) else body.encode()
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestCollectHttp:
    def test_missing_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        called = []

        def boom(*a, **kw):
            called.append(1)
            raise AssertionError("must not call network without a key")

        monkeypatch.setattr(
            "src.openrouter_balance_collector.urllib.request.urlopen", boom
        )
        assert collect_openrouter_balance() is None
        assert called == []

    def test_success_parses(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        envelope = _resp(usage=3.0, limit=10.0, limit_remaining=7.0)
        monkeypatch.setattr(
            "src.openrouter_balance_collector.urllib.request.urlopen",
            lambda req, timeout=None: _FakeResponse(json.dumps(envelope)),
        )
        b = collect_openrouter_balance(api_key="sk-test")
        assert b is not None
        assert b.usage == 3.0
        assert b.usage_fraction == pytest.approx(0.3)

    def test_env_key_used_when_no_arg(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["header"] = req.headers.get("Authorization")
            return _FakeResponse(json.dumps(_resp()))

        monkeypatch.setattr(
            "src.openrouter_balance_collector.urllib.request.urlopen", fake_urlopen
        )
        b = collect_openrouter_balance()
        assert b is not None
        assert captured["header"] == "Bearer sk-env"

    def test_http_401_returns_none(self, monkeypatch):
        import urllib.error

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-bad")
        monkeypatch.setattr(
            "src.openrouter_balance_collector.urllib.request.urlopen",
            lambda req, timeout=None: (_ for _ in ()).throw(
                urllib.error.HTTPError(
                    OPENROUTER_KEY_ENDPOINT, 401, "Unauthorized",
                    {},  # type: ignore[arg-type]
                    io.BytesIO(b"{}"),
                )
            ),
        )
        assert collect_openrouter_balance() is None

    def test_network_error_returns_none(self, monkeypatch):
        import urllib.error

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-x")
        monkeypatch.setattr(
            "src.openrouter_balance_collector.urllib.request.urlopen",
            lambda req, timeout=None: (_ for _ in ()).throw(
                urllib.error.URLError("connection refused")
            ),
        )
        assert collect_openrouter_balance() is None

    def test_non_200_returns_none(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-x")
        monkeypatch.setattr(
            "src.openrouter_balance_collector.urllib.request.urlopen",
            lambda req, timeout=None: _FakeResponse(b"{}", status=500),
        )
        assert collect_openrouter_balance() is None

    def test_bad_json_returns_none(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-x")
        monkeypatch.setattr(
            "src.openrouter_balance_collector.urllib.request.urlopen",
            lambda req, timeout=None: _FakeResponse(b"<<<not json>>>"),
        )
        assert collect_openrouter_balance() is None

    def test_unlimited_live_shape(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        envelope = _resp(usage=123.0, limit=None, limit_remaining=None)
        monkeypatch.setattr(
            "src.openrouter_balance_collector.urllib.request.urlopen",
            lambda req, timeout=None: _FakeResponse(json.dumps(envelope)),
        )
        b = collect_openrouter_balance(api_key="sk-u")
        assert b is not None
        assert b.is_unlimited is True
        assert b.usage_fraction == 0.0


# ════════════════════════════════════════════════════════════════════════════
# SQLite persistence
# ════════════════════════════════════════════════════════════════════════════
class TestStorage:
    def test_store_and_read_back(self, tmp_path):
        db = str(tmp_path / "bal.db")
        b = OpenRouterBalance(
            usage=2.0, limit=10.0, limit_remaining=8.0,
            usage_fraction=0.2, is_unlimited=False, is_free_tier=False,
            limit_reset="monthly", raw={"usage": 2.0},
        )
        assert store_openrouter_balance(db, b) is True

        got = get_latest_openrouter_balance(db)
        assert got is not None
        assert got.usage == 2.0
        assert got.limit == 10.0
        assert got.limit_remaining == 8.0
        assert got.usage_fraction == pytest.approx(0.2)
        assert got.is_unlimited is False
        assert got.is_free_tier is False

    def test_table_creation_idempotent(self, tmp_path):
        import sqlite3

        db = str(tmp_path / "bal.db")
        b = OpenRouterBalance(1.0, 10.0, 9.0, 0.1, False)
        store_openrouter_balance(db, b)
        store_openrouter_balance(db, b)
        conn = sqlite3.connect(db)
        tabs = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        assert tabs.count("provider_balances") == 1

    def test_time_series_latest_wins(self, tmp_path):
        db = str(tmp_path / "bal.db")
        old = OpenRouterBalance(5.0, 10.0, 5.0, 0.5, False,
                                collected_at=time.time() - 100)
        new = OpenRouterBalance(9.0, 10.0, 1.0, 0.9, False,
                                collected_at=time.time())
        store_openrouter_balance(db, old)
        store_openrouter_balance(db, new)
        got = get_latest_openrouter_balance(db)
        assert got is not None
        assert got.usage_fraction == pytest.approx(0.9)

    def test_store_none_returns_false(self, tmp_path):
        db = str(tmp_path / "bal.db")
        assert store_openrouter_balance(db, None) is False  # type: ignore[arg-type]

    def test_get_latest_empty_returns_none(self, tmp_path):
        db = str(tmp_path / "bal.db")
        assert get_latest_openrouter_balance(db) is None

    def test_store_db_error_returns_false(self, tmp_path):
        b = OpenRouterBalance(1.0, 10.0, 9.0, 0.1, False)
        assert store_openrouter_balance("/no/such/dir/x/db", b) is False


# ════════════════════════════════════════════════════════════════════════════
# collect_and_store + cron main + default_db_path
# ════════════════════════════════════════════════════════════════════════════
class TestCollectAndStore:
    def test_success_stores_and_returns(self, tmp_path, monkeypatch):
        db = str(tmp_path / "bal.db")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(
            "src.openrouter_balance_collector.urllib.request.urlopen",
            lambda req, timeout=None: _FakeResponse(
                json.dumps(_resp(usage=4.0, limit=10.0, limit_remaining=6.0))
            ),
        )
        b = collect_and_store_openrouter(db_path=db, api_key="sk-t")
        assert b is not None
        assert b.usage_fraction == pytest.approx(0.4)
        got = get_latest_openrouter_balance(db)
        assert got is not None
        assert got.usage_fraction == pytest.approx(0.4)

    def test_collection_failure_returns_none_no_store(self, tmp_path, monkeypatch):
        db = str(tmp_path / "bal.db")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        b = collect_and_store_openrouter(db_path=db)
        assert b is None
        assert get_latest_openrouter_balance(db) is None

    def test_collect_without_db_still_returns(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(
            "src.openrouter_balance_collector.urllib.request.urlopen",
            lambda req, timeout=None: _FakeResponse(
                json.dumps(_resp(usage=0.0, limit=10.0, limit_remaining=10.0))
            ),
        )
        b = collect_and_store_openrouter(db_path=None, api_key="sk-t")
        assert b is not None
        assert b.usage_fraction == 0.0


class TestDefaultDbPath:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("API_BURN_DB", "/custom/path.db")
        assert default_db_path() == "/custom/path.db"

    def test_default(self, monkeypatch):
        import os

        monkeypatch.delenv("API_BURN_DB", raising=False)
        assert default_db_path() == os.path.expanduser("~/.hermes/bot/api_burn.db")


class TestMain:
    def test_no_key_returns_1(self, monkeypatch, capsys):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        rc = main([])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert out["provider"] == "openrouter"
        assert "not set" in out["error"]

    def test_success_returns_0(self, monkeypatch, capsys, tmp_path):
        db = str(tmp_path / "bal.db")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-m")
        monkeypatch.setattr(
            "src.openrouter_balance_collector.urllib.request.urlopen",
            lambda req, timeout=None: _FakeResponse(
                json.dumps(_resp(usage=1.0, limit=10.0, limit_remaining=9.0))
            ),
        )
        rc = main(["--db", db])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["provider"] == "openrouter"
        assert out["usage_fraction"] == pytest.approx(0.1)
        assert out["used_pct"] == pytest.approx(10.0)
        assert out["db_path"] == db

    def test_api_failure_returns_1(self, monkeypatch, capsys, tmp_path):
        db = str(tmp_path / "bal.db")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-m")
        monkeypatch.setattr(
            "src.openrouter_balance_collector.urllib.request.urlopen",
            lambda req, timeout=None: (_ for _ in ()).throw(
                __import__("urllib.error", fromlist=["URLError"]).URLError("down")
            ),
        )
        rc = main(["--db", db])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert "failed" in out["error"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
