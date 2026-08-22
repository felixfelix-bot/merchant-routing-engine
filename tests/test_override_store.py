"""Tests for src/override_store.py — cost-gate override mechanism (CG-4, §3).

Covers the plan §3 contract:
  - principals: Felix / merchant-routing CW / orchestrator CW only (workers: no)
  - marker file ``.cost_gate_override`` JSON {scope, expires_ts, issued_by, reason}
  - TTL mandatory (default 4 h, max 24 h); expiry is enforced on read
  - single scope per grant (never a list, never a second active grant)
  - ``cost_gate_overrides`` audit rows on issue + consumption (append-only)
  - ``anomaly_events`` INFO entries on issue + consumption
  - corrupt/absent marker fails CLOSED (behaves as no override, never raises)
  - freeze-marker / dead-key hard blocks stay override-IMMUNE (integration with
    the CG-1 decision function)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.cost_gate import OVERRIDE_SCOPES  # single source of truth (CG-1)
from src.override_store import (
    ALLOWED_PRINCIPALS,
    DEFAULT_TTL_SECONDS,
    MARKER_PATH_DEFAULT,
    MAX_TTL_SECONDS,
    OverrideStore,
    OverrideError,
    parse_ttl,
    UnauthorizedPrincipal,
)


NOW = 1_000_000.0
FOUR_H = 4 * 3600.0
DAY = 24 * 3600.0


# ── helpers ──────────────────────────────────────────────────────────────────


def make_store(tmp_path):
    marker = tmp_path / ".cost_gate_override"
    db = tmp_path / "zai_usage.db"
    return OverrideStore(marker_path=str(marker), db_path=str(db)), marker, db


def read_marker_json(marker):
    return json.loads(marker.read_text(encoding="utf-8"))


def rows(db, table):
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(f"SELECT * FROM {table}")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []  # store correctly never touched the DB
        raise
    finally:
        conn.close()


def anomaly_rows(db):
    out = rows(db, "anomaly_events")
    return [r for r in out if r.get("category") == "cost_gate_override"]


def override_rows(db):
    return rows(db, "cost_gate_overrides")


def issue_ok(store, **kw):
    defaults = dict(
        scope="infra_down",
        ttl_seconds=FOUR_H,
        issued_by="felix",
        reason="proxy price feed restart window",
        now_ts=NOW,
    )
    defaults.update(kw)
    return store.issue_override(**defaults)


# ── constants & principal allowlist ─────────────────────────────────────────


class TestConstants:
    def test_principals_exactly_plan_set(self):
        assert ALLOWED_PRINCIPALS == frozenset(
            {"felix", "merchant-routing-cw", "orchestrator-cw"})

    def test_ttl_defaults_plan_values(self):
        assert DEFAULT_TTL_SECONDS == 4 * 3600.0
        assert MAX_TTL_SECONDS == 24 * 3600.0

    def test_default_marker_path_is_plan_path(self):
        assert MARKER_PATH_DEFAULT.endswith("/.cost_gate_override")
        assert ".hermes/bot" in MARKER_PATH_DEFAULT

    def test_scopes_come_from_cost_gate(self):
        assert OverrideStore.OVERRIDE_SCOPES is OVERRIDE_SCOPES


class TestPrincipalAllowlist:
    def test_felix_can_issue(self, tmp_path):
        store, marker, db = make_store(tmp_path)
        issue_ok(store, issued_by="felix")
        assert read_marker_json(marker)["issued_by"] == "felix"

    def test_merchant_routing_cw_can_issue(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        issue_ok(store, issued_by="merchant-routing-cw")
        assert read_marker_json(marker)["issued_by"] == "merchant-routing-cw"

    def test_orchestrator_cw_can_issue(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        issue_ok(store, issued_by="orchestrator-cw")
        assert read_marker_json(marker)["issued_by"] == "orchestrator-cw"

    def test_worker_principal_denied(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        with pytest.raises(UnauthorizedPrincipal):
            issue_ok(store, issued_by="worker-admin")
        assert not marker.exists()  # nothing written on denial

    def test_unknown_principal_denied(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        with pytest.raises(UnauthorizedPrincipal):
            issue_ok(store, issued_by="mallory")

    def test_principal_normalized_case_and_space(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        issue_ok(store, issued_by="  Felix ")
        assert read_marker_json(marker)["issued_by"] == "felix"


# ── issue: scope / ttl / reason validation ──────────────────────────────────


class TestIssueValidation:
    def test_unknown_scope_rejected(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        with pytest.raises(OverrideError):
            issue_ok(store, scope="all_the_things")
        assert not marker.exists()

    @pytest.mark.parametrize("scope", sorted(OVERRIDE_SCOPES))
    def test_each_plan_scope_accepted(self, tmp_path, scope):
        store, marker, _ = make_store(tmp_path)
        issue_ok(store, scope=scope)
        assert read_marker_json(marker)["scope"] == scope

    def test_ttl_over_24h_rejected(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        with pytest.raises(OverrideError):
            issue_ok(store, ttl_seconds=DAY + 1)

    def test_ttl_exactly_24h_allowed(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        issue_ok(store, ttl_seconds=DAY)
        assert read_marker_json(marker)["expires_ts"] == NOW + DAY

    def test_ttl_zero_rejected(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        with pytest.raises(OverrideError):
            issue_ok(store, ttl_seconds=0)

    def test_ttl_negative_rejected(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        with pytest.raises(OverrideError):
            issue_ok(store, ttl_seconds=-FOUR_H)

    def test_ttl_nan_rejected(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        with pytest.raises(OverrideError):
            issue_ok(store, ttl_seconds=float("nan"))

    def test_ttl_default_is_4h(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        issue_ok(store, ttl_seconds=None)
        assert read_marker_json(marker)["expires_ts"] == NOW + FOUR_H

    def test_reason_mandatory(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        with pytest.raises(OverrideError):
            issue_ok(store, reason="")

    def test_reason_whitespace_only_rejected(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        with pytest.raises(OverrideError):
            issue_ok(store, reason="   ")

    def test_marker_is_exactly_the_four_plan_fields(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        issue_ok(store)
        grant = read_marker_json(marker)
        assert set(grant) == {"scope", "expires_ts", "issued_by", "reason"}


# ── single-scope grants ─────────────────────────────────────────────────────


class TestSingleScope:
    def test_scope_list_rejected(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        with pytest.raises(OverrideError):
            issue_ok(store, scope=["budget", "infra_down"])

    def test_second_active_grant_rejected(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        issue_ok(store, scope="infra_down")
        with pytest.raises(OverrideError):
            issue_ok(store, scope="budget")
        # first grant untouched
        assert read_marker_json(marker)["scope"] == "infra_down"

    def test_reissue_allowed_after_expiry(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        issue_ok(store, scope="infra_down", ttl_seconds=60.0, now_ts=NOW)
        issue_ok(store, scope="budget", ttl_seconds=60.0, now_ts=NOW + 120.0)
        assert read_marker_json(marker)["scope"] == "budget"

    def test_reissue_allowed_after_revoke(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        issue_ok(store)
        store.revoke(now_ts=NOW + 10, revoked_by="felix", reason="done")
        issue_ok(store, scope="budget")  # no active grant anymore


# ── TTL expiry on read ──────────────────────────────────────────────────────


class TestTtlExpiry:
    def test_active_before_expiry(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        issue_ok(store, ttl_seconds=FOUR_H, now_ts=NOW)
        loaded = store.load_override(now_ts=NOW + FOUR_H - 1)
        assert loaded["active"] is True
        assert loaded["grant"]["scope"] == "infra_down"
        assert loaded["error"] is None

    def test_inactive_at_exact_expiry(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        issue_ok(store, ttl_seconds=FOUR_H, now_ts=NOW)
        loaded = store.load_override(now_ts=NOW + FOUR_H)
        assert loaded["active"] is False

    def test_inactive_after_expiry(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        issue_ok(store, ttl_seconds=60.0, now_ts=NOW)
        loaded = store.load_override(now_ts=NOW + 3600.0)
        assert loaded["active"] is False
        assert "expired" in (loaded["error"] or "")

    def test_marker_not_deleted_on_expiry(self, tmp_path):
        # TTL self-governs; the marker may linger — reads must handle it
        store, marker, _ = make_store(tmp_path)
        issue_ok(store, ttl_seconds=60.0, now_ts=NOW)
        store.load_override(now_ts=NOW + 3600.0)
        assert marker.exists()


# ── corrupt / absent marker fails closed ────────────────────────────────────


class TestFailClosedMarker:
    def test_absent_marker_inactive_no_error(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        loaded = store.load_override(now_ts=NOW)
        assert loaded["active"] is False
        assert loaded["grant"] is None

    def test_garbage_marker_fails_closed(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        marker.write_text("{{{not json", encoding="utf-8")
        loaded = store.load_override(now_ts=NOW)
        assert loaded["active"] is False
        assert loaded["error"] is not None

    def test_wrong_shape_marker_fails_closed(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        marker.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        loaded = store.load_override(now_ts=NOW)
        assert loaded["active"] is False

    def test_marker_missing_scope_fails_closed(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        marker.write_text(json.dumps(
            {"expires_ts": NOW + 100, "issued_by": "felix", "reason": "x"}),
            encoding="utf-8")
        loaded = store.load_override(now_ts=NOW)
        assert loaded["active"] is False

    def test_marker_with_scopes_list_fails_closed(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        marker.write_text(json.dumps({
            "scope": ["budget", "infra_down"],  # multi-scope forgery
            "expires_ts": NOW + 100,
            "issued_by": "felix",
            "reason": "x"}), encoding="utf-8")
        loaded = store.load_override(now_ts=NOW)
        assert loaded["active"] is False

    def test_marker_non_numeric_expiry_fails_closed(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        marker.write_text(json.dumps({
            "scope": "budget",
            "expires_ts": "next tuesday",
            "issued_by": "felix",
            "reason": "x"}), encoding="utf-8")
        loaded = store.load_override(now_ts=NOW)
        assert loaded["active"] is False

    def test_unreadable_marker_fails_closed(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        marker.write_text("{}", encoding="utf-8")
        os.chmod(marker, 0o000)
        try:
            loaded = store.load_override(now_ts=NOW)
            assert loaded["active"] is False
        finally:
            os.chmod(marker, 0o600)

    def test_corrupt_marker_leaves_no_override_in_gate(self, tmp_path):
        # end-to-end: the CG-1 decision function must see NO override
        from src.cost_gate import evaluate_cost_gate
        store, marker, _ = make_store(tmp_path)
        marker.write_text("garbage", encoding="utf-8")
        loaded = store.load_override(now_ts=NOW)
        verdict = evaluate_cost_gate(
            model="glm-5.2", task_type="coding", deferrable=True,
            effective_price_usd_per_m=None, price_unreachable=True,
            price_history=[], budget_cap_usd=15.0,
            override=loaded["grant"], now_ts=NOW)
        assert verdict["decision"] == "DENY"


# ── audit rows ──────────────────────────────────────────────────────────────


class TestAuditRows:
    def test_issue_writes_override_row(self, tmp_path):
        store, _, db = make_store(tmp_path)
        issue_ok(store, scope="infra_down", ttl_seconds=FOUR_H)
        rs = override_rows(db)
        assert len(rs) == 1
        r = rs[0]
        assert r["kind"] == "issued"
        assert r["issued_by"] == "felix"
        assert r["scope"] == "infra_down"
        assert r["ttl_seconds"] == FOUR_H
        assert r["expires_ts"] == NOW + FOUR_H
        assert r["reason"] == "proxy price feed restart window"
        assert r["ts"] == NOW

    def test_issue_writes_anomaly_info_row(self, tmp_path):
        store, _, db = make_store(tmp_path)
        issue_ok(store)
        evs = anomaly_rows(db)
        assert len(evs) == 1
        assert evs[0]["severity"] == "INFO"
        assert "infra_down" in evs[0]["title"]
        detail = json.loads(evs[0]["detail"])
        assert detail["issued_by"] == "felix"

    def test_audit_precedes_marker(self, tmp_path):
        # fail-closed ordering: if the DB write fails, NO marker is written
        store, marker, db = make_store(tmp_path)
        db.mkdir()                   # path is a directory → connect fails
        with pytest.raises(OverrideError):
            issue_ok(store)
        assert not marker.exists()

    def test_consume_writes_row_with_task(self, tmp_path):
        store, _, db = make_store(tmp_path)
        grant = issue_ok(store)
        store.consume_override(
            grant, now_ts=NOW + 60, task="nightly-billing-rebuild",
            would_have_been={"decision": "DENY", "reason_code": "infra_down"})
        rs = override_rows(db)
        assert [r["kind"] for r in rs] == ["issued", "consumed"]
        c = rs[1]
        assert c["consumed_at_ts"] == NOW + 60
        assert c["task"] == "nightly-billing-rebuild"
        assert c["scope"] == "infra_down"
        assert c["issued_by"] == "felix"

    def test_consume_writes_anomaly_info_row(self, tmp_path):
        store, _, db = make_store(tmp_path)
        grant = issue_ok(store)
        store.consume_override(
            grant, now_ts=NOW + 60, task="t_job",
            would_have_been={"decision": "DENY", "reason_code": "infra_down"})
        evs = anomaly_rows(db)
        assert len(evs) == 2  # issue + consumption
        assert evs[1]["severity"] == "INFO"
        assert "consumed" in evs[1]["title"]
        detail = json.loads(evs[1]["detail"])
        assert detail["task"] == "t_job"
        assert detail["would_have_been"]["reason_code"] == "infra_down"

    def test_consume_keeps_marker(self, tmp_path):
        # TTL governs lifetime; every consuming invocation is audited
        store, marker, _ = make_store(tmp_path)
        grant = issue_ok(store)
        store.consume_override(grant, now_ts=NOW + 60, task="a",
                               would_have_been={})
        assert read_marker_json(marker)["scope"] == "infra_down"

    def test_double_consumption_two_rows(self, tmp_path):
        store, _, db = make_store(tmp_path)
        grant = issue_ok(store)
        store.consume_override(grant, now_ts=NOW + 60, task="a",
                               would_have_been={})
        store.consume_override(grant, now_ts=NOW + 120, task="b",
                               would_have_been={})
        rs = override_rows(db)
        assert [r["kind"] for r in rs] == ["issued", "consumed", "consumed"]
        assert rs[1]["task"] == "a" and rs[2]["task"] == "b"

    def test_consume_expired_grant_rejected(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        grant = issue_ok(store, ttl_seconds=60.0, now_ts=NOW)
        with pytest.raises(OverrideError):
            store.consume_override(grant, now_ts=NOW + 120, task="late",
                                   would_have_been={})

    def test_consume_none_grant_noop(self, tmp_path):
        store, _, db = make_store(tmp_path)
        store.consume_override(None, now_ts=NOW, task="x",
                               would_have_been={})
        assert override_rows(db) == []

    def test_revoke_writes_row_and_clears_marker(self, tmp_path):
        store, marker, db = make_store(tmp_path)
        issue_ok(store)
        store.revoke(now_ts=NOW + 30, revoked_by="felix", reason="incident over")
        rs = override_rows(db)
        assert rs[-1]["kind"] == "revoked"
        assert rs[-1]["issued_by"] == "felix"
        assert not marker.exists()

    def test_revoke_principal_checked(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        issue_ok(store)
        with pytest.raises(UnauthorizedPrincipal):
            store.revoke(now_ts=NOW + 30, revoked_by="worker-admin",
                         reason="nope")

    def test_revoke_without_marker_noop(self, tmp_path):
        store, _, db = make_store(tmp_path)
        store.revoke(now_ts=NOW, revoked_by="felix", reason="nothing")
        assert override_rows(db) == []

    def test_denied_issue_writes_no_audit(self, tmp_path):
        store, _, db = make_store(tmp_path)
        with pytest.raises(UnauthorizedPrincipal):
            issue_ok(store, issued_by="worker-admin")
        assert override_rows(db) == []
        assert anomaly_rows(db) == []


# ── marker file hygiene ─────────────────────────────────────────────────────


class TestMarkerHygiene:
    def test_marker_mode_0600(self, tmp_path):
        store, marker, _ = make_store(tmp_path)
        issue_ok(store)
        assert (os.stat(marker).st_mode & 0o777) == 0o600

    def test_marker_parent_created(self, tmp_path):
        marker = tmp_path / "bot" / "dir" / ".cost_gate_override"
        store = OverrideStore(marker_path=str(marker),
                              db_path=str(tmp_path / "u.db"))
        issue_ok(store)
        assert marker.exists()

    def test_overwrite_via_issue_rejected_leaves_atomic(self, tmp_path):
        # concurrent-safe: a failed second issue must not corrupt the first
        store, marker, _ = make_store(tmp_path)
        issue_ok(store)
        try:
            issue_ok(store, scope="budget")
        except OverrideError:
            pass
        assert read_marker_json(marker)["scope"] == "infra_down"


# ── parse_ttl (CLI support — CG-7 wires argparse to this) ───────────────────


class TestParseTtl:
    @pytest.mark.parametrize("text,seconds", [
        ("4h", 4 * 3600.0),
        ("24h", 24 * 3600.0),
        ("30m", 1800.0),
        ("90s", 90.0),
        ("3600", 3600.0),
        ("1.5h", 5400.0),
    ])
    def test_formats(self, text, seconds):
        assert parse_ttl(text) == seconds

    @pytest.mark.parametrize("bad", ["", "abc", "4x", "-1h", "0h", "1d", "4 h"])
    def test_bad_formats_raise(self, bad):
        with pytest.raises(OverrideError):
            parse_ttl(bad)

    def test_over_max_rejected(self):
        with pytest.raises(OverrideError):
            parse_ttl("25h")


# ── integration: gate consumes what the store produces ──────────────────────


class TestGateIntegration:
    def _gate(self, override, **kw):
        from src.cost_gate import evaluate_cost_gate
        defaults = dict(
            model="glm-5.2", task_type="coding", deferrable=True,
            effective_price_usd_per_m=10.0, price_source="test",
            price_history=[float(i) for i in range(1, 101)],
            budget_cap_usd=15.0, now_ts=NOW,
        )
        defaults.update(kw)
        return evaluate_cost_gate(override=override, **defaults)

    def test_issue_then_gate_rescues_infra_down(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        issue_ok(store, scope="infra_down", now_ts=NOW)
        loaded = store.load_override(now_ts=NOW + 60)
        assert loaded["active"] is True
        v = self._gate(loaded["grant"], price_unreachable=True)
        assert v["decision"] == "ALLOW"
        assert v["reason_code"] == "infra_down_override"
        assert v["override_consumed"]["issued_by"] == "felix"
        # CG-4 persists exactly the record the gate produced
        store.consume_override(
            v["override_consumed"], now_ts=NOW + 60, task="cron-x",
            would_have_been=v["override_consumed"]["would_have_been"])

    def test_scope_isolation_budget_vs_infra_down(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        issue_ok(store, scope="budget", now_ts=NOW)
        loaded = store.load_override(now_ts=NOW)
        v = self._gate(loaded["grant"], price_unreachable=True)
        assert v["decision"] == "DENY"      # budget scope ≠ infra_down rescue
        assert v["reason_code"] == "infra_down"

    def test_freeze_marker_immune_to_any_override(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        for scope in sorted(OVERRIDE_SCOPES):
            issue_ok(store, scope=scope, now_ts=NOW)
            loaded = store.load_override(now_ts=NOW)
            v = self._gate(loaded["grant"], freeze_marker=True)
            assert v["decision"] == "DENY"
            assert v["reason_code"] == "freeze_marker"
            store.revoke(now_ts=NOW, revoked_by="felix", reason="next")

    def test_dead_key_immune_to_any_override(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        for scope in sorted(OVERRIDE_SCOPES):
            issue_ok(store, scope=scope, now_ts=NOW)
            loaded = store.load_override(now_ts=NOW)
            v = self._gate(loaded["grant"], zai_key_dead_or_locked=True)
            assert v["decision"] == "DENY"
            assert v["reason_code"] == "dead_or_locked_key"
            store.revoke(now_ts=NOW, revoked_by="felix", reason="next")

    def test_full_round_trip_audit_trail(self, tmp_path):
        store, _, db = make_store(tmp_path)
        grant = issue_ok(store, scope="infra_down", now_ts=NOW)
        loaded = store.load_override(now_ts=NOW + 30)
        v = self._gate(loaded["grant"], price_unreachable=True)
        store.consume_override(v["override_consumed"], now_ts=NOW + 30,
                               task="t_roundtrip",
                               would_have_been=v["override_consumed"]["would_have_been"])
        store.revoke(now_ts=NOW + 600, revoked_by="felix", reason="window over")
        rs = override_rows(db)
        assert [r["kind"] for r in rs] == ["issued", "consumed", "revoked"]
        # plan field names present on every row
        for r in rs:
            assert {"issued_by", "scope", "ts"} <= set(r)
