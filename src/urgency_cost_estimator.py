#!/usr/bin/env python3
"""CG-12: Kalman-based cost estimator for urgency options.

Composes three existing signals into a per-urgency dollar cost + confidence:

1. **Kalman burn trajectory** (``kalman_samples`` table) → predicts when
   free quota resets (``exhausts_in_hours``). This is the *timing* component.
2. **Price observations** (``price_observations`` table) → current $/M per
   provider. This is the *price* component.
3. **Token distribution** (``api_calls`` table) → per-model mean + std
   token count per call. This is the *volume* component.

The estimator composes these into a per-urgency cost:

    cost = DIRECT (token cost) + BLEED (waiting cost) + OPPORTUNITY (soft)

**DIRECT**: if free quota is available → $0. If quota exhausted →
``tokens × paid_price_per_M / 1M``.

**BLEED**: if NOT dispatching now and the task is actively bleeding (paid
spend accumulating while a fix waits) → ``bleed_rate_per_hour ×
time_until_free_window``.

**OPPORTUNITY**: informational only — surfaced as a note, not a dollar
amount. The Kalman timing uncertainty determines confidence.

Usage::

    from src.urgency_cost_estimator import (
        fetch_current_state, estimate_cost, format_all_urgencies)

    state = fetch_current_state()
    print(format_all_urgencies(state.get("task_tokens", 56_000), state))

The pure core (:func:`estimate_cost`) takes a pre-fetched ``state`` dict
and is testable without a live DB. The I/O wrapper (:func:`fetch_current_state`)
reads ``zai_usage.db`` (read-only) and ``zai_state.json``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from urllib.request import pathname2url

__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_STATE_PATH",
    "DEFAULT_PAID_PRICE_PER_M",
    "DEFAULT_FREE_PRICE_PER_M",
    "DEFAULT_TOKEN_STD_FRACTION",
    "DEFAULT_TASK_TOKENS",
    "URGENCIES",
    "fetch_current_state",
    "estimate_cost",
    "format_all_urgencies",
]

# ── constants ────────────────────────────────────────────────────────────────

#: Production usage DB — READ-ONLY (never written by this module).
DEFAULT_DB_PATH: str = os.path.expanduser("~/.hermes/bot/zai_usage.db")

#: zai_state.json — live quota state from the proxy.
DEFAULT_STATE_PATH: str = os.path.expanduser("~/.hermes/bot/zai_state.json")

#: OpenRouter measured rate 2026-08-22 (price_observations). Fallback when
#: no measured price is available.
DEFAULT_PAID_PRICE_PER_M: float = 0.47

#: zai flat sub amortized rate (price_observations provider=friend).
DEFAULT_FREE_PRICE_PER_M: float = 0.001

#: Default relative error when no token std is available (30%).
DEFAULT_TOKEN_STD_FRACTION: float = 0.30

#: Fleet-mean tokens per call (glm-5.2, 2026-08-22, 29K samples).
DEFAULT_TASK_TOKENS: int = 56_000

#: The four urgency levels, in display order.
URGENCIES = ("now", "soon", "defer", "batch")


# ── I/O helpers ──────────────────────────────────────────────────────────────


def _ro(db_path: str) -> sqlite3.Connection:
    """Open ``db_path`` strictly read-only (``mode=ro`` URI)."""
    uri = "file:" + pathname2url(os.path.abspath(db_path)) + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def fetch_current_state(
    db_path: str = DEFAULT_DB_PATH,
    state_path: str = DEFAULT_STATE_PATH,
) -> dict:
    """Read ``zai_usage.db`` (ro) + ``zai_state.json`` → ``current_state`` dict.

    Every signal degrades gracefully: a missing DB, a missing table, or a
    parse error on ``zai_state.json`` results in a ``None`` value for that
    key — never an exception. The pure :func:`estimate_cost` handles ``None``
    inputs with conservative fallbacks.

    Returns a dict with these keys (all optional — missing → fallback in
    :func:`estimate_cost`):

    - ``free_quota_available`` (bool): quota is live and has headroom.
    - ``paid_price_per_m`` (float): cheapest measured paid rate.
    - ``bleed_rate_per_hour`` (float): 24h paid spend / 24.
    - ``quota_resets_in_hours`` (float | None): when free quota opens.
    - ``kalman_uncertainty`` (float): Kalman uncertainty in tokens.
    - ``task_tokens`` (int): per-model mean tokens (glm-5.2 default).
    - ``token_std`` (float): per-model std (sqrt of variance).
    """
    s: dict = {}

    # 1. quota state from zai_state.json
    try:
        with open(state_path) as f:
            st = json.load(f)
        pct = float(st.get("friend_token_pct", 0) or 0)
        s["free_quota_available"] = pct > 0
        reset_ms = st.get("friend_reset_ms")
        if reset_ms and isinstance(reset_ms, (int, float)):
            hours = max(0.0, (reset_ms / 1000.0 - time.time()) / 3600.0)
            if hours > 0:
                s["quota_resets_in_hours"] = hours
    except Exception:
        s["free_quota_available"] = False

    # 2. latest paid price from price_observations (cheapest non-free)
    try:
        c = _ro(db_path)
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT rate_per_m FROM price_observations "
            "WHERE provider NOT IN ('friend', 'ours') "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row and row["rate_per_m"]:
            s["paid_price_per_m"] = float(row["rate_per_m"])
        c.close()
    except Exception:
        pass
    s.setdefault("paid_price_per_m", DEFAULT_PAID_PRICE_PER_M)

    # 3. latest Kalman sample (burn trajectory + exhaustion timing)
    try:
        c = _ro(db_path)
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT burn_rate_tph, exhausts_in_hours, uncertainty "
            "FROM kalman_samples ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row:
            eh = row["exhausts_in_hours"]
            if eh is not None and float(eh) > 0:
                s["quota_resets_in_hours"] = float(eh)
            s["kalman_uncertainty"] = float(row["uncertainty"] or 0)
        c.close()
    except Exception:
        pass
    s.setdefault("quota_resets_in_hours", None)

    # 4. bleed rate: 24h paid spend / 24
    try:
        c = _ro(db_path)
        row = c.execute(
            "SELECT SUM(cost_usd) FROM api_calls "
            "WHERE cost_usd > 0 AND ts > ?",
            (time.time() - 86400,),
        ).fetchone()
        total = float(row[0] or 0)
        s["bleed_rate_per_hour"] = total / 24.0
        c.close()
    except Exception:
        s["bleed_rate_per_hour"] = 0.0

    # 5. per-model token stats (glm-5.2 fleet default, 7d window)
    try:
        c = _ro(db_path)
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT AVG(total_tokens) as mean, "
            "AVG(total_tokens * total_tokens) - AVG(total_tokens) * AVG(total_tokens) as var "
            "FROM api_calls WHERE model = 'glm-5.2' AND total_tokens > 0 "
            "AND ts > ?",
            (time.time() - 86400 * 7,),
        ).fetchone()
        if row and row["mean"]:
            s["task_tokens"] = int(row["mean"])
            s["token_std"] = float(row["var"] or 0) ** 0.5
        c.close()
    except Exception:
        pass

    return s


# ── pure core ────────────────────────────────────────────────────────────────


def _confidence(state: dict, band: float, cost: float) -> str:
    """Classify confidence from band width relative to cost."""
    if cost == 0 and band == 0:
        return "high"
    if state.get("free_quota_available"):
        return "high"
    if band > max(cost * 0.5, 0.01):
        return "low"
    if band > max(cost * 0.2, 0.005):
        return "medium"
    return "high"


def estimate_cost(urgency: str, task_tokens: int, state: dict) -> dict:
    """Pure: compute direct + bleed + confidence for one urgency level.

    No I/O — takes a pre-fetched ``state`` dict (from
    :func:`fetch_current_state`). Always returns a complete dict with
    a numeric ``cost_usd``, never raises.

    Parameters
    ----------
    urgency : str
        One of ``"now"``, ``"soon"``, ``"defer"``, ``"batch"``.
    task_tokens : int
        Estimated tokens for the task (from ``token_predictor`` or
        ``state["task_tokens"]`` or :data:`DEFAULT_TASK_TOKENS`).
    state : dict
        Pre-fetched current state (see :func:`fetch_current_state`).

    Returns
    -------
    dict
        ``{"urgency", "cost_usd", "confidence_interval", "confidence",
        "breakdown": {"direct", "bleed"}, "explanation", "bleed_note"}``
    """
    free = state.get("free_quota_available", False)
    paid = state.get("paid_price_per_m", DEFAULT_PAID_PRICE_PER_M)
    bleed_rate = state.get("bleed_rate_per_hour", 0.0)
    resets_in = state.get("quota_resets_in_hours")

    # ── DIRECT ──────────────────────────────────────────────────────────
    if free:
        direct, reason = 0.0, "free quota available"
    elif urgency == "batch":
        direct, reason = 0.0, "free only (batch never pays)"
    elif urgency in ("soon", "defer") and resets_in is not None and resets_in > 0:
        direct, reason = 0.0, f"quota resets in ~{resets_in:.1f}h, free dispatch"
    else:
        direct = task_tokens * paid / 1_000_000
        reason = f"paid failover, ~{task_tokens // 1000}K tokens @ ${paid:.2f}/M"

    # ── BLEED ───────────────────────────────────────────────────────────
    # Only applies when NOT dispatching now (waiting costs money).
    bleed: float = 0.0
    bleed_note: str | None = None
    if urgency != "now" and bleed_rate > 0:
        if resets_in is not None and resets_in > 0:
            bleed = bleed_rate * resets_in
            bleed_note = (
                f"waiting costs ~${bleed:.2f} in bleed "
                f"({resets_in:.1f}h × ${bleed_rate:.2f}/h)"
            )
        else:
            bleed_note = "waiting costs ~$? in bleed (timing unknown — Kalman unconverged)"

    # ── CONFIDENCE ──────────────────────────────────────────────────────
    tok_std = state.get("token_std", task_tokens * DEFAULT_TOKEN_STD_FRACTION)
    sigma_tok = (tok_std / max(task_tokens, 1)) * direct if direct > 0 else 0.0
    band = sigma_tok
    conf = _confidence(state, band, direct)

    # cost_usd = dispatch token cost (direct). Bleed is shown separately
    # in the "BUT:" line — the operator compares direct vs bleed to decide.
    return {
        "urgency": urgency,
        "cost_usd": round(direct, 4),
        "bleed_usd": round(bleed, 4),
        "confidence_interval": [
            round(max(0.0, direct - band), 4),
            round(direct + band, 4),
        ],
        "confidence": conf,
        "breakdown": {
            "direct": round(direct, 4),
            "bleed": round(bleed, 4),
        },
        "explanation": reason,
        "bleed_note": bleed_note,
    }


# ── formatter ───────────────────────────────────────────────────────────────


def format_all_urgencies(task_tokens: int, state: dict) -> str:
    """Format the multi-line cost display for the clarify question.

    Prints one line per urgency level + a ``BUT:`` line for bleed (if any).
    The operator sees this above the input prompt in the ask-wrapper.
    """
    lines: list[str] = []
    for u in URGENCIES:
        e = estimate_cost(u, task_tokens, state)
        ci = e["confidence_interval"]
        w = (ci[1] - ci[0]) / 2
        lines.append(
            f"{u.upper():5} ~${e['cost_usd']:.2f} ± ${w:.2f}"
            f"  ({e['explanation']})"
        )
    # Bleed note from the first urgency that has one (soon > defer)
    for u in ("soon", "defer"):
        e = estimate_cost(u, task_tokens, state)
        if e["bleed_note"]:
            lines.append(f"BUT:  {e['bleed_note']}")
            break
    return "\n".join(lines)
