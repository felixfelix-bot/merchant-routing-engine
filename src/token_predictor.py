"""token_predictor.py — per-model p50/p90 token burn predictor (CG-3).

Plan: ``docs/PLAN-cost-gate-reform-v2-2026-08-21.md`` §6 CG-3 (supersedes v1
CG-2 core, unchanged).  Predicts how many tokens a task will burn, per
model, from the ``api_calls`` history in the production usage DB:

- **Source:** ``~/.hermes/bot/zai_usage.db`` (production proxy DB) —
  READ-ONLY access, via ``mode=ro`` URI connections; this module NEVER
  writes to the production DB.  Only ``status_code=200`` rows inside a
  recent window (default trailing 30 days) count.
- **Stats store:** a repo-local SQLite DB (default ``data/token_stats.db``,
  gitignored) written by the seed script — seed-then-replace semantics so a
  re-seed fully swaps the fleet picture (drift-friendly, no stale models).
- **Task dimension:** scaled by ``TASK_PROFILES.budget_mult`` from
  :mod:`src.dispatch_gate` (imported, never copied) until the proxy's
  ``task_type`` column matures (CG-5).  A3 (v1 review): the historical join
  key ``(model, task_type)`` cannot be seeded today — the column is all
  NULL; per-model + budget_mult is the approved interim shape.
- **Confidence:** bucketed by sample count.  ``n < 30 → low`` (plan-pinned);
  ``30 ≤ n < 200 → medium``; ``n ≥ 200 → high``.  Stats older than 7 days
  are flagged ``stale`` and forced to ``low`` confidence.
- **Cold model:** an unknown model still gets an answer — the worst
  observed per-model p90 across the fleet × :data:`COLD_MODEL_PENALTY`
  (conservative), or :data:`DEFAULT_COLD_TOKENS` when no stats exist at
  all.  The predictor ALWAYS returns a number with a confidence flag; the
  cost gate fails closed on *price* (:mod:`src.cost_gate`), never on token
  history (plan §5/v1 §3: "predictor must always return a number").
- **NO Kalman inputs.**  ``kalman_health.py --short`` (the
  ``kalman-convergence-check`` skill's authoritative check) was verified
  **✗ unhealthy** on 2026-08-22 — re-verify with the script, never assume.
  Until it reports ``healthy``/``improving`` this module consumes no
  Kalman state at all; :data:`KALMAN_INPUTS_ENABLED` is the single switch
  to flip (with tests pinning it off) when convergence goes green.

Split (same discipline as :mod:`src.cost_gate`): :func:`predict_tokens`
and :func:`confidence_bucket` are pure; :func:`compute_model_stats`,
:func:`seed_token_stats` and :func:`load_token_stats` are the I/O-adjacent
helpers used by ``scripts/seed_token_stats.py`` and the CG-7 CLI.

Usage::

    from src.token_predictor import (
        load_token_stats, predict_tokens, gate_estimated_tokens)

    stats = load_token_stats("data/token_stats.db")   # None-safe
    pred = predict_tokens(stats, model="glm-5.2", task_type="research")
    # pred["predicted_p90_tokens"]  → task-scaled p90 (raw × budget_mult)

    # Feeding the CG-1 gate — pass the RAW per-model p90; the gate applies
    # budget_mult itself, so scaling must happen exactly once:
    verdict = evaluate_cost_gate(..., estimated_tokens=gate_estimated_tokens(
        stats, model="glm-5.2", task_type="research"))
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Mapping
from urllib.request import pathname2url

from src.cost_gate import percentile
from src.dispatch_gate import TASK_PROFILES, normalize_task_type, resolve_task_profile

__all__ = [
    "KALMAN_INPUTS_ENABLED",
    "LOW_CONFIDENCE_MAX_N",
    "HIGH_CONFIDENCE_MIN_N",
    "COLD_MODEL_PENALTY",
    "DEFAULT_COLD_TOKENS",
    "DEFAULT_WINDOW_DAYS",
    "STALE_STATS_MAX_AGE_DAYS",
    "DEFAULT_SOURCE_DB",
    "DEFAULT_STATS_DB",
    # re-exported from dispatch_gate (composition, no duplication)
    "TASK_PROFILES",
    # functions
    "confidence_bucket",
    "compute_model_stats",
    "seed_token_stats",
    "load_token_stats",
    "predict_tokens",
    "gate_estimated_tokens",
]


# ── constants ────────────────────────────────────────────────────────────────

#: Kalman convergence gate (plan CG-3 prerequisite).  Verified with
#: ``python3 ~/.hermes/bot/kalman_health.py --short`` — run it, don't assume.
#: History: 2026-08-21 ✗ unhealthy (plan), 2026-08-22 ✗ unhealthy (re-verified
#: at implementation time).  Flip to True ONLY when the verdict is
#: healthy/improving, together with the accuracy-input wiring + tests.
KALMAN_INPUTS_ENABLED: bool = False

#: Plan-pinned confidence threshold: fewer samples than this → "low".
LOW_CONFIDENCE_MAX_N: int = 30

#: At or above this many samples the percentile estimate is "high"
#: (a p90 estimated from ≥200 calls is stable to within a few percent on
#: this fleet's per-model volumes).
HIGH_CONFIDENCE_MIN_N: int = 200

#: Cold-model penalty applied to the conservative fleet default (v1 plan:
#: "conservative default × penalty").
COLD_MODEL_PENALTY: float = 1.5

#: Absolute fallback when NO stats exist at all (stats DB missing/empty).
#: Calibrated 2026-08-22 against the trailing-30d fleet: worst per-model
#: p90 = 108 752 (glm-5.2), fleet pooled p90 = 102 303 → 200 000 sits above
#: worst-p90 × 1.5 with headroom for a heavy single coding session.
DEFAULT_COLD_TOKENS: int = 200_000

#: Trailing window the seed aggregates over, days.
DEFAULT_WINDOW_DAYS: int = 30

#: Stats older than this are stale → confidence forced to "low".
STALE_STATS_MAX_AGE_DAYS: float = 7.0

#: Production usage DB — READ-ONLY source (never written by this module).
DEFAULT_SOURCE_DB: str = os.path.expanduser("~/.hermes/bot/zai_usage.db")

#: Repo-local stats store (gitignored: *.db).  Written by the seed script.
DEFAULT_STATS_DB: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "token_stats.db"
)


# ── I/O helpers (used by scripts/seed_token_stats.py and the CG-7 CLI) ───────


def _ro_connect(db_path: str) -> sqlite3.Connection:
    """Open ``db_path`` STRICTLY read-only; a missing file is an error.

    ``mode=ro`` refuses to create the file — this is the guard that makes
    the production-DB access pattern safe by construction.
    """
    uri = "file:" + pathname2url(os.path.abspath(db_path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    return conn


def compute_model_stats(
    source_db: str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Aggregate per-model token stats from an ``api_calls``-shaped DB.

    Reads ONLY ``status_code=200`` rows with ``total_tokens > 0`` whose
    ``ts`` falls inside the trailing ``window_days``.  The source is opened
    read-only (:func:`_ro_connect`).

    Returns ``{"models": {model: {n, p50, p90, mean, max, first_ts,
    last_ts}}, "meta": {seeded_at, window_days, source, n_models,
    total_rows}}``.  Percentiles use the same linear-interpolation
    definition as :func:`src.cost_gate.percentile` (imported, not
    duplicated).
    """
    import time as _time

    ts_now = float(_time.time()) if now_ts is None else float(now_ts)
    cutoff = ts_now - float(window_days) * 86400.0

    conn = _ro_connect(source_db)
    try:
        cur = conn.execute(
            "SELECT model, total_tokens, ts FROM api_calls "
            "WHERE status_code = 200 AND total_tokens > 0 AND ts >= ?",
            (cutoff,),
        )
        buckets: dict[str, list[float]] = {}
        spans: dict[str, list[float]] = {}
        total_rows = 0
        for model, tokens, ts in cur:
            if model is None:
                continue
            total_rows += 1
            buckets.setdefault(model, []).append(float(tokens))
            span = spans.setdefault(model, [float(ts), float(ts)])
            span[0] = min(span[0], float(ts))
            span[1] = max(span[1], float(ts))
    finally:
        conn.close()

    models: dict[str, dict[str, Any]] = {}
    for model, vals in buckets.items():
        models[model] = {
            "n": len(vals),
            "p50": percentile(vals, 50.0),
            "p90": percentile(vals, 90.0),
            "mean": sum(vals) / len(vals),
            "max": max(vals),
            "first_ts": spans[model][0],
            "last_ts": spans[model][1],
        }
    return {
        "models": models,
        "meta": {
            "seeded_at": ts_now,
            "window_days": int(window_days),
            "source": str(source_db),
            "n_models": len(models),
            "total_rows": total_rows,
        },
    }


def seed_token_stats(
    source_db: str,
    stats_db: str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Seed the stats DB from the production source — seed-then-replace.

    Drift-friendly by design (Felix's seed-then-replace): a re-seed fully
    REPLACES the previous contents in one transaction — models that no
    longer appear in the window disappear, counts are never merged.  If the
    source yields no usable rows the old stats are left untouched and
    :class:`ValueError` is raised (fail-closed against wiping stats on a
    broken read).
    """
    stats = compute_model_stats(
        source_db, window_days=window_days, now_ts=now_ts
    )
    if not stats["models"]:
        raise ValueError(
            f"no status_code=200 token rows in the last {window_days}d "
            f"of {source_db} — refusing to wipe existing stats"
        )

    os.makedirs(os.path.dirname(os.path.abspath(stats_db)) or ".", exist_ok=True)
    conn = sqlite3.connect(stats_db)
    try:
        with conn:  # single atomic transaction
            conn.execute(
                "CREATE TABLE IF NOT EXISTS token_stats ("
                " model TEXT PRIMARY KEY, n INTEGER NOT NULL, p50 REAL NOT NULL,"
                " p90 REAL NOT NULL, mean REAL NOT NULL, max REAL NOT NULL,"
                " first_ts REAL, last_ts REAL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS token_stats_meta ("
                " key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute("DELETE FROM token_stats")
            conn.execute("DELETE FROM token_stats_meta")
            conn.executemany(
                "INSERT INTO token_stats"
                " (model, n, p50, p90, mean, max, first_ts, last_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        m,
                        d["n"],
                        d["p50"],
                        d["p90"],
                        d["mean"],
                        d["max"],
                        d["first_ts"],
                        d["last_ts"],
                    )
                    for m, d in sorted(stats["models"].items())
                ],
            )
            meta = stats["meta"]
            conn.executemany(
                "INSERT INTO token_stats_meta (key, value) VALUES (?, ?)",
                [
                    ("seeded_at", repr(meta["seeded_at"])),
                    ("window_days", str(meta["window_days"])),
                    ("source", str(meta["source"])),
                    ("n_models", str(meta["n_models"])),
                ],
            )
    finally:
        conn.close()
    return stats


def load_token_stats(stats_db: str) -> dict[str, Any] | None:
    """Load the seeded stats DB; ``None`` when missing (cold path handles it).

    Never raises for a missing file — the caller feeds ``None`` straight
    into :func:`predict_tokens`, which still answers with the conservative
    default.
    """
    if not os.path.exists(stats_db):
        return None
    conn = _ro_connect(stats_db)
    try:
        models: dict[str, dict[str, Any]] = {}
        for row in conn.execute(
            "SELECT model, n, p50, p90, mean, max, first_ts, last_ts"
            " FROM token_stats"
        ):
            models[row[0]] = {
                "n": int(row[1]),
                "p50": float(row[2]),
                "p90": float(row[3]),
                "mean": float(row[4]),
                "max": float(row[5]),
                "first_ts": float(row[6]) if row[6] is not None else None,
                "last_ts": float(row[7]) if row[7] is not None else None,
            }
        meta: dict[str, Any] = {}
        for key, value in conn.execute(
            "SELECT key, value FROM token_stats_meta"
        ):
            if key == "seeded_at":
                meta[key] = float(value)
            elif key == "window_days":
                meta[key] = int(value)
            else:
                meta[key] = value
    finally:
        conn.close()
    if not models and not meta:
        return None
    return {"models": models, "meta": meta}


# ── pure prediction ─────────────────────────────────────────────────────────


def confidence_bucket(n: int) -> str:
    """Sample-count confidence bucket — plan pins ``n < 30 → low``.

    >>> confidence_bucket(29)
    'low'
    >>> confidence_bucket(30)
    'medium'
    >>> confidence_bucket(200)
    'high'
    """
    n = int(n)
    if n < LOW_CONFIDENCE_MAX_N:
        return "low"
    if n < HIGH_CONFIDENCE_MIN_N:
        return "medium"
    return "high"


def _resolve_raw(stats: Mapping[str, Any] | None, model: str) -> dict[str, Any]:
    """Resolve the RAW (task-unscaled) per-model stats for ``model``.

    Exact model match first; cold models get the conservative fleet default
    (worst observed per-model p90 × :data:`COLD_MODEL_PENALTY`), or
    :data:`DEFAULT_COLD_TOKENS` when there are no stats at all.  Always
    returns a numeric answer.
    """
    models: Mapping[str, Mapping[str, Any]] = (stats or {}).get("models") or {}
    entry = models.get(model)
    if entry is not None:
        return {
            "raw_p50": float(entry["p50"]),
            "raw_p90": float(entry["p90"]),
            "n": int(entry["n"]),
            "source": "model_stats",
            "cold_model": False,
        }
    # cold path — conservative fleet default
    base = max((float(e["p90"]) for e in models.values()), default=None)
    if base is None:
        base = float(DEFAULT_COLD_TOKENS) / COLD_MODEL_PENALTY
    default = base * COLD_MODEL_PENALTY
    return {
        "raw_p50": default,
        "raw_p90": default,
        "n": 0,
        "source": "cold_default",
        "cold_model": True,
    }


def predict_tokens(
    stats: Mapping[str, Any] | None,
    *,
    model: str | None = None,
    task_type: str | None = None,
    now_ts: float = 0.0,
) -> dict[str, Any]:
    """Predict p50/p90 token burn for (model, task_type) — pure, always answers.

    ``task_type`` resolves via dispatch_gate's ``TASK_PROFILES`` (unknown →
    coding); ``model=None`` uses the profile's model.  Task scaling:
    ``predicted = raw per-model percentile × budget_mult``.  The RAW
    percentiles are also returned (``raw_p50_tokens`` / ``raw_p90_tokens``)
    — feed those, not the scaled values, to
    ``evaluate_cost_gate(estimated_tokens=...)`` (see
    :func:`gate_estimated_tokens`).

    ``stats`` is a :func:`load_token_stats` dict or ``None`` (cold path).
    Stale stats (> :data:`STALE_STATS_MAX_AGE_DAYS`) are flagged and forced
    to ``low`` confidence.  The prediction carries
    ``kalman_inputs=False`` until :data:`KALMAN_INPUTS_ENABLED` flips.
    """
    profile = resolve_task_profile(task_type)
    canonical_type = normalize_task_type(task_type)
    resolved_model = model if model is not None else profile["model"]
    mult = float(profile["budget_mult"])

    raw = _resolve_raw(stats, resolved_model)

    meta: Mapping[str, Any] = (stats or {}).get("meta") or {}
    seeded_at = meta.get("seeded_at")
    stale = (
        seeded_at is not None
        and (float(now_ts) - float(seeded_at)) > STALE_STATS_MAX_AGE_DAYS * 86400.0
    )

    confidence = confidence_bucket(raw["n"])
    if raw["cold_model"]:
        confidence = "low"
    if stale:
        confidence = "low"

    return {
        "model": resolved_model,
        "task_type": canonical_type,
        "budget_mult": mult,
        "raw_p50_tokens": raw["raw_p50"],
        "raw_p90_tokens": raw["raw_p90"],
        "predicted_p50_tokens": int(round(raw["raw_p50"] * mult)),
        "predicted_p90_tokens": int(round(raw["raw_p90"] * mult)),
        "confidence": confidence,
        "n_samples": raw["n"],
        "source": raw["source"],
        "cold_model": raw["cold_model"],
        "stale": bool(stale),
        "seeded_at": seeded_at,
        "window_days": meta.get("window_days"),
        "kalman_inputs": KALMAN_INPUTS_ENABLED,
    }


def gate_estimated_tokens(
    stats: Mapping[str, Any] | None,
    *,
    model: str | None = None,
    task_type: str | None = None,
    now_ts: float = 0.0,
) -> int:
    """RAW per-model p90 as the ``estimated_tokens`` feed for the CG-1 gate.

    ``evaluate_cost_gate`` applies the profile ``budget_mult`` to
    ``estimated_tokens`` itself, so the predictor must hand over the
    UNscaled value — this helper exists so the wiring cannot double-scale
    by accident.  Always returns a positive int.
    """
    pred = predict_tokens(
        stats, model=model, task_type=task_type, now_ts=now_ts
    )
    return max(1, int(round(pred["raw_p90_tokens"])))
