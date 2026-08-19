"""realtime_pricing.py — single source of truth for measured $/M.

Per ``docs/realtime-pricing-design.md``. Replaces every hardcoded rate constant
with a value the system *measures* from real billing data, smoothed by
``PriceKalman``, refreshed on a 5–30 min batch cycle, and served to all
consumers from one thread-safe singleton.

Lifecycle:
    • ``get_instance()``   — lazy singleton (matches LiveRouter/ShadowHook)
    • ``refresh()``        — batch collector; cron-driven, NOT per-request
    • ``snapshot()``       — O(1) cached read; per-request hot path
    • ``get_rate(...)``    — single-provider convenience lookup
    • ``get_provider_rates()`` — {provider: rate} dict (drop-in for _base_rates)
    • ``feed_kalman(...)`` — push latest observation into a caller's PriceKalman

Guarantees:
    • NEVER raises from public methods (logs + returns last snapshot).
    • ``snapshot()`` is lock-free for readers (returns immutable RateSnapshot).
    • ``refresh()`` is serialised by an RLock; concurrent calls no-op.
    • A failed collector leaves the previous observation in place and marks
      the provider ``source='cold_start_fallback'`` for that cycle.

Data sources (5 collectors):
    1. z.ai amortized   — monthly_fee / (SUM(total_tokens)/1e6) from api_calls
    2. ollama billing   — activity.cost / activity.tokens from /api/usage
    3. PPQ ledger       — SUM(cost_usd)/(SUM(total_tokens)/1e6) from api_burn.db
    4. DeepInfra spend  — SUM(spend_usd)/(SUM(token_count)/1e6) from daily_spend
    5. OpenRouter spend — same pattern; falls back to published list price
"""
from __future__ import annotations

import calendar
import logging
import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Final

from price_kalman import MIN_EFFECTIVE_PRICE, PriceKalman

__all__ = [
    "RateObservation",
    "RateSnapshot",
    "RealtimePricing",
    "SRC_COLD_START",
    "SRC_DEEPINFRA_ACTUAL",
    "SRC_OLLAMA_BILLING",
    "SRC_OPENROUTER_ACTUAL",
    "SRC_PQQ_LEDGER",
    "SRC_PUBLISHED",
    "SRC_ZAI_AMORTIZED",
    "MEASURED_SOURCES",
    "DEFAULT_COLD_START_RATES",
    # ── PM-T5: per-model spend (daily_spend model column) ───────────────────
    "published_model_rate",
    "published_model_rates",
    "migrate_daily_spend_add_model",
    "is_realtime_pricing_enabled",
]

_log = logging.getLogger(__name__)

# ── Provenance strings (plain str for SQLite ergonomics) ─────────────────────

SRC_OLLAMA_BILLING: Final = "ollama_billing_api"      # measured
SRC_PQQ_LEDGER: Final = "ppq_ledger"                  # measured
SRC_DEEPINFRA_ACTUAL: Final = "deepinfra_actual"      # measured
SRC_OPENROUTER_ACTUAL: Final = "openrouter_actual"    # measured
SRC_ZAI_AMORTIZED: Final = "zai_amortized"             # estimated (config / tokens)
SRC_PUBLISHED: Final = "published_list"               # estimated (list price)
SRC_COLD_START: Final = "cold_start_fallback"         # estimated (no data yet)

MEASURED_SOURCES: Final[frozenset[str]] = frozenset({
    SRC_OLLAMA_BILLING, SRC_PQQ_LEDGER,
    SRC_DEEPINFRA_ACTUAL, SRC_OPENROUTER_ACTUAL,
})

# ── Cold-start seed values (startup fallbacks, is_measured=False) ─────────────
# These are the ONLY constants. After the first refresh() cycle the Kalman
# output is independent of the seed. See docs/extra-usage-real-data-analysis.md
# for provenance of the measured values.
DEFAULT_COLD_START_RATES: Final[dict[str, float]] = {
    "ours":                     0.001,   # sunk cost, floored at MIN_EFFECTIVE_PRICE
    "friend":                   0.001,   # sunk cost, floored at MIN_EFFECTIVE_PRICE
    "ollama_cloud":             0.0155,  # MEASURED included rate
    "ollama_cloud_extra_glm52": 0.46,    # MEASURED extra-usage rate (glm-5.2)
    "ollama_cloud_kimi3":       7.53,    # MEASURED exclusive model (always extra)
    "ppq":                      0.14,    # known rate
    "openrouter":               0.135,   # known rate
    "deepinfra":                1.30,    # known rate
}

# z.ai monthly fees (from config/providers.yaml). The optimizer applies the
# 21% friend premium (ADR-005) on top; we keep base rates honest here.
_ZAI_FEES: Final[dict[str, float]] = {"ours": 155.0, "friend": 0.0}
_OLLAMA_MONTHLY_FEE: Final = 100.0

# Published list prices (fallback when no measured data and no cold-start)
_PUBLISHED_PRICES: Final[dict[str, float]] = {
    "ppq": 0.14,
    "openrouter": 0.135,
    "deepinfra": 1.30,
}

# Per-source Kalman measurement noise (see design doc §2.4 / Appendix B)
_KALMAN_NOISE: Final[dict[str, float]] = {
    SRC_ZAI_AMORTIZED:     1e-6,
    SRC_OLLAMA_BILLING:    1e-4,
    SRC_PQQ_LEDGER:        1e-4,
    SRC_DEEPINFRA_ACTUAL:  1e-3,
    SRC_OPENROUTER_ACTUAL: 1e-3,
    SRC_PUBLISHED:         1e-2,
    SRC_COLD_START:        1.0,  # very noisy — trust the first real obs over this
}

# Spend look-back window for DB-backed collectors (seconds)
SPEND_WINDOW_S: Final = 7 * 86400  # 7 days

# ── Per-model published list prices (PM-T5 fallback) ─────────────────────────
# config/providers.yaml → external.<provider>.models.<model> carries
# cost_per_1m_input / cost_per_1m_output. Until daily_spend is migrated to
# carry a `model` column (see migrate_daily_spend_add_model), the per-model
# resolution chain falls back to these published list prices (plan §3.6
# step 3 / §3.5). Blended $/M = (cost_per_1m_input + cost_per_1m_output) / 2.

_DEFAULT_PROVIDERS_YAML: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "providers.yaml",
)


def published_model_rate(
    provider: str, model: str, providers_yaml: str | None = None
) -> float | None:
    """Published list-price $/M for one ``(provider, model)`` pair from
    config/providers.yaml. Returns None if the model isn't listed. The
    per-model fallback for the resolution chain (plan §3.6 step 3). Never
    raises.
    """
    return published_model_rates(provider, providers_yaml).get(model)


def published_model_rates(
    provider: str, providers_yaml: str | None = None
) -> dict[str, float]:
    """All published per-model list prices ``{model: blended_$/M}`` for
    *provider* from config/providers.yaml → ``external.<provider>.models``.
    Blended rate is ``(cost_per_1m_input + cost_per_1m_output) / 2``. Returns
    ``{}`` on any failure or missing section. Never raises.
    """
    path = providers_yaml or _DEFAULT_PROVIDERS_YAML
    out: dict[str, float] = {}
    try:
        import yaml  # type: ignore[import-untyped]

        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        ext = data.get("external", {}) or {}
        prov_cfg = ext.get(provider, {}) or {}
        for model, mcfg in (prov_cfg.get("models", {}) or {}).items():
            if not isinstance(mcfg, dict):
                continue
            cin = _safe_float(mcfg.get("cost_per_1m_input"))
            cout = _safe_float(mcfg.get("cost_per_1m_output"))
            if cin is None or cout is None:
                continue
            out[model] = (cin + cout) / 2.0
    except Exception:
        _log.debug(
            "published model rates load failed for %s", provider, exc_info=True
        )
    return out


def _spend_table_has_model(conn: sqlite3.Connection) -> bool:
    """True iff a daily_spend table with a ``model`` column is attached to
    *conn*. False when the table is absent or pre-migration (no model column).
    Never raises.
    """
    try:
        cols = conn.execute("PRAGMA table_info(daily_spend)").fetchall()
    except Exception:
        return False
    return any((r[1] or "") == "model" for r in cols)


def migrate_daily_spend_add_model(db_path: str) -> dict[str, object]:
    """PM-T5 schema migration: give daily_spend a per-model dimension.

    Adds a ``model TEXT NOT NULL DEFAULT 'unknown'`` column and upgrades the
    primary key from ``(date, tier)`` to ``(date, tier, model)`` so the spend
    collector can record one row per model per day. Existing rows are
    back-filled to ``model='unknown'`` (aggregated onto the new key).

    SQLite cannot ALTER a primary key, so the migration rebuilds the table:
    create ``daily_spend_new`` → copy + aggregate → drop old → rename, all in
    one ``BEGIN IMMEDIATE`` transaction. On any error it rolls back and leaves
    the original table untouched.

    Idempotent: a no-op (bar a NULL→'unknown' sweep) on an already-migrated
    table. Safe on a DB without daily_spend (returns early). Never raises —
    returns a report dict instead::

        {"table_exists": bool, "had_model_column": bool,
         "migrated": bool, "rows": int}
    """
    report: dict[str, object] = {
        "table_exists": False,
        "had_model_column": False,
        "migrated": False,
        "rows": 0,
    }
    try:
        conn = sqlite3.connect(db_path, timeout=10)
    except Exception:
        _log.warning("migrate: cannot open %s", db_path, exc_info=True)
        return report
    try:
        tbl = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='daily_spend'"
        ).fetchone()
        if not tbl:
            return report
        report["table_exists"] = True
        has_model = _spend_table_has_model(conn)
        report["had_model_column"] = has_model

        if has_model:
            # Already migrated — just sweep stray NULLs to 'unknown'.
            conn.execute(
                "UPDATE daily_spend SET model='unknown' WHERE model IS NULL"
            )
            conn.commit()
            report["rows"] = conn.execute(
                "SELECT COUNT(*) FROM daily_spend"
            ).fetchone()[0]
            return report

        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE daily_spend_new (
                date        TEXT    NOT NULL,
                tier        TEXT    NOT NULL,
                model       TEXT    NOT NULL DEFAULT 'unknown',
                spend_usd   REAL    DEFAULT 0,
                call_count  INTEGER DEFAULT 0,
                token_count INTEGER DEFAULT 0,
                PRIMARY KEY (date, tier, model)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO daily_spend_new
                (date, tier, model, spend_usd, call_count, token_count)
            SELECT date, tier, 'unknown',
                   SUM(spend_usd), SUM(call_count), SUM(token_count)
            FROM daily_spend
            GROUP BY date, tier
            """
        )
        conn.execute("DROP TABLE daily_spend")
        conn.execute("ALTER TABLE daily_spend_new RENAME TO daily_spend")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_spend_tier_date "
            "ON daily_spend(tier, date)"
        )
        conn.commit()
        report["migrated"] = True
        report["rows"] = conn.execute(
            "SELECT COUNT(*) FROM daily_spend"
        ).fetchone()[0]
        return report
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        _log.warning(
            "daily_spend model migration failed for %s", db_path, exc_info=True
        )
        return report
    finally:
        conn.close()


# ── Public data structures ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RateObservation:
    """A single measured-or-estimated $/M for a (provider, model) pair."""

    provider: str
    model: str | None              # None = provider-level aggregate
    rate_per_m: float              # $ per 1M tokens, >= MIN_EFFECTIVE_PRICE
    source: str                    # one of the SRC_* constants
    is_measured: bool              # True iff source in MEASURED_SOURCES
    confidence: float              # 0.0–1.0 (see _confidence)
    sample_tokens: int             # tokens behind this observation
    sample_cost_usd: float         # cost behind this observation (0 if amortized)
    ts: float                      # observation timestamp (unix)
    velocity: float = 0.0          # Kalman velocity (Δ$/M per cycle)

    @property
    def is_stale(self) -> bool:
        """True if no observation in the last 30 min."""
        return (time.time() - self.ts) > 1800.0


@dataclass(frozen=True, slots=True)
class RateSnapshot:
    """Immutable point-in-time view of all rates. Safe to share across threads."""

    ts: float
    by_provider_model: dict[tuple[str, str | None], RateObservation]
    by_provider: dict[str, RateObservation]   # token-weighted aggregate per provider
    any_cold_start: bool                      # True if any provider lacks a real obs
    refresh_count: int


# ── Helpers ───────────────────────────────────────────────────────────────────


def _floor_rate(rate: float) -> float:
    """Floor any rate at MIN_EFFECTIVE_PRICE; guard against NaN/inf."""
    if rate != rate or math.isinf(rate) or rate <= 0:
        return MIN_EFFECTIVE_PRICE
    return max(rate, MIN_EFFECTIVE_PRICE)


def _confidence(sample_tokens: int, is_measured: bool) -> float:
    """0.0–1.0. Measured sources ramp 0.3→1.0 at 10M tokens; estimated cap 0.7."""
    if not is_measured:
        return min(0.7, 0.3 + (sample_tokens / 10_000_000) * 0.4)
    return min(1.0, 0.3 + (sample_tokens / 10_000_000) * 0.7)


def _month_start_ts(now: float | None = None) -> float:
    """Unix timestamp of the first second of the current month (UTC)."""
    if now is None:
        now = time.time()
    gm = time.gmtime(now)
    return float(calendar.timegm((gm.tm_year, gm.tm_mon, 1, 0, 0, 0, 0, 0, 0)))


def _safe_float(val: Any) -> float | None:
    """Best-effort float coercion. Returns None on any failure."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f != f or math.isinf(f):
        return None
    return f


# ── Kill switch (design doc §6 — category 6) ─────────────────────────────────
# When REALTIME_PRICING_ENABLED is set to a falsy value, all collectors are
# skipped and refresh() returns the cold-start snapshot unchanged — reproducing
# the old static-rate behaviour.  Any other value (or unset) means enabled.
_FALSY = frozenset({"false", "0", "no", "off", ""})


def is_realtime_pricing_enabled() -> bool:
    """True unless ``REALTIME_PRICING_ENABLED`` is explicitly falsy."""
    return os.environ.get("REALTIME_PRICING_ENABLED", "true").strip().lower() not in _FALSY


# ── The singleton ────────────────────────────────────────────────────────────


class RealtimePricing:
    """Single source of truth for measured $/M. Thread-safe singleton.

    See module docstring for the full lifecycle and guarantees.
    """

    _instance: RealtimePricing | None = None
    _instance_lock = threading.Lock()

    DEFAULT_REFRESH_SECONDS = 300.0
    STALE_THRESHOLD_SECONDS = 1800.0
    MIN_SAMPLE_TOKENS = 1_000_000

    # ── Singleton lifecycle ──────────────────────────────────────────────

    def __init__(
        self,
        *,
        zai_db_path: str = "~/.hermes/bot/zai_usage.db",
        burn_db_path: str = "~/.hermes/bot/api_burn.db",
        providers_yaml: str | None = None,
        cold_start_rates: dict[str, float] | None = None,
    ) -> None:
        self._zai_db = os.path.expanduser(zai_db_path)
        self._burn_db = os.path.expanduser(burn_db_path)
        self._providers_yaml = providers_yaml
        self._cold_start = dict(cold_start_rates) if cold_start_rates else dict(DEFAULT_COLD_START_RATES)

        self._lock = threading.RLock()
        self._kalmans: dict[tuple[str, str | None], PriceKalman] = {}
        self._refresh_count = 0

        # Build the initial cold-start snapshot so snapshot() works before the
        # first refresh(). Every provider starts as cold_start_fallback.
        self._snapshot = self._build_cold_start_snapshot()

        # Ensure the persistence table exists (idempotent, best-effort).
        try:
            self._ensure_schema()
        except Exception:
            _log.debug("price_observations table init failed (non-fatal)", exc_info=True)

    @classmethod
    def get_instance(
        cls,
        *,
        zai_db_path: str = "~/.hermes/bot/zai_usage.db",
        burn_db_path: str = "~/.hermes/bot/api_burn.db",
        providers_yaml: str | None = None,
        cold_start_rates: dict[str, float] | None = None,
    ) -> RealtimePricing:
        """Get or create the singleton. Thread-safe."""
        # Fast path: already created
        if cls._instance is not None:
            return cls._instance
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(
                    zai_db_path=zai_db_path,
                    burn_db_path=burn_db_path,
                    providers_yaml=providers_yaml,
                    cold_start_rates=cold_start_rates,
                )
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Drop the singleton (for tests)."""
        with cls._instance_lock:
            cls._instance = None

    # ── Batch collector (cron-driven) ────────────────────────────────────

    def refresh(self) -> RateSnapshot:
        """Collect fresh observations from all sources, feed the internal
        Kalman grid per (provider, model), persist to price_observations,
        and return the new snapshot.

        Idempotent under concurrent calls (RLock-guarded). Designed to be
        invoked by a cron job, NOT from a request handler.
        """
        # Kill switch (§6): when REALTIME_PRICING_ENABLED=false, skip all
        # collectors and return the cold-start snapshot unchanged — reproduces
        # the old static-rate behaviour.
        if not is_realtime_pricing_enabled():
            return self._snapshot

        # Serialise: if another refresh is running, just return current snapshot.
        if not self._lock.acquire(blocking=False):
            return self._snapshot
        try:
            now = time.time()
            merged: dict[tuple[str, str | None], RateObservation] = {}

            # Run each collector independently — a failure in one must not
            # affect the others.
            for collector in (
                self._measure_zai_amortized,
                self._measure_ollama_billing,
                self._measure_ppq_ledger,
                self._measure_deepinfra_spend,
                self._measure_openrouter_spend,
            ):
                try:
                    obs_map = collector()
                    if obs_map:
                        merged.update(obs_map)
                except Exception:
                    _log.warning("collector %s failed", collector.__name__, exc_info=True)

            # Ensure every known provider has at least a cold-start observation.
            for prov in ("ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra"):
                if not any(k[0] == prov for k in merged):
                    merged[(prov, None)] = self._cold_start_obs(prov, now)

            # Feed the Kalman grid and attach velocity.
            for key, ob in list(merged.items()):
                kalman = self._kalmans.get(key)
                if kalman is None:
                    kalman = PriceKalman(
                        initial_rate=ob.rate_per_m,
                        measurement_noise=_KALMAN_NOISE.get(ob.source, 1e-3),
                    )
                    self._kalmans[key] = kalman
                else:
                    kalman.update(ob.rate_per_m)
                merged[key] = replace(ob, velocity=kalman.velocity)

            # Persist (best-effort, INSERT-only on a separate connection).
            try:
                self._write_observations(merged)
            except Exception:
                _log.debug("price_observations write failed (non-fatal)", exc_info=True)

            # Build the token-weighted provider-level aggregate.
            by_provider = self._aggregate_by_provider(merged, now)

            # any_cold_start: True if ANY of the 6 core providers is cold-start
            any_cold = any(
                by_provider.get(p) is not None
                and by_provider[p].source == SRC_COLD_START
                for p in ("ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra")
            )

            self._refresh_count += 1
            snapshot = RateSnapshot(
                ts=now,
                by_provider_model=dict(merged),
                by_provider=by_provider,
                any_cold_start=any_cold,
                refresh_count=self._refresh_count,
            )
            # Atomic reference swap — readers never see a partially-built snapshot.
            self._snapshot = snapshot
            return snapshot
        finally:
            self._lock.release()

    # ── Hot-path readers (per-request, lock-free) ────────────────────────

    def snapshot(self) -> RateSnapshot:
        """Return the latest cached snapshot. O(1), lock-free read of an
        immutable object. Never blocks, never raises."""
        return self._snapshot

    def get_rate(self, provider: str, model: str | None = None) -> RateObservation:
        """Convenience lookup. Falls back to cold-start if unknown.
        Never raises."""
        try:
            snap = self._snapshot
            ob = snap.by_provider_model.get((provider, model))
            if ob is not None:
                return ob
            ob = snap.by_provider.get(provider)
            if ob is not None:
                return ob
        except Exception:
            pass
        return self._cold_start_obs(provider, time.time())

    def get_provider_rates(self) -> dict[str, float]:
        """Provider-level {provider: rate_per_m} dict — the shape the existing
        routers expect for seeding PriceKalman (drop-in for _base_rates)."""
        snap = self._snapshot
        rates: dict[str, float] = {}
        for prov, ob in snap.by_provider.items():
            rates[prov] = ob.rate_per_m
        # Ensure all 6 core providers are present (cold-start fallback).
        for prov in ("ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra"):
            if prov not in rates:
                rates[prov] = self._cold_start.get(prov, MIN_EFFECTIVE_PRICE)
        return rates

    def feed_kalman(self, kalman: PriceKalman, provider: str) -> bool:
        """Push the latest measured observation for *provider* into the given
        PriceKalman (calls kalman.update(rate)). Returns False if no
        observation exists. Never raises."""
        try:
            ob = self.get_rate(provider)
            kalman.update(ob.rate_per_m)
            return True
        except Exception:
            _log.debug("feed_kalman failed for %s", provider, exc_info=True)
            return False

    # ── Per-source collectors (private) ──────────────────────────────────

    def _measure_zai_amortized(self) -> dict[tuple[str, str | None], RateObservation]:
        """z.ai flat-rate: annualized cost from trailing data (up to 365d).

        Uses ALL available data (trailing 365 days, or less if the DB is
        younger). This replaces the old month-to-date approach, which reset
        monthly and was noisy at month boundaries. The trailing window gives
        a smoother base rate that converges as more data accumulates.

        Query:  SELECT key_name, SUM(total_tokens), MIN(ts) FROM api_calls
                WHERE key_name IN ('ours','friend') AND ts >= trailing_cutoff
                GROUP BY key_name
        Annualized: annual_fee / (trailing_tokens * (365/trailing_days) / 1e6)

        Source: 'zai_amortized' (is_measured=False). friend gets fee=0 → floored
        at MIN_EFFECTIVE_PRICE.
        """
        now = time.time()
        trailing_cutoff = now - 365 * 86400  # 365-day trailing window
        result: dict[tuple[str, str | None], RateObservation] = {}

        try:
            conn = sqlite3.connect(self._zai_db, timeout=2)
            try:
                rows = conn.execute(
                    "SELECT key_name, COALESCE(SUM(total_tokens), 0), "
                    "MIN(ts) "
                    "FROM api_calls "
                    "WHERE key_name IN ('ours', 'friend') AND ts >= ? "
                    "GROUP BY key_name",
                    (trailing_cutoff,),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            _log.debug("zai amortized query failed", exc_info=True)
            return result

        for key_name, tokens, min_ts in rows:
            tokens = int(tokens or 0)
            monthly_fee = _ZAI_FEES.get(key_name, 0.0)
            annual_fee = monthly_fee * 12.0
            # Require minimum sample to avoid cold-start explosion.
            if tokens < self.MIN_SAMPLE_TOKENS:
                result[(key_name, None)] = self._cold_start_obs(key_name, now)
                continue
            # Compute trailing_days from the actual data span.
            # min_ts is the earliest record in the trailing window.
            trailing_days = max(1.0, (now - float(min_ts or now)) / 86400.0)
            # Annualize: extrapolate trailing tokens to a full 365-day year.
            annualized_tokens = tokens * (365.0 / trailing_days)
            rate = _floor_rate(annual_fee / (annualized_tokens / 1e6))
            result[(key_name, None)] = RateObservation(
                provider=key_name,
                model=None,
                rate_per_m=rate,
                source=SRC_ZAI_AMORTIZED,
                is_measured=False,
                confidence=_confidence(tokens, False),
                sample_tokens=tokens,
                sample_cost_usd=annual_fee,
                ts=now,
            )
        return result

    def _measure_ollama_billing(self) -> dict[tuple[str, str | None], RateObservation]:
        """Ollama Cloud: parse fetch_ollama_usage() for extra-usage billing.

        The ``/api/usage`` ``activity`` payload is::

            {"cost": "60.00",                       # total extra spend (4wk)
             "period": {"type": "last_4_weeks", ...},
             "models": [                            # ONLY extra-usage models
                {"name": "glm-5.2", "request_count": 954, "cost": "32.25"}, ...]}

        ``activity.cost`` is total extra-usage spend over ``period``; each entry
        in ``activity.models`` is an extra-usage-billed model with its extra cost
        and request count. The API does NOT report per-model token counts, so we
        estimate them proportionally — ``request_count * avg_tokens_per_call``
        (from the trailing 4-week api_calls volume) — the method documented in
        docs/extra-usage-real-data-analysis.md (validates glm-5.2 ≈ $0.46/M).

        Source: 'ollama_billing' (is_measured=True). Fallback chain when the API
        is unavailable or returns no models: amortize $100/mo over tokens →
        cold_start.
        """
        now = time.time()
        result: dict[tuple[str, str | None], RateObservation] = {}

        # Import here to avoid a hard dependency at module-import time (the
        # function may be unavailable in some environments).
        try:
            from src.ollama_extra_usage import fetch_ollama_usage
        except ImportError:
            from ollama_extra_usage import fetch_ollama_usage
        try:
            api_data = fetch_ollama_usage()
        except Exception:
            api_data = None

        if api_data is not None:
            activity = api_data.get("activity")
            activity_total_cost = (
                _safe_float(activity.get("cost")) if isinstance(activity, dict) else None
            )
            models_list = (
                activity.get("models") if isinstance(activity, dict) else None
            )

            if isinstance(models_list, list) and models_list:
                # Trailing 4-week ollama token volume (matches activity.period
                # "last_4_weeks"): basis for proportional per-model token
                # estimation and the provider-level blended rate.
                window_total_tokens = 0
                window_total_calls = 0
                try:
                    win_cutoff = now - 28 * 86400
                    conn = sqlite3.connect(self._zai_db, timeout=2)
                    try:
                        row = conn.execute(
                            "SELECT COALESCE(SUM(total_tokens), 0), COUNT(*) "
                            "FROM api_calls "
                            "WHERE key_name = 'ollama_cloud' AND ts >= ?",
                            (win_cutoff,),
                        ).fetchone()
                    finally:
                        conn.close()
                    window_total_tokens = int(row[0] or 0)
                    window_total_calls = int(row[1] or 0)
                except Exception:
                    _log.debug("ollama 4w token window query failed", exc_info=True)

                avg_tokens_per_call = (
                    window_total_tokens / window_total_calls
                    if window_total_calls > 0
                    else 0.0
                )

                for entry in models_list:
                    if not isinstance(entry, dict):
                        continue
                    model_name = entry.get("name")
                    if not model_name:
                        continue
                    cost = _safe_float(entry.get("cost"))
                    req_count = _safe_float(entry.get("request_count"))
                    if req_count is None:
                        req_count = _safe_float(entry.get("requests"))
                    # request_count > 0 signals extra-usage billing (the API
                    # only reports extra-usage-billed requests). Skip otherwise.
                    if cost is None or cost <= 0 or req_count is None or req_count <= 0:
                        continue
                    # Proportional token estimate for this model's extra usage.
                    est_tokens = int(req_count * avg_tokens_per_call)
                    if est_tokens > 0:
                        extra_rate = _floor_rate(cost / (est_tokens / 1e6))
                        result[("ollama_cloud", model_name)] = RateObservation(
                            provider="ollama_cloud",
                            model=model_name,
                            rate_per_m=extra_rate,
                            source=SRC_OLLAMA_BILLING,
                            is_measured=True,
                            confidence=_confidence(est_tokens, True),
                            sample_tokens=est_tokens,
                            sample_cost_usd=cost,
                            ts=now,
                        )

                # Provider-level blended rate: total extra spend over the 4-week
                # token volume (the MEASURED effective rate per the analysis
                # doc). Falls through to the amortization/cold-start fallbacks
                # below if there is no token volume to divide by.
                if activity_total_cost is not None and window_total_tokens > 0:
                    rate = _floor_rate(
                        activity_total_cost / (window_total_tokens / 1e6)
                    )
                    result[("ollama_cloud", None)] = RateObservation(
                        provider="ollama_cloud",
                        model=None,
                        rate_per_m=rate,
                        source=SRC_OLLAMA_BILLING,
                        is_measured=True,
                        confidence=_confidence(window_total_tokens, True),
                        sample_tokens=window_total_tokens,
                        sample_cost_usd=activity_total_cost,
                        ts=now,
                    )
                    return result

        # Fallback: amortize $100/mo over ollama tokens this month.
        try:
            month_start = _month_start_ts(now)
            conn = sqlite3.connect(self._zai_db, timeout=2)
            try:
                row = conn.execute(
                    "SELECT COALESCE(SUM(total_tokens), 0) FROM api_calls "
                    "WHERE key_name = 'ollama_cloud' AND ts >= ?",
                    (month_start,),
                ).fetchone()
            finally:
                conn.close()
            tokens = int(row[0]) if row else 0
            if tokens >= self.MIN_SAMPLE_TOKENS:
                rate = _floor_rate(_OLLAMA_MONTHLY_FEE / (tokens / 1e6))
                result[("ollama_cloud", None)] = RateObservation(
                    provider="ollama_cloud",
                    model=None,
                    rate_per_m=rate,
                    source=SRC_ZAI_AMORTIZED,
                    is_measured=False,
                    confidence=_confidence(tokens, False),
                    sample_tokens=tokens,
                    sample_cost_usd=_OLLAMA_MONTHLY_FEE,
                    ts=now,
                )
                return result
        except Exception:
            _log.debug("ollama amortized fallback failed", exc_info=True)

        # Final fallback: cold-start.
        result[("ollama_cloud", None)] = self._cold_start_obs("ollama_cloud", now)
        return result

    def _measure_ppq_ledger(self) -> dict[tuple[str, str | None], RateObservation]:
        """PPQ: SUM(cost_usd)/(SUM(total_tokens)/1e6) from ppq_queries (7d).
        Source: 'ppq_ledger' (is_measured=True). Empty → cold_start."""
        now = time.time()
        since = now - SPEND_WINDOW_S
        try:
            conn = sqlite3.connect(self._burn_db, timeout=2)
            try:
                rows = conn.execute(
                    "SELECT model, COALESCE(SUM(cost_usd), 0), COALESCE(SUM(total_tokens), 0) "
                    "FROM ppq_queries WHERE ts > ? GROUP BY model",
                    (since,),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            _log.debug("ppq ledger query failed", exc_info=True)
            return {}

        result: dict[tuple[str, str | None], RateObservation] = {}
        total_cost = 0.0
        total_tokens = 0
        for model, cost, tokens in rows:
            cost = float(cost or 0)
            tokens = int(tokens or 0)
            total_cost += cost
            total_tokens += tokens
            if tokens > 0 and cost > 0:
                rate = _floor_rate(cost / (tokens / 1e6))
                result[("ppq", model)] = RateObservation(
                    provider="ppq",
                    model=model,
                    rate_per_m=rate,
                    source=SRC_PQQ_LEDGER,
                    is_measured=True,
                    confidence=_confidence(tokens, True),
                    sample_tokens=tokens,
                    sample_cost_usd=cost,
                    ts=now,
                )

        if total_tokens > 0 and total_cost > 0:
            rate = _floor_rate(total_cost / (total_tokens / 1e6))
            result[("ppq", None)] = RateObservation(
                provider="ppq",
                model=None,
                rate_per_m=rate,
                source=SRC_PQQ_LEDGER,
                is_measured=True,
                confidence=_confidence(total_tokens, True),
                sample_tokens=total_tokens,
                sample_cost_usd=total_cost,
                ts=now,
            )
            return result

        # No PPQ data — fall back to cold-start seed.
        if not any(k[0] == "ppq" and k[1] is None for k in result):
            result[("ppq", None)] = self._cold_start_obs("ppq", now)
        return result

    def _measure_deepinfra_spend(self) -> dict[tuple[str, str | None], RateObservation]:
        """DeepInfra: SUM(spend_usd)/(SUM(token_count)/1e6) from daily_spend
        WHERE tier='deepinfra' (7d). Source: 'deepinfra_actual' (measured)."""
        return self._measure_spend_tier("deepinfra", SRC_DEEPINFRA_ACTUAL)

    def _measure_openrouter_spend(self) -> dict[tuple[str, str | None], RateObservation]:
        """OpenRouter: same daily_spend pattern WHERE tier='openrouter'.

        With no measured data, fall back to **per-model** published list prices
        from providers.yaml (PM-T5; is_measured=False). If providers.yaml has no
        per-model entry for openrouter, fall back to the provider-level
        published price.
        """
        result = self._measure_spend_tier("openrouter", SRC_OPENROUTER_ACTUAL)
        if result:
            return result
        now = time.time()
        published = published_model_rates("openrouter", self._providers_yaml)
        for model, rate in published.items():
            result[("openrouter", model)] = RateObservation(
                provider="openrouter",
                model=model,
                rate_per_m=_floor_rate(rate),
                source=SRC_PUBLISHED,
                is_measured=False,
                confidence=_confidence(0, False),
                sample_tokens=0,
                sample_cost_usd=0.0,
                ts=now,
            )
        if result:
            return result
        # No per-model config either — provider-level published fallback.
        result[("openrouter", None)] = RateObservation(
            provider="openrouter",
            model=None,
            rate_per_m=_floor_rate(_PUBLISHED_PRICES.get("openrouter", 0.135)),
            source=SRC_PUBLISHED,
            is_measured=False,
            confidence=_confidence(0, False),
            sample_tokens=0,
            sample_cost_usd=0.0,
            ts=now,
        )
        return result

    def _measure_spend_tier(
        self, tier: str, source: str
    ) -> dict[tuple[str, str | None], RateObservation]:
        """Shared spend-table query for daily_spend-backed providers.

        PM-T5 — per-model: when daily_spend carries a ``model`` column (after
        :func:`migrate_daily_spend_add_model`), emits one measured
        :class:`RateObservation` per model (``GROUP BY model``) plus a
        token-weighted provider-level ``(tier, None)`` aggregate — mirroring
        :meth:`_measure_ppq_ledger`. Before the schema is migrated the query
        stays provider-level (the original behaviour), so cold-start / measured
        semantics for every provider are unchanged.
        """
        now = time.time()
        cutoff_date = time.strftime("%Y-%m-%d", time.gmtime(now - SPEND_WINDOW_S))
        result: dict[tuple[str, str | None], RateObservation] = {}
        rows: list[tuple] | None = None
        single: tuple | None = None
        try:
            conn = sqlite3.connect(self._zai_db, timeout=2)
            try:
                if _spend_table_has_model(conn):
                    rows = conn.execute(
                        "SELECT model, COALESCE(SUM(spend_usd), 0), "
                        "COALESCE(SUM(token_count), 0) "
                        "FROM daily_spend WHERE tier = ? AND date >= ? "
                        "GROUP BY model",
                        (tier, cutoff_date),
                    ).fetchall()
                else:
                    single = conn.execute(
                        "SELECT COALESCE(SUM(spend_usd), 0), "
                        "COALESCE(SUM(token_count), 0) "
                        "FROM daily_spend WHERE tier = ? AND date >= ?",
                        (tier, cutoff_date),
                    ).fetchone()
            finally:
                conn.close()
        except Exception:
            _log.debug("%s spend query failed", tier, exc_info=True)
            return {}

        # ── Per-model path (migrated schema) ──────────────────────────────
        if rows is not None:
            total_cost = 0.0
            total_tokens = 0
            for model, cost, tokens in rows:
                cost = float(cost or 0)
                tokens = int(tokens or 0)
                if tokens <= 0 or cost <= 0:
                    continue
                total_cost += cost
                total_tokens += tokens
                model = model or "unknown"
                result[(tier, model)] = RateObservation(
                    provider=tier,
                    model=model,
                    rate_per_m=_floor_rate(cost / (tokens / 1e6)),
                    source=source,
                    is_measured=True,
                    confidence=_confidence(tokens, True),
                    sample_tokens=tokens,
                    sample_cost_usd=cost,
                    ts=now,
                )
            if total_tokens > 0 and total_cost > 0:
                result[(tier, None)] = RateObservation(
                    provider=tier,
                    model=None,
                    rate_per_m=_floor_rate(total_cost / (total_tokens / 1e6)),
                    source=source,
                    is_measured=True,
                    confidence=_confidence(total_tokens, True),
                    sample_tokens=total_tokens,
                    sample_cost_usd=total_cost,
                    ts=now,
                )
            return result

        # ── Provider-level path (pre-migration schema) ────────────────────
        assert single is not None  # rows is None ⇒ single was queried
        spend = float(single[0] or 0)
        tokens = int(single[1] or 0)
        if tokens > 0 and spend > 0:
            result[(tier, None)] = RateObservation(
                provider=tier,
                model=None,
                rate_per_m=_floor_rate(spend / (tokens / 1e6)),
                source=source,
                is_measured=True,
                confidence=_confidence(tokens, True),
                sample_tokens=tokens,
                sample_cost_usd=spend,
                ts=now,
            )
        return result

    # ── Persistence ──────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        """Create price_observations table if it doesn't exist (idempotent)."""
        conn = sqlite3.connect(self._zai_db, timeout=2)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_observations (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts              REAL    NOT NULL,
                    provider        TEXT    NOT NULL,
                    model           TEXT,
                    rate_per_m      REAL    NOT NULL,
                    source          TEXT    NOT NULL,
                    is_measured     INTEGER NOT NULL,
                    confidence      REAL    DEFAULT 1.0,
                    sample_tokens   INTEGER,
                    sample_cost_usd REAL,
                    velocity        REAL    DEFAULT 0.0,
                    note            TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_price_obs_provider_ts "
                "ON price_observations(provider, model, ts)"
            )
            conn.commit()
        finally:
            conn.close()

    def _write_observations(
        self, obs: dict[tuple[str, str | None], RateObservation]
    ) -> None:
        """INSERT each observation into price_observations (WAL, separate
        connection, INSERT-only — no locks against the hot-path reader)."""
        conn = sqlite3.connect(self._zai_db, timeout=2)
        try:
            conn.executemany(
                """
                INSERT INTO price_observations
                    (ts, provider, model, rate_per_m, source, is_measured,
                     confidence, sample_tokens, sample_cost_usd, velocity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        ob.ts,
                        ob.provider,
                        ob.model,
                        ob.rate_per_m,
                        ob.source,
                        1 if ob.is_measured else 0,
                        ob.confidence,
                        ob.sample_tokens,
                        ob.sample_cost_usd,
                        ob.velocity,
                    )
                    for ob in obs.values()
                ],
            )
            conn.commit()
        finally:
            conn.close()

    # ── Internal helpers ─────────────────────────────────────────────────

    def _cold_start_obs(self, provider: str, now: float) -> RateObservation:
        """Build a cold-start fallback observation for a provider."""
        rate = self._cold_start.get(provider, MIN_EFFECTIVE_PRICE)
        return RateObservation(
            provider=provider,
            model=None,
            rate_per_m=_floor_rate(rate),
            source=SRC_COLD_START,
            is_measured=False,
            confidence=0.0,
            sample_tokens=0,
            sample_cost_usd=0.0,
            ts=now,
        )

    def _build_cold_start_snapshot(self) -> RateSnapshot:
        """Initial snapshot before the first refresh — all providers cold-start."""
        now = time.time()
        by_pm: dict[tuple[str, str | None], RateObservation] = {}
        by_prov: dict[str, RateObservation] = {}
        for prov in ("ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra"):
            ob = self._cold_start_obs(prov, now)
            by_pm[(prov, None)] = ob
            by_prov[prov] = ob
        return RateSnapshot(
            ts=now,
            by_provider_model=by_pm,
            by_provider=by_prov,
            any_cold_start=True,
            refresh_count=0,
        )

    def _aggregate_by_provider(
        self,
        merged: dict[tuple[str, str | None], RateObservation],
        now: float,
    ) -> dict[str, RateObservation]:
        """Token-weighted aggregate per provider. Prefers the provider-level
        (model=None) observation; if only model-level exists, weights by tokens."""
        by_prov: dict[str, RateObservation] = {}
        for (prov, model), ob in merged.items():
            if model is None:
                # Provider-level observation takes precedence.
                by_prov[prov] = ob
        # For providers without a model=None entry, compute a token-weighted
        # aggregate from their model-level entries.
        for (prov, model), ob in merged.items():
            if model is not None and prov not in by_prov:
                existing = by_prov.get(prov)
                if existing is None or existing.sample_tokens == 0:
                    by_prov[prov] = ob
                else:
                    # Weight by sample tokens for the aggregate rate.
                    t_total = existing.sample_tokens + ob.sample_tokens
                    if t_total > 0:
                        w_rate = (
                            existing.rate_per_m * existing.sample_tokens
                            + ob.rate_per_m * ob.sample_tokens
                        ) / t_total
                        by_prov[prov] = replace(
                            existing,
                            rate_per_m=_floor_rate(w_rate),
                            sample_tokens=t_total,
                            sample_cost_usd=existing.sample_cost_usd + ob.sample_cost_usd,
                        )
        return by_prov
