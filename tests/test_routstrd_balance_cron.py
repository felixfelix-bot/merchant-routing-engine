"""Tests for scripts/routstrd_balance_cron.sh — the routstrd wallet collector.

Locks the 2026-09-26 path repoint: the collector must read
``~/.routstrd/wallet/coco.db`` (via routstrd_funding_guard.py --balance-json),
record the NETWORK sats (testnut mint excluded) into api_burn.db
``provider_balances``, and FAIL LOUDLY (non-zero, no row) on a missing/legacy
wallet instead of writing a silent 0-sat row.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO, "scripts", "routstrd_balance_cron.sh")

MINIBITS = "https://mint.minibits.cash/Bitcoin"
ORANGESYNC = "https://mint.orangesync.tech"

_BALANCES_DDL = """
CREATE TABLE provider_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, collected_at REAL,
    usage REAL, limit_credits REAL, limit_remaining REAL, usage_fraction REAL,
    is_unlimited INTEGER, is_free_tier INTEGER, raw_json TEXT)
"""

_PROOFS_DDL = """
CREATE TABLE coco_cashu_proofs (
    mintUrl TEXT NOT NULL, id TEXT NOT NULL, amount INTEGER NOT NULL,
    secret TEXT NOT NULL, C TEXT NOT NULL, dleqJson TEXT, witnessJson TEXT,
    state TEXT NOT NULL CHECK (state IN ('inflight', 'ready', 'spent')),
    createdAt INTEGER NOT NULL, usedByOperationId TEXT, createdByOperationId TEXT,
    PRIMARY KEY (mintUrl, secret))
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _wallet(path, proofs):
    con = sqlite3.connect(str(path))
    con.execute(_PROOFS_DDL)
    for i, (mint, amount, state) in enumerate(proofs):
        con.execute(
            "INSERT INTO coco_cashu_proofs (mintUrl, id, amount, secret, C, state,"
            " createdAt) VALUES (?,?,?,?,?,?,?)",
            (mint, f"i{i}", amount, f"s{i}", f"C{i}", state, 1758900000 + i))
    con.commit()
    con.close()


def _burn_db(path):
    con = sqlite3.connect(str(path))
    con.execute(_BALANCES_DDL)
    con.commit()
    con.close()


def _run(tmp_path, wallet_db, *, cli=""):
    burn = tmp_path / "burn.db"
    _burn_db(burn)
    env = dict(os.environ)
    env.update({
        "ROUTSTRD_REPO": _REPO,
        "ROUTSTRD_WALLET_DB": str(wallet_db),
        "ROUTSTRD_CLI": cli,          # "" disables the daemon reader → DB path
        "API_BURN_DB": str(burn),
    })
    p = subprocess.run(["bash", _SCRIPT], check=False, capture_output=True,
                       text=True, env=env, timeout=120)
    con = sqlite3.connect(str(burn))
    rows = con.execute(
        "SELECT limit_remaining, raw_json FROM provider_balances "
        "WHERE provider='routstrd'").fetchall()
    con.close()
    return p, rows


def test_reads_new_wallet_and_excludes_testnut(tmp_path):
    wallet = tmp_path / "coco.db"
    _wallet(wallet, [(MINIBITS, 35511, "ready"), (ORANGESYNC, 5000, "ready"),
                     (MINIBITS, 526, "spent")])
    p, rows = _run(tmp_path, wallet)
    assert p.returncode == 0, p.stdout + p.stderr
    assert len(rows) == 1
    usd, raw_json = rows[0]
    assert usd == pytest.approx(35511 / 1e8 * 100000)
    raw = json.loads(raw_json)
    assert raw["balance_sats"] == 35511          # testnut excluded
    assert raw["network_sats"] == 35511
    assert raw["per_mint"][ORANGESYNC] == 5000   # but still reported per mint


def test_missing_wallet_fails_loudly_and_writes_no_row(tmp_path):
    p, rows = _run(tmp_path, tmp_path / "nope" / "coco.db")
    assert p.returncode != 0
    assert rows == []
    assert "read FAILED" in (p.stdout + p.stderr)


def test_legacy_zero_table_wallet_fails_loudly(tmp_path):
    legacy = tmp_path / "coco.db"
    legacy.write_bytes(b"")
    p, rows = _run(tmp_path, legacy)
    assert p.returncode != 0
    assert rows == []
    assert "read FAILED" in (p.stdout + p.stderr)
