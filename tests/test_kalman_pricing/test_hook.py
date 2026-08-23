"""Tests for the VPS2 Kalman pricing hook — kalman-pricing-hook.py.

Tests cover:

* ``query_nostr_kalman_events()`` — queries relays via nak CLI, parses JSON
* ``pick_freshest_event()`` — deduplicates + selects newest by created_at
* ``update_provider_db()`` — updates routstrd DB (sqlite or docker exec)
* ``main()`` — the full hook flow: query → parse → update/disable

All Nostr relay queries (nak CLI), routstrd DB access (sqlite3 + docker),
and time functions are mocked.  No live services are contacted.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch, call

import pytest


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_nostr_event(
    created_at: int,
    zai_available: bool = True,
    price: float | None = 0.068,
    locked_reason: str | None = None,
    source: str = "T470",
    event_id: str = "evt001",
) -> dict:
    """Build a synthetic kind-30315 Nostr event dict."""
    content = {
        "timestamp": created_at,
        "source": source,
        "zai_available": zai_available,
        "zai_effective_price_usd_per_m": price,
        "zai_locked_reason": locked_reason,
        "is_peak_hour": False,
        "providers": {},
    }
    return {
        "id": event_id,
        "kind": 30315,
        "created_at": created_at,
        "content": json.dumps(content),
        "pubkey": "abc123",
        "tags": [["d", "kalman-pricing"], ["t", "routstr"]],
    }


@pytest.fixture
def mock_time(hook_mod):
    """Mock hook.time so we can control event-age calculations."""
    with patch.object(hook_mod, "time") as m:
        m.time.return_value = 10_000  # "now" = 10000
        m.sleep = MagicMock()  # no-op sleep
        yield m


# ── pick_freshest_event ──────────────────────────────────────────────────────


class TestPickFreshestEvent:
    def test_returns_none_for_empty(self, hook_mod):
        assert hook_mod.pick_freshest_event([]) is None

    def test_returns_single_event(self, hook_mod):
        evt = _make_nostr_event(created_at=9000)
        result = hook_mod.pick_freshest_event([evt])
        assert result is not None
        assert result["id"] == "evt001"

    def test_picks_newest(self, hook_mod):
        old = _make_nostr_event(created_at=100, event_id="old")
        new = _make_nostr_event(created_at=9000, event_id="new")
        result = hook_mod.pick_freshest_event([old, new])
        assert result["id"] == "new"

    def test_deduplicates_by_id(self, hook_mod):
        # Same event id, different created_at → only one in output
        dup_a = _make_nostr_event(created_at=100, event_id="same")
        dup_b = _make_nostr_event(created_at=9000, event_id="same")
        result = hook_mod.pick_freshest_event([dup_a, dup_b])
        assert result is not None
        assert result["id"] == "same"

    def test_returns_none_when_all_empty_ids(self, hook_mod):
        """Events without an 'id' key are skipped (id defaults to '' = falsy)."""
        evt = {"created_at": 100}  # no 'id' key
        result = hook_mod.pick_freshest_event([evt])
        assert result is None  # empty-id events are skipped


# ── query_nostr_kalman_events ────────────────────────────────────────────────


class TestQueryNostrEvents:
    """Tests for the nak CLI query wrapper."""

    def test_parses_valid_events(self, hook_mod):
        """nak req returns JSON events → parsed and returned."""
        evt_json = json.dumps(_make_nostr_event(created_at=9500))
        nak_output = evt_json + "\n"

        def _fake_run(cmd, **kw):
            m = MagicMock()
            if "which" in cmd:
                m.returncode = 0
                m.stdout = "/usr/local/bin/nak"
                m.stderr = ""
            else:
                m.returncode = 0
                m.stdout = nak_output
                m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=_fake_run):
            events = hook_mod.query_nostr_kalman_events()
        assert len(events) >= 1
        assert events[0]["kind"] == 30315

    def test_handles_no_events(self, hook_mod):
        """nak req returns empty → empty list."""
        def _fake_run(cmd, **kw):
            m = MagicMock()
            if "which" in cmd:
                m.returncode = 0
                m.stdout = "/usr/local/bin/nak"
                m.stderr = ""
            else:
                m.returncode = 0
                m.stdout = ""
                m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=_fake_run):
            events = hook_mod.query_nostr_kalman_events()
        assert events == []

    def test_skips_non_json_lines(self, hook_mod):
        """nak output includes connection messages → only JSON events parsed."""
        evt_json = json.dumps(_make_nostr_event(created_at=9500))
        nak_output = "Connected to relay...\n" + evt_json + "\nnot json\n"

        def _fake_run(cmd, **kw):
            m = MagicMock()
            if "which" in cmd:
                m.returncode = 0
                m.stdout = "/usr/local/bin/nak"
                m.stderr = ""
            else:
                m.returncode = 0
                m.stdout = nak_output
                m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=_fake_run):
            events = hook_mod.query_nostr_kalman_events()
        assert len(events) >= 1
        assert events[0]["kind"] == 30315

    def test_returns_empty_when_nak_not_found(self, hook_mod):
        """When nak binary is missing → returns empty list gracefully."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
            events = hook_mod.query_nostr_kalman_events()
        assert events == []


# ── update_provider_db ───────────────────────────────────────────────────────


class TestUpdateProviderDb:
    def test_sqlite_success(self, hook_mod):
        """Direct sqlite3 access works → returns True."""
        mock_conn = MagicMock()
        with patch("os.path.exists", return_value=True), \
             patch("sqlite3.connect", return_value=mock_conn):
            result = hook_mod.update_provider_db(enabled=True, fee=2.5)
        assert result is True
        mock_conn.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    def test_sqlite_disabled_sets_zero(self, hook_mod):
        """When enabled=False → enabled=0 in SQL."""
        mock_conn = MagicMock()
        with patch("os.path.exists", return_value=True), \
             patch("sqlite3.connect", return_value=mock_conn):
            hook_mod.update_provider_db(enabled=False, fee=1.43)
        args = mock_conn.execute.call_args[0]
        assert args[1][0] == 0  # enabled=0

    def test_docker_fallback_on_sqlite_failure(self, hook_mod):
        """When DB path doesn't exist → docker exec fallback."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "OK"
        mock_result.stderr = ""
        with patch("os.path.exists", return_value=False), \
             patch("subprocess.run", return_value=mock_result) as mock_run:
            result = hook_mod.update_provider_db(enabled=True, fee=3.0)
        assert result is True
        # Should have called docker exec
        called_cmd = mock_run.call_args[0][0]
        assert "docker" in called_cmd
        assert "exec" in called_cmd

    def test_returns_false_when_everything_fails(self, hook_mod):
        """Both sqlite and docker exec fail → returns False."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error"
        with patch("os.path.exists", return_value=False), \
             patch("subprocess.run", return_value=mock_result):
            result = hook_mod.update_provider_db(enabled=False, fee=1.43)
        assert result is False


# ── main() — fresh event path ────────────────────────────────────────────────


class TestMainFreshEvent:
    """main() with a fresh, valid event → updates DB with enabled=True."""

    def test_fresh_event_enables_provider(self, hook_mod, mock_time):
        """Event age < 300s, zai_available=True, price > 0 → enable."""
        fresh_evt = _make_nostr_event(created_at=9800, zai_available=True, price=0.068)
        mock_time.time.return_value = 10_000  # age = 200s

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[fresh_evt]), \
             patch.object(hook_mod, "update_provider_db", return_value=True) as mock_update, \
             patch.object(hook_mod, "verify_provider_db", return_value=None):
            rc = hook_mod.main()

        assert rc == 0
        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args
        assert call_kwargs[1]["enabled"] is True
        assert call_kwargs[1]["fee"] > 0

    def test_fresh_event_fee_is_price_divided_by_base_rate(self, hook_mod, mock_time):
        """fee = zai_effective_price / LITELLM_ZAI_BASE_RATE."""
        price = 0.42
        fresh_evt = _make_nostr_event(created_at=9900, zai_available=True, price=price)
        mock_time.time.return_value = 10_000  # age = 100s

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[fresh_evt]), \
             patch.object(hook_mod, "update_provider_db", return_value=True) as mock_update, \
             patch.object(hook_mod, "verify_provider_db", return_value=None):
            hook_mod.main()

        expected_fee = price / hook_mod.LITELLM_ZAI_BASE_RATE
        actual_fee = mock_update.call_args[1]["fee"]
        assert actual_fee == pytest.approx(expected_fee, rel=1e-6)

    def test_fee_clamped_to_max(self, hook_mod, mock_time):
        """Fee must not exceed MAX_FEE."""
        extreme_price = hook_mod.LITELLM_ZAI_BASE_RATE * 100  # fee would be 100
        fresh_evt = _make_nostr_event(
            created_at=9900, zai_available=True, price=extreme_price
        )
        mock_time.time.return_value = 10_000

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[fresh_evt]), \
             patch.object(hook_mod, "update_provider_db", return_value=True) as mock_update, \
             patch.object(hook_mod, "verify_provider_db", return_value=None):
            hook_mod.main()

        actual_fee = mock_update.call_args[1]["fee"]
        assert actual_fee <= hook_mod.MAX_FEE


# ── main() — stale event path ───────────────────────────────────────────────


class TestMainStaleEvent:
    def test_stale_event_disables(self, hook_mod, mock_time):
        """Event older than 300s → disable_provider_safe called."""
        stale_evt = _make_nostr_event(created_at=5000, zai_available=True, price=0.068)
        mock_time.time.return_value = 10_000  # age = 5000s > 300

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[stale_evt]), \
             patch.object(hook_mod, "disable_provider_safe", return_value=True) as mock_disable:
            rc = hook_mod.main()

        assert rc == 0
        mock_disable.assert_called_once()

    def test_event_exactly_at_threshold_is_fresh(self, hook_mod, mock_time):
        """Event age == 300s should NOT be stale (boundary check, > not >=)."""
        boundary_evt = _make_nostr_event(created_at=9700, zai_available=True, price=0.068)
        mock_time.time.return_value = 10_000  # age = 300s exactly

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[boundary_evt]), \
             patch.object(hook_mod, "update_provider_db", return_value=True) as mock_update, \
             patch.object(hook_mod, "disable_provider_safe", return_value=True) as mock_disable, \
             patch.object(hook_mod, "verify_provider_db", return_value=None):
            rc = hook_mod.main()

        assert rc == 0
        mock_disable.assert_not_called()
        mock_update.assert_called_once()


# ── main() — no events path ──────────────────────────────────────────────────


class TestMainNoEvents:
    def test_no_events_disables(self, hook_mod, mock_time):
        """When query returns no events → disable_provider_safe called."""
        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[]), \
             patch.object(hook_mod, "disable_provider_safe", return_value=True) as mock_disable:
            rc = hook_mod.main()

        assert rc == 0
        mock_disable.assert_called_once()

    def test_no_events_disable_fails_returns_1(self, hook_mod, mock_time):
        """When disable_provider_safe fails → main returns 1 (critical)."""
        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[]), \
             patch.object(hook_mod, "disable_provider_safe", return_value=False):
            rc = hook_mod.main()
        assert rc == 1


# ── main() — zero-price guard ────────────────────────────────────────────────


class TestMainZeroPriceGuard:
    def test_zero_price_disables(self, hook_mod, mock_time):
        """zai_available=True but price=0 → disable."""
        zero_evt = _make_nostr_event(created_at=9900, zai_available=True, price=0.0)
        mock_time.time.return_value = 10_000

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[zero_evt]), \
             patch.object(hook_mod, "disable_provider_safe", return_value=True) as mock_disable:
            rc = hook_mod.main()

        assert rc == 0
        mock_disable.assert_called_once()

    def test_none_price_disables(self, hook_mod, mock_time):
        """zai_available=True but price=None → disable."""
        none_evt = _make_nostr_event(created_at=9900, zai_available=True, price=None)
        mock_time.time.return_value = 10_000

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[none_evt]), \
             patch.object(hook_mod, "disable_provider_safe", return_value=True) as mock_disable:
            rc = hook_mod.main()

        assert rc == 0
        mock_disable.assert_called_once()

    def test_negative_price_disables(self, hook_mod, mock_time):
        """Negative price (shouldn't happen, but guard must catch it) → disable."""
        neg_evt = _make_nostr_event(created_at=9900, zai_available=True, price=-0.5)
        mock_time.time.return_value = 10_000

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[neg_evt]), \
             patch.object(hook_mod, "disable_provider_safe", return_value=True) as mock_disable:
            rc = hook_mod.main()

        assert rc == 0
        mock_disable.assert_called_once()

    def test_unavailable_disables(self, hook_mod, mock_time):
        """zai_available=False → disable, regardless of price."""
        unavail_evt = _make_nostr_event(created_at=9900, zai_available=False, price=0.068)
        mock_time.time.return_value = 10_000

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[unavail_evt]), \
             patch.object(hook_mod, "disable_provider_safe", return_value=True) as mock_disable:
            rc = hook_mod.main()

        assert rc == 0
        mock_disable.assert_called_once()


# ── main() — unparsable content ──────────────────────────────────────────────


class TestMainUnparsable:
    def test_bad_json_disables(self, hook_mod, mock_time):
        """Event content is not valid JSON → disable."""
        bad_evt = {
            "id": "bad",
            "kind": 30315,
            "created_at": 9900,
            "content": "not json at all",
        }
        mock_time.time.return_value = 10_000

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[bad_evt]), \
             patch.object(hook_mod, "disable_provider_safe", return_value=True) as mock_disable:
            rc = hook_mod.main()

        assert rc == 0
        mock_disable.assert_called_once()


# ── main() — disable failure paths ────────────────────────────────────────────


class TestMainDisableFailures:
    def test_stale_event_disable_fails_returns_1(self, hook_mod, mock_time):
        """Stale event + disable fails → main returns 1 (critical)."""
        stale_evt = _make_nostr_event(created_at=5000, zai_available=True, price=0.068)
        mock_time.time.return_value = 10_000  # age = 5000s > 300

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[stale_evt]), \
             patch.object(hook_mod, "disable_provider_safe", return_value=False):
            rc = hook_mod.main()
        assert rc == 1

    def test_bad_json_disable_fails_returns_1(self, hook_mod, mock_time):
        """Bad JSON + disable fails → main returns 1."""
        bad_evt = {"id": "b", "kind": 30315, "created_at": 9900, "content": "x"}
        mock_time.time.return_value = 10_000

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[bad_evt]), \
             patch.object(hook_mod, "disable_provider_safe", return_value=False):
            rc = hook_mod.main()
        assert rc == 1

    def test_zero_price_disable_fails_returns_1(self, hook_mod, mock_time):
        """Zero price + disable fails → main returns 1."""
        zero_evt = _make_nostr_event(created_at=9900, zai_available=True, price=0.0)
        mock_time.time.return_value = 10_000

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[zero_evt]), \
             patch.object(hook_mod, "disable_provider_safe", return_value=False):
            rc = hook_mod.main()
        assert rc == 1

    def test_no_events_disable_fails_returns_1(self, hook_mod, mock_time):
        """No events + disable fails → main returns 1."""
        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[]), \
             patch.object(hook_mod, "disable_provider_safe", return_value=False):
            rc = hook_mod.main()
        assert rc == 1

    def test_events_exist_but_unparseable_all_disables(self, hook_mod, mock_time):
        """Events exist but pick_freshest returns None (all empty IDs) → disable."""
        empty_evt = {"kind": 30315, "created_at": 9900, "content": "{}"}
        mock_time.time.return_value = 10_000

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[empty_evt]), \
             patch.object(hook_mod, "disable_provider_safe", return_value=True) as mock_disable:
            rc = hook_mod.main()
        assert rc == 0
        mock_disable.assert_called_once()


# ── main() — successful update with verification ─────────────────────────────


class TestMainSuccessWithVerify:
    def test_fresh_event_updates_and_verifies(self, hook_mod, mock_time):
        """Fresh event → update DB → verify_provider_db → log state."""
        fresh_evt = _make_nostr_event(created_at=9900, zai_available=True, price=0.068)
        mock_time.time.return_value = 10_000

        verify_result = {"slug": "zai-coding", "provider_fee": 0.4857, "enabled": True}
        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[fresh_evt]), \
             patch.object(hook_mod, "update_provider_db", return_value=True), \
             patch.object(hook_mod, "verify_provider_db", return_value=verify_result):
            rc = hook_mod.main()

        assert rc == 0

    def test_fresh_event_update_fails_non_critical(self, hook_mod, mock_time):
        """Fresh event but DB update fails → non-critical, returns 0 (continues)."""
        fresh_evt = _make_nostr_event(created_at=9900, zai_available=True, price=0.068)
        mock_time.time.return_value = 10_000

        with patch.object(hook_mod, "query_nostr_kalman_events", return_value=[fresh_evt]), \
             patch.object(hook_mod, "update_provider_db", return_value=False):
            # Should NOT call disable — just log and continue
            with patch.object(hook_mod, "disable_provider_safe") as mock_disable:
                rc = hook_mod.main()

        assert rc == 0
        mock_disable.assert_not_called()


# ── verify_provider_db ──────────────────────────────────────────────────────


class TestVerifyProviderDb:
    def test_returns_none_when_db_missing(self, hook_mod):
        with patch("os.path.exists", return_value=False):
            assert hook_mod.verify_provider_db() is None

    def test_returns_dict_on_success(self, hook_mod):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("zai-coding", 2.5, 1)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("os.path.exists", return_value=True), \
             patch("sqlite3.connect", return_value=mock_conn):
            result = hook_mod.verify_provider_db()

        assert result is not None
        assert result["slug"] == "zai-coding"
        assert result["provider_fee"] == 2.5
        assert result["enabled"] is True

    def test_returns_none_on_exception(self, hook_mod):
        with patch("os.path.exists", return_value=True), \
             patch("sqlite3.connect", side_effect=Exception("DB locked")):
            assert hook_mod.verify_provider_db() is None

    def test_returns_none_when_row_missing(self, hook_mod):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("os.path.exists", return_value=True), \
             patch("sqlite3.connect", return_value=mock_conn):
            assert hook_mod.verify_provider_db() is None


# ── query error handling ──────────────────────────────────────────────────────


class TestQueryErrorHandling:
    def test_timeout_expired_returns_empty(self, hook_mod):
        """nak req times out → that relay skipped, returns collected events."""
        import subprocess

        evt_json = json.dumps(_make_nostr_event(created_at=9500))

        call_count = [0]

        def _fake_run(cmd, **kw):
            call_count[0] += 1
            m = MagicMock()
            if "which" in cmd:
                m.returncode = 0
                m.stdout = "/usr/local/bin/nak"
                m.stderr = ""
            else:
                # Second relay call times out
                if call_count[0] == 2:
                    raise subprocess.TimeoutExpired(cmd=cmd, timeout=15)
                m.returncode = 0
                m.stdout = evt_json + "\n"
                m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=_fake_run):
            events = hook_mod.query_nostr_kalman_events()
        # Should still have collected events from non-timed-out relays
        assert len(events) >= 1

    def test_general_exception_returns_empty(self, hook_mod):
        """nak req raises generic Exception → that relay skipped gracefully."""
        def _fake_run(cmd, **kw):
            m = MagicMock()
            if "which" in cmd:
                m.returncode = 0
                m.stdout = "/usr/local/bin/nak"
                m.stderr = ""
            else:
                raise Exception("Network unreachable")
            return m

        with patch("subprocess.run", side_effect=_fake_run):
            events = hook_mod.query_nostr_kalman_events()
        assert events == []


# ── log() function ────────────────────────────────────────────────────────────


class TestLogFunction:
    def test_log_writes_to_file(self, hook_mod, monkeypatch, tmp_path):
        """The real log() function writes a timestamped line to LOG_FILE."""
        # Undo the autouse no-op mock to restore the real log function,
        # then redirect LOG_FILE to a temp path.
        monkeypatch.undo()
        monkeypatch.setattr(hook_mod, "LOG_FILE", str(tmp_path / "test.log"))

        hook_mod.log("test message")
        log_file = tmp_path / "test.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "test message" in content
