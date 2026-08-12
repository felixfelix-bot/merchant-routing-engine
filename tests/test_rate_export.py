"""Tests for src/rate_export.py — RP-5a CVM dashboard rate export.

Covers the RP-5a acceptance:
  - export_rates() returns the 4 dashboard providers with the required fields
  - provider with measured cost_usd data → source="measured", measured=True, rate>0
  - empty / missing DB → every provider degrades to "fallback" (never raises)
  - --all adds deepinfra + openrouter
  - CLI --out writes valid JSON; stdout prints valid JSON
  - bad db_path never raises (dashboard always renders a complete table)
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout

import pytest

from src import rate_export
from src.rate_export import DASHBOARD_PROVIDERS, ALL_PROVIDERS, export_rates, main

# Production schema (mirrors tests/test_real_price_tracker.py)
_API_CALLS_DDL = """
CREATE TABLE api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    key_name TEXT,
    key_suffix TEXT,
    model TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    tier TEXT,
    cache_hit INTEGER DEFAULT 0,
    ollama_hit INTEGER DEFAULT 0,
    ppq_hit INTEGER DEFAULT 0,
    status_code INTEGER,
    error TEXT,
    duration_ms INTEGER,
    cost_usd REAL,
    cost_source TEXT
)
"""


@pytest.fixture
def db():
    """Fresh temp DB with the production api_calls schema. Clears the tracker
    cache before/after so each test is isolated."""
    from src.real_price_tracker import clear_cache

    clear_cache()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(_API_CALLS_DDL)
    conn.commit()
    conn.close()
    yield path
    clear_cache()
    try:
        os.unlink(path)
    except OSError:
        pass


def _seed(db_path, rows):
    """Insert (ts, key_name, model, total_tokens, cost_usd) rows."""
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO api_calls (ts, key_name, model, total_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _now():
    import time

    return time.time()


REQUIRED_KEYS = {"provider", "rate_per_m", "source", "measured", "window_hours", "fallback_reason"}


# ── export_rates: shape & defaults ───────────────────────────────────────────


class TestExportRates:
    def test_default_returns_four_dashboard_providers(self, db):
        out = export_rates(db_path=db)
        assert [e["provider"] for e in out] == list(DASHBOARD_PROVIDERS)
        assert len(out) == 4

    def test_every_entry_has_required_fields_and_types(self, db):
        out = export_rates(db_path=db)
        for e in out:
            assert REQUIRED_KEYS <= set(e.keys())
            assert isinstance(e["provider"], str)
            assert isinstance(e["rate_per_m"], float)
            assert e["source"] in ("measured", "fallback")
            assert isinstance(e["measured"], bool)
            assert e["measured"] == (e["source"] == "measured")
            assert e["fallback_reason"] is None or isinstance(e["fallback_reason"], str)

    def test_empty_db_all_fallback_never_measured(self, db):
        """No costed data anywhere → every provider is a fallback, rate still a float."""
        out = export_rates(db_path=db)
        assert len(out) == 4
        for e in out:
            assert e["source"] == "fallback"
            assert e["measured"] is False
            assert e["rate_per_m"] > 0  # seed/last-resort, never zero/negative
            assert e["fallback_reason"] in ("seed", "last_resort", "unknown")

    def test_measured_provider_reports_measured(self, db):
        """Seed >= MIN_CALLS_FOR_RATE costed ppq calls → ppq row is measured."""
        from src.real_price_tracker import MIN_CALLS_FOR_RATE

        now = _now()
        n = MIN_CALLS_FOR_RATE + 50  # well above the threshold
        # n calls × (1000 tokens, $0.0001) → rate = 0.1 $/M
        rows = [(now - 100, "ppq", "deepseek-v4-flash", 1000, 0.0001) for _ in range(n)]
        _seed(db, rows)

        out = {e["provider"]: e for e in export_rates(db_path=db)}
        ppq = out["ppq"]
        assert ppq["source"] == "measured"
        assert ppq["measured"] is True
        assert ppq["fallback_reason"] is None
        assert ppq["rate_per_m"] == pytest.approx(0.1, rel=1e-6)

    def test_window_hours_reflects_provider_config(self, db):
        """window_hours matches PROVIDER_WINDOW_HOURS per provider."""
        from src.real_price_tracker import PROVIDER_WINDOW_HOURS

        out = {e["provider"]: e for e in export_rates(db_path=db)}
        for p, entry in out.items():
            assert entry["window_hours"] == PROVIDER_WINDOW_HOURS[p]

    def test_all_includes_all_tracked_providers(self, db):
        out = export_rates(ALL_PROVIDERS, db_path=db)
        assert [e["provider"] for e in out] == list(ALL_PROVIDERS)
        assert len(out) == len(ALL_PROVIDERS)
        assert "deepinfra" in [e["provider"] for e in out]
        assert "openrouter" in [e["provider"] for e in out]

    def test_bad_db_path_never_raises_all_fallback(self):
        """A non-existent DB path must not raise; every row degrades to fallback."""
        out = export_rates(db_path="/nonexistent/path/nope.db")
        assert len(out) == 4
        for e in out:
            assert e["source"] == "fallback"
            assert e["rate_per_m"] > 0

    def test_explicit_provider_list_respected(self, db):
        out = export_rates(["ppq", "ollama_cloud"], db_path=db)
        assert [e["provider"] for e in out] == ["ppq", "ollama_cloud"]


# ── CLI: main() ──────────────────────────────────────────────────────────────


class TestCLI:
    def test_stdout_prints_valid_json_with_four_providers(self, db):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--db", db])
        assert rc == 0
        payload = json.loads(buf.getvalue())
        assert payload["source"] == "real_price_tracker.get_rate_with_fallback"
        assert "generated_at" in payload
        provs = payload["providers"]
        assert [p["provider"] for p in provs] == list(DASHBOARD_PROVIDERS)

    def test_out_writes_atomic_json_file(self, db, tmp_path):
        out_file = str(tmp_path / "rates.json")
        rc = main(["--db", db, "--out", out_file])
        assert rc == 0
        assert os.path.exists(out_file)
        with open(out_file) as f:
            payload = json.load(f)
        assert len(payload["providers"]) == 4
        for e in payload["providers"]:
            assert REQUIRED_KEYS <= set(e.keys())

    def test_all_flag_adds_extras(self, db, tmp_path):
        out_file = str(tmp_path / "rates_all.json")
        rc = main(["--db", db, "--out", out_file, "--all"])
        assert rc == 0
        with open(out_file) as f:
            payload = json.load(f)
        names = [e["provider"] for e in payload["providers"]]
        assert names == list(ALL_PROVIDERS)
        assert len(names) == len(ALL_PROVIDERS)

    def test_pretty_indents(self, db):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--db", db, "--pretty"])
        text = buf.getvalue()
        # pretty output is multi-line with 2-space indentation
        assert "\n  " in text
        # still valid JSON
        assert json.loads(text)["providers"]

    def test_bad_db_stdout_still_valid(self):
        """CLI against a missing DB must exit 0 and emit a complete fallback table."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--db", "/nonexistent/nope.db"])
        assert rc == 0
        payload = json.loads(buf.getvalue())
        assert len(payload["providers"]) == 4
        for e in payload["providers"]:
            assert e["source"] == "fallback"
