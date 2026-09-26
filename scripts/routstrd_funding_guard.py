#!/usr/bin/env python3
"""routstrd_funding_guard.py — keep the routstrd wallet's NETWORK-spendable
ecash (real mints: minibits, cubabitcoin) above a 5,000-sat floor.

Run every 5 min from cron (installed by the tollgate-infrastructure-kit role
`routstrd_funding_guard`). Behavior:

  1. Read the routstrd wallet per-mint balance.
  2. network_sats = sats held at every mint EXCEPT mint.orangesync.tech
     (orangesync is our private testnut mint — not accepted by network nodes).
  3. Log a 'routstrd_network' row into api_burn.db provider_balances so the
     efficiency monitor and proxy quota views can see the real float.
  4. If network_sats >= FLOOR (5000): clear the alert transition flag, exit.
  5. Else: reuse a fresh (<2h) top-up invoice or create a fixed 10k-sat
     Lightning invoice at the lower-balance real mint, then surface it:
       - ~/.hermes/bot/routstrd_topup_invoice.txt (invoice + lightning: URI)
       - notify-send persistent desktop notification
       - one-shot espeak voice alert per underfunded transition

2026-09-26 — WALLET MIGRATION (this is the whole point of the reader rewrite):
the cocod daemon and its unix socket ``~/.cocod/cocod.sock`` are gone;
``~/.cocod/coco.db`` is now a 0-byte file whose schema reads as ZERO tables
(the legacy file is preserved at ``~/.cocod/wallet-migrated-*``). The active
wallet is ``~/.routstrd/wallet/coco.db`` (17 tables, ``coco_cashu_proofs``
with ``mintUrl`` / ``amount`` / ``state IN ('inflight','ready','spent')``),
owned by the routstrd daemon on 127.0.0.1:8008. Reading the dead path made the
guard see a phantom "0 sats" wallet.

Read strategy (see docs/routstr-ops-runbook.md §2 "WAL-Safe Ledger Reads"):

  primary  — the daemon itself: ``routstrd wallet balance`` on the box
             (the daemon owns the wallet, so it is always WAL-consistent).
  fallback — WAL-safe sqlite read of ``~/.routstrd/wallet/coco.db``: copy
             db + ``-wal`` + ``-shm`` together into a private temp dir, then
             open the COPY read-write and ``PRAGMA wal_checkpoint(TRUNCATE)``.
             A read-only/``immutable=1`` reader does NOT replay the WAL and
             would silently miss freshly-minted proofs. The live wallet is
             never opened, so it can never be corrupted by this guard.

Failure mode is EXPLICIT: a wallet that is missing, has no
``coco_cashu_proofs`` table (legacy/empty file) or holds zero proofs is an
ERROR — the guard logs and exits non-zero (2) instead of reporting a false
0-sat balance that would fire spurious top-ups. Silent (exit 0) when healthy
and when routstrd is not installed on the host at all; one-line status when
acting. Never raises.

``--balance-json`` prints ``{"balances":{mint:sats}, "network_sats":N,
"source":"daemon|db", "wallet_db":path}`` for the balance-collector crons and
exits non-zero on a read failure.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

HOME = Path.home()
# Active wallet (2026-09-26 migration) — NOT ~/.cocod/coco.db any more.
WALLET_DIR = HOME / ".routstrd" / "wallet"
WALLET_DB = WALLET_DIR / "coco.db"
WALLET_DB_ENV = "ROUTSTRD_WALLET_DB"
# routstrd CLI lives in the routstrd checkout (bun); `routstrd` may be on PATH.
ROUTSTRD_CLI_JS = HOME / "routstrd" / "dist" / "index.js"
CLI_ENV = "ROUTSTRD_CLI"
CLI_TIMEOUT = 30

BOT_DIR = HOME / ".hermes" / "bot"
API_BURN_DB = BOT_DIR / "api_burn.db"
STATE_FILE = BOT_DIR / ".routstrd_funding_guard_state.json"
INVOICE_FILE = BOT_DIR / "routstrd_topup_invoice.txt"

FLOOR_SATS = 5000
TOPUP_SATS = 10_000
INVOICE_FRESH_SECS = 2 * 3600
BTC_USD_RATE = float(os.environ.get("BTC_USD_RATE", "100000"))
TESTNUT_MINT = "https://mint.orangesync.tech"
REAL_MINTS = ["https://mint.minibits.cash/Bitcoin",
              "https://mint.cubabitcoin.org"]

READ_FAILED_EXIT = 2


class WalletReadError(RuntimeError):
    """The routstrd wallet could not be read — never to be reported as 0 sats."""


def _wallet_db_path() -> Path:
    return Path(os.environ.get(WALLET_DB_ENV) or WALLET_DB)


def _local_bun() -> str | None:
    """bun installed under ~/.bun/bin but not on cron's PATH (systemd unit does
    this explicitly), or None."""
    cand = HOME / ".bun" / "bin" / "bun"
    return str(cand) if cand.exists() else None


def _cli_command() -> list[str] | None:
    """Command prefix for the routstrd CLI, or None when it is not available.

    ``ROUTSTRD_CLI`` (env) overrides resolution; set-but-empty disables the
    daemon reader entirely (tests, hosts without routstrd).
    """
    if CLI_ENV in os.environ:
        parts = shlex.split(os.environ.get(CLI_ENV) or "")
        return parts or None
    exe = shutil.which("routstrd")
    if exe:
        return [exe]
    if ROUTSTRD_CLI_JS.exists():
        bun = shutil.which("bun") or _local_bun()
        if bun:
            return [bun, str(ROUTSTRD_CLI_JS)]
    return None


def _read_balance_cli(cli_cmd: list[str] | None = None) -> dict[str, int]:
    """Per-mint sat balance from the routstrd daemon CLI. Raises on failure.

    The daemon owns the wallet, so this is the WAL-correct view when it is up.
    """
    cmd = list(cli_cmd) if cli_cmd else _cli_command()
    if not cmd:
        raise WalletReadError(
            "routstrd daemon CLI not found (no `routstrd` on PATH and no "
            f"bun + {ROUTSTRD_CLI_JS})")
    try:
        r = subprocess.run(cmd + ["wallet", "balance"], check=False,
                           capture_output=True, text=True, timeout=CLI_TIMEOUT)
    except Exception as e:  # surfaced as an explicit read error
        raise WalletReadError(f"daemon CLI failed to run ({cmd[0]}): {e}") from e
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        raise WalletReadError(
            f"daemon CLI exit {r.returncode}: {detail[-1][:200] if detail else '(no output)'}")
    try:
        data = json.loads(r.stdout)
        balances = data["balances"]
        if not isinstance(balances, dict) or not balances:
            raise ValueError("payload carries no mints")
        return {str(m): int(v) for m, v in balances.items()}
    except Exception as e:
        raise WalletReadError(f"daemon CLI returned an unusable payload: {e}") from e


def _read_balance_db(db_path: Path | None = None) -> dict[str, int]:
    """Per-mint READY-proof sat balance from a WAL-safe copy of the wallet DB.

    The live wallet is never opened: db + ``-wal`` + ``-shm`` are copied to a
    private temp dir and the COPY is opened read-write so the WAL is replayed
    (a read-only/immutable reader would silently miss WAL-resident proofs).
    Raises WalletReadError on a missing/legacy/empty wallet.
    """
    path = Path(db_path) if db_path is not None else _wallet_db_path()
    if not path.exists():
        raise WalletReadError(f"wallet db missing: {path}")
    tmp = tempfile.mkdtemp(prefix="routstrd-wallet-")
    try:
        dst = Path(tmp) / path.name
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(path) + suffix)
            if src.exists():
                shutil.copy2(src, str(dst) + suffix)
        try:
            con = sqlite3.connect(str(dst))
        except sqlite3.Error as e:
            raise WalletReadError(f"wallet db unreadable at {path}: {e}") from e
        try:
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "coco_cashu_proofs" not in tables:
                raise WalletReadError(
                    f"wallet db has no coco_cashu_proofs table "
                    f"({len(tables)} tables) — empty/legacy wallet at {path}?")
            total = con.execute(
                "SELECT COUNT(*) FROM coco_cashu_proofs").fetchone()[0]
            if not total:
                raise WalletReadError(
                    f"wallet db holds zero proofs at {path} — empty wallet "
                    "(wrong path after the cocod→routstrd migration?)")
            rows = con.execute(
                "SELECT mintUrl, SUM(amount) FROM coco_cashu_proofs "
                "WHERE state = 'ready' GROUP BY mintUrl").fetchall()
        except sqlite3.Error as e:
            raise WalletReadError(f"wallet db query failed at {path}: {e}") from e
        finally:
            con.close()
        return {str(mint): int(sats or 0) for mint, sats in rows}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _read_balance() -> tuple[dict[str, int], str]:
    """(per-mint balances, source) from the daemon, else the wallet DB.

    Raises WalletReadError naming every source that failed — callers must NOT
    fall back to a 0-sat balance.
    """
    errors: list[str] = []
    for source, reader in (("daemon", _read_balance_cli), ("db", _read_balance_db)):
        try:
            return reader(), source
        except WalletReadError as e:
            errors.append(f"{source}: {e}")
        except Exception as e:  # noqa: BLE001 — a reader bug is a read failure
            errors.append(f"{source}: unexpected {type(e).__name__}: {e}")
    raise WalletReadError(" | ".join(errors))


def network_sats_from(balances: Mapping[str, int]) -> int:
    """Spendable-with-network-nodes sats: every mint except the testnut one."""
    return sum(int(sats) for mint, sats in balances.items() if mint != TESTNUT_MINT)


def _log_balance(network_sats: int, balances: Mapping[str, int] | None = None,
                 source: str = "") -> None:
    usd = network_sats / 1e8 * BTC_USD_RATE
    raw = {"network_sats": network_sats, "btc_usd": BTC_USD_RATE}
    if balances is not None:
        raw["per_mint"] = {str(m): int(s) for m, s in balances.items()}
    if source:
        raw["source"] = source
    # API_BURN_DB env override matches the other balance crons/src collectors.
    db = Path(os.environ.get("API_BURN_DB") or API_BURN_DB)
    try:
        conn = sqlite3.connect(db)
        conn.execute(
            """INSERT INTO provider_balances
               (provider, collected_at, usage, limit_credits, limit_remaining,
                usage_fraction, is_unlimited, is_free_tier, raw_json)
               VALUES ('routstrd_network', ?, 0, 0, ?, 0, 0, 0, ?)""",
            (time.time(), round(usd, 6), json.dumps(raw)),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=1))
    except Exception:
        pass


def _create_invoice(mint: str) -> str | None:
    """Fixed top-up invoice via the routstrd CLI. Returns bolt11 or None."""
    cmd = _cli_command()
    if not cmd:
        return None
    try:
        r = subprocess.run(
            cmd + ["wallet", "receive", "bolt11", str(TOPUP_SATS),
                   "--mint-url", mint],
            capture_output=True, text=True, timeout=60)
        blob = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"(lnbc[a-z0-9]+)", blob)
        return m.group(1) if m else None
    except Exception:
        return None


def _notify(title: str, body: str) -> None:
    """Persistent desktop notification. Split out so tests can silence it."""
    try:
        subprocess.run(["notify-send", "-u", "critical", "-t", "0", title, body],
                       timeout=15, check=False)
    except Exception:
        pass


def _surface(invoice: str, network_sats: int) -> None:
    uri = f"lightning:{invoice}"
    try:
        INVOICE_FILE.write_text(
            f"routstrd wallet low: {network_sats} network sats (< {FLOOR_SATS})\n"
            f"Top up {TOPUP_SATS} sats:\n{invoice}\n{uri}\n"
            f"created: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
    except Exception:
        pass
    _notify("routstrd wallet low on real ecash",
            f"{network_sats} network sats (< {FLOOR_SATS}). "
            f"Top up {TOPUP_SATS} sats: {uri}")


def _espeak(msg: str) -> None:
    try:
        # Natural Piper voice (en_US-ryan-high); espeak-ng fallback inside wrapper
        subprocess.run(["/home/c03rad0r/.hermes/voices/say.sh", msg],
                       timeout=60, check=False)
    except Exception:
        pass


def _balance_json(balances: Mapping[str, int], source: str) -> str:
    return json.dumps({
        "balances": {str(m): int(s) for m, s in balances.items()},
        "network_sats": network_sats_from(balances),
        "source": source,
        "wallet_db": str(_wallet_db_path()),
        "testnut_mint": TESTNUT_MINT,
    })


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--balance-json" in args

    # routstrd not installed on this host at all → nothing to guard, stay silent.
    if (not WALLET_DIR.exists() and not _wallet_db_path().exists()
            and not _cli_command()):
        if as_json:
            print("routstrd wallet not present on this host")
        return 0

    try:
        balances, source = _read_balance()
    except WalletReadError as e:
        print(f"⚠️ funding guard: routstrd wallet read FAILED — {e}")
        return READ_FAILED_EXIT

    network_sats = network_sats_from(balances)

    if as_json:
        print(_balance_json(balances, source))
        return 0

    _log_balance(network_sats, balances, source)

    if network_sats >= FLOOR_SATS:
        state = _load_state()
        if state.get("alerted"):
            state["alerted"] = False
            state.pop("invoice", None)
            _save_state(state)
        return 0

    state = _load_state()
    invoice = state.get("invoice") or ""
    ts = float(state.get("ts") or 0)
    fresh = invoice and (time.time() - ts) < INVOICE_FRESH_SECS

    if not fresh:
        real = {m: balances.get(m, 0) for m in REAL_MINTS}
        mint = min(real, key=real.get)
        invoice = _create_invoice(mint)
        if not invoice:
            print(f"🪫 funding guard: {network_sats} sats < {FLOOR_SATS} and "
                  f"invoice creation FAILED at {mint}")
            return 0
        state = {"invoice": invoice, "mint": mint, "ts": time.time()}
        print(f"🪫 funding guard: {network_sats} network sats < {FLOOR_SATS} "
              f"→ new {TOPUP_SATS}-sat invoice at {mint}")
    elif not state.get("alerted"):
        print(f"🪫 funding guard: still low ({network_sats} sats), "
              f"reusing invoice from {state.get('mint')}")

    state["alerted"] = True
    _save_state(state)
    _surface(invoice, network_sats)

    if not state.get("spoken"):
        _espeak(f"Warning. routstr wallet below {FLOOR_SATS // 1000} thousand sats. "
                f"Top up invoice created.")
        state["spoken"] = True
        _save_state(state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
