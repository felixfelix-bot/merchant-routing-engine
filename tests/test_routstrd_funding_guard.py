"""Tests for scripts/routstrd_funding_guard.py — routstrd wallet balance reader.

2026-09-26: the wallet MIGRATED out of the cocod daemon. The guard used to read
the cocod daemon over ``~/.cocod/cocod.sock``; that socket is gone and
``~/.cocod/coco.db`` is now a 0-byte file whose schema reads as ZERO tables.
The active wallet is ``~/.routstrd/wallet/coco.db`` (17 tables,
``coco_cashu_proofs`` with ``mintUrl`` / ``amount`` / ``state``) owned by the
routstrd daemon on 127.0.0.1:8008.

Covers:
  - per-mint READY balances read from the new wallet schema (mintUrl/amount/state)
  - spent / inflight proofs excluded from the spendable balance
  - WAL-safe reads: rows living only in ``-wal`` are visible (the repo's
    documented gotcha is that a naive immutable/read-only reader misses them)
  - the source wallet is never modified by a read
  - EMPTY (schema present, zero proofs) / legacy 0-table / missing wallet are
    ERRORS — never a silent "0 sats"
  - the daemon CLI reader (``routstrd wallet balance``) as the primary source,
    with the DB as fallback and an explicit failure when both fail
  - network_sats excludes the private testnut mint (https://mint.orangesync.tech)
  - main(): healthy → logs the network row and exits 0; unreadable → exits
    non-zero and logs NO zero-sat row; low → invoice surfaced; --balance-json
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import stat

import pytest

# scripts/ is not a Python package — load the guard by path (same pattern as
# tests/test_cost_column.py).
_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "routstrd_funding_guard.py",
)
_spec = importlib.util.spec_from_file_location("routstrd_funding_guard", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

MINIBITS = "https://mint.minibits.cash/Bitcoin"
ORANGESYNC = "https://mint.orangesync.tech"
CUBABITCOIN = "https://mint.cubabitcoin.org"

# Production DDL for the migrated wallet (verbatim from ~/.routstrd/wallet/coco.db).
_PROOFS_DDL = """
CREATE TABLE coco_cashu_proofs (
        mintUrl   TEXT NOT NULL,
        id        TEXT NOT NULL,
        amount    INTEGER NOT NULL,
        secret    TEXT NOT NULL,
        C         TEXT NOT NULL,
        dleqJson  TEXT,
        witnessJson   TEXT,
        state     TEXT NOT NULL CHECK (state IN ('inflight', 'ready', 'spent')),
        createdAt INTEGER NOT NULL, usedByOperationId TEXT, createdByOperationId TEXT,
        PRIMARY KEY (mintUrl, secret)
      )
"""

_MINT_OPS_DDL = """
CREATE TABLE coco_cashu_mint_operations (
        id TEXT PRIMARY KEY,
        mintUrl TEXT NOT NULL,
        quoteId TEXT,
        state TEXT NOT NULL,
        createdAt INTEGER NOT NULL,
        updatedAt INTEGER NOT NULL
      )
"""


def _mkwallet(path, proofs=(), *, schema=True, wal=False):
    """Create a fixture wallet DB. ``proofs`` = iterable of (mintUrl, amount, state).

    Returns the open connection when ``wal=True`` (so the rows stay resident in
    the ``-wal`` file — the realistic daemon-held scenario), else None.
    """
    con = sqlite3.connect(str(path))
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    if schema:
        con.execute(_PROOFS_DDL)
        con.execute(_MINT_OPS_DDL)
    for i, (mint, amount, state) in enumerate(proofs):
        con.execute(
            "INSERT INTO coco_cashu_proofs (mintUrl, id, amount, secret, C, state,"
            " createdAt) VALUES (?,?,?,?,?,?,?)",
            (mint, f"id{i}", amount, f"secret{i}", f"C{i}", state, 1758900000 + i),
        )
    con.commit()
    if wal:
        return con
    con.close()
    return None


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _fake_cli(dirpath, payload, *, rc=0):
    """Write an executable stand-in for the routstrd CLI. Returns its path."""
    p = dirpath / "fake-routstrd"
    body = "#!/usr/bin/env python3\nimport sys\n"
    if isinstance(payload, str):
        body += f"sys.stdout.write({payload!r})\n"
    else:
        body += f"sys.stdout.write({json.dumps(payload)!r})\n"
    body += f"sys.exit({rc})\n"
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


@pytest.fixture
def no_cli(monkeypatch):
    """Disable daemon-CLI resolution so the DB path is exercised deterministically."""
    monkeypatch.setenv("ROUTSTRD_CLI", "")


@pytest.fixture
def guard_tmp(monkeypatch, tmp_path):
    """Point every guard output path at tmp_path — never touch ~/.hermes/bot."""
    bot = tmp_path / "bot"
    bot.mkdir()
    monkeypatch.setattr(guard, "BOT_DIR", bot)
    monkeypatch.setattr(guard, "API_BURN_DB", bot / "api_burn.db")
    monkeypatch.setattr(guard, "STATE_FILE", bot / ".state.json")
    monkeypatch.setattr(guard, "INVOICE_FILE", bot / "routstrd_topup_invoice.txt")
    monkeypatch.delenv("API_BURN_DB", raising=False)
    # no desktop notification and no speaking during tests
    monkeypatch.setattr(guard, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(guard, "_espeak", lambda *a, **k: None)
    # create the provider_balances table _log_balance writes into
    con = sqlite3.connect(bot / "api_burn.db")
    con.execute(
        """CREATE TABLE provider_balances (
             id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, collected_at REAL,
             usage REAL, limit_credits REAL, limit_remaining REAL,
             usage_fraction REAL, is_unlimited INTEGER, is_free_tier INTEGER,
             raw_json TEXT)"""
    )
    con.commit()
    con.close()
    return bot


def _network_rows(db):
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT provider, limit_remaining, raw_json FROM provider_balances "
        "WHERE provider='routstrd_network'"
    ).fetchall()
    con.close()
    return rows


# ── DB reader ────────────────────────────────────────────────────────────────


class TestReadBalanceDb:
    def test_per_mint_ready_balances(self, no_cli, tmp_path):
        db = tmp_path / "coco.db"
        _mkwallet(db, [(MINIBITS, 35511, "ready"), (ORANGESYNC, 5000, "ready")])
        assert guard._read_balance_db(db) == {MINIBITS: 35511, ORANGESYNC: 5000}

    def test_spent_and_inflight_excluded(self, no_cli, tmp_path):
        db = tmp_path / "coco.db"
        _mkwallet(db, [
            (MINIBITS, 35511, "ready"),
            (MINIBITS, 526, "spent"),
            (CUBABITCOIN, 2500, "inflight"),
        ])
        assert guard._read_balance_db(db) == {MINIBITS: 35511}

    def test_wal_resident_rows_visible(self, no_cli, tmp_path):
        """Rows still only in -wal (writer connection open, like the daemon)."""
        db = tmp_path / "coco.db"
        writer = _mkwallet(db, [(MINIBITS, 35511, "ready"), (ORANGESYNC, 5000, "ready")],
                           wal=True)
        try:
            assert (db.parent / (db.name + "-wal")).exists()
            assert guard._read_balance_db(db) == {MINIBITS: 35511, ORANGESYNC: 5000}
        finally:
            writer.close()

    def test_naive_immutable_reader_misses_wal_rows(self, no_cli, tmp_path):
        """Documents the repo's gotcha: an immutable URI does NOT replay the WAL."""
        db = tmp_path / "coco.db"
        writer = _mkwallet(db, [(MINIBITS, 35511, "ready")], wal=True)
        try:
            con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
            with pytest.raises(sqlite3.Error):
                con.execute("SELECT COUNT(*) FROM coco_cashu_proofs").fetchone()
            con.close()
        finally:
            writer.close()

    def test_source_wallet_never_modified(self, no_cli, tmp_path):
        db = tmp_path / "coco.db"
        _mkwallet(db, [(MINIBITS, 35511, "ready")])
        before = _sha(db)
        guard._read_balance_db(db)
        assert _sha(db) == before

    def test_empty_wallet_is_error(self, no_cli, tmp_path):
        """Schema present but ZERO proofs → ERROR, never a silent 0 sats."""
        db = tmp_path / "coco.db"
        _mkwallet(db, [])
        with pytest.raises(guard.WalletReadError):
            guard._read_balance_db(db)

    def test_legacy_zero_table_db_is_error(self, no_cli, tmp_path):
        """The 0-byte ~/.cocod/coco.db case: file exists, schema has no tables."""
        db = tmp_path / "coco.db"
        db.write_bytes(b"")
        with pytest.raises(guard.WalletReadError):
            guard._read_balance_db(db)

    def test_missing_wallet_is_error(self, no_cli, tmp_path):
        with pytest.raises(guard.WalletReadError):
            guard._read_balance_db(tmp_path / "nope" / "coco.db")


# ── daemon CLI reader ────────────────────────────────────────────────────────


class TestReadBalanceCli:
    def test_parses_json_balances(self, monkeypatch, tmp_path):
        cli = _fake_cli(tmp_path, {"balances": {MINIBITS: 35511, ORANGESYNC: 5000},
                                   "unit": "sat"})
        monkeypatch.setenv("ROUTSTRD_CLI", str(cli))
        assert guard._read_balance_cli() == {MINIBITS: 35511, ORANGESYNC: 5000}

    def test_nonzero_exit_raises(self, monkeypatch, tmp_path):
        cli = _fake_cli(tmp_path, "boom", rc=3)
        monkeypatch.setenv("ROUTSTRD_CLI", str(cli))
        with pytest.raises(guard.WalletReadError):
            guard._read_balance_cli()

    def test_malformed_json_raises(self, monkeypatch, tmp_path):
        cli = _fake_cli(tmp_path, "not json at all")
        monkeypatch.setenv("ROUTSTRD_CLI", str(cli))
        with pytest.raises(guard.WalletReadError):
            guard._read_balance_cli()

    def test_empty_balances_raises(self, monkeypatch, tmp_path):
        cli = _fake_cli(tmp_path, {"balances": {}})
        monkeypatch.setenv("ROUTSTRD_CLI", str(cli))
        with pytest.raises(guard.WalletReadError):
            guard._read_balance_cli()

    def test_no_cli_available_raises(self, monkeypatch):
        monkeypatch.setenv("ROUTSTRD_CLI", "")
        with pytest.raises(guard.WalletReadError):
            guard._read_balance_cli()


# ── CLI resolution ───────────────────────────────────────────────────────────


class TestCliResolution:
    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("ROUTSTRD_CLI", "/opt/x/routstrd --flag")
        assert guard._cli_command() == ["/opt/x/routstrd", "--flag"]

    def test_env_empty_disables(self, monkeypatch):
        monkeypatch.setenv("ROUTSTRD_CLI", "")
        assert guard._cli_command() is None

    def test_path_routstrd_wins_over_bun(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ROUTSTRD_CLI", raising=False)
        monkeypatch.setattr(guard.shutil, "which",
                            lambda n: {"routstrd": "/usr/bin/routstrd",
                                       "bun": "/usr/local/bin/bun"}.get(n))
        assert guard._cli_command() == ["/usr/bin/routstrd"]

    def test_falls_back_to_bun_plus_dist_js(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ROUTSTRD_CLI", raising=False)
        monkeypatch.setattr(guard.shutil, "which",
                            lambda n: "/usr/local/bin/bun" if n == "bun" else None)
        js = tmp_path / "index.js"
        js.write_text("")
        monkeypatch.setattr(guard, "ROUTSTRD_CLI_JS", js)
        assert guard._cli_command() == ["/usr/local/bin/bun", str(js)]

    def test_local_bun_fallback_used_when_path_lacks_it(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ROUTSTRD_CLI", raising=False)
        monkeypatch.setattr(guard.shutil, "which", lambda n: None)
        js = tmp_path / "index.js"
        js.write_text("")
        monkeypatch.setattr(guard, "ROUTSTRD_CLI_JS", js)
        monkeypatch.setattr(guard, "_local_bun", lambda: "/home/x/.bun/bin/bun")
        assert guard._cli_command() == ["/home/x/.bun/bin/bun", str(js)]

    def test_nothing_available_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ROUTSTRD_CLI", raising=False)
        monkeypatch.setattr(guard.shutil, "which", lambda n: None)
        monkeypatch.setattr(guard, "ROUTSTRD_CLI_JS", tmp_path / "absent.js")
        assert guard._cli_command() is None


# ── fallback chain ───────────────────────────────────────────────────────────


class TestReadBalanceFallback:
    def test_daemon_cli_is_primary(self, monkeypatch, tmp_path):
        cli = _fake_cli(tmp_path, {"balances": {MINIBITS: 1234}})
        monkeypatch.setenv("ROUTSTRD_CLI", str(cli))
        monkeypatch.setattr(guard, "WALLET_DB", tmp_path / "does-not-exist.db")
        balances, source = guard._read_balance()
        assert balances == {MINIBITS: 1234}
        assert source == "daemon"

    def test_falls_back_to_db_when_cli_fails(self, monkeypatch, tmp_path):
        cli = _fake_cli(tmp_path, "boom", rc=1)
        monkeypatch.setenv("ROUTSTRD_CLI", str(cli))
        db = tmp_path / "coco.db"
        _mkwallet(db, [(MINIBITS, 35511, "ready"), (ORANGESYNC, 5000, "ready")])
        monkeypatch.setattr(guard, "WALLET_DB", db)
        balances, source = guard._read_balance()
        assert balances == {MINIBITS: 35511, ORANGESYNC: 5000}
        assert source == "db"

    def test_both_failing_raises_mentioning_both(self, monkeypatch, tmp_path):
        cli = _fake_cli(tmp_path, "boom", rc=1)
        monkeypatch.setenv("ROUTSTRD_CLI", str(cli))
        monkeypatch.setattr(guard, "WALLET_DB", tmp_path / "nope.db")
        with pytest.raises(guard.WalletReadError) as ei:
            guard._read_balance()
        msg = str(ei.value)
        assert "daemon" in msg and "db" in msg


# ── network_sats ─────────────────────────────────────────────────────────────


class TestNetworkSats:
    def test_excludes_testnut_mint(self):
        assert guard.network_sats_from(
            {MINIBITS: 35511, ORANGESYNC: 5000}) == 35511

    def test_all_private_mint_is_zero(self):
        assert guard.network_sats_from({ORANGESYNC: 5000}) == 0

    def test_empty_is_zero(self):
        assert guard.network_sats_from({}) == 0

    def test_floor_and_topup_constants_unchanged(self):
        assert guard.FLOOR_SATS == 5000
        assert guard.TOPUP_SATS == 10_000
        assert guard.TESTNUT_MINT == ORANGESYNC


# ── main() ───────────────────────────────────────────────────────────────────


class TestMain:
    def test_healthy_wallet_logs_row_and_exits_0(self, monkeypatch, tmp_path,
                                                 guard_tmp, no_cli):
        db = tmp_path / "coco.db"
        _mkwallet(db, [(MINIBITS, 35511, "ready"), (ORANGESYNC, 5000, "ready")])
        monkeypatch.setattr(guard, "WALLET_DB", db)
        assert guard.main([]) == 0
        rows = _network_rows(guard_tmp / "api_burn.db")
        assert len(rows) == 1
        raw = json.loads(rows[0][2])
        assert raw["network_sats"] == 35511      # orangesync excluded
        assert raw["per_mint"][ORANGESYNC] == 5000

    def test_unreadable_wallet_exits_nonzero_and_logs_no_zero_row(
            self, monkeypatch, tmp_path, guard_tmp, no_cli):
        """The regression: an empty/0-byte wallet must NOT read as '0 sats'."""
        db = tmp_path / "coco.db"
        db.write_bytes(b"")
        monkeypatch.setattr(guard, "WALLET_DB", db)
        assert guard.main([]) != 0
        assert _network_rows(guard_tmp / "api_burn.db") == []

    def test_low_balance_creates_invoice_and_writes_invoice_file(
            self, monkeypatch, tmp_path, guard_tmp, no_cli):
        db = tmp_path / "coco.db"
        _mkwallet(db, [(MINIBITS, 1000, "ready"), (ORANGESYNC, 5000, "ready")])
        monkeypatch.setattr(guard, "WALLET_DB", db)
        monkeypatch.setattr(guard, "_create_invoice", lambda mint: "lnbc1faketopup")
        assert guard.main([]) == 0
        rows = _network_rows(guard_tmp / "api_burn.db")
        assert json.loads(rows[0][2])["network_sats"] == 1000
        text = (guard_tmp / "routstrd_topup_invoice.txt").read_text()
        assert "lnbc1faketopup" in text
        assert "lightning:lnbc1faketopup" in text
        state = json.loads((guard_tmp / ".state.json").read_text())
        assert state["alerted"] is True

    def test_not_installed_is_silent_skip(self, monkeypatch, tmp_path, guard_tmp,
                                         no_cli):
        monkeypatch.setattr(guard, "WALLET_DB", tmp_path / "none" / "coco.db")
        monkeypatch.setattr(guard, "WALLET_DIR", tmp_path / "none")
        assert guard.main([]) == 0
        assert _network_rows(guard_tmp / "api_burn.db") == []

    def test_balance_json_flag(self, monkeypatch, tmp_path, capsys, no_cli):
        db = tmp_path / "coco.db"
        _mkwallet(db, [(MINIBITS, 35511, "ready"), (ORANGESYNC, 5000, "ready")])
        monkeypatch.setattr(guard, "WALLET_DB", db)
        assert guard.main(["--balance-json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["balances"] == {MINIBITS: 35511, ORANGESYNC: 5000}
        assert out["network_sats"] == 35511
        assert out["source"] == "db"

    def test_balance_json_flag_exits_nonzero_on_bad_wallet(
            self, monkeypatch, tmp_path, guard_tmp, no_cli):
        db = tmp_path / "coco.db"
        db.write_bytes(b"")
        monkeypatch.setattr(guard, "WALLET_DB", db)
        assert guard.main(["--balance-json"]) != 0
