"""Tests for scripts/ollama_usage_shape_canary.py — usage-API shape canary.

TDD: written BEFORE the implementation. The canary is a silent cron
watchdog over ollama.com/api/usage: empty stdout = nothing changed, any
output = delivered to the operator chat. Covers:
- shape fingerprint extraction (session/weekly presence, both API shapes)
- credit|pool|allowance|balance|entitlement detection (credit-pool sunset signal)
- drift vs no-drift (values changing is NOT drift — only shape/keys)
- failure handling: 1 error silent, 2 consecutive -> unreachable message
- first-run baseline, state-file lifecycle, OLLAMA_CANARY_STATE env override
- secrets never leak: no API key / raw response body in any output or state
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.ollama_usage_shape_canary import (  # noqa: E402
    CREDIT_TOKENS,
    DEFAULT_STATE,
    fetch_usage,
    main,
    run,
    shape_fingerprint,
)


# Fixture matching the real /api/usage shape the engine consumes:
# data.limits.session.usage (0-1 fraction, 5h) / data.limits.weekly.usage (7d).
REAL_SHAPE = {
    "data": {
        "limits": {
            "session": {"usage": 0.06, "window": "5h"},
            "weekly": {"usage": 0.11, "window": "7d"},
        }
    }
}

# The src/ollama_extra_usage.py consumer reads limits at the response root —
# the canary must recognise both shapes as "session/weekly present".
ROOT_SHAPE = {"limits": {"session": {"usage": 0.06}, "weekly": {"usage": 0.11}}}

STATE = "state.json"


def _ok_fetch(data, status=200):
    return lambda: (status, data)


def _err_fetch(exc):
    def fetch():
        raise exc
    return fetch


# ── Shape fingerprint extraction ──────────────────────────────────────────────


class TestShapeFingerprint:
    def test_session_weekly_present_in_real_shape(self):
        fp = shape_fingerprint(200, REAL_SHAPE)
        assert fp["session_limit"] is True
        assert fp["weekly_limit"] is True

    def test_session_weekly_present_in_root_shape(self):
        """Consumer shape (limits at root) also flags session/weekly present."""
        fp = shape_fingerprint(200, ROOT_SHAPE)
        assert fp["session_limit"] is True
        assert fp["weekly_limit"] is True

    def test_missing_limits_flag_false(self):
        fp = shape_fingerprint(200, {"unrelated": 1})
        assert fp["session_limit"] is False
        assert fp["weekly_limit"] is False

    def test_http_status_recorded(self):
        assert shape_fingerprint(200, {})["http_status"] == 200
        assert shape_fingerprint(204, {})["http_status"] == 204

    def test_top_level_keys_sorted(self):
        fp = shape_fingerprint(200, {"b": 1, "a": 2, "c": 3})
        assert fp["top_level_keys"] == ["a", "b", "c"]

    def test_no_credit_like_keys_in_current_shape(self):
        assert shape_fingerprint(200, REAL_SHAPE)["credit_like_keys"] == []

    def test_usage_values_never_in_fingerprint(self):
        """GATE: usage fractions change every run — they must NOT drift."""
        fp = json.dumps(shape_fingerprint(200, REAL_SHAPE))
        assert "0.06" not in fp
        assert "0.11" not in fp


# ── Credit-pool (sunset signal) detection ─────────────────────────────────────


class TestCreditLikeDetection:
    def test_nested_credit_balance_detected(self):
        resp = {"data": {"limits": {}, "billing": {"credit_balance": 5}}}
        fp = shape_fingerprint(200, resp)
        assert fp["credit_like_keys"] == ["data.billing.credit_balance"]

    def test_case_insensitive_match(self):
        resp = {"Entitlements": {"tier": "pro"}}
        assert shape_fingerprint(200, resp)["credit_like_keys"] == ["Entitlements"]

    def test_nested_inside_list(self):
        resp = {"plans": [{"pool": "starter"}, {"name": "x"}]}
        assert shape_fingerprint(200, resp)["credit_like_keys"] == ["plans[0].pool"]

    def test_all_five_signal_tokens_detected(self):
        for token in CREDIT_TOKENS:
            resp = {token: 1}
            assert shape_fingerprint(200, resp)["credit_like_keys"] == [token]

    def test_benign_keys_not_flagged(self):
        resp = {"data": {"limits": {"session": {"usage": 0.5, "resets_in": 60}}}}
        assert shape_fingerprint(200, resp)["credit_like_keys"] == []


# ── Drift / no-drift ───────────────────────────────────────────────────────────


class TestDriftDetection:
    def test_first_run_records_baseline(self, tmp_path):
        state = tmp_path / STATE
        msg = run(state, fetch=_ok_fetch(REAL_SHAPE))
        assert msg.startswith("baseline recorded: ")
        assert "status=200" in msg
        saved = json.loads(state.read_text())
        assert saved["last_fingerprint"] == shape_fingerprint(200, REAL_SHAPE)
        assert saved["consecutive_failures"] == 0
        assert saved["last_success_iso"]

    def test_no_drift_empty_output(self, tmp_path):
        state = tmp_path / STATE
        run(state, fetch=_ok_fetch(REAL_SHAPE))
        assert run(state, fetch=_ok_fetch(REAL_SHAPE)) == ""

    def test_usage_value_change_is_not_drift(self, tmp_path):
        """Same shape, different usage fractions — still silent."""
        state = tmp_path / STATE
        run(state, fetch=_ok_fetch(REAL_SHAPE))
        hotter = {"data": {"limits": {
            "session": {"usage": 0.99, "window": "5h"},
            "weekly": {"usage": 0.50, "window": "7d"},
        }}}
        assert run(state, fetch=_ok_fetch(hotter)) == ""

    def test_drift_on_new_credit_pool_field(self, tmp_path):
        state = tmp_path / STATE
        run(state, fetch=_ok_fetch(REAL_SHAPE))
        new = {"data": {"limits": {}, "credit_pool": 100}}
        msg = run(state, fetch=_ok_fetch(new))
        assert "DRIFT" in msg
        assert "credit_pool" in msg
        assert "old:" in msg and "new:" in msg
        # state now holds the NEW fingerprint
        saved = json.loads(state.read_text())
        assert saved["last_fingerprint"]["credit_like_keys"] == ["data.credit_pool"]

    def test_drift_on_removed_weekly_key(self, tmp_path):
        state = tmp_path / STATE
        run(state, fetch=_ok_fetch(REAL_SHAPE))
        shrunk = {"data": {"limits": {"session": {"usage": 0.1}}}}
        msg = run(state, fetch=_ok_fetch(shrunk))
        assert "DRIFT" in msg

    def test_drift_on_http_status_change(self, tmp_path):
        state = tmp_path / STATE
        run(state, fetch=_ok_fetch(REAL_SHAPE))
        msg = run(state, fetch=_ok_fetch(REAL_SHAPE, status=204))
        assert "DRIFT" in msg
        assert "status=204" in msg

    def test_drift_report_has_timestamp_and_no_values(self, tmp_path):
        state = tmp_path / STATE
        run(state, fetch=_ok_fetch(REAL_SHAPE))
        msg = run(state, fetch=_ok_fetch({"data": {"limits": {}, "credit_balance": 1}}))
        assert "20" in msg.splitlines()[0]  # ISO timestamp in the drift header
        assert "0.06" not in msg           # no raw response values
        assert "0.11" not in msg

    def test_corrupt_state_file_treated_as_first_run(self, tmp_path):
        state = tmp_path / STATE
        state.write_text("not json {{{")
        msg = run(state, fetch=_ok_fetch(REAL_SHAPE))
        assert msg.startswith("baseline recorded: ")


# ── Failure handling ───────────────────────────────────────────────────────────


class TestFailureHandling:
    ERR = urllib.error.URLError("Connection refused")

    def test_single_failure_silent(self, tmp_path):
        state = tmp_path / STATE
        assert run(state, fetch=_err_fetch(self.ERR)) == ""
        assert json.loads(state.read_text())["consecutive_failures"] == 1

    def test_two_consecutive_failures_unreachable(self, tmp_path):
        state = tmp_path / STATE
        run(state, fetch=_err_fetch(self.ERR))
        msg = run(state, fetch=_err_fetch(self.ERR))
        assert msg.startswith("unreachable 2 runs in a row:")
        assert "Connection refused" in msg

    def test_third_failure_counts_up(self, tmp_path):
        state = tmp_path / STATE
        for _ in range(3):
            msg = run(state, fetch=_err_fetch(self.ERR))
        assert msg.startswith("unreachable 3 runs in a row:")

    def test_http_error_counts_as_failure(self, tmp_path):
        state = tmp_path / STATE
        err = urllib.error.HTTPError(
            "https://ollama.com/api/usage", 401, "Unauthorized", None, None
        )
        run(state, fetch=_err_fetch(err))
        msg = run(state, fetch=_err_fetch(err))
        assert msg.startswith("unreachable 2 runs in a row:")
        assert "401" in msg

    def test_failure_never_overwrites_fingerprint(self, tmp_path):
        state = tmp_path / STATE
        baseline = run(state, fetch=_ok_fetch(REAL_SHAPE))
        assert baseline.startswith("baseline recorded")
        run(state, fetch=_err_fetch(self.ERR))
        run(state, fetch=_err_fetch(self.ERR))
        saved = json.loads(state.read_text())
        assert saved["last_fingerprint"] == shape_fingerprint(200, REAL_SHAPE)
        assert saved["consecutive_failures"] == 2

    def test_success_after_failures_diffs_no_drift_and_resets(self, tmp_path):
        state = tmp_path / STATE
        run(state, fetch=_ok_fetch(REAL_SHAPE))
        run(state, fetch=_err_fetch(self.ERR))
        run(state, fetch=_err_fetch(self.ERR))
        # API recovered with the SAME shape -> silent, counter reset, no drift
        assert run(state, fetch=_ok_fetch(REAL_SHAPE)) == ""
        assert json.loads(state.read_text())["consecutive_failures"] == 0


# ── fetch_usage: HTTP layer ────────────────────────────────────────────────────


class TestFetchUsage:
    def _mock_response(self, data, status=200):
        mock = MagicMock()
        mock.status = status
        mock.read.return_value = json.dumps(data).encode("utf-8")
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        return mock

    @patch("scripts.ollama_usage_shape_canary.urllib.request.urlopen")
    def test_fetch_returns_status_and_parsed_data(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response(REAL_SHAPE)
        status, data = fetch_usage(api_key="k")
        assert status == 200
        assert data == REAL_SHAPE

    @patch("scripts.ollama_usage_shape_canary.urllib.request.urlopen")
    def test_bearer_header_set(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response({})
        fetch_usage(api_key="my-secret-key")
        req = mock_urlopen.call_args[0][0]
        assert req.headers["Authorization"] == "Bearer my-secret-key"

    @patch("scripts.ollama_usage_shape_canary.urllib.request.urlopen")
    def test_timeout_is_5s(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response({})
        fetch_usage(api_key="k")
        assert mock_urlopen.call_args[1]["timeout"] == 5

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OLLAMA_CLOUD_API_KEY"):
            fetch_usage()


# ── main() wiring + secret safety ──────────────────────────────────────────────


class TestMainAndSecretSafety:
    def test_env_state_path_override(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("OLLAMA_CANARY_STATE", str(tmp_path / "s.json"))
        with patch("scripts.ollama_usage_shape_canary.fetch_usage",
                   return_value=(200, REAL_SHAPE)):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert out.startswith("baseline recorded: ")
        assert (tmp_path / "s.json").exists()

    def test_default_state_path(self):
        assert DEFAULT_STATE == Path(
            "~/.merchant-routing/ollama-usage-shape-state.json"
        ).expanduser()

    def test_no_drift_prints_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("OLLAMA_CANARY_STATE", str(tmp_path / "s.json"))
        with patch("scripts.ollama_usage_shape_canary.fetch_usage",
                   return_value=(200, REAL_SHAPE)):
            with pytest.raises(SystemExit):
                main()                     # baseline
            capsys.readouterr()            # drain baseline output
            with pytest.raises(SystemExit):
                main()                     # identical shape
        assert capsys.readouterr().out == ""

    def test_api_key_and_body_never_in_output_or_state(
        self, tmp_path, monkeypatch
    ):
        """GATE: fingerprints only — key/raw body must never leak."""
        monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "sk-super-secret-value-42")
        state = tmp_path / STATE
        msgs = [run(state, fetch=_ok_fetch(REAL_SHAPE))]
        new = {"data": {"limits": {}, "credit_balance": "RAW-BODY-MARKER"}}
        msgs.append(run(state, fetch=_ok_fetch(new)))
        msgs.append(run(state, fetch=_err_fetch(RuntimeError("boom"))))
        blob = "\n".join(msgs) + state.read_text()
        assert "sk-super-secret-value-42" not in blob
        assert "RAW-BODY-MARKER" not in blob
        assert "0.06" not in blob