#!/usr/bin/env python3
"""zai_proxy — local reverse proxy for z.ai that auto-rotates API keys.

ContextVM-pattern: a local service that fetches + caches external data (key quotas)
and serves routing decisions transparently. Hermes points base_url here; the proxy
picks the best key per request + retries on 429.

Endpoints:
  POST /* → forwarded to z.ai (with the healthiest key; retries on 429)
  GET  /quota → both keys' cached quotas + which is active
  GET  /health → simple liveness check

Usage logging (separate SQLite DB at ~/.hermes/bot/zai_usage.db, WAL mode):
  api_calls      — one row per request (tokens, model, key, status, duration,
                   cache/ollama/ppq hit flags)
  key_decisions  — one row per key-selection decision (chosen key, reason, both
                   quota percentages, availability flags)
Logging never raises — all write paths are wrapped to swallow errors so a
logging failure can never break a proxied request.
"""
from __future__ import annotations
import json, os, sqlite3, sys, threading, time, urllib.request, urllib.error
from datetime import datetime, timezone, date as _date
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── Shadow mode (Phase 2) — price-first optimizer running read-only ──────────
# Import bridge: logs shadow routing decisions alongside live best_key() picks.
# Wrapped so a missing repo or import error NEVER breaks production routing.
_shadow_hook = None
try:
    _MRE_PATH = os.path.expanduser("~/merchant-routing-engine")
    if _MRE_PATH not in sys.path:
        sys.path.insert(0, _MRE_PATH)
    from src.shadow_hook import ShadowHook
    # ── Token audit (Phase 2.5.4) ──────────────────────────────────────
    # Extracted into src/token_audit.py so the billed-vs-actual mismatch
    # check is unit-testable.  Falls back to a local stub below if missing.
    from src.token_audit import audit_token_count as _audit_token_count
    # ── Converged rates (Phase 3.0) ────────────────────────────────────
    # Load converged Kalman base rates from historical daily_spend data at
    # startup instead of using static seed costs.  This gives the shadow
    # optimizer an immediately-converged cost model.  Falls back to seeds
    # (inside the ShadowHook constructor) on any failure.
    _converged_rates: dict[str, float] | None = None
    try:
        from scripts.feed_historical_costs import load_historical_rates
        _converged_rates = load_historical_rates()
        if _converged_rates:
            print(f"[shadow] Converged rates loaded:", flush=True)
            for _p, _r in sorted(_converged_rates.items()):
                print(f"[shadow]   {_p:15s}  ${_r:.6f}/M", flush=True)
    except Exception as _ce:
        print(f"[shadow] converged-rate load failed — using seed costs: {_ce}", flush=True)
        _converged_rates = None
    _shadow_hook = ShadowHook(
        db_path=os.path.expanduser("~/.hermes/bot/zai_usage.db"),
        converged_rates=_converged_rates,
    )
    print(f"[shadow] ShadowHook initialized — logging to zai_usage.db", flush=True)
except Exception as _e:
    print(f"[shadow] DISABLED — {_e}", flush=True)
    _shadow_hook = None
    _converged_rates = None

# ── Dispatch gate (P5.1) ─────────────────────────────────────────────────────
# Pure three-dimension decision fn (hardware → quota-margin → price) extracted
# into src/dispatch_gate.py so it is unit-testable.  Falls back to None on any
# import error — the endpoint then degrades to a coarse candidate check.
_evaluate_dispatch = None
try:
    from src.dispatch_gate import evaluate_dispatch as _evaluate_dispatch
except Exception as _dge:
    print(f"[dispatch_gate] DISABLED — {_dge}", flush=True)
    _evaluate_dispatch = None

# ── Ollama Cloud quota tracker (EUv2-5) ──────────────────────────────────────
# Real quota regime from cumulative token usage in zai_usage.db.
# Falls back to "included" (no penalty) on any failure — never breaks routing.
_ollama_quota_status = None
try:
    from src.ollama_quota_tracker import get_quota_status as _get_quota_status
    from src.ollama_quota_tracker import DEFAULT_SESSION_LIMIT as _OC_SESSION_LIMIT
    from src.ollama_quota_tracker import DEFAULT_MONTHLY_LIMIT as _OC_MONTHLY_LIMIT
    from src.ollama_quota_tracker import _load_limits as _oc_load_limits
except Exception as _oqe:
    print(f"[ollama_quota] DISABLED — {_oqe}", flush=True)
    _get_quota_status = None
    _OC_SESSION_LIMIT = 500_000_000  # fallback default
    _OC_MONTHLY_LIMIT = 3_500_000_000  # fallback default
    _oc_load_limits = None

# ── Cost extraction (RP-2) ───────────────────────────────────────────────────
# Parses the real $ cost from each provider's API response body. Falls back to
# None (no per-call cost extraction) on any import error — the proxy's
# _extract_cost wrapper then zeroes flat-rate providers and estimates ollama.
_extract_cost_module = None
try:
    from src.cost_extraction import extract_cost as _ce_extract_cost
    _extract_cost_module = _ce_extract_cost
except Exception as _cee:
    print(f"[cost_extraction] DISABLED — {_cee}", flush=True)
    _extract_cost_module = None

# ── Real price tracker (RP-4) ────────────────────────────────────────────────
# Replaces ALL hardcoded rate constants with real measured rates from
# real_price_tracker.get_rate_with_fallback(). The tracker resolves:
#   1. Real measured cost_usd data from the DB
#   2. Ollama billing API (for ollama_cloud)
#   3. LAST_RESORT_RATES (clearly-marked estimates)
# Every import failure degrades gracefully to the inline _FALLBACK_RATES below.
_rpt_get_rate = None
try:
    from src.real_price_tracker import get_rate_with_fallback as _rpt_get_rate
    print("[real_price_tracker] loaded — cost estimation uses measured rates", flush=True)
except Exception as _rpte:
    print(f"[real_price_tracker] DISABLED — {_rpte}", flush=True)
    _rpt_get_rate = None

# Kill switch: set OLLAMA_EXTRA_USAGE_ENABLED=false to disable regime-based pricing
_OLLAMA_EXTRA_USAGE_ENABLED = os.environ.get("OLLAMA_EXTRA_USAGE_ENABLED", "false").lower() in ("1", "true", "yes")

# Cache the quota status to avoid DB queries on every snapshot call.
# Updated by _snapshot_quota() at most every _OLLAMA_QUOTA_CACHE_TTL seconds.
# Per-key caches so each Ollama Cloud subscription has independent tracking.
_ollama_quota_cache: dict[str, dict] = {}
_ollama_quota_cache_ts: dict[str, float] = {}
_OLLAMA_QUOTA_CACHE_TTL = 30.0  # seconds

# OpenCode Go allowance cache — parsed from response body's cost.allowance_remaining_usd.
# Go is a $10/mo flat-rate subscription with an allowance that depletes; once it
# hits $0, requests switch to pay-per-use. Tracking it lets the flat router apply
# per-model pressure (models burning more of the allowance get penalized).
_opencode_go_allowance: dict = {"remaining_usd": None, "ts": 0.0}
_OPENCODE_GO_INITIAL_ALLOWANCE = 10.0  # $10/mo

# Per-(provider, model) burn share cache. Updated from api_calls table by
# _compute_model_burn_share(). Used by the flat router's effective price
# formula to push high-burn models off scarce providers.
_model_burn_cache: dict[str, dict[str, float]] = {}  # provider → {model → share}
_model_burn_cache_ts: float = 0.0
_MODEL_BURN_CACHE_TTL = 60.0  # seconds

# Key-state transition tracking for sustained-down alerts.
# _key_down_since[name] = timestamp when the key first went unhealthy.
# _key_alerted[name] = True after the 15-min CRITICAL alert fires (once per down period).
_key_down_since: dict[str, float] = {}
_key_alerted: dict[str, bool] = {}
_KEY_SUSTAINED_DOWN_THRESHOLD = 900.0  # 15 minutes

# ── Garbage circuit-breaker (per-(provider, model) demotion, task t_b7725426) ─
# NeuralWatt's glm-5.3 endpoint intermittently streams degenerate token-spam
# (e.g. 77,366 completion tokens / 727s, HTTP 200, no coherent stop) when z.ai
# keys exhaust and routing falls through to NW's glm-5.3. Existing guardrails
# are too coarse: the daily-cap / "drop neuralwatt until UTC midnight"
# mechanism drops the WHOLE provider, and the key-health backoff reacts only to
# HTTP errors (a 200 with garbage output defeats it).
#
# This breaker detects degenerate oversized completions PER (provider, model)
# and demotes ONLY that pair from rotation for GARBAGE_CB_TTL_SECONDS (24h),
# alerting once per trip, while sibling lanes on the same provider stay
# eligible. It is keyed like _zai_key_health but with a composite (provider,
# model) key. Restart-safe: the state is module-local and cold-starts EMPTY
# (optimistic — never blocks routing); every read/write path is wrapped so a
# DB/logging error can never break a request (revert-safe, same as the
# daily-cap guardrail). Out of scope: provider-level daily cap + HTTP-error
# backoff (both already exist).
#
# Config (env, defaults):
#   GARBAGE_CB_ENABLED            = "1"   (set "0"/"false"/"no" to disable)
#   GARBAGE_MAX_COMPLETION_TOKENS = "32000"  (ceiling on completion_tokens)
#   GARBAGE_MAX_TOKENS_PER_SEC    = "0"   (0 = disabled; pathological t/s rate)
#   GARBAGE_CB_TTL_SECONDS        = "86400" (24h demotion window)
#
# Trigger: a successful completion with completion_tokens > the ceiling, OR
# (when the t/s threshold is configured) with completion_tokens / elapsed >
# the t/s threshold. Seeded from the observed 77k-token / 727s incident.
_GARBAGE_CB_ENABLED = os.environ.get("GARBAGE_CB_ENABLED", "1").strip().lower() \
    not in ("0", "false", "no", "")
_GARBAGE_MAX_COMPLETION_TOKENS = int(os.environ.get(
    "GARBAGE_MAX_COMPLETION_TOKENS", "32000"))
_GARBAGE_MAX_TOKENS_PER_SEC = float(os.environ.get(
    "GARBAGE_MAX_TOKENS_PER_SEC", "0"))
GARBAGE_CB_TTL_SECONDS = float(os.environ.get(
    "GARBAGE_CB_TTL_SECONDS", "86400"))
# Demotion state: {(provider, model): {"healthy": bool, "retry_after": ts,
#   "reason": str, "completion_tokens": int, "demoted_at": ts}}
_garbage_cb: dict[tuple[str, str], dict] = {}
# Once-per-trip alert tracking: {(provider, model): True} while demoted.
_garbage_cb_alerted: dict[tuple[str, str], bool] = {}
# Last seen completion window per pair — used only for the t/s path.
_garbage_cb_last: dict[tuple[str, str], tuple] = {}

def _get_ollama_quota_status(key_name: str = "ollama_cloud") -> dict:
    """Get cached or fresh ollama_cloud quota status for a specific key.
    Thread-safe.

    Returns a dict with: regime, session_used_pct, weekly_used_pct,
    monthly_used_pct, session_tokens, weekly_tokens, monthly_tokens. Falls
    back to an 'included' default on any error so routing is never broken.

    For monthly-budget plans (ollama_cloud_3) the monthly_used_pct is driven
    by the server /api/usage ``limits.monthly.usage`` fraction (authoritative);
    oc/oc2 use session+weekly windows as before.
    """
    if _get_quota_status is None or not _OLLAMA_EXTRA_USAGE_ENABLED:
        return {
            "regime": "included",
            "session_used_pct": 0.0,
            "weekly_used_pct": 0.0,
            "monthly_used_pct": 0.0,
            "session_tokens": 0,
            "weekly_tokens": 0,
            "monthly_tokens": 0,
            "monthly_limit": _OC_MONTHLY_LIMIT,
        }
    now = time.time()
    cached = _ollama_quota_cache.get(key_name)
    cache_ts = _ollama_quota_cache_ts.get(key_name, 0.0)
    if cached is not None and (now - cache_ts) < _OLLAMA_QUOTA_CACHE_TTL:
        return cached
    try:
        status = _get_quota_status(str(USAGE_DB), key_name=key_name)
        # Monthly-budget key (ollama_cloud_3): override monthly_used_pct with the
        # authoritative server fraction (limits.monthly.usage); fall back to the
        # local rolling estimate if the server fetch is unavailable.
        if key_name == "ollama_cloud_3":
            _moly = status.get("monthly_used_pct", 0.0)
            try:
                from src.ollama_extra_usage import fetch_ollama_usage
                if OLLAMA_CLOUD_KEY_3:
                    _usage = fetch_ollama_usage(api_key=OLLAMA_CLOUD_KEY_3, now=now)
                    if _usage and _usage.get("limits", {}).get("monthly", {}):
                        _frac = float(_usage["limits"]["monthly"].get("usage", 0) or 0)
                        _moly = _frac * 100.0
            except Exception:
                pass
            status["monthly_used_pct"] = _moly
            status["regime"] = "exhausted" if _moly >= 100.0 else status.get("regime", "included")
            status["monthly_tokens"] = int(status.get("monthly_tokens", 0))
            # Surface the monthly budget limit (BUG A fix): the tracker now
            # returns it, but older cached/fallback dicts may lack it — resolve
            # from the tracker's _load_limits (config override) or the default.
            if not status.get("monthly_limit"):
                try:
                    if _oc_load_limits is not None:
                        status["monthly_limit"] = _oc_load_limits()[2]
                    else:
                        status["monthly_limit"] = _OC_MONTHLY_LIMIT
                except Exception:
                    status["monthly_limit"] = _OC_MONTHLY_LIMIT
        _ollama_quota_cache[key_name] = status
        _ollama_quota_cache_ts[key_name] = now
        return status
    except Exception:
        if cached is not None:
            return cached
        return {
            "regime": "included",
            "session_used_pct": 0.0,
            "weekly_used_pct": 0.0,
            "monthly_used_pct": 0.0,
            "session_tokens": 0,
            "weekly_tokens": 0,
            "monthly_tokens": 0,
            "monthly_limit": _OC_MONTHLY_LIMIT,
        }

def _probe_hardware(hardware_req: str) -> dict:
    """Probe physical hardware state for the dispatch gate (Dimension 1).

    Only runs when ``hardware_req != "none"``.  Fault-tolerant: missing files,
    failed udevadm/ssh calls, and parse errors all degrade to safe defaults
    (absent / unknown / unreachable).  Sources per IMPL-SPEC v2:
      - board presence: ``ls /dev/ttyACM*``
      - board identity: ``udevadm`` serial of the first ttyACM device
      - lock status: ``~/.hermes/peripheral_locks/board-lock-monitor.json``
      - DQ05 reachability: ``ssh -o ConnectTimeout=3 dq05 true``
    """
    import glob as _glob, subprocess as _sp
    if hardware_req == "none":
        return {"required": "none"}
    state: dict = {}
    if hardware_req in ("board", "dual_board"):
        acm: list = []
        try:
            acm = sorted(_glob.glob("/dev/ttyACM*"))
            state["board_present"] = len(acm) > 0
            state["board_count"] = len(acm)
        except Exception:
            state["board_present"] = False
            state["board_count"] = 0
        # Board identity (udevadm serial of first device).
        try:
            if acm:
                out = _sp.run(
                    ["udevadm", "info", "-q", "property", "-n", acm[0]],
                    capture_output=True, text=True, timeout=3,
                ).stdout
                for _line in out.splitlines():
                    if _line.startswith("ID_SERIAL_SHORT="):
                        state["board_id"] = _line.split("=", 1)[1]
                        break
        except Exception:
            pass
        # Lock status from the board-lock monitor JSON.
        try:
            import json as _json
            _lp = os.path.expanduser(
                "~/.hermes/peripheral_locks/board-lock-monitor.json")
            with open(_lp) as _f:
                _lmon = _json.load(_f) or {}
            _locks = _lmon.get("locks", []) or []
            _board_locks = [l for l in _locks
                            if str(l.get("resource", "")).startswith("board")]
            _free = [l for l in _board_locks if l.get("status") == "free"]
            _held = [l for l in _board_locks if l.get("status") == "locked"]
            state["lock_status"] = (
                "free" if _free else ("held" if _held else "unknown"))
            state["queue_depth"] = len(_held)
            state["estimated_wait_minutes"] = sum(
                int(l.get("age_minutes", 0) or 0) for l in _held)
        except Exception:
            state.setdefault("lock_status", "unknown")
    elif hardware_req == "dq05":
        # Lightweight reachability probe — 3s connect timeout, no shell.
        try:
            _r = _sp.run(
                ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes",
                 "dq05", "true"],
                capture_output=True, timeout=6)
            state["dq05_reachable"] = (_r.returncode == 0)
        except Exception:
            state["dq05_reachable"] = False
    return state

# ── Token-audit fallback (Phase 2.5.4) ──────────────────────────────────────
# If the src import above failed, define a never-raising stub so the request
# path's audit call is still safe.  Real logic lives in src/token_audit.py.
#
# IMPORTANT (false-positive fix): `billed_tokens` MUST be the provider's
# completion_tokens — NOT total_tokens.  The estimate is derived from
# len(response_buffer)//4, and the response buffer contains ONLY the completion
# text (the prompt is never echoed back).  Passing total_tokens (prompt +
# completion) makes the billed count always much larger than the completion-only
# estimate, which guarantees a spurious >20% mismatch on any request with a
# non-trivial prompt.
if "_audit_token_count" not in globals():

    def _audit_token_count(billed_tokens, response_buffer, threshold=0.20):
        try:
            _buf = response_buffer if response_buffer is not None else b""
            _actual = len(_buf) // 4
            _billed = int(billed_tokens or 0)
            if _billed <= 0 or _actual <= 0:
                return (_actual, False, 0.0)
            _rate = abs(_billed - _actual) / max(_billed, 1)
            return (_actual, _rate > threshold, _rate)
        except Exception:
            return (0, False, 0.0)

# ── LiveRouter (Phase 1.2) — Kalman-driven failover selection ───────────────
# LiveRouter wraps the RoutingOptimizer for LIVE failover routing.  It is
# ONLY called when BOTH z.ai keys are exhausted (best_key() Phase 4 sets
# chosen = None).  Normal ours/friend routing is completely unaffected.
#
# Kill switch: touch ~/.hermes/bot/.enable_live_routing to enable.
#             rm    ~/.hermes/bot/.enable_live_routing to disable.
# No restart needed — the flag is checked on every failover call.
#
# Safety: every LiveRouter call is wrapped in try/except.  If LiveRouter
# fails (import error, exception, no provider found), best_key() returns
# None and the existing hardcoded ollama → ppq → openrouter chain runs.
_LIVE_ROUTER = None
_LIVE_ROUTING_FLAG = os.path.expanduser("~/.hermes/bot/.enable_live_routing")
try:
    from src.live_router import LiveRouter as _LiveRouterCls
    _LIVE_ROUTER = _LiveRouterCls.get_instance(
        db_path=os.path.expanduser("~/.hermes/bot/zai_usage.db"),
        converged_rates=_converged_rates,
    )
    print(f"[live] LiveRouter initialized — failover selection ready "
          f"(kill switch: {_LIVE_ROUTING_FLAG})", flush=True)
except Exception as _le:
    print(f"[live] LiveRouter DISABLED — {_le}", flush=True)
    _LIVE_ROUTER = None

# ── PPQ credit-balance bridge (P3-PPQ) ───────────────────────────────────────
# quota_state['ppq'] used to be hardcoded {'used_pct': 0.0} — PPQ credit
# depletion never reached the pricing engine. This imports the bridge fn from
# the merged collector (src.balance_collectors.ppq_quota_entry), which reads
# the newest 'ppq' row from provider_balances in api_burn.db (written by
# the every-5min balance_collectors --provider ppq cron). _snapshot_quota()
# calls _ppq_quota_snapshot() instead of the old hardcoded dict. Revert-safe:
# any failure → the old optimistic {'used_pct': 0.0} so routing never breaks.
_ppq_quota_entry_fn = None
try:
    from src.balance_collectors import ppq_quota_entry as _ppq_quota_entry_fn
    print("[ppq] balance bridge loaded — quota_state['ppq'] reads real credit balance",
          flush=True)
except Exception as _pqe:
    print(f"[ppq] balance bridge DISABLED — {_pqe}", flush=True)
    _ppq_quota_entry_fn = None

# ── OpenRouter credit-balance bridge (T1T3) ──────────────────────────────────
# quota_state['openrouter'] was hardcoded {used_pct:0.0, remaining:inf} — credit
# depletion never reached the pricing engine. Mirrors the PPQ bridge above:
# imports openrouter_quota_entry from the merged balance_collectors module
# (reads newest 'openrouter' row from provider_balances, written by the
# every-5min balance_collectors --provider openrouter cron). Revert-safe: any
# failure → old optimistic {used_pct:0.0, remaining:inf} so routing never
# breaks. REVERT: delete this block + restore the one-line hardcode
# `snap["openrouter"] = {"used_pct": 0.0, "remaining": float("inf")}`
# in _snapshot_quota().
_openrouter_quota_entry_fn = None
try:
    from src.balance_collectors import openrouter_quota_entry as _openrouter_quota_entry_fn
    print("[openrouter] balance bridge loaded — quota_state['openrouter'] reads real credit balance",
          flush=True)
except Exception as _oqe:
    print(f"[openrouter] balance bridge DISABLED — {_oqe}", flush=True)
    _openrouter_quota_entry_fn = None

# ── Telnyx self-tracking balance bridge (TELNYX-3.2) ───────────────────────────
# quota_state['telnyx'] was hardcoded {used_pct:0.0, remaining:inf} — balance
# depletion never reached the pricing engine. Mirrors the PPQ/OpenRouter
# bridges above: imports telnyx_quota_entry from the merged balance_collectors
# module (reads newest 'telnyx' row from provider_balances, written by the
# every-5min balance_collectors --provider telnyx cron). Revert-safe: any
# failure → old optimistic {used_pct:0.0, remaining:inf} so routing never
# breaks. REVERT: delete this block + restore the one-line hardcode
# `snap["telnyx"] = {"used_pct": 0.0, "remaining": float("inf")}`
# in _snapshot_quota().
_telnyx_quota_entry_fn = None
try:
    from src.balance_collectors import telnyx_quota_entry as _telnyx_quota_entry_fn
    print("[telnyx] balance bridge loaded — quota_state['telnyx'] reads real balance",
          flush=True)
except Exception as _tqe:
    print(f"[telnyx] balance bridge DISABLED — {_tqe}", flush=True)
    _telnyx_quota_entry_fn = None

_routstr_quota_entry_fn = None
try:
    from src.balance_collectors import routstr_quota_entry as _routstr_quota_entry_fn
    print("[routstr] balance bridge loaded — quota_state['routstr'] reads real sats balance",
          flush=True)
except Exception as _rqe:
    print(f"[routstr] balance bridge DISABLED — {_rqe}", flush=True)
    _routstr_quota_entry_fn = None

# ── NeuralWatt credit-balance bridge (NW-API) ───────────────────────────────
# quota_state['neuralwatt'] was hardcoded {used_pct:0.0, remaining:inf} —
# spend tracking was invisible to the pricing engine. The $258-in-one-day
# incident burned ~3,300 glm-5.2 calls through neuralwatt with no guardrail
# in place because the proxy literally saw "infinity remaining".
#
# 2026-08-23: Felix found NeuralWatt's REAL billing API:
#   * GET /v1/quota           → balance, subscription, kWh allowance
#   * GET /v1/usage/summary   → daily cost time_series (period → today → total)
#
# NeuralWatt uses ENERGY-based pricing (kWh, not per-token). The Pro plan
# ($100/mo) includes 13.33 kWh/month. 94% of prompt tokens are cached at a
# 5× discount, so our per-token cost estimate OVERCOUNTS by ~5.7×. The bridge
# imports `neuralwatt_quota_entry` from src.balance_collectors — which calls
# /v1/quota for real kWh remaining and /v1/usage/summary for today's real USD
# spend — exposes a NEURALWATT_DAILY_CAP guardrail (default $10/day), and a
# `get_neuralwatt_cost_correction_factor()` helper used by
# `_estimate_cost_usd()` to scale our over-estimated per-token cost down to
# the real spend ratio.
#
# When daily spend exceeds the cap (real number from the API), the entry
# surfaces is_daily_cap_exceeded=True and we mark the key unhealthy so the
# router drops neuralwatt from rotation until UTC midnight. Revert-safe: any
# failure → the old optimistic {used_pct:0.0, remaining:inf} so routing
# never breaks.
# REVERT: delete this block + restore the one-line hardcode
# `snap["neuralwatt"] = {"used_pct": 0.0, "remaining": float("inf")}` in
# _snapshot_quota(), and `h["neuralwatt"] = True` in _snapshot_health().
_neuralwatt_quota_entry_fn = None
try:
    from src.balance_collectors import neuralwatt_quota_entry as _neuralwatt_quota_entry_fn
    print("[neuralwatt] balance bridge loaded — quota_state['neuralwatt'] reads real /v1/quota API",
          flush=True)
except Exception as _nwe:
    print(f"[neuralwatt] balance bridge DISABLED — {_nwe}", flush=True)
    _neuralwatt_quota_entry_fn = None

# Optional: mirror key_health gate so the daily-cap call can flag exhaustion.
_NEURALWATT_DAILY_CAP_DEFAULT = 10.0  # USD/day — synced with balance_collectors

# Cost-correction factor (cached) — applied in _estimate_cost_usd() so the
# per-token cost estimate is brought in-line with the real NeuralWatt bill.
# Computed as real_api_total / db_sum_cost_usd, refreshed every 10 minutes.
_neuralwatt_cost_correction_fn = None
try:
    from src.balance_collectors import (
        get_neuralwatt_cost_correction_factor as _neuralwatt_cost_correction_fn,
    )
    print("[neuralwatt] cost-correction bridge loaded — _estimate_cost_usd will scale to real API spend",
          flush=True)
except Exception as _nce:
    print(f"[neuralwatt] cost-correction bridge DISABLED — {_nce}", flush=True)
    _neuralwatt_cost_correction_fn = None

# ── Pressure FSM bridge (S2b two-layer pressure routing, t_4dfaf0d5) ────────
# Layer-2 request-time half of DESIGN-two-layer-pressure-routing.md:
# GREEN/AMBER/RED band FSM over friend-key quota + Kalman exhaust
# predictions. SHADOW MODE ONLY — computes and logs the decision it
# WOULD make (pressure_decisions table); never reroutes a live request.
# Kill switches: touch ~/.hermes/bot/.pressure_routing_disabled OR set
# pressure_policy.json {"mode":"off"}. Revert-safe: None → all hooks
# no-op. Enforce mode is a later stage (S2c+) and behaves as shadow here.
_pressure_tracker = None
try:
    from pressure_fsm import PressureTracker as _PressureTrackerCls
    _pressure_tracker = _PressureTrackerCls()
    print(f"[pressure] FSM bridge loaded — shadow mode "
          f"(state: {_pressure_tracker.mode()}, kill switch: "
          f"{_pressure_tracker.flag_path})", flush=True)
except Exception as _pre:
    print(f"[pressure] FSM bridge DISABLED — {_pre}", flush=True)
    _pressure_tracker = None


def _pressure_shadow(model: str | None, session_id: str | None,
                     ollama_regime: str | None = None) -> object | None:
    """Shadow-only pressure decision (t_4dfaf0d5). NEVER raises.

    Reads the Ollama regime from the live quota status when the caller
    doesn't supply one, and forwards the proxy's current friend-key lock
    verdict (LOCK_THRESHOLDS) so decisions never overstate capacity
    (cold review pass 1). Returns a pressure_fsm.Decision or None
    (tracker missing / kill switch active / any internal error).
    """
    if _pressure_tracker is None or not model:
        return None
    try:
        regime = ollama_regime
        if regime is None:
            regime = _get_ollama_quota_status().get("regime")
        friend_locked = False
        try:
            windows = quota_cache.get("friend", ([], 0.0))[0]
            friend_locked = bool(is_key_locked("friend", windows)[0])
        except Exception:
            pass  # no cache yet / helper missing -> unlocked default
        return _pressure_tracker.shadow_decision(
            model, session_id=session_id, ollama_regime=regime,
            friend_locked=friend_locked)
    except Exception:
        return None  # shadow hooks must never break request handling

# ── ProfitTracker (consumer-mode savings ledger, MRE Phase 2.3) ─────────────
# Records every external-failover routing decision + savings vs the next-best
# alternative into the routing_profit table. Fire-and-forget daemon writer;
# revert-safe (None → calls are skipped).
_PROFIT_TRACKER = None
try:
    from src.profit_tracker import ProfitTracker as _ProfitTrackerCls
    _PROFIT_TRACKER = _ProfitTrackerCls()
    print("[profit] ProfitTracker loaded — routing_profit savings ledger active",
          flush=True)
except Exception as _pte:
    print(f"[profit] ProfitTracker DISABLED — {_pte}", flush=True)
    _PROFIT_TRACKER = None

# ── config ──────────────────────────────────────────────────────────────────
def _load_keys():
    """Load keys from the manager .env (gitignored, never in repo)."""
    keys = {}
    for ep in [Path.home()/".hermes/profiles/manager/.env", Path.home()/".hermes/.env",
               Path.home()/".hermes/bot/.env"]:
        if ep.exists():
            for line in ep.read_text(errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("ZAI_API_KEY=") and "ZAI_OUR_KEY" not in line and "friend" not in keys:
                    keys["friend"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("ZAI_OUR_KEY=") and "ours" not in keys:
                    keys["ours"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
    return keys

KEYS = _load_keys()
# Per-window lock thresholds: a key is "locked" when ANY window's used_pct
# meets/exceeds its threshold for that key name.  Burst protection on the short
# window, quota preservation on the weekly window for the friend key.
LOCK_THRESHOLDS = {
    "5-hour":  {"ours": 90, "friend": 80},   # burst protection; switch off friend earlier (80%)
    "weekly":  {"ours": 60, "friend": 80},   # proactive: switch off ours at 60% (40% buffer)
    "monthly": {"ours": 95, "friend": 95},   # tools limit (high — rarely hit)
}

# Cost-aware routing tie-breaker. Cheapest key wins when both are unlocked
# AND healthy. NOTE: cost is a TIE-BREAKER only — Kalman exhaustion prediction
# and per-window lock thresholds remain the primary signals in best_key().
#
#   ours          1.0   — base rate (z.ai subscription); CHEAPEST when healthy.
#                         Subscription may be cancelled → mark dead with:
#                         touch ~/.hermes/bot/.key_disabled_ours
#   friend        1.21  — z.ai courtesy key (21% premium over base rate).
#   ollama_cloud  1.0   — flat-rate cloud ($100/mo, rate from real_price_tracker). Preferred
#                         during z.ai peak hours (UTC 6-10) or when z.ai is dead.
#   ppq           — pay-per-token; most expensive, last-resort failover only.
_KEY_COST_MULTIPLIER = {"ours": 1.0, "friend": 1.21, "ollama_cloud": 1.0, "ollama_cloud_2": 1.0, "ollama_cloud_3": 1.0, "opencode_go": 1.0, "neuralwatt": 1.0}
UPSTREAM   = "https://api.z.ai/api/coding/paas/v4"
QUOTA_URL  = "https://api.z.ai/api/monitor/usage/quota/limit"
CACHE_TTL  = 300                                # 5 min
PORT       = int(os.environ.get("PORT", "9099"))
STATE_FILE = Path.home() / ".hermes" / "bot" / "zai_proxy_state.json"

# ── external failover providers ─────────────────────────────────────────────
def _load_external_keys():
    """Load PPQ, OpenRouter, Ollama Cloud, DeepInfra, and Telnyx keys from .env."""
    keys = {}
    for ep in [Path.home()/".hermes/profiles/manager/.env", Path.home()/".hermes/.env",
               Path.home()/".hermes/bot/.env"]:
        if ep.exists():
            for line in ep.read_text(errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("PPQ_API_KEY=") and "ppq" not in keys:
                    keys["ppq"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("OPENROUTER_API_KEY=") and "openrouter" not in keys:
                    keys["openrouter"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("OLLAMA_CLOUD_API_KEY=") and "ollama_cloud" not in keys:
                    keys["ollama_cloud"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("OLLAMA_CLOUD_API_KEY_2=") and "ollama_cloud_2" not in keys:
                    keys["ollama_cloud_2"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("OLLAMA_CLOUD_API_KEY_3_STOIC_HERSCHEL_499=") and "ollama_cloud_3" not in keys:
                    keys["ollama_cloud_3"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("DEEPINFRA_API_KEY=") and "deepinfra" not in keys:
                    keys["deepinfra"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("DEEPINFRA_STARTING_BALANCE=") and "deepinfra_balance" not in keys:
                    keys["deepinfra_balance"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("TELNYX_API_KEY=") and "telnyx" not in keys:
                    keys["telnyx"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("TELNYX_STARTING_BALANCE=") and "telnyx_balance" not in keys:
                    keys["telnyx_balance"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("ROUTSTR_API_KEY=") and "routstr" not in keys:
                    keys["routstr"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("ROUTSTR_BASE=") and "routstr_base" not in keys:
                    keys["routstr_base"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("ROUTSTRD_API_KEY=") and "routstrd" not in keys:
                    keys["routstrd"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("ROUTSTRD_BASE=") and "routstrd_base" not in keys:
                    keys["routstrd_base"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("OPENCODE_GO_API_KEY=") and "opencode_go" not in keys:
                    keys["opencode_go"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
                elif line.startswith("NEURALWATT_API_KEY=") and "neuralwatt" not in keys:
                    keys["neuralwatt"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
    return keys

_EXTERNAL_KEYS = _load_external_keys()

# Ollama Cloud — primary provider (same tier as z.ai, not just failover)
OLLAMA_CLOUD_KEY = _EXTERNAL_KEYS.get("ollama_cloud", "")
OLLAMA_CLOUD_BASE = "https://ollama.com/v1"
# Ollama Cloud key #2 — second subscription account (market-routed, own Kalman)
OLLAMA_CLOUD_KEY_2 = _EXTERNAL_KEYS.get("ollama_cloud_2", "")
# Ollama Cloud key #3 (stoic_herschel_499) — $20/mo MONTHLY-budget credit-pool plan
# (no 5h session windows; limits.monthly only). Market-routed as T4-included with a
# monthly window + ~90% delist guard (dead-time penalty ~30 days). See plan
# plans/ollama3-burn-reduction-2026-09-02.md Phases A/C.
OLLAMA_CLOUD_KEY_3 = _EXTERNAL_KEYS.get("ollama_cloud_3", "")

# All registered Ollama Cloud keys — the _try_ollama_cloud_any dispatcher
# iterates this list. Key #1 first (backward compat), key #2 as failover, key
# #3 LAST as the monthly-budget relief valve (oc/oc2 drain 5h windows daily, so
# they stay primary; oc3 absorbs overflow to protect its ~30-day pool from early
# exhaustion). Empty keys are dropped below.
_OLLAMA_CLOUD_KEYS: list[tuple[str, str]] = [
    ("ollama_cloud", OLLAMA_CLOUD_KEY),
    ("ollama_cloud_2", OLLAMA_CLOUD_KEY_2),
    ("ollama_cloud_3", OLLAMA_CLOUD_KEY_3),
]
_OLLAMA_CLOUD_KEYS = [(n, k) for n, k in _OLLAMA_CLOUD_KEYS if k]  # drop empty

# DeepInfra — preferred external failover (prompt caching reduces effective cost)
DEEPINFRA_KEY = _EXTERNAL_KEYS.get("deepinfra", "")
DEEPINFRA_BASE = "https://api.deepinfra.com/v1/openai"
DEEPINFRA_STARTING_BALANCE = float(_EXTERNAL_KEYS.get("deepinfra_balance", "5.0") or "5.0")

# Telnyx — Kimi K3 failover provider (demo endpoint needs no API key)
# Demo endpoint: POST https://telnyx.com/api/inference (10 req/min, SSE streaming)
# Production API: https://api.telnyx.com/v2/ai (requires account + API key)
TELNYX_KEY = _EXTERNAL_KEYS.get("telnyx", "")
TELNYX_BASE = "https://api.telnyx.com/v2/ai"
TELNYX_DEMO_URL = "https://telnyx.com/api/inference"
TELNYX_STARTING_BALANCE = float(_EXTERNAL_KEYS.get("telnyx_balance", "10.0") or "10.0")

# OpenCode Go — $10/month flat-rate subscription (GLM-5.2/5.3, Kimi, DeepSeek)
OPENCODE_GO_KEY = _EXTERNAL_KEYS.get("opencode_go", "")
OPENCODE_GO_BASE = "https://opencode.ai/zen/go/v1"

# NeuralWatt — per-token (deepseek-v4-flash $0.14/M, prompt caching at $0.03/M)
NEURALWATT_KEY = _EXTERNAL_KEYS.get("neuralwatt", "")
NEURALWATT_BASE = "https://api.neuralwatt.com/v1"

# Startup diagnostics — print key/balance status like other external providers
print(f"[telnyx] key={'loaded' if TELNYX_KEY else 'MISSING'} "
      f"suffix={TELNYX_KEY[-4:] if TELNYX_KEY else 'N/A'} "
      f"starting_balance=${TELNYX_STARTING_BALANCE:.2f}", flush=True)

# Models that have Telnyx fallback when Ollama Cloud fails
_TELNYX_FALLBACK_MODELS = {"kimi-k2.7-code", "kimi-k3:cloud", "kimi-k3"}

# Models that route DIRECTLY to Telnyx (bypass z.ai entirely — these models
# don't exist on z.ai).  The proxy sends them straight to Telnyx's API.
_TELNYX_DIRECT_MODELS = {"kimi-k3"}

# Provider failover sort: cost is primary (cheapest first). When costs tie,
# providers with fewer recent failures are tried first (from _provider_health).
# The old static _PROVIDER_PRIORITY dict was removed — the Kalman-adjusted
# cost from _get_provider_cost() + failure_count from _provider_health now
# fully determine ordering. LiveRouter (Kalman-based) runs as the primary
# routing system; this only affects the fallback path.

# Per-provider model name translation.
# PPQ/OpenRouter use canonical short IDs (e.g., "deepseek/deepseek-v4-pro")
# but DeepInfra expects case-sensitive dotted form (e.g., "deepseek-ai/DeepSeek-V4-Pro").
# Any provider not in this dict uses ext_model verbatim.
_PROVIDER_MODEL_NAMES = {
    "deepinfra": {
        "deepseek/deepseek-v4-pro":   "deepseek-ai/DeepSeek-V4-Pro",
        "deepseek/deepseek-v4-flash": "deepseek-ai/DeepSeek-V4-Flash",
        "glm-5.2":                    "zai-org/GLM-5.2",
        "glm-5.3":                    "zai-org/GLM-5.3",
    },
    # Telnyx is Kimi-only by operator decision (2026-08-20): glm-5.2 ran
    # ~$12/M blended here vs ~$0.26-1.30/M on deepinfra/ppq/openrouter.
    # The generic failover guard below skips Telnyx for any model missing
    # from this map (verbatim-name passthrough would otherwise still hit it).
    "telnyx": {
        "kimi-k3":         "moonshotai/Kimi-K3",
        "kimi-k2.5":       "moonshotai/Kimi-K2.5",
        "gpt-5":           "openai/gpt-5",
        "claude-haiku-4-5": "anthropic/claude-haiku-4-5",
        "minimax-m3":      "MiniMaxAI/MiniMax-M3-MXFP8",
        "kimi-k3:cloud":   "moonshotai/Kimi-K2.5",  # Fallback to cheaper K2.5 (K3 costs extra on Ollama Cloud)
        "kimi-k2.7-code":  "moonshotai/Kimi-K2.5",  # K2.5 closest to K2.7 on Telnyx
    },
    "openrouter": {
        "glm-5.2":                    "z-ai/glm-5.2",
        "kimi-k3":                    "moonshotai/kimi-k3",
        "deepseek/deepseek-v4-flash":  "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro":    "deepseek/deepseek-v4-pro",
    },
    "ppq": {
        "glm-5.2":                    "z-ai/glm-5.2",
        "kimi-k3":                    "moonshotai/kimi-k3",
        "deepseek/deepseek-v4-flash":  "deepseek/deepseek-v4-flash",
    },
    "opencode_go": {
        "glm-5.2":                    "glm-5.2",
        "glm-5.3":                    "glm-5.3",
        "kimi-k3":                    "kimi-k3",
        "kimi-k2.7-code":             "kimi-k2.7-code",
        "deepseek/deepseek-v4-pro":    "deepseek-v4-pro",
        "deepseek/deepseek-v4-flash":  "deepseek-v4-flash",
    },
    "neuralwatt": {
        "glm-5.2":                    "glm-5.2",
        "kimi-k3":                    "kimi-k3",
        "kimi-k2.7-code":             "kimi-k2.7-code",
        "deepseek/deepseek-v4-flash":  "deepseek-v4-flash",
        "deepseek/deepseek-v4-pro":    "deepseek-v4-pro",
        "deepseek/gemma-4-31b":        "gemma-4-31b",
    },
    "ollama_cloud": {
        "deepseek/deepseek-v4-flash":  "deepseek-v4-flash:0731",
        "deepseek/deepseek-v4-pro":    "deepseek-v4-pro:0813",
    },
    "ollama_cloud_2": {
        "deepseek/deepseek-v4-flash":  "deepseek-v4-flash:0731",
        "deepseek/deepseek-v4-pro":    "deepseek-v4-pro:0813",
    },
    "ollama_cloud_3": {
        "deepseek/deepseek-v4-flash":  "deepseek-v4-flash:0731",
        "deepseek/deepseek-v4-pro":    "deepseek-v4-pro:0813",
    },
}

# NeuralWatt per-model pricing ($/M tokens) for accurate cost_usd logging.
# The Kalman filter refines from this seed as real cost_usd data accumulates.
NEURALWATT_RATES: dict[str, dict[str, float]] = {
    "deepseek-v4-flash":     {"input": 0.14, "cached_input": 0.03, "output": 0.28},
    "deepseek-v4-pro":       {"input": 1.00, "cached_input": 0.10, "output": 3.00},
    "gemma-4-31b":            {"input": 0.14, "cached_input": 0.01, "output": 0.42},
    "glm-5.2":               {"input": 1.45, "cached_input": 0.14, "output": 4.50},
    "kimi-k3":               {"input": 1.45, "cached_input": 0.14, "output": 4.50},  # same tier as glm
}

EXTERNAL_PROVIDERS = {
    "deepinfra": {
        "base_url": DEEPINFRA_BASE,
        "key": DEEPINFRA_KEY,
    },
    "telnyx": {
        "base_url": TELNYX_BASE,
        "key": TELNYX_KEY,
    },
    "ppq": {
        "base_url": "https://api.ppq.ai/v1",
        "key": _EXTERNAL_KEYS.get("ppq", ""),
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key": _EXTERNAL_KEYS.get("openrouter", ""),
    },
    "routstr": {
        # Our own VPS2 routstr node — z.ai-backed upstream, Cashu-metered.
        # Identity model mapping (node uses the same IDs as us).
        "base_url": _EXTERNAL_KEYS.get("routstr_base", "http://23.182.128.51:8009") + "/v1",
        "key": _EXTERNAL_KEYS.get("routstr", ""),
    },
    "routstrd": {
        # Local routstrd daemon — buys from cheapest network node via Cashu.
        # Model IDs come from the network catalog (same short IDs as ours).
        "base_url": _EXTERNAL_KEYS.get("routstrd_base", "http://localhost:8008") + "/v1",
        "key": _EXTERNAL_KEYS.get("routstrd", ""),
    },
    "neuralwatt": {
        # NeuralWatt — per-token, deepseek-v4-flash $0.14/M, prompt caching.
        "base_url": NEURALWATT_BASE,
        "key": NEURALWATT_KEY,
    },
}

# Fallback models — chosen based on the requesting profile's quality tier.
# Manager (glm-5.2): quality floor at deepseek-v4-pro (55.4% SWE-bench).
#   NEVER falls back to flash — returns error instead of low-quality output.
# Workers (glm-4.5-flash): cheapest available is fine (output gets vetted).
MANAGER_FALLBACK_MODEL = "glm-5.2"
WORKER_FALLBACK_MODEL = "deepseek/deepseek-v4-flash"

# ── Known external model detection (FIX: silent substitution, 2026-08-25) ───
# Models that are recognized by at least one external provider. If a model
# is in this set (or starts with a known provider prefix), _try_external_failover
# passes it through to the provider verbatim instead of silently substituting
# WORKER_FALLBACK_MODEL. Unknown models cause failover to return False so the
# caller sends a proper 404/503 — never HTTP 200 with a different model.
_KNOWN_EXTERNAL_PREFIXES = ("deepseek/", "qwen", "minimax", "mimo")

def _is_known_external_model(model: str | None) -> bool:
    """Check if a model is known to at least one external provider.

    A model is 'known' if:
      1. It appears in _PROVIDER_MODEL_NAMES for any provider, OR
      2. It starts with a known provider prefix (deepseek/, qwen, minimax, mimo), OR
      3. It matches any model in the flat router's PROVIDER_MODELS sets.

    Short-form aliases (e.g. "deepseek-v4-flash") are canonicalized first
    (single source of truth: flat_router.canonicalize_model) so they are
    recognized even though registries list the canonical ID.
    """
    if not model:
        return False
    model = model.strip()
    # Canonicalize aliases before any registry/prefix check
    try:
        from flat_router import canonicalize_model as _fr_canonicalize
        model = _fr_canonicalize(model) or model
    except Exception:
        pass
    # Check known prefixes
    if model.startswith(_KNOWN_EXTERNAL_PREFIXES):
        return True
    # Check _PROVIDER_MODEL_NAMES (per-provider model mapping)
    for _prov_map in _PROVIDER_MODEL_NAMES.values():
        if model in _prov_map:
            return True
    # Check flat router PROVIDER_MODELS
    try:
        from flat_router import PROVIDER_MODELS
        for _models in PROVIDER_MODELS.values():
            if model in _models:
                return True
    except Exception:
        pass
    # Check z.ai native models (these are handled by z.ai keys, but if
    # they reach failover, ollama_cloud can serve them)
    if model in ("glm-5.2", "glm-5.3", "glm-4.5-flash", "glm-4.5-air", "glm-4.5"):
        return True
    return False

# z.ai peak hours: Beijing 14:00-18:00 = UTC 6-10. During peak, z.ai burns 3x quota.
# Ollama Cloud has no peak pricing — prefer it during these hours.
_PEAK_HOURS_UTC = {6, 7, 8, 9, 10}

def _is_peak_hour() -> bool:
    """Check if current UTC hour is a z.ai peak hour."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).hour in _PEAK_HOURS_UTC

# ── provider funding tracker ────────────────────────────────────────────────
# Tracks which providers have credits remaining. A 402 response marks a
# provider unfunded for 1 hour (credits may be replenished). The failover
# logic only tries funded providers, sorted by cost.
_UNFUNDED_RETRY_SECONDS = 300  # retry unfunded provider after 5 min

_provider_health: dict[str, dict] = {}


def _is_provider_funded(name: str) -> bool:
    """Check if a provider has credits. Unfunded providers are retried
    after _UNFUNDED_RETRY_SECONDS."""
    h = _provider_health.get(name)
    if not h or h.get("funded", True):
        return True
    return time.time() >= h.get("retry_after", 0)


def _mark_unfunded(name: str) -> None:
    """Mark a provider as out of credits (after receiving 402)."""
    _provider_health[name] = {
        "funded": False,
        "last_402": time.time(),
        "retry_after": time.time() + _UNFUNDED_RETRY_SECONDS,
    }


def _mark_funded(name: str) -> None:
    """Mark a provider as funded again (successful response)."""
    _provider_health[name] = {"funded": True}


# ── z.ai key health tracker ─────────────────────────────────────────────────
# Same pattern as _provider_health, but for z.ai keys. When a key returns
# an empty response or 429, it's marked exhausted with BINARY EXPONENTIAL
# BACKOFF: each consecutive failure doubles the retry-after delay (capped
# at 1 hour). best_key() skips exhausted keys. When both are exhausted,
# the proxy fails over to external providers (PPQ/OpenRouter).
#
# Manual override: drop a flag file ~/.hermes/bot/.key_disabled_<name> to
# force a key to be treated as unhealthy (e.g. a cancelled subscription).
# Re-enable with: rm ~/.hermes/bot/.key_disabled_<name>

# Exponential backoff ramp for QUOTA-EXHAUSTION failures (429 / empty response).
# Spec: 2s→4s→8s→16s→32s→60s (capped). A single 429 blocks a key for only 2s so
# the other key / external failover covers traffic immediately; repeated 429s
# escalate up to the 60s cap.
# Extended for high failure counts: after 6 consecutive exhaustions, the key is
# clearly quota-exhausted (not transient). Backoff escalates to 5min (11-20
# failures) then 15min (21+) to stop retry storms on keys that take hours to
# reset. This prevents the ollama_cloud_2 scenario (79 consecutive 60s retries).
_BACKOFF_SEQUENCE = (2, 4, 8, 16, 32, 60)
_BACKOFF_HIGH_THRESHOLD = 6     # failures beyond _BACKOFF_SEQUENCE length
_BACKOFF_HIGH_SECONDS = 300     # 5 min for failures 7-20
_BACKOFF_VERY_HIGH_THRESHOLD = 20  # failures beyond this → 15 min
_BACKOFF_VERY_HIGH_SECONDS = 900   # 15 min for 21+ failures

# Dead key (401/403) — auth failure, likely revoked/cancelled. Flat 1h: a dead
# key will not recover by retrying quickly, so park it for an hour.
_DEAD_KEY_BACKOFF_SECONDS = 3600

# Upstream server error (500/502/503/504) — transient, not the key's fault.
# Medium flat backoff.
_SERVER_ERROR_BACKOFF_SECONDS = 30

# Log a KEY_DEAD anomaly after this many consecutive failures of any type —
# surfaces persistently-failing keys (e.g. a cancelled subscription) to dashboards.
_KEY_DEAD_THRESHOLD     = 7

_zai_key_health: dict[str, dict] = {}


def _disabled_flag_path(name: str) -> Path:
    """Filesystem flag path used to manually disable key *name*."""
    return Path.home() / ".hermes" / "bot" / f".key_disabled_{name}"


# ── Ollama Cloud paywall flag (G1/G2) ───────────────────────────────────────
# Ollama signals quota exhaustion on the largest plan as 403 "requires a
# subscription" — NOT 429 — and the quota tracker's LOCAL token counting
# can't see the server-side weekly window. The 403 handler arms this file
# with the next Monday-00:00-UTC reset (minus probe margin). While fresh:
#   * _is_key_healthy('ollama_cloud') → False (skip round-trips)
#   * quota state used_pct=100 → quota_pressure +inf (price-level avoidance)
_OLLAMA_PAYWALL_FLAG = Path.home() / ".hermes" / "bot" / ".ollama_exhausted_until"

# Per-key paywall flag paths. Key #1 keeps the historical filename (no
# migration needed); key #2 gets its own suffix. Looked up by key_name.
_OLLAMA_PAYWALL_FLAGS: dict[str, Path] = {
    "ollama_cloud": _OLLAMA_PAYWALL_FLAG,
    "ollama_cloud_2": Path.home() / ".hermes" / "bot" / ".ollama_exhausted_until_2",
    "ollama_cloud_3": Path.home() / ".hermes" / "bot" / ".ollama_exhausted_until_3",
}


def _next_monday_utc(now: float | None = None) -> float:
    """Next Monday 00:00 UTC strictly after *now* (86400*4 probe margin is
    subtracted by the caller, not here)."""
    import datetime as _dt
    now = now if now is not None else time.time()
    d = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
    days_ahead = (7 - d.weekday()) % 7  # Monday=0
    if days_ahead == 0 and d.time() == _dt.time(0, 0):
        days_ahead = 7
    nxt = (d + _dt.timedelta(days=days_ahead)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()


def _arm_ollama_paywall_flag(key_name: str = "ollama_cloud") -> float:
    """Arm the per-key paywall flag until next Monday 00:00 UTC minus a 4h
    probe margin. Returns the armed expiry. Never raises."""
    try:
        until = _next_monday_utc() - 4 * 3600
        flag = _OLLAMA_PAYWALL_FLAGS.get(key_name, _OLLAMA_PAYWALL_FLAG)
        flag.write_text(str(until))
        return until
    except Exception:
        return 0.0


def _ollama_paywall_active(key_name: str = "ollama_cloud") -> bool:
    """True while the per-key persisted paywall flag is fresh. Never raises."""
    try:
        flag = _OLLAMA_PAYWALL_FLAGS.get(key_name, _OLLAMA_PAYWALL_FLAG)
        if not flag.exists():
            return False
        return time.time() < float(flag.read_text().strip())
    except Exception:
        return False


def _is_manually_disabled(name: str) -> bool:
    """True iff the operator has touched ~/.hermes/bot/.key_disabled_<name>.

    Lightweight check (no logging) — safe to call inside loops (e.g. the retry
    order filter). ``_is_key_healthy`` does its own check + dashboard log, so
    prefer this helper anywhere that would otherwise spam key_decisions.
    Fails OPEN: a filesystem error is treated as 'not disabled'."""
    try:
        return _disabled_flag_path(name).exists()
    except Exception:
        return False


def _backoff_for_failure(failure_count: int) -> float:
    """Exponential backoff (seconds) for the Nth consecutive *exhaustion* failure
    (1-indexed): returns 2,4,8,16,32,60 for failures 1-6, then 300 (5min) for
    failures 7-20, then 900 (15min) for 21+. This prevents retry storms on
    quota-exhausted keys that take hours to reset."""
    if failure_count <= 0:
        return 0.0
    if failure_count > _BACKOFF_VERY_HIGH_THRESHOLD:
        return float(_BACKOFF_VERY_HIGH_SECONDS)
    if failure_count > _BACKOFF_HIGH_THRESHOLD:
        return float(_BACKOFF_HIGH_SECONDS)
    idx = min(failure_count - 1, len(_BACKOFF_SEQUENCE) - 1)
    return float(_BACKOFF_SEQUENCE[idx])


def _ensure_anomaly_table() -> None:
    """Ensure the anomaly_events table exists. Swallows all errors.

    Uses the SHARED monitoring schema (severity/category/title/detail) that the
    bot's anomaly detector also writes to. On systems where the table already
    exists this is a defensive no-op (CREATE IF NOT EXISTS)."""
    try:
        _usage_db().execute(
            "CREATE TABLE IF NOT EXISTS anomaly_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts REAL NOT NULL,"
            "severity TEXT NOT NULL,"
            "category TEXT NOT NULL,"
            "title TEXT,"
            "detail TEXT,"
            "alerted INTEGER DEFAULT 0,"
            "resolved INTEGER DEFAULT 0)")
        _usage_db().execute(
            "CREATE INDEX IF NOT EXISTS idx_anomaly_ts ON anomaly_events(ts)")
    except Exception:
        pass


def _log_anomaly(severity: str, category: str, title: str,
                 detail: str, key_name: str | None = None) -> None:
    """Insert one row into the shared anomaly_events table.

    Writes to the monitoring schema (severity/category/title/detail). ``detail``
    is stored as a JSON object so dashboards can parse key_name + extras.
    Swallows all errors.
    """
    try:
        _ensure_anomaly_table()
        payload: dict = {"detail": detail}
        if key_name is not None:
            payload["key_name"] = key_name
        _usage_db().execute(
            "INSERT INTO anomaly_events (ts, severity, category, title, detail) "
            "VALUES (?,?,?,?,?)",
            (time.time(), severity, category, title, json.dumps(payload)))
    except Exception:
        pass


def _is_key_healthy(name: str) -> bool:
    """Check if a z.ai key has quota remaining.

    Returns False immediately if a manual-disable flag file exists:
        ~/.hermes/bot/.key_disabled_<name>
    Re-enable by removing the file, e.g.:
        rm ~/.hermes/bot/.key_disabled_ours
    """
    # G2: Ollama paywall flag — skip round-trips while the largest plan is
    # quota-exhausted (403) until the Monday reset. Per-key since 2026-08-23
    # (ollama_cloud_2 has its own flag file).
    if name in _OLLAMA_PAYWALL_FLAGS and _ollama_paywall_active(name):
        return False
    # Manual disable via flag file — checked first, overrides everything.
    try:
        if (Path.home() / ".hermes" / "bot" / f".key_disabled_{name}").exists():
            _log_key_decision(chosen_key=None,
                              reason=f"manually_disabled_{name}")
            return False
    except Exception:
        pass

    # BUG B-h fix: oc3 monthly-budget delist gate (~90% monthly usage). oc3 is
    # a monthly-only key (no 5h session / weekly windows), so the existing
    # session/weekly health logic never trips for it — it would burn straight
    # to 100% with a ~30-day dead-time penalty. Delist at >= 90% monthly.
    # _get_ollama_quota_status reads the tracker directly (no `lock`), so this
    # cannot deadlock against _snapshot_quota's `lock`; fail-open on any error.
    if name == "ollama_cloud_3":
        try:
            _oc3_status = _get_ollama_quota_status("ollama_cloud_3")
            if float(_oc3_status.get("monthly_used_pct", 0.0)) >= 90.0:
                _log_key_decision(chosen_key=None,
                                  reason="oc3_monthly_delisted_90pct")
                return False
        except Exception:
            pass  # fail-open — never break routing on a tracker hiccup

    h = _zai_key_health.get(name)
    if not h or h.get("healthy", True):
        return True
    return time.time() >= h.get("retry_after", 0)


# ── Garbage circuit-breaker helpers (per-(provider, model), task t_b7725426) ─
# The breaker detects degenerate oversized completions per (provider, model)
# and demotes ONLY that pair from rotation for 24h. These helpers are the
# single source of truth; flat_router.select_provider() resolves
# _is_pair_garbage_demoted() to skip a demoted pair while the provider stays
# eligible for other models. All paths fail OPEN / never raise.

def _garbage_cb_key(name: str, model: str) -> tuple[str, str]:
    """Canonical composite key. Model is normalized lower/whitespace-stripped
    so dispatch-time names ('glm-5.3', 'glm-5.3 ' etc.) map consistently."""
    try:
        return (str(name or "").strip().lower(),
                str(model or "").strip().lower())
    except Exception:
        return (str(name or ""), str(model or ""))


def _garbage_cb_pair_demoted(name: str, model: str) -> bool:
    """True if THIS (provider, model) pair is currently demoted from rotation.

    Pure function of in-memory state + wall clock. Never raises — on any error
    it returns False (optimistic, never blocks routing). Sibling lanes on the
    same provider are unaffected: the check is keyed by the COMPOSITE pair.
    """
    try:
        if not _GARBAGE_CB_ENABLED:
            return False
        key = _garbage_cb_key(name, model)
        st = _garbage_cb.get(key)
        if not st or st.get("healthy", True):
            return False
        if time.time() < st.get("retry_after", 0):
            return True
        # Expired — clear so a future trip re-demotes + re-alerts.
        _garbage_cb.pop(key, None)
        _garbage_cb_alerted.pop(key, None)
        return False
    except Exception:
        return False


def _garbage_cb_alert(name: str, model: str, completion_tokens: int,
                      duration_ms: int | None, reason: str) -> None:
    """Fire exactly ONE alert per trip (mirrors the _key_alerted pattern).

    Uses _garbage_cb_alerted[(name, model)] to suppress repeat alerts while the
    pair is demoted; the flag is cleared on expiry in _garbage_cb_pair_demoted
    so a LATER trip re-alerts. Writes a CRITICAL anomaly + journald line.
    Never raises."""
    try:
        key = _garbage_cb_key(name, model)
        if _garbage_cb_alerted.get(key):
            return
        _garbage_cb_alerted[key] = True
        dur_s = (duration_ms or 0) / 1000.0
        tps = (completion_tokens / dur_s) if (dur_s and completion_tokens) else 0.0
        _text = (
            f"GARBAGE CIRCUIT BREAKER: demoted ({name}, {model}) for "
            f"{GARBAGE_CB_TTL_SECONDS/3600:.0f}h — {completion_tokens} "
            f"completion tokens (~{tps:.0f} tok/s), reason: {reason}"
        )
        _log_anomaly("CRITICAL", "GARBAGE_CIRCUIT_BREAKER",
                     f"({name}, {model}) demoted — garbage output",
                     _text, key_name=name)
        print(f"\n{'='*60}\n{_text}\n{'='*60}\n", flush=True)
    except Exception:
        pass


def _check_garbage_cb(name: str, model: str, completion_tokens: int,
                      duration_ms: int | None = None,
                      finish_reason: str | None = None) -> bool:
    """Detector + breaker: after a SUCCESSFUL completion, decide whether the
    (provider, model) pair is degenerate and demote it.

    Called on success (HTTP 200) when completion_tokens is known. Trips when:
      * completion_tokens > GARBAGE_MAX_COMPLETION_TOKENS (primary, ceiling)
      * GARBAGE_MAX_TOKENS_PER_SEC configured AND
        completion_tokens/duration exceeds it (pathological sustained rate)
    finish_reason is optional; when provided and != 'stop'/'length', it
    strengthens the ceiling trip (rep-token spam that never reaches a coherent
    stop). Returns True if the pair was (re)demoted this call, else False.

    Restart-safe + revert-safe: every path is wrapped; a DB/log/anomaly error
    never raises into the request handler. The breaker state is module-local
    and cold-starts empty (optimistic).
    """
    try:
        if not _GARBAGE_CB_ENABLED:
            return False
        completion_tokens = int(completion_tokens or 0)
        if completion_tokens <= 0:
            return False
        key = _garbage_cb_key(name, model)
        now = time.time()

        # Already demoted + in window → no-op (do not re-trip, do not re-alert).
        st = _garbage_cb.get(key)
        if st and not st.get("healthy", True) and now < st.get("retry_after", 0):
            return False

        # ── Decide whether THIS completion is degenerate ──
        tripped = False
        reasons: list[str] = []
        if completion_tokens > _GARBAGE_MAX_COMPLETION_TOKENS:
            tripped = True
            reasons.append(
                f"completion_tokens {completion_tokens} > "
                f"ceiling {_GARBAGE_MAX_COMPLETION_TOKENS}")
        _dur_ms = (duration_ms or 0)
        if _GARBAGE_MAX_TOKENS_PER_SEC > 0 and _dur_ms > 0:
            tps = (completion_tokens / 1000.0) / (_dur_ms / 1000.0)
            if tps > _GARBAGE_MAX_TOKENS_PER_SEC:
                tripped = True
                reasons.append(
                    f"{tps:.0f} tok/s > "
                    f"{_GARBAGE_MAX_TOKENS_PER_SEC:.0f} tok/s ceiling")
        if finish_reason is not None and \
                str(finish_reason).lower() not in ("stop", "length", ""):
            # Non-terminating finish (still ends) OR missing stop — a weak
            # signal on its own; only strengthens an already-tripped ceiling.
            if tripped:
                reasons.append(f"finish_reason={finish_reason!r} (not stop)")

        if not tripped:
            # Healthy completion — reset any expired/demoted state lazily and
            # clear a stale alert flag so a future trip can fire.
            _garbage_cb.pop(key, None)
            return False

        # ── Demote the pair for GARBAGE_CB_TTL_SECONDS ──
        retry_after = now + GARBAGE_CB_TTL_SECONDS
        _garbage_cb[key] = {
            "healthy": False,
            "retry_after": retry_after,
            "reason": "; ".join(reasons) or "garbage completion",
            "completion_tokens": completion_tokens,
            "demoted_at": now,
            "duration_ms": duration_ms,
            "finish_reason": finish_reason,
        }
        # Fire exactly one alert per trip (suppressed by _garbage_cb_alerted
        # while demoted; re-armed on expiry).
        _garbage_cb_alert(name, model, completion_tokens, duration_ms,
                          "; ".join(reasons))
        print(f"[garbage-cb] demoted ({name}, {model}) for "
              f"{GARBAGE_CB_TTL_SECONDS/3600:.0f}h — {'; '.join(reasons)}",
              flush=True)
        return True
    except Exception:
        return False


def _parse_opencode_reset_seconds(text: str | None) -> float | None:
    """Parse an upstream 'Resets in N days[/hours/minutes]' message → seconds.

    Used by the opencode_go 429 path so the backoff matches the REAL reset
    time (e.g. weekly cap → 'Resets in 3 days' → 259200s) instead of the
    flat exponential default. Returns None when nothing parseable is found.
    Never raises.
    """
    if not text:
        return None
    try:
        import re as _re
        m = _re.search(
            r"resets?\s+in\s+"
            r"((?:\d+\s*(?:days?|hours?|hrs?|h|minutes?|mins?|m|"
            r"seconds?|secs?|s)[,\s]*)+)",
            text, _re.IGNORECASE)
        if not m:
            return None
        total = 0.0
        found = False
        for num, unit in _re.findall(
                r"(\d+)\s*(days?|hours?|hrs?|h|minutes?|mins?|m|"
                r"seconds?|secs?|s)", m.group(1), _re.IGNORECASE):
            n = float(num)
            u = unit.lower()
            if u.startswith("day"):
                total += n * 86400.0
            elif u.startswith("hour") or u.startswith("hr") or u == "h":
                total += n * 3600.0
            elif u.startswith("min") or u == "m":
                total += n * 60.0
            else:  # seconds / secs / s
                total += n
            found = True
        if not found or total <= 0:
            return None
        return min(total, 14 * 86400.0)  # sanity cap: 14 days
    except Exception:
        return None


def _mark_key_failure(name: str, error_type: str = "exhausted",
                      retry_after_seconds: float | None = None) -> None:
    """Record one failure for *name* and arm the appropriate backoff window.

    error_type selects the backoff strategy (req 2 — dead-key detection):
      "exhausted" (429 / empty) → exponential ramp 2→60s (req 1)
      "dead"     (401/403)      → flat 1h (key likely revoked)
      "server"   (500/502/503/504) → flat 30s (transient upstream issue)

    retry_after_seconds, when provided (>0), OVERRIDES the computed backoff
    with the upstream-declared reset time (e.g. parsed from a 429
    "Resets in N days" body). Capped at 14 days.

    Bumps the consecutive-failure counter (reset on success), mirrors the new
    state to the ``key_health`` table (req 4), and logs backoff/KEY_DEAD
    anomalies for dashboards. Never raises — a logging failure must never break
    a proxied request."""
    try:
        now = time.time()
        prev = _zai_key_health.get(name, {})
        prev_healthy = prev.get("healthy", True)
        failures = int(prev.get("consecutive_failures", 0)) + 1

        # Track health transition: available → unavailable
        if prev_healthy:
            _key_down_since[name] = now
            _key_alerted.pop(name, None)  # new down period, allow re-alert
        if error_type == "dead":
            backoff = _DEAD_KEY_BACKOFF_SECONDS
        elif error_type in ("server", "dispatch_fail"):
            backoff = _SERVER_ERROR_BACKOFF_SECONDS
        else:  # "exhausted" — 429 / empty response
            backoff = _backoff_for_failure(failures)
        if retry_after_seconds is not None:
            try:
                _parsed_reset = float(retry_after_seconds)
                if _parsed_reset > 0:
                    backoff = min(_parsed_reset, 14 * 86400.0)
            except Exception:
                pass
        retry_after = now + backoff
        disabled = _is_manually_disabled(name)
        _zai_key_health[name] = {
            "healthy": False,
            "last_empty": now,
            "retry_after": retry_after,
            "consecutive_failures": failures,
            "backoff_seconds": backoff,
            "last_error_type": error_type,
            "last_failure_ts": now,
            "backoff_until": retry_after,
            "disabled_manually": disabled,
        }
        # Mirror per-key state to the key_health table (req 4 — circuit breaker
        # state: failure_count, last_failure_ts, last_error_type, backoff_until,
        # disabled_manually). _log_key_health is defined near the other _log_*
        # helpers below; forward reference is resolved at call time.
        _log_key_health(name, _zai_key_health[name])
        # Anomaly logging (dashboard visibility). _log_anomaly signature is
        # (severity, category, title, detail, key_name=None) — the logger
        # JSON-encodes detail+key_name into the shared anomaly_events table.
        _log_anomaly("WARN", "key_backoff",
                     f"{name} {error_type} failure #{failures}",
                     f"backoff {backoff}s; error_type={error_type}",
                     key_name=name)
        # A definitive auth failure means the key is dead right now — surface it
        # immediately rather than waiting for the N-failure threshold.
        if error_type == "dead":
            _log_anomaly("CRITICAL", "KEY_DEAD",
                         f"{name} marked dead (auth failure 401/403)",
                         f"backoff {_DEAD_KEY_BACKOFF_SECONDS}s; error_type=dead",
                         key_name=name)
        elif failures == _KEY_DEAD_THRESHOLD:
            if error_type == "exhausted":
                _log_anomaly("WARN", "QUOTA_EXHAUSTED",
                             f"{name} reached {failures} consecutive quota-exhaustion failures",
                             f"backoff {backoff}s; transient — will recover on window reset",
                             key_name=name)
            else:
                _log_anomaly("CRITICAL", "KEY_DEAD",
                             f"{name} reached {failures} consecutive failures",
                             f"backoff {backoff}s; likely dead",
                             key_name=name)
    except Exception:
        pass


def _mark_key_exhausted(name: str) -> None:
    """Backward-compat shim — record a quota-exhaustion failure (429 / empty).

    Preserved so existing call sites (and the ollama_cloud 429 path) keep
    working; routes into the circuit breaker with error_type='exhausted'."""
    _mark_key_failure(name, error_type="exhausted")


def _mark_key_dead(name: str) -> None:
    """Record an auth failure (401/403) — long flat backoff, key may be revoked."""
    _mark_key_failure(name, error_type="dead")


def _mark_key_server_error(name: str) -> None:
    """Record a 5xx upstream error — medium flat backoff (not the key's fault)."""
    _mark_key_failure(name, error_type="server")


def _mark_key_healthy(name: str) -> None:
    """Mark a key healthy (successful response) and reset its failure counter.

    Resets consecutive_failures to 0 so the next exhaustion starts fresh from
    the minimum backoff. A manually-disabled key is kept disabled even on a
    success — the flag file is the operator's explicit override and is not
    auto-cleared here (remove the file to re-enable). Mirrors the reset to the
    key_health table. Never raises."""
    try:
        disabled = _is_manually_disabled(name)
        prev = _zai_key_health.get(name, {})
        _zai_key_health[name] = {
            "healthy": not disabled,
            "consecutive_failures": 0,
            "last_error_type": None,
            "last_failure_ts": prev.get("last_failure_ts", 0),
            "backoff_until": 0 if not disabled else prev.get("backoff_until", 0),
            "backoff_seconds": 0,
            "disabled_manually": disabled,
            # legacy fields
            "last_empty": prev.get("last_empty", 0),
            "retry_after": 0 if not disabled else prev.get("retry_after", 0),
        }
        _log_key_health(name, _zai_key_health[name])
        # Track recovery transition: unavailable → available
        if name in _key_down_since:
            down_duration = time.time() - _key_down_since.pop(name)
            _key_alerted.pop(name, None)
            _log_anomaly("INFO", "KEY_RECOVERED",
                         f"{name} recovered after {down_duration/60:.0f}min",
                         f"down_since={time.ctime(_key_down_since.get(name, 0))}",
                         key_name=name)
    except Exception:
        pass


def _mark_unfunded(name: str) -> None:
    """Mark a provider as out of credits (after receiving 402)."""
    _provider_health[name] = {
        "funded": False,
        "last_402": time.time(),
        "retry_after": time.time() + _UNFUNDED_RETRY_SECONDS,
    }


def _mark_funded(name: str) -> None:
    """Mark a provider as funded again (successful response)."""
    _provider_health[name] = {"funded": True}


_measured_rate_cache: dict = {"rates": None, "ts": 0.0}


def _get_measured_rate(provider: str, model: str) -> float | None:
    """Return the latest measured $/M rate from the measured_rates table.

    Source: routstr_probe.py (daily 03:00 crontab) sends a fixed prompt
    through each Cashu provider, records the wallet balance delta, and
    computes sats/M → USD/M. Also seeded from published sats_pricing
    (verified against actual wallet burn). 5-min in-memory cache; returns
    None if no measurement <24h old, so _get_provider_cost falls through
    to catalog-based estimation.
    """
    if time.time() - _measured_rate_cache["ts"] < 300 and _measured_rate_cache["rates"] is not None:
        rates = _measured_rate_cache["rates"]
    else:
        try:
            c = sqlite3.connect(f"file:{USAGE_DB}?mode=ro", uri=True, timeout=3)
            c.row_factory = sqlite3.Row
            cutoff = time.time() - 86400
            rows = c.execute(
                "SELECT provider, model, usd_per_M FROM measured_rates "
                "WHERE measured_at > ? AND usd_per_M IS NOT NULL "
                "ORDER BY measured_at DESC", (cutoff,)).fetchall()
            c.close()
            rates = {}
            for r in rows:
                key = (r["provider"], r["model"])
                if key not in rates:
                    rates[key] = r["usd_per_M"]
            _measured_rate_cache["rates"] = rates
            _measured_rate_cache["ts"] = time.time()
        except Exception:
            return None
    return _measured_rate_cache["rates"].get((provider, model))


def _compute_model_burn_share(provider: str, model: str,
                               window_s: float = 3600.0) -> float:
    """Compute model's share of a provider's token burn in the last window.

    Returns a float in [0.0, 1.0]: what fraction of the provider's total
    tokens in the window came from this model. Used by the flat router's
    per-model pressure formula: when a provider's quota is depleting,
    models contributing the most burn get penalized first (pushed to
    alternative providers). Cached for _MODEL_BURN_CACHE_TTL seconds.
    """
    global _model_burn_cache, _model_burn_cache_ts
    try:
        now = time.time()
        if now - _model_burn_cache_ts < _MODEL_BURN_CACHE_TTL:
            prov_cache = _model_burn_cache.get(provider, {})
            return prov_cache.get(model, 0.0)

        # Query api_calls for per-model token counts in the window
        since = now - window_s
        rows = _usage_db().execute(
            "SELECT model, SUM(total_tokens) as tokens "
            "FROM api_calls WHERE key_name=? AND ts>? AND total_tokens>0 "
            "GROUP BY model", (provider, since)).fetchall()
        total = sum(r[1] for r in rows) if rows else 0
        prov_cache = {}
        if total > 0:
            for r in rows:
                prov_cache[r[0]] = r[1] / total
        _model_burn_cache[provider] = prov_cache
        _model_burn_cache_ts = now
        return prov_cache.get(model, 0.0)
    except Exception:
        return 0.0


def _opencode_go_quota_fraction() -> float:
    """Return Go allowance remaining as a fraction [0.0, 1.0].
    1.0 = full allowance, 0.0 = depleted (pay-per-use mode)."""
    try:
        rem = _opencode_go_allowance.get("remaining_usd")
        if rem is None:
            return 1.0  # unknown → optimistic
        return max(0.0, min(1.0, rem / _OPENCODE_GO_INITIAL_ALLOWANCE))
    except Exception:
        return 1.0


def _get_provider_cost(name: str, model_id: str) -> float:
    """Look up the combined cost per 1M tokens for a model on a provider.
    Resolution: per-model rate tables → model_matrix.json → real_price_tracker → PPQ_PRICING.
    Returns 999.0 if unknown."""
    # 1. Per-model rate tables (accurate, verified from provider APIs)
    provider_model = _MODEL_ID_TO_PROVIDER_ID.get(model_id, {}).get(name)
    if provider_model:
        rates = None
        if name == "telnyx":
            rates = _TELNYX_MODEL_RATES.get(provider_model)
            if rates:
                return _telnyx_cache_aware_blended_rate(rates)
        elif name == "openrouter":
            rates = _OPENROUTER_MODEL_RATES.get(provider_model)
            if rates:
                return _blended_rate(rates["input"], rates["output"])
        elif name == "ppq":
            rates = _PPQ_MODEL_RATES.get(provider_model)
            if rates:
                return _blended_rate(rates["input"], rates["output"])
        elif name == "neuralwatt":
            rates = NEURALWATT_RATES.get(provider_model) or NEURALWATT_RATES.get(model_id)
            if rates:
                return _blended_rate(rates["input"], rates["output"])
    # 1b. Routstr/routstrd: check MEASURED rates first (ground truth via
    # wallet balance deltas and published sats_pricing, stored in
    # measured_rates table by routstr_probe.py daily at 03:00).
    # Falls through to catalog fetch if no measurement <24h old.
    if name in ("routstr", "routstrd"):
        measured = _get_measured_rate(name, model_id)
        if measured is not None:
            return measured
    if name == "routstr":
        rate = _get_routstr_rates().get(model_id)
        if rate is not None:
            return rate
    elif name == "routstrd":
        rate = _get_routstrd_rates().get(model_id)
        if rate is not None:
            return rate
    # 2. Try model_matrix.json (live pricing)
    try:
        matrix_path = BOT / "model_matrix.json"
        if matrix_path.exists():
            import json as _json
            matrix = _json.loads(matrix_path.read_text())
            key = f"{name}/{model_id}"
            entry = matrix.get("models", {}).get(key, {})
            if entry:
                keys = entry.get("keys", {})
                for k in keys.values():
                    return k.get("cost_per_1m_offpeak", k.get("cost_per_1m_combined", 999.0))
    except Exception:
        pass
    # 3. Try real_price_tracker (measured rates)
    if _rpt_get_rate is not None:
        try:
            tracked = _rpt_get_rate(name, model_id)
            if tracked is not None and tracked < 999.0:
                return tracked
        except Exception:
            pass
    # 4. Last-resort fallback to known pricing
    from model_matrix import PPQ_PRICING
    pricing = PPQ_PRICING.get(model_id, PPQ_PRICING.get(model_id.lower(), (0.14, 0.28)))
    return pricing[0] + pricing[1]

# Model tier map: tier name → z.ai model name (cheapest first).
# The X-Model-Tier request header selects one of these tiers to rewrite the
# model field in the proxied request body.  Absent header = no rewrite.
MODEL_TIER_MAP: dict[str, str] = {
    "flash": "glm-4.5-flash",
    "air":   "glm-4.5-air",
    "mid":   "glm-4.5",
    "heavy": "glm-5.3",
}

# ── usage logging DB (separate from response_cache.db) ──────────────────────
USAGE_DB = Path.home() / ".hermes" / "bot" / "zai_usage.db"
_usage_db_conn: sqlite3.Connection | None = None
_usage_db_lock = threading.Lock()

# ── CG-2 price-exposure state (written by collect_price_observations.py) ─────
# JSON {generated_ts, kalman_verdict, capacity_estimates}. Read by
# _build_v1_pricing() for the /v1/pricing endpoint's convergence gate and
# per-key smoothed capacity denominators. Absent file → "unverified" gate.
_PRICING_EXPOSURE_STATE = str(
    Path.home() / ".hermes" / "bot" / ".pricing_exposure_state.json"
)

# ── CG-2 provider config (z.ai fees + entitlement). Points at the MRE repo's
# providers.yaml; overridden in tests. Used by _build_v1_pricing().
PROVIDERS_YAML = str(Path.home() / "merchant-routing-engine" / "config" / "providers.yaml")

quota_cache: dict[str, tuple[list[dict], float]] = {}   # name → (windows, ts)

# ── Phase 2.4: Pace windows for LiveRouter ──────────────────────────────────
# Computed in _refresh_loop() from quota_cache + LiveRouter's ConsumptionKalman
# burn rates. Stored here so best_key() can pass them to select_failover() on
# the next failover call. Thread-safe reads via `lock`.
_pace_windows: dict[str, list[tuple[float, float, float, float, float]]] = {}
lock = threading.Lock()

# ── Shadow mode snapshot helpers ────────────────────────────────────────────
def _ppq_quota_snapshot() -> dict:
    """quota_state['ppq'] from the latest collected PPQ credit balance (P3-PPQ).

    Delegates to the extracted ``ppq_quota_entry`` (reads provider_balances in
    api_burn.db). Cold-start contract: no/stale row → ``{}`` (passes through)
    so LiveRouter's ``_compute_ppq_pressure`` applies conservative
    ``cold_start_pressure`` (Task 4) instead of the old optimistic 1.0 — a PPQ
    endpoint we have no fresh data for must not look artificially cheap.
    Only falls back to ``{'used_pct': 0.0}`` when the bridge import is disabled
    or raises. Never raises.
    """
    if _ppq_quota_entry_fn is None:
        return {"used_pct": 0.0, "remaining": float("inf")}
    try:
        entry = _ppq_quota_entry_fn()
        # Pass {} (cold-start marker) through unchanged; only fall back on a
        # genuinely bad (non-dict) return.
        return entry if isinstance(entry, dict) else {
            "used_pct": 0.0, "remaining": float("inf"),
        }
    except Exception:
        return {"used_pct": 0.0, "remaining": float("inf")}


def _openrouter_quota_entry_snapshot() -> dict:
    """quota_state['openrouter'] from the latest collected balance (T1T3).

    Mirrors _ppq_quota_snapshot: delegates to the extracted
    ``openrouter_quota_entry`` (reads provider_balances in api_burn.db). Returns
    the cold-start ``{}`` marker when there is no/stale row, which the proxy
    maps to the optimistic ``{used_pct:0.0, remaining:inf}`` below. Never raises.
    """
    if _openrouter_quota_entry_fn is None:
        return {"used_pct": 0.0, "remaining": float("inf")}
    try:
        entry = _openrouter_quota_entry_fn()
        return entry if isinstance(entry, dict) else {
            "used_pct": 0.0, "remaining": float("inf"),
        }
    except Exception:
        return {"used_pct": 0.0, "remaining": float("inf")}


def _telnyx_quota_snapshot() -> dict:
    """quota_state['telnyx'] from the latest collected balance (TELNYX-3.2).

    Mirrors _ppq_quota_snapshot / _openrouter_quota_entry_snapshot: delegates
    to the extracted ``telnyx_quota_entry`` (reads provider_balances in
    api_burn.db). Returns the cold-start ``{}`` marker when there is no/stale
    row, which the proxy maps to the optimistic ``{used_pct:0.0, remaining:inf}``
    below. Never raises.

    Fallback: when the balance_collectors bridge is disabled, queries the
    Telnyx balance API directly (https://api.telnyx.com/v2/balance) and
    derives used_pct from the starting balance.
    """
    if _telnyx_quota_entry_fn is None:
        # Direct API fallback — query Telnyx balance endpoint
        balance = _get_telnyx_balance()
        if balance is not None and TELNYX_STARTING_BALANCE > 0:
            used_pct = max(0.0, (1.0 - balance / TELNYX_STARTING_BALANCE) * 100.0)
            return {
                "used_pct": round(used_pct, 2),
                "remaining": balance,
                "total": TELNYX_STARTING_BALANCE,
            }
        return {"used_pct": 0.0, "remaining": float("inf")}
    try:
        entry = _telnyx_quota_entry_fn()
        return entry if isinstance(entry, dict) else {
            "used_pct": 0.0, "remaining": float("inf"),
        }
    except Exception:
        return {"used_pct": 0.0, "remaining": float("inf")}


def _routstr_quota_snapshot() -> dict:
    """quota_state['routstr'] from the balance bridge (provider_balances).

    Cold-start / bridge-disabled / stale row → optimistic fallback so
    routing never breaks. Never raises.
    """
    if _routstr_quota_entry_fn is None:
        return {"used_pct": 0.0, "remaining": float("inf")}
    try:
        entry = _routstr_quota_entry_fn()
        return entry if isinstance(entry, dict) else {
            "used_pct": 0.0, "remaining": float("inf"),
        }
    except Exception:
        return {"used_pct": 0.0, "remaining": float("inf")}


def _neuralwatt_quota_snapshot() -> dict:
    """quota_state['neuralwatt'] from the real /v1/quota API (NW-API).

    Mirrors _ppq_quota_snapshot / _routstr_quota_snapshot: delegates to the
    extracted ``neuralwatt_quota_entry`` (reads the newest 'neuralwatt' row
    from provider_balances in api_burn.db, written by the
    balance_collectors --provider neuralwatt cron).

    The collector itself calls GET https://api.neuralwatt.com/v1/quota for the
    real kWh allowance (kwh_used / kwh_included) and GET
    https://api.neuralwatt.com/v1/usage/summary for today's real USD spend
    via /v1/usage/summary.time_series[today].cost_usd. The shared daily-cap
    guardrail is NEURALWATT_DAILY_CAP (default $10/day). When
    ``is_daily_cap_exceeded`` is True on the returned entry, we bump
    ``used_pct`` to 100.0 so the routing layer treats neuralwatt as
    exhausted for the rest of the UTC calendar day. is_exhausted
    (kwh_remaining <= 0 or in_overage) is preserved as-is.

    Cold-start contract (matches the proxy's current hardcoded fallback):
      * bridge import disabled, OR no fresh row →
        ``{used_pct:0.0, remaining:inf}`` (current optimistic behavior).
      * fresh row → forwards the collector's dict, but if the daily cap is
        exceeded we override used_pct=100 so routing drops neuralwatt today.

    Credit-mode override (NW-API 2026-09-02, B1): the kWh subscription
    allowance can be EXHAUSTED while a pay-as-you-go CREDIT balance remains
    (user top-up). The kWh pool drives is_exhausted/used_pct, but with
    credits > 0 NeuralWatt keeps serving on credit — ADR-004 "Phase B
    overage" is real, it bills the credit balance. When that happens we
    treat CREDITS as the binding budget (is_exhausted=False, regime
    "credit", used_pct from the credit fraction) so the router keeps using
    neuralwatt as a paid rung with scarcity pricing protecting the balance.

    Revert-safe: on any failure returns the cold-start fallback dict so
    routing never breaks. Never raises.
    """
    if _neuralwatt_quota_entry_fn is None:
        return {"used_pct": 0.0, "remaining": float("inf")}
    try:
        entry = _neuralwatt_quota_entry_fn()
        if not isinstance(entry, dict):
            return {"used_pct": 0.0, "remaining": float("inf")}
        entry = dict(entry)  # copy so we don't mutate the collector's dict
        # Credit-mode override: kWh exhausted but credits remain → funded.
        remaining_credits = float(entry.get("remaining_usd", 0.0) or 0.0)
        total_credits = float(entry.get("total_credits_usd", 0.0) or 0.0)
        if remaining_credits > 0.0 and entry.get("is_exhausted"):
            used_frac = 0.0
            if total_credits > 0.0:
                used_frac = max(
                    0.0, min(1.0, 1.0 - remaining_credits / total_credits)
                )
            entry["is_exhausted"] = False
            entry["regime"] = "credit"
            entry["used_pct"] = round(used_frac * 100.0, 2)
            entry["remaining"] = remaining_credits
            entry["total"] = total_credits
        # Daily-cap enforcement: when today's spend exceeds the cap, signal
        # exhaustion to the router by clamping used_pct to 100.0. This is the
        # primary runaway-burn guardrail (prevents another $258-in-one-day).
        if entry.get("is_daily_cap_exceeded"):
            entry["used_pct"] = 100.0
            entry["regime"] = "daily-capped"
        return entry
    except Exception:
        return {"used_pct": 0.0, "remaining": float("inf")}


def _snapshot_quota() -> dict:
    """Snapshot current quota state for all providers. Thread-safe."""
    snap = {}
    try:
        with lock:
            for name in ("ours", "friend"):
                wins = quota_cache.get(name, ([], 0.0))[0]
                pct = _max_pct(wins)
                snap[name] = {
                    "used_pct": float(pct),
                    "remaining": max(0.0, 2_000_000 * (1.0 - pct / 100.0)),
                    "total": 2_000_000,
                }
        # Ollama Cloud — real quota from ollama_quota_tracker (EUv2-5)
        # Per-key since 2026-08-23: each subscription gets its own snapshot.
        # oc/oc2 = session + weekly windows; oc3 (stoic_herschel_499) = MONTHLY
        # budget pool only (no 5h sessions) — used_pct driven by the monthly
        # usage fraction from /api/usage, with a ~90% scarcity guard.
        for _oc_key in ("ollama_cloud", "ollama_cloud_2", "ollama_cloud_3"):
            oc_status = _get_ollama_quota_status(_oc_key)
            if _oc_key == "ollama_cloud_3":
                oc_used_pct = float(oc_status.get("monthly_used_pct", 0.0))
                # Monthly pools: no partial-window resets, so guard ~90% used so
                # scarcity pricing kicks in before the ~30-day dead-time.
                # BUG A fix: remaining is driven by the monthly BUDGET limit
                # (not the used monthly_tokens count) so oc3's unused monthly
                # capacity is correctly surfaced as large remaining.
                oc_total = oc_status.get("monthly_limit", 0) or _OC_MONTHLY_LIMIT
                oc_regime = oc_status.get("regime", "included")
            else:
                oc_used_pct = max(oc_status["session_used_pct"], oc_status["weekly_used_pct"])
                oc_total = _OC_SESSION_LIMIT
                oc_regime = oc_status["regime"]
            # G2: while the 403-paywall flag is fresh, force used_pct=100 so
            # quota_pressure sends the effective price to +inf (the paywall has
            # no extra-usage path) — the router avoids Ollama on PRICE, not just
            # health. Local token counting can't see the server-side window.
            if _ollama_paywall_active(_oc_key):
                oc_used_pct = 100.0
                oc_regime = "paywalled"
            oc_remaining = max(0.0, oc_total * (1.0 - oc_used_pct / 100.0)) if oc_total else float("inf")
            snap[_oc_key] = {
                "used_pct": float(oc_used_pct),
                "remaining": oc_remaining,
                "total": oc_total,
                "regime": oc_regime,
                "session_used_pct": oc_status.get("session_used_pct", 0.0),
                "weekly_used_pct": oc_status.get("weekly_used_pct", 0.0),
                "monthly_used_pct": oc_status.get("monthly_used_pct", 0.0),
                "session_tokens": oc_status.get("session_tokens", 0),
                "weekly_tokens": oc_status.get("weekly_tokens", 0),
                "monthly_tokens": oc_status.get("monthly_tokens", 0),
            }
        # Per-token providers — effectively unlimited
        snap["opencode_go"] = {
            "used_pct": 0.0,
            "remaining": float("inf"),
            "total": float("inf"),
            "regime": "included",
        }
        # NW-API: real /v1/quota (energy allowance + lifetime cost) via the
        # neuralwatt_quota_entry bridge. Falls back to "{used_pct:0.0,
        # remaining:inf}" when the bridge is disabled / no fresh row (current
        # pre-bridge behavior) so routing never breaks.
        snap["neuralwatt"] = _neuralwatt_quota_snapshot()
        snap["ppq"] = _ppq_quota_snapshot()  # P3-PPQ: real credit balance
        snap["openrouter"] = _openrouter_quota_entry_snapshot()  # T1T3: real credit balance
        snap["telnyx"] = _telnyx_quota_snapshot()  # TELNYX-3.2: real balance
        snap["routstr"] = _routstr_quota_snapshot()  # VPS2 node sats balance
        snap["routstrd"] = _routstrd_balance_snapshot()  # local daemon, 420s cache
    except Exception:
        pass
    return snap

# ── Ollama pool-weighted key ordering (2026-09-02, plan B1) ──────────────────
# Orders _OLLAMA_CLOUD_KEYS by REMAINING quota (most remaining first) so the
# dispatcher equalizes burn across subscriptions. Falls back to the static
# registration order on any error. Oc3's monthly scarcity gate and per-key
# disable flags still apply per attempt inside _try_ollama_cloud — ordering
# only decides WHO is tried first, never bypasses protection.
_ollama_order_cache: dict = {"order": None, "ts": 0.0}
_OLLAMA_ORDER_TTL = 120.0  # seconds — quota snapshots are not free


def _ollama_cloud_key_order() -> list[tuple[str, str]]:
    """Return (key_name, api_key) pairs ordered by remaining quota (desc).

    Uses a TTL-cached slice of _snapshot_quota() for the three ollama keys.
    Unknown remaining (inf / missing data) sorts LAST (conservative), and
    the static registration order breaks ties.
    """
    now = time.time()
    cached = _ollama_order_cache.get("order")
    if cached and now - _ollama_order_cache["ts"] < _OLLAMA_ORDER_TTL:
        return cached
    base = list(_OLLAMA_CLOUD_KEYS)  # (name, key) pairs, static registration order
    order = base
    try:
        rem: dict[str, float] = {}
        for name, _key in base:
            status = _get_ollama_quota_status(name)
            if name == "ollama_cloud_3":
                used_pct = float(status.get("monthly_used_pct", 0.0))
                # BUG A fix: remaining = monthly BUDGET × (1 − pct), not the
                # used monthly_tokens count (which made oc3 remaining ≈ 0 and
                # sank it last, defeating B1's promote-unused-capacity intent).
                total = status.get("monthly_limit", 0) or _OC_MONTHLY_LIMIT
            else:
                used_pct = max(float(status.get("session_used_pct", 0.0)),
                               float(status.get("weekly_used_pct", 0.0)))
                total = _OC_SESSION_LIMIT
            if _ollama_paywall_active(name):
                used_pct = 100.0  # paywalled → zero remaining → sinks last
            rem[name] = (total * (1.0 - used_pct / 100.0)) if total else 0.0
        base_pos = {name: i for i, (name, _k) in enumerate(base)}
        order = sorted(
            base,
            key=lambda nk: (-rem.get(nk[0], 0.0), base_pos[nk[0]]))
    except Exception:
        order = base
    _ollama_order_cache.update({"order": order, "ts": now})
    return order

def _snapshot_health() -> dict:
    """Snapshot health state for all providers. Thread-safe read."""
    h = {}
    try:
        for name in ("ours", "friend"):
            h[name] = _is_key_healthy(name)
        h["ollama_cloud"] = _is_key_healthy("ollama_cloud")
        h["ollama_cloud_2"] = _is_key_healthy("ollama_cloud_2")
        h["ollama_cloud_3"] = _is_key_healthy("ollama_cloud_3")
        h["opencode_go"] = _is_key_healthy("opencode_go")
        # NW-API: daily-cap guardrail (real /v1/usage/summary).
        # When today's REAL spend from /v1/usage/summary exceeds
        # NEURALWATT_DAILY_CAP (default $10/day) we mark the key unhealthy
        # so the router drops neuralwatt until UTC midnight. The previous
        # guardrail summed cost_usd from zai_usage.db which overcounts by
        # ~5.7× (NeuralWatt uses ENERGY-based pricing with 94% prompt-cache
        # hits — $258 in the DB vs $45 in real spend on 2026-08-22-to-23).
        # The real-API bridge eliminates that overcount entirely.
        if _neuralwatt_quota_entry_fn is not None:
            try:
                nw_entry = _neuralwatt_quota_entry_fn()
                h["neuralwatt"] = not bool(
                    isinstance(nw_entry, dict)
                    and nw_entry.get("is_daily_cap_exceeded")
                )
            except Exception:
                h["neuralwatt"] = True  # bridge hiccup → don't kill routing
        else:
            h["neuralwatt"] = True  # bridge disabled → per-token, always healthy unless 401/403
        h["ppq"] = _is_key_healthy("ppq")
        h["openrouter"] = True
        h["telnyx"] = _is_key_healthy("telnyx")
        h["routstr"] = _is_key_healthy("routstr")
        # DAILY SUB-CAP for routstrd (2026-09-02, plan B3): routstrd is the
        # cheapest METERED provider ($0.53/M measured), so during ollama
        # burst-flaps it became the default overflow catch-basin — $47.67 of
        # real Cashu over 7 days (2026-08-26→09-02). This doesn't distort the
        # cost ordering (routstrd stays cheapest-metered when healthy); it just
        # self-demotes the key for the rest of the UTC day once it has burned
        # ROUTSTRD_DAILY_CAP of real cash, mirroring the neuralwatt daily-cap
        # guardrail pattern. The ollama pool rebalance (B1) upstream makes
        # hitting this cap rare.
        h["routstrd"] = _is_key_healthy("routstrd") and not _routstrd_daily_cap_tripped()
    except Exception:
        pass
    return h


def _routstrd_daily_cap_tripped(cap: float | None = None) -> bool:
    """True when today's routstrd metered spend exceeded its sub-cap.

    env ROUTSTRD_DAILY_CAP (USD/day, default 10.0). Reads the daily_spend
    tier row (UTC day). Never raises — on any DB error returns False so
    routing is never broken by the guard.
    """
    try:
        _cap = float(cap if cap is not None
                     else os.environ.get("ROUTSTRD_DAILY_CAP", "10.0"))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = _usage_db().execute(
            "SELECT spend_usd FROM daily_spend WHERE date=? AND tier='routstrd'",
            (today,)).fetchone()
        return bool(row and row[0] and float(row[0]) >= _cap)
    except Exception:
        return False


def _snapshot_failures() -> dict[str, int]:
    """Build failure_counts dict from _zai_key_health for LiveRouter.

    Extracts ``consecutive_failures`` per key so the router can apply
    graduated health pricing (failed keys are penalised).  Keys with
    no failures or no health entry are excluded (zero-fill happens
    inside LiveRouter's pricing engine).
    """
    f = {}
    try:
        for name, health in _zai_key_health.items():
            fc = health.get("consecutive_failures", 0)
            if fc > 0:
                f[name] = fc
    except Exception:
        pass
    return f


def _tier_to_task_type(tier_hint: str) -> str:
    """Map an X-Model-Tier header value to a LiveRouter task_type string.

    Tier values from the client (flash/air/mid/heavy) map to task
    difficulty categories the router uses for model selection:

        * heavy     → ``\"coding\"``    (most capable, highest cost ceiling)
        * mid       → ``\"reasoning\"`` (balanced, mid-cost)
        * air/flash → ``\"simple\"``    (cheapest viable tier)
        * absent/unknown → ``\"coding\"`` (default — no downgrade assumed)

    The router only uses task_type for per-model pricing and model
    mapping (model_mapping.get_model).  glm-5.3 is served verbatim on
    failover providers (Ollama Cloud since 2026-08-29) — it is no longer
    substituted to glm-5.2.
    """
    if tier_hint in ("heavy", "high"):
        return "coding"
    elif tier_hint in ("mid", "medium"):
        return "reasoning"
    elif tier_hint in ("air", "flash", "low"):
        return "simple"
    return "coding"


# ── proactive burn-rate prediction (Phase 3) ─────────────────────────────────
# Import the burn predictor.  Wrapped so a broken burn_predictor.py never crashes
# the proxy — if the import fails, proactive switching is silently disabled and
# the proxy falls back to reactive (lock-based) key selection.
_predict_exhaustion = None
_route_request = None
try:
    sys.path.insert(0, os.path.dirname(__file__))
    from burn_predictor import predict_exhaustion as _predict_exhaustion
    from burn_predictor import route_request as _route_request
except Exception:
    pass

# ── shadow-mode decision tap (Phase 2.1, ADR-014) ────────────────────────────
# READ-ONLY tap: logs what the price-first RoutingOptimizer WOULD have chosen
# alongside the live best_key() pick, so the two strategies can be compared
# after a soak period. NEVER affects routing — every failure is swallowed and
# `_shadow_logger`/`_shadow_optimizer` stay None on any import error, leaving
# production routing 100% unchanged.
#
# NOTE on deviation from the task body template: the body called
# `RoutingOptimizer(config_path=...)` and `log_decision(live_key=...,
# shadow_decision=...)`, but those signatures DO NOT EXIST. RoutingOptimizer
# has no config loader (providers are registered via add_provider, each backed
# by a PriceKalman + ConsumptionKalman), and ShadowLogger.log_decision takes
# positional fields (ts, live_provider, live_model, shadow_provider,
# shadow_model, shadow_cost, tokens, reason, live_cost). Pasting the body
# verbatim would raise TypeError on import and silently disable the tap forever
# (the TEST row-count check would never increase). The construction below
# mirrors tests/test_integration.py::_three_provider_optimizer and the topology
# in config/providers.yaml; the tap below maps route()'s return dict to
# log_decision()'s real signature. Static seeded Kalman rates are used because
# no config->optimizer loader exists yet (out of scope for a read-only tap).
_shadow_logger = None
_shadow_optimizer = None
try:
    _SHADOW_REPO = '/home/c03rad0r/merchant-routing-engine'
    if _SHADOW_REPO not in sys.path:
        sys.path.insert(0, _SHADOW_REPO)
    from src.shadow_logger import ShadowLogger as _ShadowLogger
    from src.routing_optimizer import RoutingOptimizer as _RoutingOptimizer
    from src.price_kalman import PriceKalman as _ShadowPriceKalman
    from src.consumption_kalman import ConsumptionKalman as _ShadowConsumptionKalman

    def _shadow_pk(rate):
        kf = _ShadowPriceKalman(initial_rate=rate, process_noise=1e-6,
                                measurement_noise=1e-4)
        kf.update(rate)
        return kf

    _shadow_optimizer = _RoutingOptimizer(peak_hours_utc=(6, 10), peak_mult=3.0)
    # S3b (t_872743b5, 2026-08-17): zai_ours REMOVED from the shadow set —
    # the 'ours' z.ai key was disabled Aug 15 and retired permanently per
    # Felix (friend-only policy, never re-add). It was never health-gated
    # in this tap, so it kept winning the shadow comparison and polluting
    # routing_shadow_decisions with un-actionable 'ours' proposals
    # (~4.8k disagreeing rows/24h). Live key handling is untouched.
    # zai_friend — derived +21% premium over the historical ours rate
    # (ADR-005: ours 0.068 → friend 0.068 * 1.21), high tier
    _shadow_optimizer.add_provider(
        "zai_friend", _shadow_pk(0.068 * 1.21), _ShadowConsumptionKalman(),
        quota_remaining=1_000_000, model_tier="high", quota_total=2_000_000,
        peak_hours_utc=(6, 10), peak_mult=3.0,
    )
    # ollama_cloud — T4 included subscription: marginal cost $0, $0.001/M floor.
    # NO peak window. Initial rate = MIN_EFFECTIVE_PRICE (not $0.40 which was
    # the old per-token estimate). The Kalman filter will converge to the
    # measured rate ($0.0155/M) from real traffic.
    _shadow_optimizer.add_provider(
        "ollama_cloud", _shadow_pk(0.001), _ShadowConsumptionKalman(),
        quota_remaining=500_000, model_tier="standard", quota_total=1_000_000,
    )
    # ollama_cloud_2 — second T4 included subscription (2026-08-23)
    _shadow_optimizer.add_provider(
        "ollama_cloud_2", _shadow_pk(0.001), _ShadowConsumptionKalman(),
        quota_remaining=500_000, model_tier="standard", quota_total=1_000_000,
    )
    # ollama_cloud_3 (stoic_herschel_499) — monthly-budget pool, T4-included.
    # qu Total displayed as session-width for the legacy shadow path only.
    _shadow_optimizer.add_provider(
        "ollama_cloud_3", _shadow_pk(0.001), _ShadowConsumptionKalman(),
        quota_remaining=500_000, model_tier="standard", quota_total=1_000_000,
    )
    # opencode_go — T3 flat-rate $10/mo subscription: marginal cost $0,
    # $0.001/M floor. Native glm-5.3. Initial rate = MIN_EFFECTIVE_PRICE.
    _shadow_optimizer.add_provider(
        "opencode_go", _shadow_pk(0.001), _ShadowConsumptionKalman(),
        quota_remaining=500_000, model_tier="standard", quota_total=1_000_000,
    )
    # neuralwatt — per-token, standard tier (glm-5.2 primary model ~$2.21/M blended)
    _shadow_optimizer.add_provider(
        "neuralwatt", _shadow_pk(2.21), _ShadowConsumptionKalman(),
        quota_remaining=float("inf"), model_tier="standard", quota_total=float("inf"),
    )
    # ppq_external — per-token, low tier, most expensive, last resort
    _shadow_optimizer.add_provider(
        "ppq_external", _shadow_pk(0.80), _ShadowConsumptionKalman(),
        quota_remaining=10_000_000, model_tier="low", quota_total=20_000_000,
    )
    # deepinfra — per-token, low tier (same models as PPQ), preferred external
    # due to prompt-caching discounts. No peak window.
    try:
        import sys as _sys
        _sys.path.insert(0, '/home/c03rad0r/.hermes/bot')
        from zai_proxy import _get_deepinfra_balance as _gdb
        _di_balance = _gdb() * 1_000_000  # USD → token-equiv at $1.30/M
    except Exception:
        _di_balance = 5.0 * 1_000_000
    _shadow_optimizer.add_provider(
        "deepinfra", _shadow_pk(1.30), _ShadowConsumptionKalman(),
        quota_remaining=_di_balance, model_tier="low",
        quota_total=DEEPINFRA_STARTING_BALANCE * 1_000_000,
    )
    # telnyx — per-token, low tier (expensive per-token), last resort.
    # Seed rate: 5.40/M = blended kimi-k3 cost: (2.70*3 + 13.50*1) / 4
    _shadow_optimizer.add_provider(
        "telnyx", _shadow_pk(5.40), _ShadowConsumptionKalman(),
        quota_remaining=TELNYX_STARTING_BALANCE * 1_000_000,
        model_tier="low",
        quota_total=TELNYX_STARTING_BALANCE * 1_000_000,
    )
    # ── Flat router Phase 1: add missing providers to the shadow optimizer ──
    # openrouter — per-token, low tier, rates vary by model (~$1.50/M est)
    _shadow_optimizer.add_provider(
        "openrouter", _shadow_pk(1.50), _ShadowConsumptionKalman(),
        quota_remaining=10_000_000, model_tier="low",
        quota_total=20_000_000,
    )
    # routstr — Cashu-metered, same model IDs as proxy (~$1.00/M est)
    _shadow_optimizer.add_provider(
        "routstr", _shadow_pk(1.00), _ShadowConsumptionKalman(),
        quota_remaining=5_000_000, model_tier="low",
        quota_total=10_000_000,
    )
    # routstrd — Cashu-metered, buys from cheapest network node (~$1.00/M est)
    _shadow_optimizer.add_provider(
        "routstrd", _shadow_pk(1.00), _ShadowConsumptionKalman(),
        quota_remaining=5_000_000, model_tier="low",
        quota_total=10_000_000,
    )
    # Re-add zai_ours if the key is present in KEYS dict
    if "ours" in KEYS:
        _shadow_optimizer.add_provider(
            "zai_ours", _shadow_pk(0.068), _ShadowConsumptionKalman(),
            quota_remaining=1_000_000, model_tier="high", quota_total=2_000_000,
            peak_hours_utc=(6, 10), peak_mult=3.0,
        )
    # Defaults to ~/.hermes/bot/zai_usage.db (config/providers.yaml :: shadow_mode.db_path)
    _shadow_logger = _ShadowLogger()
except Exception:
    _shadow_logger = None
    _shadow_optimizer = None

# ── Phase 2.2: Routing Advisor (optimizer-first, hot-swappable) ──────────────
# Wraps the shadow optimizer + best_key() into the RoutingAdvisor decision
# layer (src/routing_advisor.py). This is the half-step between shadow mode
# (log only) and primary mode (replace best_key entirely): when the feature
# flag is OFF, best_key() is used exactly as before — zero behaviour change.
# When ON, the optimizer is consulted FIRST and best_key() is the fallback on
# any failure. The advisor NEVER raises — every failure degrades to best_key().
#
# Hot-swap toggle (no restart needed — checked per request):
#   touch ~/.hermes/bot/.optimizer_advisor_mode   → ENABLE
#   rm    ~/.hermes/bot/.optimizer_advisor_mode   → DISABLE
# The ROUTING_ADVISOR_ENABLED env var (1/true/yes/on) is honoured too.
_routing_advisor = None
_ADVISOR_FLAG = os.path.expanduser("~/.hermes/bot/.optimizer_advisor_mode")
try:
    if _shadow_optimizer is not None:
        from src.routing_advisor import (
            RoutingAdvisor as _RoutingAdvisorCls,
            AdvisorDecision as _AdvisorDecision,
        )

        def _best_key_adapter():
            """Adapt the production best_key() (returns str|None) to the
            AdvisorDecision contract the advisor expects. Resolved at call
            time so best_key() (defined later in this module) is in scope."""
            _k = best_key()
            _prov = ("ours" if _k == "ours"
                     else "friend" if _k == "friend"
                     else "fallback")
            return _AdvisorDecision(provider=_prov, model="", key=_k,
                                    source="best_key")

        class _ProxyRoutingAdvisor(_RoutingAdvisorCls):
            """RoutingAdvisor that ALSO honours the .optimizer_advisor_mode
            file marker, so operators can hot-swap without touching env vars."""
            def enabled(self):
                if os.path.exists(_ADVISOR_FLAG):
                    return True
                return super().enabled()

        _routing_advisor = _ProxyRoutingAdvisor(
            _shadow_optimizer, _best_key_adapter,
            env_var="ROUTING_ADVISOR_ENABLED")
except Exception:
    _routing_advisor = None

def _shadow_live_label(chosen_key):
    """Map the proxy's key name ('ours'/'friend') into the optimizer's
    provider namespace ('zai_ours'/'zai_friend') so the agreement comparison
    in ShadowLogger.log_decision is meaningful."""
    return {"ours": "zai_ours", "friend": "zai_friend"}.get(chosen_key, "zai")

# ── Model tier router DISABLED — model selection is now profile-level ──
# Each profile (manager, workers) sets its own model in config.yaml.
# Manager: always GLM-5.2 (user-facing, high quality)
# Workers: glm-4.5-flash (background, bounded tasks)
# The proxy passes through whatever model the profile requests.
_select_model_tier = None

# ── Compression model selection hook ──────────────────────────────────────────
# Parallel to _select_model_tier — when a request is tagged as a compression
# call (X-Task-Type: compression or model == "__compress__" sentinel), this
# hook selects the cheapest capable summarizer model based on cost, pressure,
# benchmarks, and context constraints. See compression_model_router.py.
#
# GAP A (verified 2026-09-04): proxy-side compression selection is UNUSED by
# live clients. Evidence from zai_usage.db: 301 requests carry
# task_type='compression' (80 in the last 3 days), but ALL of them send a
# CONCRETE model (glm-5.2 / glm-5.3 / deepseek/deepseek-v4-flash) — zero
# requests ever used the "__compress__" sentinel, and model_decisions has zero
# tier='compression' rows. Hermes profiles resolve compression.model in config
# and pass a concrete model through, so the flat-router path (which never calls
# _select_compression_model) is correct. This hook remains wired ONLY in the
# legacy path (~6240) and is therefore dead-in-production; it is left in place
# for Phase 1 removal. NO porting into the flat-router path is needed.
_select_compression_model = None
try:
    from compression_model_router import (
        select_compression_model as _cmr_select,
        is_compression_request as _cmr_is_compression,
    )
    _select_compression_model = _cmr_select
    _is_compression_request = _cmr_is_compression
except Exception:
    _is_compression_request = lambda tt, m: False

# ── Kalman-backed rate-limit predictor (unlimited retries) ───────────────────
# Models 429 inter-arrival times to predict recovery.  Falls back to capped
# exponential backoff when insufficient data.  A broken import never crashes
# the proxy — _rate_limit_predictor stays None and old backoff is used.
_rate_limit_predictor = None
try:
    from rate_limit_predictor import RateLimitPredictor as _RLP_cls
    _rate_limit_predictor = _RLP_cls()
except Exception:
    pass

_PROACTIVE_PREDICTION_TTL   = 60            # cache predictions for 60 s
_proactive_switch_state     = {"key": None, "until": 0.0}
_prediction_cache: dict[str, tuple[list[dict], float]] = {}
_prediction_cache_lock = threading.Lock()


def _fetch_predictions(key_name: str) -> list[dict]:
    """Call predict_exhaustion directly (uncached).  Returns [] if the predictor
    is unavailable or errors — callers treat [] as "no prediction, skip logic"."""
    if _predict_exhaustion is None:
        return []
    try:
        return _predict_exhaustion(key_name)
    except Exception:
        return []


def _get_predictions(key_name: str) -> list[dict]:
    """Cached wrapper around predict_exhaustion — avoids a per-request HTTP
    roundtrip to /quota.  NOTE: predict_exhaustion does a self-HTTP GET to
    /quota internally, so this must NEVER be called while holding ``lock``
    (deadlock) or from inside the /quota handler with a cold cache (recursion)."""
    now = time.time()
    with _prediction_cache_lock:
        cached = _prediction_cache.get(key_name)
        if cached and (now - cached[1]) < _PROACTIVE_PREDICTION_TTL:
            return cached[0]
    preds = _fetch_predictions(key_name)
    with _prediction_cache_lock:
        _prediction_cache[key_name] = (preds, now)
    return preds


def _get_cached_predictions(key_name: str) -> list[dict]:
    """Return cached predictions ONLY — never triggers a fetch.  Safe to call
    inside the /quota handler (avoids self-HTTP recursion deadlock)."""
    with _prediction_cache_lock:
        cached = _prediction_cache.get(key_name)
        return cached[0] if cached else []


def _will_exhaust(predictions: list[dict]) -> dict | None:
    """Return the first window predicted to exhaust, ignoring 'Insufficient data'
    entries (which carry a non-empty ``note``).  Returns None if no window is
    predicted to exhaust or there is insufficient data."""
    for p in predictions:
        if p.get("will_exhaust") and not p.get("note"):
            return p
    return None


def _usage_db() -> sqlite3.Connection:
    """Lazy WAL-mode connection to the usage DB; creates schema on first call.
    Double-checked-locked singleton. Returns the shared autocommit connection."""
    global _usage_db_conn
    if _usage_db_conn is not None:
        return _usage_db_conn
    with _usage_db_lock:
        if _usage_db_conn is not None:
            return _usage_db_conn
        conn = sqlite3.connect(str(USAGE_DB), timeout=10, isolation_level=None,
                               check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            key_name TEXT,
            key_suffix TEXT,
            model TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            tier TEXT,
            cache_hit INTEGER DEFAULT 0,
            ollama_hit INTEGER DEFAULT 0,
            ppq_hit INTEGER DEFAULT 0,
            status_code INTEGER,
            error TEXT,
            duration_ms INTEGER,
            cost_usd REAL DEFAULT NULL,
            cost_source TEXT DEFAULT NULL,
            session_id TEXT DEFAULT NULL,
            task_type TEXT DEFAULT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS key_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            chosen_key TEXT,
            reason TEXT,
            ours_pct INTEGER,
            friend_pct INTEGER,
            ours_available INTEGER,
            friend_available INTEGER
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_calls_key_model ON api_calls(key_name, model)")
        # ── Phase 1 attribution (productivity-gate §1.4) ────────────────────
        # Legacy DBs predate the session_id column (fresh DBs get it via the
        # CREATE TABLE above). Idempotent ALTER — swallow the duplicate-column
        # error, mirroring the telemetry schema pattern. Index backs the
        # attribution queries (per-session burn over time windows).
        try:
            conn.execute("ALTER TABLE api_calls ADD COLUMN session_id TEXT")
        except sqlite3.OperationalError:
            pass  # column already migrated
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_calls_session_ts "
            "ON api_calls(session_id, ts)")
        # ── CG-5 task-type attribution (cost-gate-reform-v2 §CG-5) ─────────
        # Same idempotent-ALTER pattern as session_id above: legacy DBs get
        # the nullable task_type column added on connect; fresh DBs get it
        # via the CREATE TABLE above. NO backfill — historical rows keep
        # task_type NULL by design (the value was never known, and CG-5
        # never guesses; see docs/task-type-logging.md).
        try:
            conn.execute("ALTER TABLE api_calls ADD COLUMN task_type TEXT")
        except sqlite3.OperationalError:
            pass  # column already migrated
        conn.execute("CREATE INDEX IF NOT EXISTS idx_key_decisions_ts ON key_decisions(ts)")
        conn.execute("""CREATE TABLE IF NOT EXISTS model_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            key_name TEXT,
            model TEXT,
            original_model TEXT,
            tier TEXT,
            base_tier TEXT,
            hint TEXT,
            reason TEXT,
            peak INTEGER,
            hours_left REAL,
            active_key TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_model_decisions_ts ON model_decisions(ts)")
        # ── circuit-breaker state (one row per key, upserted) ───────────────
        # Tracks failure_count, last_failure_ts, last_error_type, backoff_until,
        # disabled_manually for each key. Written by _log_key_health on every
        # state change. PK=key_name so it always reflects the LATEST state.
        conn.execute("""CREATE TABLE IF NOT EXISTS key_health (
            key_name           TEXT PRIMARY KEY,
            healthy            INTEGER NOT NULL,
            failure_count      INTEGER NOT NULL DEFAULT 0,
            last_failure_ts    REAL,
            last_error_type    TEXT,
            backoff_until      REAL,
            disabled_manually  INTEGER NOT NULL DEFAULT 0,
            backoff_seconds    INTEGER DEFAULT 0,
            updated_ts         REAL NOT NULL
        )""")
        # ── provider telemetry (Phase 2.5.1) ───────────────────────────────
        # One row per proxied request: success/fail, latency, token-mismatch.
        _ensure_telemetry_table(conn)
        _usage_db_conn = conn
    return _usage_db_conn


def _parse_usage(response_buffer: bytes) -> dict:
    """Extract the `usage` object from a z.ai response buffer.

    Handles non-streaming plain-JSON responses and streaming SSE `data: {...}`
    buffers. Returns {} if nothing usable is found. Never raises."""
    if not response_buffer:
        return {}
    # Non-streaming: whole buffer is one JSON object
    try:
        obj = json.loads(response_buffer)
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            return obj["usage"]
    except Exception:
        pass
    # Streaming: scan each `data:` line for an embedded usage object
    try:
        for line in response_buffer.decode("utf-8", "ignore").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]" or not payload:
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
                return obj["usage"]
    except Exception:
        pass
    return {}


def _classify_response(response_buffer: bytes, error_text: str | None) -> tuple:
    """Classify an upstream response buffer for provider telemetry.

    Returns (response_received, response_valid, error_type):
      * response_received — buffer was non-empty
      * response_valid    — buffer held a usable completion (JSON or SSE with
                            a ``choices`` payload)
      * error_type        — 'none' on success; the upstream ``error_text`` for
                            known failures (HTTP/proxy/network errors); 'api_error'
                            for provider error bodies; 'parse_error' ONLY for a
                            genuinely unparseable 200 body.

    Mirrors _parse_usage's SSE handling. Preserves the real ``error_text``
    instead of clobbering it with 'parse_error' when the buffer is non-JSON —
    e.g. a DNS/connection failure writes a plain-text ``'proxy error: ...'``
    body that should be reported as the connection error it is, not as a
    generic parse_error. Never raises.
    """
    resp_received = len(response_buffer) > 0
    if not resp_received:
        return (False, False, error_text or "no_response")
    try:
        rj = json.loads(response_buffer)
    except Exception:
        rj = None
    if isinstance(rj, dict):
        if "choices" in rj:
            return (True, True, "none")
        if "error" in rj:
            return (True, False, "api_error")
        # Valid JSON but no choices/error — keep the upstream error_text if any.
        return (True, False, error_text or "none")
    # Not single-JSON — likely SSE streaming format. Scan data: lines for a
    # choices/error payload (mirrors _parse_usage).
    try:
        found_valid = False
        found_error = False
        for _line in response_buffer.decode("utf-8", "ignore").splitlines():
            _line = _line.strip()
            if not _line.startswith("data:"):
                continue
            _payload = _line[5:].strip()
            if _payload == "[DONE]" or not _payload:
                continue
            try:
                _cj = json.loads(_payload)
            except Exception:
                continue
            if isinstance(_cj, dict) and "choices" in _cj:
                found_valid = True
                break
            if isinstance(_cj, dict) and "error" in _cj:
                found_error = True
        if found_valid:
            return (True, True, "none")
        if found_error:
            return (True, False, "api_error")
        # Genuinely unparseable body (non-JSON, non-SSE). Preserve the real
        # error_text when we have one (network/DNS/HTTP failures) so the
        # 'parse_error' bucket is not contaminated by known connection errors.
        return (True, False, error_text or "parse_error")
    except Exception:
        return (True, False, error_text or "parse_error")


def _extract_model(body: bytes):
    """Best-effort extraction of the `model` field from a request body."""
    if not body:
        return None
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            return obj.get("model")
    except Exception:
        pass
    return None


def _extract_task_type(body: bytes):
    """Best-effort extraction of the `task_type` field from a request body.

    CG-5 (cost-gate-reform-v2 §CG-5): only plain string values count —
    anything else (numbers, null, lists, objects) is treated as unset.
    Whitespace-only values are unset. NEVER guessed, NEVER coerced.
    """
    if not body:
        return None
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            tt = obj.get("task_type")
            if isinstance(tt, str):
                return tt.strip() or None
    except Exception:
        pass
    return None


def _resolve_task_type(headers, body: bytes):
    """Resolve the CG-5 task type for a request: X-Task-Type header wins.

    Precedence: ``X-Task-Type`` request header (stripped; empty/whitespace
    counts as absent) over the body ``task_type`` field. Returns None when
    neither is set — unknown/unset is logged as NULL, never guessed.
    Loopback trust boundary — same handling as X-Hermes-Session.
    """
    try:
        header_val = (headers.get("X-Task-Type", "") or "") if headers is not None else ""
        tt = header_val.strip()
        if tt:
            return tt
    except Exception:
        pass  # headers object misbehaving — fall through to body
    return _extract_task_type(body)


def _log_api_call(*, key_name=None, key_suffix=None, model=None,
                  prompt_tokens=0, completion_tokens=0, total_tokens=0,
                  tier=None, cache_hit=0, ollama_hit=0, ppq_hit=0,
                  status_code=None, error=None, duration_ms=None,
                  cost_usd=None, cost_source=None, session_id=None,
                  task_type=None):
    """Log one API call event. Swallows all errors — logging must never break a request.

    cost_usd / cost_source (RP-2): the real $ cost of this call and how it was
    determined ('measured' from the response, 'estimated' from a rate model,
    'flat_rate' for subscriptions). Both default to NULL when unknown.

    session_id (productivity-gate §1.4): the originating agent session, from
    the X-Hermes-Session request header. NULL when the client doesn't send it
    (pre-upgrade agents, curl probes) — those rows stay unattributed and are
    covered by the time-window fallback join.

    task_type (CG-5, cost-gate-reform-v2 §CG-5): the caller-declared task
    type, from the X-Task-Type header (wins) or the body task_type field.
    NULL when unset/unknown — NEVER guessed. Threaded into EVERY logging
    site (z.ai primary, ollama_cloud, telnyx, external failover hops).

    COST SAFETY NET: if cost_usd is None and key_name is a known provider,
    an estimated cost is computed via _estimate_cost_usd() before insert.
    This ensures cost_usd is NEVER NULL for known providers, even when
    _extract_cost() returns (None, None) due to parse failures, missing
    model rates, or unhandled provider branches. Source = 'estimated'.
    """
    # ── Cost safety net: never insert NULL cost_usd for known providers ──
    if cost_usd is None and key_name:
        try:
            _est = _estimate_cost_usd(key_name, total_tokens or 0,
                                       model=model,
                                       prompt_tokens=prompt_tokens,
                                       completion_tokens=completion_tokens)
            if _est is not None and _est != float('inf'):
                cost_usd = _est
                if cost_source is None:
                    cost_source = 'estimated'
        except Exception:
            pass
    try:
        _usage_db().execute(
            "INSERT INTO api_calls (ts, key_name, key_suffix, model, prompt_tokens, "
            "completion_tokens, total_tokens, tier, cache_hit, ollama_hit, ppq_hit, "
            "status_code, error, duration_ms, cost_usd, cost_source, session_id, "
            "task_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), key_name, key_suffix, model, prompt_tokens, completion_tokens,
             total_tokens, tier, cache_hit, ollama_hit, ppq_hit, status_code, error,
             duration_ms, cost_usd, cost_source, session_id, task_type))
    except Exception:
        # Fallback 1: task_type column absent (DB predates the CG-5
        # migration) — retry without task_type (it is nullable telemetry;
        # losing it is acceptable, losing the row is not).
        try:
            _usage_db().execute(
                "INSERT INTO api_calls (ts, key_name, key_suffix, model, prompt_tokens, "
                "completion_tokens, total_tokens, tier, cache_hit, ollama_hit, ppq_hit, "
                "status_code, error, duration_ms, cost_usd, cost_source, session_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), key_name, key_suffix, model, prompt_tokens, completion_tokens,
                 total_tokens, tier, cache_hit, ollama_hit, ppq_hit, status_code, error,
                 duration_ms, cost_usd, cost_source, session_id))
        except Exception:
            # Fallback 2: session_id column absent (DB predates the §1.4
            # migration) but cost columns present — retry without it.
            try:
                _usage_db().execute(
                    "INSERT INTO api_calls (ts, key_name, key_suffix, model, prompt_tokens, "
                    "completion_tokens, total_tokens, tier, cache_hit, ollama_hit, ppq_hit, "
                    "status_code, error, duration_ms, cost_usd, cost_source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (time.time(), key_name, key_suffix, model, prompt_tokens, completion_tokens,
                     total_tokens, tier, cache_hit, ollama_hit, ppq_hit, status_code, error,
                     duration_ms, cost_usd, cost_source))
            except Exception:
                # Fallback 3: cost_usd/cost_source columns absent too (pre-RP-1
                # DB) — retry with the base columns so we don't lose the row.
                try:
                    _usage_db().execute(
                        "INSERT INTO api_calls (ts, key_name, key_suffix, model, prompt_tokens, "
                        "completion_tokens, total_tokens, tier, cache_hit, ollama_hit, ppq_hit, "
                        "status_code, error, duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (time.time(), key_name, key_suffix, model, prompt_tokens, completion_tokens,
                         total_tokens, tier, cache_hit, ollama_hit, ppq_hit, status_code, error,
                         duration_ms))
                except Exception:
                    pass


def _log_key_decision(*, chosen_key, reason, ours_pct=0, friend_pct=0,
                      ours_available=0, friend_available=0):
    """Log one key-selection decision. Swallows all errors."""
    try:
        _usage_db().execute(
            "INSERT INTO key_decisions (ts, chosen_key, reason, ours_pct, friend_pct, "
            "ours_available, friend_available) VALUES (?,?,?,?,?,?,?)",
            (time.time(), chosen_key, reason, ours_pct, friend_pct,
             ours_available, friend_available))
    except Exception:
        pass


# ── P3.4 Fix 2: routing_live_decisions table ────────────────────────────────
# Same schema as routing_shadow_decisions (so the two strategies can be
# compared in one query) PLUS a ``pace_mults`` column (JSON text) capturing
# the per-provider pace multipliers LiveRouter actually used. Mirrors the
# inline CREATE-TABLE-then-INSERT pattern of _log_rate_limit / _log_key_decision.
_ROUTING_LIVE_DECISIONS_SQL = """\
CREATE TABLE IF NOT EXISTS routing_live_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    live_provider TEXT,
    live_model TEXT,
    shadow_provider TEXT,
    shadow_model TEXT,
    shadow_cost REAL,
    live_cost REAL,
    tokens INTEGER,
    agree INTEGER,
    reason TEXT,
    pace_mults TEXT
);
"""


def _log_live_decision(*, provider, model=None, fallback=None,
                       fallback_model=None, reason="", pace_mults=None):
    """Log one LIVE LiveRouter failover decision to ``routing_live_decisions``.

    Column mapping (deliberate reuse of the shadow schema for direct
    comparison): ``live_provider``/``live_model`` = the provider LiveRouter
    chose and we routed to; ``shadow_provider``/``shadow_model`` = the
    fallback LiveRouter considered (second-cheapest viable); ``pace_mults``
    = JSON of the per-provider pace multipliers used (Fix 2).

    ``pace_mults`` may be a dict (JSON-encoded) or an already-serialised
    string. Never raises — logging must not break the hot failover path.
    """
    try:
        db = _usage_db()
        db.execute(_ROUTING_LIVE_DECISIONS_SQL)
        if pace_mults is None:
            pace_json = None
        elif isinstance(pace_mults, str):
            pace_json = pace_mults
        else:
            pace_json = json.dumps(pace_mults, default=str)
        db.execute(
            "INSERT INTO routing_live_decisions "
            "(ts, live_provider, live_model, shadow_provider, shadow_model, "
            " shadow_cost, live_cost, tokens, agree, reason, pace_mults) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), provider, model, fallback, fallback_model,
             None, None, 0, 1, reason if reason is not None else "", pace_json))
    except Exception:
        pass


def _consult_live_router(*, model: str | None = None,
                          task_type: str = "coding",
                          failure_counts: dict[str, int] | None = None):
    """Consult LiveRouter for a failover pick. Returns
    ``(provider, model, fallback, fallback_model)`` or ``(None, None, None,
    None)`` when disabled / unavailable / no viable pick. Never raises — any
    failure yields all-None so the caller falls through to the hardcoded
    ollama->external failover chain.

    This is the single shared entry point used by BOTH the best_key() Phase 5
    gate AND the request-handler retry-loop terminal fallback (P3.4 Fix 1).
    Centralising it here fixes the latent tuple-unpack bug (the old gate did
    ``_provider, _fallback = select_failover(...)`` then used ``_provider`` —
    a ``(provider, model)`` tuple — as the provider string, so the pick was
    never routable) and ensures the retry-loop bypass path actually engages
    LiveRouter under real dual-key-exhaustion.

    Kill switch: ``_LIVE_ROUTING_FLAG`` (``.enable_live_routing``) must exist.
    Side effect: logs the decision to ``routing_live_decisions`` (Fix 2).
    """
    if _LIVE_ROUTER is None or not os.path.exists(_LIVE_ROUTING_FLAG):
        return (None, None, None, None)
    try:
        _pw = None
        with lock:
            _pw = dict(_pace_windows) if _pace_windows else None
        if failure_counts is None:
            failure_counts = _snapshot_failures()
        (pick, pick_model), (fb, fb_model) = _LIVE_ROUTER.select_failover(
            quota_state=_snapshot_quota(),
            health_state=_snapshot_health(),
            peak=_is_peak_hour(),
            pace_windows=_pw,
            failure_counts=failure_counts,
            task_type=task_type,
            model=model,
        )
        if not pick:
            return (None, None, None, None)
        # Capture the ACTUAL pace multipliers LiveRouter used (single source
        # of truth — computed inside select_failover under its lock).
        try:
            pace_mults = _LIVE_ROUTER.last_pace_mults
        except Exception:
            pace_mults = None
        _log_live_decision(provider=pick, model=pick_model,
                           fallback=fb, fallback_model=fb_model,
                           reason=f"live_kalman_failover_{pick}",
                           pace_mults=pace_mults)
        return (pick, pick_model, fb, fb_model)
    except Exception:
        return (None, None, None, None)


def _log_rate_limit(*, key_used=None, attempt=0, duration_ms=None):
    try:
        _usage_db().execute(
            "CREATE TABLE IF NOT EXISTS rate_limit_samples ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts REAL NOT NULL,"
            "key_name TEXT,"
            "attempt_num INTEGER,"
            "duration_ms INTEGER,"
            "retry_after_estimate INTEGER DEFAULT 0)",
        )
        _usage_db().execute(
            "INSERT INTO rate_limit_samples (ts, key_name, attempt_num, duration_ms) VALUES (?,?,?,?)",
            (time.time(), key_used, attempt, duration_ms))
    except Exception:
        pass


def _log_model_decision(*, key_name=None, model=None, original_model=None,
                        tier=None, base_tier=None, hint=None, reason=None,
                        peak=0, hours_left=None, active_key=None):
    """Log one model-tier decision. Swallows all errors."""
    try:
        _usage_db().execute(
            "INSERT INTO model_decisions (ts, key_name, model, original_model, "
            "tier, base_tier, hint, reason, peak, hours_left, active_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), key_name, model, original_model,
             tier, base_tier, hint, reason, peak, hours_left, active_key))
    except Exception:
        pass


def _log_key_health(name: str, state: dict) -> None:
    """Upsert the current per-key circuit-breaker state into ``key_health``.

    One row per key (PRIMARY KEY = key_name) — queryable for dashboards and
    post-mortems. Called from _mark_key_failure / _mark_key_healthy on every
    state transition. Swallows all errors — never breaks a request."""
    try:
        _usage_db().execute(
            "INSERT INTO key_health (key_name, healthy, failure_count, "
            "last_failure_ts, last_error_type, backoff_until, "
            "disabled_manually, backoff_seconds, updated_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key_name) DO UPDATE SET "
            "healthy=excluded.healthy, failure_count=excluded.failure_count, "
            "last_failure_ts=excluded.last_failure_ts, "
            "last_error_type=excluded.last_error_type, "
            "backoff_until=excluded.backoff_until, "
            "disabled_manually=excluded.disabled_manually, "
            "backoff_seconds=excluded.backoff_seconds, "
            "updated_ts=excluded.updated_ts",
            (name,
             1 if state.get("healthy") else 0,
             int(state.get("consecutive_failures", 0)),
             state.get("last_failure_ts"),
             state.get("last_error_type"),
             state.get("backoff_until", 0),
             1 if state.get("disabled_manually") else 0,
             int(state.get("backoff_seconds", 0)),
             time.time()))
    except Exception:
        pass


# ── provider telemetry (Phase 2.5.1) ────────────────────────────────────────
# One row per proxied request: success/fail, latency, token-mismatch (fraud
# signal).  This is the data foundation for CPVO (cost-per-valid-output) and
# quality probes.  NEVER raises — telemetry failure is silent and must never
# break request handling.

_TELEMETRY_SCHEMA = """CREATE TABLE IF NOT EXISTS provider_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    response_received INTEGER,
    response_valid INTEGER,
    latency_ms INTEGER,
    error_type TEXT,
    billed_tokens INTEGER,
    actual_tokens INTEGER,
    token_mismatch INTEGER,
    model TEXT
)"""


def _ensure_telemetry_table(conn: sqlite3.Connection | None) -> None:
    """Create the provider_telemetry table if it doesn't exist.

    Idempotent — safe to call on every request or at startup.  Swallows all
    errors so a schema migration failure never breaks request handling.

    Phase 4.5b: also adds the ``model`` column to legacy DBs that predate it
    (idempotent ALTER; the duplicate-column error is swallowed) so the
    model-aware CPVO calculator (``cpvo_calculator.py``) can track quality
    per ``(provider, model)`` pair.
    """
    if conn is None:
        return
    try:
        conn.execute(_TELEMETRY_SCHEMA)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telemetry_ts "
            "ON provider_telemetry(ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telemetry_provider "
            "ON provider_telemetry(provider)"
        )
        # Ensure legacy DBs (created before the model column was added to
        # _TELEMETRY_SCHEMA) get the column too.  Idempotent: the
        # OperationalError on a duplicate column is expected and swallowed.
        try:
            conn.execute(
                "ALTER TABLE provider_telemetry ADD COLUMN model TEXT"
            )
        except Exception:
            pass
    except Exception:
        pass


def _log_provider_telemetry(
    *,
    conn: sqlite3.Connection | None,
    provider: str | None,
    response_received: bool | None,
    response_valid: bool | None,
    latency_ms: int | None,
    error_type: str | None,
    billed_tokens: int | None,
    actual_tokens: int | None,
    token_mismatch: bool | None,
    model: str | None = None,
) -> None:
    """Insert one telemetry row.  NEVER raises — telemetry failure is silent.

    Called from the _proxy() finally block after every request completes.
    One INSERT per request using the existing shared DB connection.

    Phase 4.5b: ``model`` is the model that served the request.  When present
    it lets ``cpvo_calculator.CPVOCalculator`` track quality per
    ``(provider, model)`` pair.  Defaults to ``None`` for backward
    compatibility (legacy callers / pre-existing rows stay NULL).
    """
    if conn is None:
        return
    try:
        _ensure_telemetry_table(conn)
        conn.execute(
            "INSERT INTO provider_telemetry "
            "(ts, provider, response_received, response_valid, "
            "latency_ms, error_type, billed_tokens, actual_tokens, "
            "token_mismatch, model) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(),
             provider or "unknown",
             int(response_received) if response_received is not None else 0,
             int(response_valid) if response_valid is not None else 0,
             int(latency_ms) if latency_ms is not None else 0,
             error_type or "none",
             int(billed_tokens) if billed_tokens is not None else 0,
             int(actual_tokens) if actual_tokens is not None else 0,
             int(token_mismatch) if token_mismatch is not None else 0,
             model),
        )
    except Exception:
        pass


# ── global spend cap (runaway-loop circuit breaker) ─────────────────────────
# Tracks cumulative daily spend across ALL providers (z.ai, PPQ, OpenRouter).
# When the daily cap for a tier is exceeded, the proxy returns 503 — preventing
# runaway agent loops from burning unlimited tokens.
#
# z.ai models are $0/1M (subscription). External failover models have real
# per-token cost. The cap protects against the expensive external path.

# Spend caps deactivated (2026-08-20): the merchant module markets and
# wallet balance decide routing. Hard-coded caps block UX when we need
# things to work. Set env vars to re-enable if ever needed.
_SPEND_CAP_MANAGER = float(os.environ.get("SPEND_CAP_MANAGER", "inf"))
_SPEND_CAP_WORKER  = float(os.environ.get("SPEND_CAP_WORKER", "inf"))

# D6 (2026-09-02): metered-only cash breaker tiers. These are the providers
# where a request spends NEW money (pay-per-token / credits). Subscription
# lanes are deliberately absent — see _check_global_spend_cap docstring.
_METERED_SPEND_TIERS = frozenset({
    "neuralwatt", "routstr", "routstrd", "deepinfra",
    "telnyx", "ppq", "openrouter",
})

# ── Cost per 1M tokens (RP-4: real_price_tracker is the source of truth) ──────
# Hardcoded rate constants have been replaced by real_price_tracker.
# get_rate_with_fallback() resolves: real data → Ollama API → LAST_RESORT_RATES.
# The values below are EMERGENCY FALLBACKS for when the tracker module fails to
# import — they mirror LAST_RESORT_RATES in src/real_price_tracker.py.
_FALLBACK_OLLAMA_CLOUD_BASE = 0.0155    # was 0.024 (35% wrong); measured = 0.0155
_FALLBACK_OLLAMA_CLOUD_EXTRA = 0.15     # above-quota rate
_FALLBACK_RATES: dict[str, float] = {
    "ollama_cloud": _FALLBACK_OLLAMA_CLOUD_BASE,
    "ollama_cloud_2": _FALLBACK_OLLAMA_CLOUD_BASE,
    "ollama_cloud_3": _FALLBACK_OLLAMA_CLOUD_BASE,
    "opencode_go":  0.0155,   # $10/mo flat-rate → marginal $0, floored
    "neuralwatt":   2.21,     # glm-5.2 blended (primary model, conservative seed)
    "friend":       0.015,    # z.ai $300/yr amortized seed
    "ours":         0.03,     # z.ai $960/yr amortized seed
    "deepinfra":    1.30,
    "telnyx":       1.50,     # blended fallback (was 5.40 — 100x too high)
}

# ── Telnyx per-model rates ($/M tokens) ──────────────────────────────────────
# Sourced from https://telnyx.com/pricing.md (2026-08-21).  Three rate tiers:
#   input        — uncached prompt tokens
#   cached_input — prompt tokens served from Telnyx's prompt cache (~17% of
#                  input price; Telnyx applies caching automatically and
#                  reports hit count via usage.prompt_tokens_details.cached_tokens)
#   output       — completion tokens (never cached)
# Used by _extract_cost() for granular per-request cost tracking.  The
# calibration factor from periodic balance API checks is now a slow
# corrective on top of the cached-aware per-call math (should converge to
# ~1.0 instead of the old 0.001 floor).
_TELNYX_MODEL_RATES: dict[str, dict[str, float]] = {
    "moonshotai/Kimi-K3":           {"input": 2.70,  "cached_input": 0.46,  "output": 13.50},
    "moonshotai/Kimi-K2.5":         {"input": 0.95,  "cached_input": 0.16,  "output": 4.00},
    "moonshotai/Kimi-K2-5":         {"input": 0.95,  "cached_input": 0.16,  "output": 4.00},
    "zai-org/GLM-5.2":              {"input": 1.40,  "cached_input": 0.26,  "output": 4.40},  # GLM-5.1-FP8 rate
    "thudm/glm-5.1-fp8":            {"input": 1.40,  "cached_input": 0.26,  "output": 4.40},
    "MiniMaxAI/MiniMax-M3-MXFP8":   {"input": 0.51,  "cached_input": 0.102, "output": 2.04},
    "minimax/minimax-m2-5":         {"input": 0.51,  "cached_input": 0.102, "output": 2.04},
    "openai/gpt-5":                 {"input": 1.25,  "cached_input": 0.21,  "output": 10.00},  # 17% est.
    "anthropic/claude-haiku-4-5":   {"input": 1.00,  "cached_input": 0.17,  "output": 5.00},   # 17% est.
}

# OpenRouter per-model rates (from openrouter.ai/api/v1/models, 2026-08-14).
# Used by _get_provider_cost so the failover sorts by REAL prices.
_OPENROUTER_MODEL_RATES: dict[str, dict[str, float]] = {
    "z-ai/glm-5.2":                {"input": 0.63,  "output": 1.98},
    "moonshotai/kimi-k3":          {"input": 3.00,  "output": 15.00},
    "deepseek/deepseek-v4-flash":  {"input": 0.14,  "output": 0.28},
    "deepseek/deepseek-v4-pro":    {"input": 0.53,  "output": 2.12},
}

# PPQ per-model rates (from api.ppq.ai/v1/models pricing field, 2026-08-14).
_PPQ_MODEL_RATES: dict[str, dict[str, float]] = {
    "z-ai/glm-5.2":                {"input": 1.477,  "output": 4.642},
    "moonshotai/kimi-k3":          {"input": 3.165,  "output": 15.825},
    "deepseek/deepseek-v4-flash":  {"input": 0.1477, "output": 0.2954},
}

# Map proxy model IDs to provider-specific model IDs for rate lookup.
# The proxy uses short IDs (e.g. "glm-5.2") but providers use vendor-prefixed IDs.
_MODEL_ID_TO_PROVIDER_ID: dict[str, dict[str, str]] = {
    "glm-5.2": {
        "telnyx": "zai-org/GLM-5.2",
        "openrouter": "z-ai/glm-5.2",
        "ppq": "z-ai/glm-5.2",
        "neuralwatt": "glm-5.2",
    },
    # glm-5.3 entry REMOVED (2026-09-05) — neuralwatt no longer routes glm-5.3
    # (see flat_router PROVIDER_MODELS). This map (_MODEL_ID_TO_PROVIDER_ID) is
    # cost-lookup only; the leftover "neuralwatt":"glm-5.3" identity entry is now
    # dead and was a no-op (NEURALWATT_RATES has no glm-5.3 key).
    "kimi-k3": {
        "telnyx": "moonshotai/Kimi-K3",
        "openrouter": "moonshotai/kimi-k3",
        "ppq": "moonshotai/kimi-k3",
        "neuralwatt": "kimi-k3",
    },
    "deepseek/deepseek-v4-flash": {
        "openrouter": "deepseek/deepseek-v4-flash",
        "ppq": "deepseek/deepseek-v4-flash",
        "neuralwatt": "deepseek-v4-flash",
    },
    "deepseek-v4-flash": {
        "neuralwatt": "deepseek-v4-flash",
    },
    "deepseek-v4-pro": {
        "neuralwatt": "deepseek-v4-pro",
    },
    "gemma-4-31b": {
        "neuralwatt": "gemma-4-31b",
    },
}

# Blended rate ratio: typical LLM usage is ~3:1 input:output tokens.
# Used to compute a single $/M number from input + output rates.
_BLENDED_RATIO_INPUT = 0.75  # 3 parts input
_BLENDED_RATIO_OUTPUT = 0.25  # 1 part output


def _blended_rate(input_rate: float, output_rate: float) -> float:
    """Compute blended $/M from input + output rates (3:1 ratio)."""
    return input_rate * _BLENDED_RATIO_INPUT + output_rate * _BLENDED_RATIO_OUTPUT


# Rolling Telnyx cache-hit ratio (prompt tokens served from cache vs total).
# Used by _get_provider_cost so the failover sort ranks Telnyx fairly for
# our cache-heavy (repeated-context) workload — without it, Telnyx's full
# input rate makes it look ~6x more expensive than it actually is.
_telnyx_cache_hit_ratio: float = 0.99  # default: our workload is ~99% cache
_telnyx_cache_ratio_ts: float = 0.0


def _refresh_telnyx_cache_hit_ratio() -> float:
    """Recompute the rolling cache-hit ratio from recent telnyx api_calls.

    Cached/total prompt tokens over the last 200 telnyx calls. Falls back
    to the last known value (default 0.99) if the DB is unreachable or no
    rows have cached_tokens yet. Refreshed lazily (5-min TTL)."""
    global _telnyx_cache_hit_ratio, _telnyx_cache_ratio_ts
    now = time.time()
    if now - _telnyx_cache_ratio_ts < 300:  # 5-min TTL
        return _telnyx_cache_hit_ratio
    _telnyx_cache_ratio_ts = now
    try:
        row = _usage_db().execute(
            "SELECT SUM(prompt_tokens), SUM(cache_hit) FROM ("
            "  SELECT prompt_tokens, cache_hit FROM api_calls "
            "  WHERE tier='telnyx' AND prompt_tokens > 0 "
            "  ORDER BY ts DESC LIMIT 200)"
        ).fetchone()
        total_prompt = int(row[0] or 0)
        total_cached = int(row[1] or 0) if row[1] is not None else 0
        if total_prompt > 0 and total_cached > 0:
            _telnyx_cache_hit_ratio = max(0.0, min(1.0, total_cached / total_prompt))
    except Exception:
        pass  # keep last known / default
    return _telnyx_cache_hit_ratio


def _telnyx_cache_aware_blended_rate(rates: dict) -> float:
    """Blended $/M for Telnyx accounting for the prompt-cache hit ratio.

    input is split into cached (× cached_input rate) and uncached (× input
    rate) per the rolling hit ratio; output stays at full rate. Then the
    standard 3:1 input:output blend is applied, plus the calibration factor.
    """
    input_rate = rates.get("input", 0.95)
    cached_rate = rates.get("cached_input", input_rate * 0.17)
    output_rate = rates.get("output", 4.00)
    hit = _refresh_telnyx_cache_hit_ratio()
    effective_input = cached_rate * hit + input_rate * (1.0 - hit)
    return _blended_rate(effective_input, output_rate) * _telnyx_calibration_factor


# Cached Routstr model rates (dynamic — refreshed every 10 min from the node's
# /v1/models endpoint, which is auth-free). Returns {model_id: blended_usd_per_M}.
_routstr_rates_cache: dict = {"rates": None, "ts": 0.0}
# Same, for the local routstrd daemon (network catalog prices).
_routstrd_rates_cache: dict = {"rates": None, "ts": 0.0}

# Endpoint liveness probes (5-min cache). A routstr/routstrd endpoint that
# accepts TCP but cannot serve (e.g. VPS2 memory pressure, dead Cashu mint)
# would otherwise stall the failover chain for the full 180s timeout.
_endpoint_alive_cache: dict = {"probes": {}, "ts": 0.0}


def _endpoint_alive(base_url: str) -> bool:
    """Cheap GET health probe with a 5-min cache. Never raises."""
    now = time.time()
    probes = _endpoint_alive_cache["probes"]
    if now - _endpoint_alive_cache["ts"] > 300:
        probes.clear()
        _endpoint_alive_cache["ts"] = now
    if base_url in probes:
        return probes[base_url]
    alive = False
    try:
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        path = "/health" if ":8008" in root else "/v1/models"
        req = urllib.request.Request(root + path, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            alive = 200 <= resp.status < 500
    except Exception:
        alive = False
    probes[base_url] = alive
    return alive


def _get_routstr_rates() -> dict:
    """Fetch per-model blended $/M rates from our Routstr node (10-min cache).

    The node's /v1/models returns per-token USD pricing in
    pricing.prompt / pricing.completion. Multiply by 1e6 and blend 3:1.
    Never raises; on failure returns the last known cache (or {}).
    """
    if time.time() - _routstr_rates_cache["ts"] < 600 and _routstr_rates_cache["rates"] is not None:
        return _routstr_rates_cache["rates"]
    base = _EXTERNAL_KEYS.get("routstr_base", "http://23.182.128.51:8009")
    rates = _fetch_openrouter_style_rates(base, _routstr_rates_cache)
    return rates


def _get_routstrd_rates() -> dict:
    """Per-model blended $/M rates from the local routstrd daemon (10-min cache).

    The daemon's /v1/models is auth-free and returns network-catalog prices
    in pricing.prompt / pricing.completion per token. Never raises.
    """
    if time.time() - _routstrd_rates_cache["ts"] < 600 and _routstrd_rates_cache["rates"] is not None:
        return _routstrd_rates_cache["rates"]
    base = _EXTERNAL_KEYS.get("routstrd_base", "http://localhost:8008")
    return _fetch_openrouter_style_rates(base, _routstrd_rates_cache)


# ── CG-2 GET /v1/pricing (price exposure, plan v2.1 §2.1 / §0.5) ─────────────
# Builds the price-exposure payload: z.ai subscription rows (fee ÷ entitlement
# baseline × pressure × peak, plus a Kalman-gated forecast) and flat-tier rows
# (routstrd / routstr catalog rates). Delegates to src.pricing_exposure so the
# gate and the endpoint cannot drift. Never raises — degrades to a minimal
# payload on any error so the endpoint stays responsive.
def _build_v1_pricing(
    model: str | None = None,
    horizon_min: int | None = None,
) -> dict:
    # Lazy import: pull the pure builders from the MRE repo (already on
    # sys.path via the shadow-hook bridge). Any failure degrades gracefully.
    try:
        from src.pricing_exposure import (
            build_flat_row,
            build_pricing_payload,
            build_zai_pricing_row,
            load_zai_fees,
            trailing_usage_tokens,
        )
    except Exception:
        return {
            "error": "pricing_exposure unavailable",
            "providers": {},
            "kalman_convergence": {"green": False, "verdict": "unverified"},
        }

    now_ts = time.time()

    # ── collector state: kalman verdict + smoothed capacity estimates ──────
    kalman_verdict: str | None = None
    capacity_estimates: dict[str, float] = {}
    try:
        with open(_PRICING_EXPOSURE_STATE, encoding="utf-8") as fh:
            st = json.load(fh)
        kalman_verdict = st.get("kalman_verdict") if isinstance(st, dict) else None
        se = st.get("capacity_estimates") if isinstance(st, dict) else None
        if isinstance(se, dict):
            capacity_estimates = {str(k): float(v) for k, v in se.items() if v}
    except Exception:
        kalman_verdict = None  # missing/unreadable → unverified gate

    rows: dict[str, dict] = {}

    # ── z.ai subscription rows (per configured key) ─────────────────────────
    try:
        fees = load_zai_fees(PROVIDERS_YAML)
    except Exception:
        fees = {}

    zai_keys = [k for k in KEYS if k in fees]
    for key_name in zai_keys:
        fee_cfg = fees.get(key_name) or {}
        windows = quota_cache.get(key_name, ([], 0.0))[0]
        try:
            projections = _get_cached_predictions(key_name)
        except Exception:
            projections = []
        try:
            row = build_zai_pricing_row(
                provider=key_name,
                monthly_fee_usd=float(fee_cfg.get("monthly_fee_usd", 0.0) or 0.0),
                entitlement_tokens_mo=float(
                    fee_cfg.get("entitlement_tokens_mo", 18.45e9)
                ),
                capacity_estimate_tokens=capacity_estimates.get(key_name),
                trailing_30d_tokens=float(trailing_usage_tokens(USAGE_DB, key_name, 30)),
                windows=windows,
                projections=projections,
                now_ts=now_ts,
                kalman_verdict=kalman_verdict,
                extra_horizons_min=([int(horizon_min)] if horizon_min else ()),
            )
            if row.get("provider"):
                rows[key_name] = row
        except Exception as _e:
            print(f"[v1/pricing] zai row failed for {key_name}: {_e}", flush=True)

    # ── flat-tier rows (routstrd, routstr) from catalog rates ───────────────
    for prov_name in ("routstrd", "routstr"):
        try:
            rates = (_get_routstrd_rates() if prov_name == "routstrd"
                     else _get_routstr_rates()) or {}
            usable = {k: v for k, v in rates.items() if isinstance(v, (int, float))}
            if not usable:
                continue
            if model and model in usable:
                catalog = float(usable[model])
            else:
                catalog = float(min(usable.values()))
            rows[prov_name] = build_flat_row(
                provider=prov_name,
                catalog_price_usd_per_m=catalog,
                measured=False,
                now_ts=now_ts,
            )
        except Exception as _e:
            print(f"[v1/pricing] flat row failed for {prov_name}: {_e}", flush=True)

    payload = build_pricing_payload(
        rows=rows,
        kalman_verdict=kalman_verdict,
        model=model,
        horizon_min=int(horizon_min) if horizon_min else None,
        now_ts=now_ts,
    )
    return payload


# Balance snapshot cache for routstrd — fixes the 300s/300s race condition
# (test_sp_routstrd_balance_race). TTL is 420s (7 min) so the cache always
# overlaps with the 5-min collector cron. On fetch failure, the last known
# good entry is preserved instead of fail-closing (stale-but-alive ≠ dead).
_routstrd_bal_cache: dict = {"ts": 0.0, "entry": None}
_ROUTSTRD_BAL_TTL = 420.0  # seconds — 7 min, > 5-min collector cadence


def _routstrd_balance_snapshot() -> dict:
    """Fetch routstrd wallet balance with 420s cache + last-known-good preservation.

    Race-condition fix (2026-08-23): the 5-min balance collector and the
    proxy's 300s cache TTL created a window where the cache expired just
    before the collector refreshed → fail-closed → skip → fall through to
    the more expensive openrouter. This function:
      - Uses a 420s TTL so the cache always overlaps the 5-min collector.
      - On fetch failure with a last-known-good entry, returns that entry
        instead of fail-closing (the endpoint liveness probe already
        confirmed the daemon is up — a stale balance ≠ exhausted wallet).
      - Only fails closed ({used_pct:100, remaining:0}) when there is no
        prior good entry AND the fetch fails (genuine cold-start blackout).

    Returns dict with keys: used_pct, remaining, balance_sats. Never raises.
    """
    now = time.time()
    # Fresh cache → return immediately (no network call)
    if now - _routstrd_bal_cache["ts"] < _ROUTSTRD_BAL_TTL and _routstrd_bal_cache["entry"] is not None:
        return dict(_routstrd_bal_cache["entry"])

    # Stale or empty cache → fetch fresh balance from the routstrd daemon
    fail_closed = {"used_pct": 100.0, "remaining": 0.0, "balance_sats": 0}
    base = _EXTERNAL_KEYS.get("routstrd_base", "http://localhost:8008")
    key = _EXTERNAL_KEYS.get("routstrd", "")
    root = base.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    entry = None
    for path in ("/v1/balance/info", "/v1/wallet/balance"):
        try:
            hdrs = {"Authorization": f"Bearer {key}"} if key else {}
            req = urllib.request.Request(base.rstrip("/") + path, headers=hdrs)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            bal_sats = data.get("balance")
            if bal_sats is not None:
                bal_sats = int(bal_sats)
                # starting balance: use env or default 25000 sats (matches collector)
                try:
                    starting = int(os.environ.get("ROUTSTR_STARTING_SATS", "25000"))
                except ValueError:
                    starting = 25000
                used_pct = max(0.0, (1.0 - (bal_sats / starting)) * 100.0) if starting > 0 else 0.0
                entry = {
                    "used_pct": round(used_pct, 2),
                    "remaining": float(bal_sats),
                    "balance_sats": bal_sats,
                }
                break
        except Exception:
            continue

    if entry is not None:
        # Fetch succeeded → update cache
        _routstrd_bal_cache["entry"] = dict(entry)
        _routstrd_bal_cache["ts"] = now
        return entry

    # Fetch failed → preserve last known good if we have one
    if _routstrd_bal_cache["entry"] is not None:
        return dict(_routstrd_bal_cache["entry"])

    # No prior good entry → fail closed (genuine cold-start blackout)
    return fail_closed


def _fetch_openrouter_style_rates(base: str, cache: dict) -> dict:
    """Shared fetcher for OpenAI-style /v1/models pricing (per-token USD)."""
    try:
        root = base.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        req = urllib.request.Request(root + "/v1/models", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        rates: dict[str, float] = {}
        for m in data.get("data", []):
            pricing = m.get("pricing", {}) or {}
            try:
                p = float(pricing.get("prompt", 0) or 0)
                c = float(pricing.get("completion", 0) or 0)
            except (TypeError, ValueError):
                continue
            rates[m.get("id", "")] = _blended_rate(p * 1e6, c * 1e6)
        if rates:
            cache["rates"] = rates
            cache["ts"] = time.time()
        return cache.get("rates", {}) or {}
    except Exception:
        return cache.get("rates", {}) or {}

# Calibration factor applied to telnyx cost estimates. Updated every 10 min
# by _calibrate_telnyx_rates() in _refresh_loop() by comparing the real
# balance API delta with the sum of estimated costs. Default 1.0 (no
# calibration until the first balance check runs).
_telnyx_calibration_factor: float = 1.0


def _rpt_rate(provider: str, model: str | None = None) -> float:
    """Get a $/M rate from real_price_tracker, falling back to inline constants.

    Resolution chain: real_price_tracker.get_rate_with_fallback() →
    inline _FALLBACK_RATES → 0.0 (safe zero for unknown providers).
    Never raises.
    """
    if _rpt_get_rate is not None:
        try:
            return _rpt_get_rate(provider, model)
        except Exception:
            pass
    return _FALLBACK_RATES.get(provider, 0.0)


def _spend_tier(key_name: str | None) -> str:
    """Classify a request by key type for cost tracking.
    Key types: ours (z.ai subscription), friend (courtesy key),
    ollama_cloud (flat-rate), deepinfra (pay-per-use with prompt caching),
    telnyx/ppq/openrouter/routstr (paid external failover providers)."""
    if key_name in ("ours", "friend"):
        return key_name
    elif key_name in ("ollama_cloud", "ollama_cloud_2", "ollama_cloud_3"):
        return key_name
    elif key_name == "deepinfra":
        return "deepinfra"
    elif key_name in ("telnyx", "ppq", "openrouter", "routstr", "routstrd",
                      "opencode_go", "neuralwatt"):
        return key_name
    return "unknown"


def _get_ollama_cloud_cost_per_1m() -> float:
    """Dynamic cost per 1M tokens for ollama_cloud based on quota regime.

    RP-4: Rates are sourced from real_price_tracker.get_rate_with_fallback()
    which uses real measured cost_usd data, falling back to LAST_RESORT_RATES.
    - included:  real measured rate (≈ $0.0155/M)
    - extra:     above-quota rate (≈ $0.15/M, above PPQ $0.14/M → optimizer reroutes)
    - exhausted: float('inf') (effectively removes from routing)
    """
    regime = _get_ollama_quota_status()["regime"]
    if regime == "extra":
        if _rpt_get_rate is not None:
            try:
                return _rpt_get_rate("ollama_cloud_extra")
            except Exception:
                pass
        return _FALLBACK_OLLAMA_CLOUD_EXTRA
    elif regime == "exhausted":
        return float("inf")
    return _rpt_rate("ollama_cloud")


def _estimate_cost_usd(key_name: str | None, total_tokens: int,
                       model: str | None = None,
                       prompt_tokens: int | None = None,
                       completion_tokens: int | None = None) -> float:
    """Estimate USD cost for a request based on key type. Returns 0.0 for unknown/free keys.

    RP-4: Rates are sourced from real_price_tracker.get_rate_with_fallback()
    which uses real measured cost_usd data from the DB, falling back to
    LAST_RESORT_RATES estimates. For ollama_cloud, applies dynamic pricing
    based on the current quota regime (included/extra/exhausted).
    For NeuralWatt, uses per-model rates from NEURALWATT_RATES when the
    model and token breakdown are available, instead of the flat provider
    rate ($0.21/M) that severely undercounts glm-5.2 (~$2.21/M blended).
    """
    if not key_name or total_tokens <= 0:
        return 0.0
    if key_name == "ollama_cloud":
        cost_per_1m = _get_ollama_cloud_cost_per_1m()
    elif key_name == "ollama_cloud_3":
        # 2026-09-02 (plan B2): oc3 shares the $20/mo ollama plan family and
        # the same marginal $/M (~$0.0155/M measured). Its own tracker entry
        # has no independent measured basis, and UNKNOWN_PROVIDER_FALLBACK
        # ($1.00/M) poisoned its attributed rows on 2026-09-01/02 ($9.8/day
        # phantom spend corrupting dashboards and the cost governor's ratio
        # inputs). Attribute at the measured oc-family rate; scarcity gating
        # for oc3 stays where it belongs (monthly-budget dispatch guard).
        cost_per_1m = _rpt_rate("ollama_cloud")
    elif key_name == "neuralwatt" and model:
        # Try exact match, then stripped prefix (deepseek/deepseek-v4-flash → deepseek-v4-flash)
        rates = NEURALWATT_RATES.get(model) or NEURALWATT_RATES.get(model.split("/")[-1])
        # NW-API: NeuralWatt uses ENERGY-based pricing. Our per-token estimate
        # overcounts by ~5.7× because 94% of prompt tokens are cached at a 5×
        # discount. Bring the logged cost_usd in line with the real bill by
        # multiplying by the correction factor from /v1/usage/summary totals
        # (real_api_total / db_sum_cost_usd, cached 10 min). Defaults to 1.0
        # (no correction) on any failure / bridge disabled, so we never
        # under-report.
        nw_correction = 1.0
        if _neuralwatt_cost_correction_fn is not None:
            try:
                nw_correction = float(_neuralwatt_cost_correction_fn() or 1.0)
            except Exception:
                nw_correction = 1.0
        if rates and prompt_tokens is not None and completion_tokens is not None:
            return (prompt_tokens * rates.get("input", 0.14)
                    + completion_tokens * rates.get("output", 0.28)) / 1_000_000 * nw_correction
        elif rates:
            return _blended_rate(rates["input"], rates["output"]) * total_tokens / 1_000_000 * nw_correction
        # No model match → fall through to flat provider rate (also scaled)
        cost_per_1m = _rpt_rate(key_name)
        if cost_per_1m == float("inf"):
            return float("inf")
        return (total_tokens / 1_000_000) * cost_per_1m * nw_correction
    else:
        cost_per_1m = _rpt_rate(key_name)
    if cost_per_1m == float("inf"):
        return float("inf")
    return (total_tokens / 1_000_000) * cost_per_1m


def _record_spend(key_name: str | None, model: str | None, total_tokens: int,
                  actual_cost: float | None = None,
                  prompt_tokens: int | None = None,
                  completion_tokens: int | None = None) -> None:
    """Record spend for today. Called from the finally block of every request.

    When actual_cost is provided (e.g., from DeepInfra's estimated_cost field),
    it is used directly instead of computing from the tracker. This
    captures prompt-caching discounts and real-time pricing changes.
    """
    try:
        tier = _spend_tier(key_name)
        cost = actual_cost if actual_cost is not None else _estimate_cost_usd(
            key_name, total_tokens, model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')  # UTC, not local IST
        _usage_db().execute(
            "INSERT INTO daily_spend (date, tier, spend_usd, call_count, token_count) "
            "VALUES (?,?,?,1,?) ON CONFLICT(date, tier) "
            "DO UPDATE SET spend_usd = spend_usd + excluded.spend_usd, "
            "call_count = call_count + 1, "
            "token_count = token_count + excluded.token_count",
            (today, tier, cost, total_tokens))
    except Exception:
        pass


# ── PPQ good-use policy (D6) ─────────────────────────────────────────────────
# Guardrails for when PPQ (api.ppq.ai) is refilled and again eligible as the
# last-resort external failover. Before D6, PPQ burned 21.6M tokens in 48h via
# unattended fallback traffic. Policy, enforced BEFORE any PPQ request:
#
#   * daily spend cap        (default $2.00/day)  — PPQ suspended at cap
#   * max requests per hour  (default 20)         — hard rate ceiling
#   * crash-retry-storm block (same request-body sha256 attempted >=3 times
#     within 10 minutes = crash-loop signature; PPQ refuses that prompt —
#     other prompts unaffected, failover falls through to openrouter)
#   * warning at 80% of the daily cap, raised through the existing
#     anomaly_events chain (anomaly-notify.sh 5-min cron delivers it;
#     alert_dedup.py backoff applies)
#
# Config precedence: PPQ_* env vars > ~/.hermes/bot/ppq_policy.json > defaults.
# Runtime state lives in the `ppq_daily_used` table in zai_usage.db — one row
# per day (spend_usd, requests, tokens, storm_blocked, per-hour counts).
# Fail-open on internal errors: a broken tracker must not block failover, the
# caps themselves are the safety net and they never raise.

PPQ_POLICY_FILE = Path.home() / ".hermes" / "bot" / "ppq_policy.json"
_PPQ_POLICY_DEFAULTS: dict = {
    "enabled": True,
    "daily_cap_usd": float('inf'),  # deactivated — merchant module governs spend
    "max_requests_per_hour": float('inf'),  # deactivated
    "storm_min_hits": float('inf'),  # deactivated
    "storm_window_s": 600,
    "alert_pct": 0.8,
}
_ppq_policy_cache: tuple[float, dict] = (0.0, dict(_PPQ_POLICY_DEFAULTS))
_ppq_prompt_attempts: dict[str, list[float]] = {}   # sha256(body) -> [timestamps]
_ppq_state_lock = threading.Lock()

_PPQ_ANOMALY_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS anomaly_events (\n"
    "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "    ts REAL NOT NULL,\n"
    "    severity TEXT NOT NULL,\n"
    "    category TEXT NOT NULL,\n"
    "    title TEXT,\n"
    "    detail TEXT,\n"
    "    alerted INTEGER DEFAULT 0,\n"
    "    resolved INTEGER DEFAULT 0\n"
    ")"
)


def _ppq_policy() -> dict:
    """Merged PPQ policy: env vars override ppq_policy.json overrides defaults.

    Cached for 60s — the failover path is rare (z.ai down), so a cached
    dict lookup per call is effectively free and keeps file IO off the
    hot path.
    """
    global _ppq_policy_cache
    now = time.time()
    cached_at, cached = _ppq_policy_cache
    if now - cached_at < 60:
        return cached
    merged = dict(_PPQ_POLICY_DEFAULTS)
    try:
        with open(PPQ_POLICY_FILE) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            merged.update(loaded)
    except Exception:
        pass  # no/invalid policy file -> defaults
    for env_key, cfg_key, conv in (
        ("PPQ_POLICY_ENABLED", "enabled",
         lambda v: str(v).strip().lower() not in ("0", "false", "no", "off")),
        ("PPQ_DAILY_CAP_USD", "daily_cap_usd", float),
        ("PPQ_MAX_REQ_PER_HOUR", "max_requests_per_hour", int),
        ("PPQ_STORM_MIN_HITS", "storm_min_hits", int),
        ("PPQ_STORM_WINDOW_S", "storm_window_s", int),
        ("PPQ_ALERT_PCT", "alert_pct", float),
    ):
        raw = os.environ.get(env_key)
        if raw is not None and raw.strip() != "":
            try:
                merged[cfg_key] = conv(raw)
            except (TypeError, ValueError):
                pass
    _ppq_policy_cache = (now, merged)
    return merged


def _ppq_hash_body(body: bytes) -> str:
    """Stable identity of a chat request for storm detection.

    Hashes the raw client body as received (before per-provider model
    rewriting) — a crash-retry loop re-sends byte-identical payloads.
    """
    import hashlib
    return hashlib.sha256(body or b"").hexdigest()


def _ppq_today() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')  # UTC, not local IST


def _ppq_hour_bucket() -> str:
    """UTC hour bucket, e.g. '2026-08-15T14' — matches alert/usage day basis."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def _ppq_usage_row() -> dict:
    """Today's ppq_daily_used row (zeroed dict when absent). Creates table."""
    today = _ppq_today()
    db = _usage_db()
    db.execute(
        "CREATE TABLE IF NOT EXISTS ppq_daily_used (\n"
        "    date TEXT PRIMARY KEY,\n"
        "    spend_usd REAL NOT NULL DEFAULT 0,\n"
        "    requests INTEGER NOT NULL DEFAULT 0,\n"
        "    tokens INTEGER NOT NULL DEFAULT 0,\n"
        "    storm_blocked INTEGER NOT NULL DEFAULT 0,\n"
        "    hour_requests TEXT NOT NULL DEFAULT '{}',\n"
        "    last_ts REAL NOT NULL DEFAULT 0\n"
        ")"
    )
    # Day rollover hygiene: auto-resolve yesterday's ppq_budget anomalies so
    # the anomaly chain doesn't show stale alerts after the cap resets.
    try:
        day_start = time.mktime(time.strptime(today, "%Y-%m-%d"))
        db.execute(
            "UPDATE anomaly_events SET resolved=1 "
            "WHERE category='ppq_budget' AND resolved=0 AND ts < ?",
            (day_start,))
    except Exception:
        pass
    row = db.execute(
        "SELECT spend_usd, requests, tokens, storm_blocked, hour_requests, last_ts "
        "FROM ppq_daily_used WHERE date=?", (today,)).fetchone()
    if row is None:
        return {"date": today, "spend_usd": 0.0, "requests": 0, "tokens": 0,
                "storm_blocked": 0, "hour_requests": "{}", "last_ts": 0.0}
    return {"date": today, "spend_usd": float(row[0]), "requests": int(row[1]),
            "tokens": int(row[2]), "storm_blocked": int(row[3]),
            "hour_requests": row[4] or "{}", "last_ts": float(row[5])}


def _ppq_alert(severity: str, title: str, detail: str) -> None:
    """Raise an anomaly via the existing anomaly_events chain. Never raises.

    anomaly-notify.sh (5-min no-agent cron) delivers unalerted+unresolved
    events; alert_dedup.py applies exponential backoff on repeated stable
    titles. Local dedup: one unresolved alert per title at a time.
    """
    try:
        db = _usage_db()
        db.execute(_PPQ_ANOMALY_SCHEMA)
        dup = db.execute(
            "SELECT 1 FROM anomaly_events WHERE category='ppq_budget' "
            "AND title=? AND resolved=0", (title,)).fetchone()
        if dup:
            return
        db.execute(
            "INSERT INTO anomaly_events (ts, severity, category, title, detail, "
            "alerted, resolved) VALUES (?,?, 'ppq_budget', ?,?,0,0)",
            (time.time(), severity, title, detail))
        print(f"[ppq-policy] ALERT [{severity}] {title}: {detail}", flush=True)
    except Exception as e:
        print(f"[ppq-policy] anomaly insert failed: {e}", flush=True)


def _ppq_gate_ok(prompt_hash: str) -> tuple[bool, str]:
    """Decide whether PPQ may serve this request. Pure-local, no network.

    Returns (ok, reason). reason explains the block in [ppq-policy] logs.
    Fail-open: if the tracker read raises, PPQ is allowed (the 402 path and
    spend caps downstream still bound the damage).
    """
    pol = _ppq_policy()
    if not pol.get("enabled", True):
        return True, "policy_disabled"
    try:
        usage = _ppq_usage_row()
    except Exception as e:
        print(f"[ppq-policy] tracker read failed ({e}) — fail-open", flush=True)
        return True, "tracker_error_fail_open"
    try:
        if usage["spend_usd"] >= float(pol["daily_cap_usd"]):
            _ppq_alert(
                "critical", "PPQ daily cap reached",
                f"${usage['spend_usd']:.2f} of ${float(pol['daily_cap_usd']):.2f} "
                f"used today — PPQ failover suspended until the cap resets at 00:00")
            return False, (f"daily_cap ${usage['spend_usd']:.2f}/"
                           f"${float(pol['daily_cap_usd']):.2f}")
        hours = json.loads(usage["hour_requests"] or "{}")
        this_hour = int(hours.get(_ppq_hour_bucket(), 0))
        if this_hour >= int(pol["max_requests_per_hour"]):
            return False, (f"hourly_cap {this_hour}/"
                           f"{int(pol['max_requests_per_hour'])}")
    except Exception as e:
        print(f"[ppq-policy] gate check failed ({e}) — fail-open", flush=True)
        return True, "gate_error_fail_open"
    # Crash-retry storm: same prompt attempted >= storm_min_hits times within
    # the storm window. Blocking is per-hash — other traffic still allowed.
    now = time.time()
    window = int(pol["storm_window_s"])
    min_hits = int(pol["storm_min_hits"])
    try:
        with _ppq_state_lock:
            ts = [t for t in _ppq_prompt_attempts.get(prompt_hash, [])
                  if now - t < window]
            if len(ts) >= min_hits:
                _ppq_count_storm_block()
                _ppq_alert(
                    "warning", "PPQ retry-storm blocked",
                    f"identical prompt (sha256 {prompt_hash[:8]}) attempted "
                    f"{len(ts)}x in {window}s — crash-loop signature, PPQ refused")
                return False, f"retry_storm {len(ts)} hits/{window}s"
    except Exception as e:
        print(f"[ppq-policy] storm check failed ({e}) — fail-open", flush=True)
        return True, "storm_error_fail_open"
    return True, "ok"


def _ppq_note_attempt(prompt_hash: str) -> None:
    """Record a PPQ attempt for storm detection (called pre-request)."""
    now = time.time()
    global _ppq_prompt_attempts
    with _ppq_state_lock:
        ts = _ppq_prompt_attempts.setdefault(prompt_hash, [])
        ts.append(now)
        _ppq_prompt_attempts[prompt_hash] = [t for t in ts if now - t < 3600]
        # Opportunistic GC — bounded memory even under pathological load.
        if len(_ppq_prompt_attempts) > 512:
            cutoff = now - 3600
            _ppq_prompt_attempts = {
                h: [t for t in v if t >= cutoff]
                for h, v in _ppq_prompt_attempts.items()
                if any(t >= cutoff for t in v)}


def _ppq_count_storm_block() -> None:
    """Bump today's storm_blocked counter (best-effort)."""
    try:
        _usage_db().execute(
            "INSERT INTO ppq_daily_used (date, storm_blocked) VALUES (?,1) "
            "ON CONFLICT(date) DO UPDATE SET storm_blocked = storm_blocked + 1",
            (_ppq_today(),))
    except Exception:
        pass


def _ppq_record_success(total_tokens: int, cost_usd: float | None) -> None:
    """Update the ppq_daily_used tracker after a successful PPQ response.

    Also fires the 80%-of-cap warning exactly once per crossing.
    Never raises — spend tracking must not break the response path.
    """
    try:
        pol = _ppq_policy()
        cap = float(pol["daily_cap_usd"])
        usage = _ppq_usage_row()
        prev = usage["spend_usd"]
        cost = cost_usd
        if cost is None:
            cost = _estimate_cost_usd("ppq", total_tokens)
        if cost == float("inf") or cost != cost:  # inf or NaN
            cost = 0.0
        cost = max(0.0, float(cost))
        hours = json.loads(usage["hour_requests"] or "{}")
        hb = _ppq_hour_bucket()
        hours[hb] = int(hours.get(hb, 0)) + 1
        _usage_db().execute(
            "INSERT INTO ppq_daily_used (date, spend_usd, requests, tokens, "
            "storm_blocked, hour_requests, last_ts) VALUES (?,?,1,?,0,?,?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "spend_usd = spend_usd + excluded.spend_usd, "
            "requests = requests + 1, "
            "tokens = tokens + excluded.tokens, "
            "hour_requests = excluded.hour_requests, "
            "last_ts = excluded.last_ts",
            (_ppq_today(), cost, total_tokens, json.dumps(hours), time.time()))
        new_total = prev + cost
        alert_at = float(pol.get("alert_pct", 0.8)) * cap
        if cap > 0 and prev < alert_at <= new_total:
            _ppq_alert(
                "warning", "PPQ daily spend at 80% of cap",
                f"${new_total:.2f} of ${cap:.2f} used today — PPQ failover "
                f"suspends at ${cap:.2f}")
    except Exception as e:
        print(f"[ppq-policy] record_success failed: {e}", flush=True)


# ── DeepInfra local credit balance tracking (no billing API available) ───────
def _init_deepinfra_balance():
    """Initialize DeepInfra balance table with starting balance if not exists."""
    try:
        db = _usage_db()
        db.execute(
            "CREATE TABLE IF NOT EXISTS deepinfra_balance ("
            "id INTEGER PRIMARY KEY CHECK (id = 1),"
            "balance_usd REAL NOT NULL,"
            "last_updated REAL NOT NULL,"
            "total_deducted REAL DEFAULT 0.0,"
            "total_requests INTEGER DEFAULT 0)")
        row = db.execute("SELECT balance_usd FROM deepinfra_balance WHERE id=1").fetchone()
        if not row:
            db.execute(
                "INSERT INTO deepinfra_balance (id, balance_usd, last_updated) VALUES (1, ?, ?)",
                (DEEPINFRA_STARTING_BALANCE, time.time()))
        db.commit()
    except Exception:
        pass


def _deduct_deepinfra_balance(cost: float) -> float:
    """Deduct actual cost from local DeepInfra balance. Returns remaining balance.

    When balance drops below $1.0, marks DeepInfra as unfunded so the failover
    system skips to the next provider (PPQ). Mirrors the existing 402 handler.
    """
    if cost <= 0:
        return _get_deepinfra_balance()
    try:
        db = _usage_db()
        db.execute(
            "UPDATE deepinfra_balance SET "
            "balance_usd = balance_usd - ?, "
            "last_updated = ?, "
            "total_deducted = total_deducted + ?, "
            "total_requests = total_requests + 1 WHERE id=1",
            (cost, time.time(), cost))
        db.commit()
        row = db.execute("SELECT balance_usd FROM deepinfra_balance WHERE id=1").fetchone()
        remaining = row[0] if row else 0.0
        if remaining < 1.0:
            _mark_unfunded("deepinfra")
        return remaining
    except Exception:
        return DEEPINFRA_STARTING_BALANCE


def _get_deepinfra_balance() -> float:
    """Get current DeepInfra balance. Returns starting balance on error."""
    try:
        row = _usage_db().execute("SELECT balance_usd FROM deepinfra_balance WHERE id=1").fetchone()
        return row[0] if row else DEEPINFRA_STARTING_BALANCE
    except Exception:
        return DEEPINFRA_STARTING_BALANCE


# Initialize balance on module load
if DEEPINFRA_KEY:
    _init_deepinfra_balance()


# ── Telnyx balance tracking (direct API) ────────────────────────────────────
def _get_telnyx_balance() -> float | None:
    """Fetch the current Telnyx account balance via the Telnyx API.

    GET https://api.telnyx.com/v2/balance → response.data.balance (USD).
    Returns None on any error (network, parse, auth). Never raises.
    """
    if not TELNYX_KEY:
        return None
    try:
        req = urllib.request.Request(
            "https://api.telnyx.com/v2/balance",
            headers={
                "Authorization": f"Bearer {TELNYX_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            # Telnyx balance API returns: {"data": {"balance": "12.34", ...}}
            balance = data.get("data", {}).get("balance")
            if balance is not None:
                return float(balance)
            return None
    except Exception:
        return None


def _extract_cost(provider: str | None, response_buffer: bytes | bytearray,
                  total_tokens: int = 0) -> tuple[float | None, str | None]:
    """Extract the real USD cost for one API call (RP-2).

    Returns ``(cost_usd, cost_source)`` or ``(None, None)``. Never raises.

    Resolution order:
      1. **Measured** — if the provider returns cost in the response body
         (openrouter ``usage.cost``, deepinfra ``usage.estimated_cost``, ppq
         and telnyx multi-path probe), parse it via src/cost_extraction.py.
         Source = 'measured'.
      2. **Flat-rate** — ours/friend (z.ai subscription): marginal cost is $0.
         Source = 'flat_rate'.
      3. **Estimated** — ollama_cloud (flat-rate, but compute an estimated
         per-call cost from the current quota regime rate × tokens so the
         real_price_tracker has a non-zero signal). Source = 'estimated'.
      4. **Rate-derived** — telnyx (when no in-body cost field is found):
         compute from token count × published rate so the balance collector
         has a non-zero spend signal. Source = 'rate_derived'.
      5. **Unknown** — provider is None/unknown or cost can't be determined.
         Returns (None, None).
    """
    try:
        if not provider:
            return (None, None)
        # 1. Paid providers: parse real cost from the response body.
        if _extract_cost_module is not None:
            cost, source = _extract_cost_module(provider, bytes(response_buffer))
            if cost is not None:
                return (cost, source)
        # 2. z.ai flat-rate subscription — marginal cost is always $0.
        if provider in ("ours", "friend"):
            return (0.0, "flat_rate")
        # 3. ollama_cloud flat-rate — estimate from regime rate × tokens.
        if provider == "ollama_cloud":
            rate = _get_ollama_cloud_cost_per_1m()
            if rate == float("inf"):
                # Exhausted regime — no meaningful cost; let it stay NULL.
                return (None, None)
            return ((total_tokens / 1_000_000) * rate, "estimated")
        # 3b. opencode_go — flat-rate $10/mo Go plan. Responses have no cost
        # field, but the opencode.ai dashboard meters usage at roughly
        # $0.43/M blended equivalent (derived from dashboard Nutzungsverlauf:
        # ~3M tokens ≈ $1.30 across mixed glm-5.3 sessions). Track the
        # equivalent cost so burn is visible in daily_spend and the Kalman
        # PriceKalman gets a real signal. Marginal cash cost remains $0
        # (included in plan); this is the metered-equivalent burn against
        # the plan's usage limits.
        if provider == "opencode_go":
            og_rate = 0.43  # $/M blended equivalent (dashboard-derived)
            return ((total_tokens / 1_000_000) * og_rate, "estimated")
        # 4. telnyx — if no in-body cost was found (step 1), derive from
        # per-model rates × token breakdown so the balance collector (which
        # sums cost_usd) has a non-zero signal. Source = 'rate_derived'.
        # Calibration factor from periodic balance API checks adjusts for
        # prompt-caching discounts (which Telnyx applies automatically).
        if provider == "telnyx":
            usage = _parse_usage(bytes(response_buffer))
            prompt_toks = int(usage.get("prompt_tokens") or 0)
            completion_toks = int(usage.get("completion_tokens") or 0)
            # Prompt-caching: Telnyx reports cached prompt tokens via the
            # OpenAI-compatible usage.prompt_tokens_details.cached_tokens
            # field.  Cached tokens are billed at ~17% of the input rate.
            # Without this, all prompt tokens were billed at the full input
            # rate → estimates ran ~1000x above real spend (prompt caching
            # makes our repeated-context workload ~99% cache hits).
            cached_toks = 0
            _ptd = usage.get("prompt_tokens_details") or {}
            if isinstance(_ptd, dict):
                cached_toks = int(_ptd.get("cached_tokens") or 0)
            # Fallback: if the response didn't report cached_tokens (Telnyx
            # SSE often omits prompt_tokens_details), estimate from the
            # rolling cache-hit ratio.  Without this, ALL prompt tokens get
            # billed at the full input rate, inflating cost ~6× and making
            # the calibration factor fight a losing battle.
            if cached_toks == 0 and prompt_toks > 0:
                hit = _refresh_telnyx_cache_hit_ratio()
                if hit > 0:
                    cached_toks = int(prompt_toks * hit)
            uncached_toks = max(prompt_toks - cached_toks, 0)
            # Extract model from the response to look up per-model rates
            model_name = None
            try:
                _obj = json.loads(bytes(response_buffer))
                if isinstance(_obj, dict):
                    model_name = _obj.get("model")
            except Exception:
                pass
            rates = _TELNYX_MODEL_RATES.get(model_name or "", {})
            if rates:
                input_rate = rates.get("input", 0.95)
                cached_rate = rates.get("cached_input", input_rate * 0.17)
                output_rate = rates.get("output", 4.00)
                raw_cost = (
                    uncached_toks * input_rate
                    + cached_toks * cached_rate
                    + completion_toks * output_rate
                ) / 1_000_000
                cost_source = "cached_rate_derived" if cached_toks > 0 else "rate_derived"
            else:
                # Fallback to blended rate if model not in table
                rate = _rpt_rate("telnyx")
                if rate == float("inf") or rate <= 0:
                    return (None, None)
                raw_cost = (total_tokens / 1_000_000) * rate
                cost_source = "rate_derived"
            calibrated = raw_cost * _telnyx_calibration_factor
            return (calibrated, cost_source)
        # 4b. NeuralWatt — per-model rate-derived cost from NEURALWATT_RATES.
        # NeuralWatt does not return a cost field in the response body, so
        # _extract_cost_module won't find it. Compute from per-model rates
        # × actual token breakdown for accurate cost_usd logging.
        if provider == "neuralwatt":
            usage = _parse_usage(bytes(response_buffer))
            prompt_toks = int(usage.get("prompt_tokens") or 0)
            completion_toks = int(usage.get("completion_tokens") or 0)
            cached_toks = 0
            _ptd = usage.get("prompt_tokens_details") or {}
            if isinstance(_ptd, dict):
                cached_toks = int(_ptd.get("cached_tokens") or 0)
            uncached_toks = max(prompt_toks - cached_toks, 0)
            model_name = None
            try:
                _obj = json.loads(bytes(response_buffer))
                if isinstance(_obj, dict):
                    model_name = _obj.get("model")
            except Exception:
                pass
            rates = NEURALWATT_RATES.get(model_name or "")
            if rates:
                input_rate = rates.get("input", 0.14)
                cached_rate = rates.get("cached_input", input_rate * 0.17)
                output_rate = rates.get("output", 0.28)
                raw_cost = (
                    uncached_toks * input_rate
                    + cached_toks * cached_rate
                    + completion_toks * output_rate
                ) / 1_000_000
                cost_source = "cached_rate_derived" if cached_toks > 0 else "rate_derived"
                # NW-CORRECTION: NeuralWatt overcounts usage by 3.6x (energy-based
                # pricing with cached token discounts not reflected in per-token
                # model). Hardcoded 0.2762 correction (1/3.6) — the lazy-loaded
                # function approach failed at runtime due to import scope /
                # threading issues. See design doc §8.
                nw_correction = 0.2762
                raw_cost = raw_cost * nw_correction
                return (raw_cost, cost_source)
            # Fallback: blended rate for the provider (still better than NULL)
            rate = _rpt_rate("neuralwatt")
            if rate == float("inf") or rate <= 0:
                return (None, None)
            raw_cost = (total_tokens / 1_000_000) * rate
            # NW-CORRECTION: Also apply hardcoded correction to the fallback
            # blended rate path for consistency (see comment above).
            nw_correction = 0.2762
            raw_cost = raw_cost * nw_correction
            return (raw_cost, "rate_derived")
        # 4c. Catch-all fallback — for ANY provider without a specific branch
        # above (e.g. routstr, routstrd, deepinfra, ppq, openrouter,
        # ollama_cloud_2, and any future provider), derive cost from the
        # Kalman-measured $/M rate × total_tokens. These providers don't
        # return a cost field in their response body (so step 1 misses them)
        # and don't have a dedicated extraction branch, so without this they
        # fell through to (None, None) → api_calls.cost_usd stayed NULL →
        # invisible burn. This prevents that for ALL current and future
        # no-branch providers. Source = 'rate_derived_fallback'.
        rate = _rpt_rate(provider)
        if rate is not None and rate > 0 and rate != float("inf"):
            return ((total_tokens / 1_000_000) * rate, "rate_derived_fallback")
        # 5. Unknown / unhandled provider.
        return (None, None)
    except Exception:
        return (None, None)


def _check_spend_cap(key_name: str | None) -> tuple[bool, float, float]:
    """Check if the daily spend cap allows this request.

    DEACTIVATED (2026-08-20): always allows. The merchant module markets
    and wallet balance decide routing, not hard-coded caps.
    """
    return (True, 0.0, float('inf'))


def _check_global_spend_cap() -> tuple[bool, float, float]:
    """Check today's METERED spend (real cash) against SPEND_CAP_METERED.

    Reactivated 2026-09-02 (D6, burn-reduction plan), scoped to METERED
    providers only (neuralwatt, routstr, routstrd, deepinfra, telnyx, ppq,
    openrouter) — the tiers where a request spends NEW money. Subscription
    lanes (ours/friend/ollama_cloud*/opencode_go) are excluded: their
    daily_spend rows are opportunity-cost accounting against already-paid
    quotas, and blocking them would 503 sessions while real quota remains
    (the failure mode that made the original 2026-08-20 hard caps harmful).

    env SPEND_CAP_METERED (USD/day, default 25.0). The market-based pricing
    (pressure FSM, depletion penalties, neuralwatt daily cap) remains the
    primary mechanism — this is the last-resort cash circuit breaker.
    """
    cap = float(os.environ.get("SPEND_CAP_METERED", "25.0"))
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = _usage_db().execute(
            "SELECT tier, spend_usd FROM daily_spend WHERE date=?",
            (today,)).fetchall()
    except Exception:
        return (True, 0.0, cap)
    metered = 0.0
    for tier, spend in rows or []:
        if tier in _METERED_SPEND_TIERS and spend:
            metered += float(spend)
    return (metered < cap, round(metered, 4), cap)


def _init_spend_table() -> None:
    """Create the daily_spend table if it doesn't exist."""
    try:
        _usage_db().execute(
            "CREATE TABLE IF NOT EXISTS daily_spend ("
            "date TEXT NOT NULL, "
            "tier TEXT NOT NULL, "
            "spend_usd REAL DEFAULT 0, "
            "call_count INTEGER DEFAULT 0, "
            "token_count INTEGER DEFAULT 0, "
            "PRIMARY KEY (date, tier))")
    except Exception:
        pass


_init_spend_table()


# ── quota polling (background thread) ───────────────────────────────────────

# Mapping from z.ai limit unit codes to human names + hour durations.
# Observed from the z.ai /api/monitor/usage/quota/limit endpoint:
#   TOKENS_LIMIT unit=3 (hour),   number=N → N-hour token window
#   TOKENS_LIMIT unit=6 (week),   number=N → N-week token window (168 h each)
#   TIME_LIMIT   unit=5 (month),  number=N → N-month tool-call window (720 h each)
# As of 2026-08-23, z.ai renamed TOKENS_LIMIT → CREDIT_LIMIT (same unit codes).
#   CREDIT_LIMIT unit=3, number=5 → 5-hour credit window
#   CREDIT_LIMIT unit=6, number=1 → weekly credit window (168 h)
_UNIT_META = {
    # (type, unit) → (label_for_single, hours_per_unit)
    # z.ai renamed TOKENS_LIMIT → CREDIT_LIMIT (observed 2026-08-23).
    # Both types use the same unit codes, so we map them identically.
    ("TOKENS_LIMIT", 3): ("hour",   1),
    ("TOKENS_LIMIT", 6): ("weekly", 168),
    ("TIME_LIMIT",   5): ("monthly", 720),
    ("CREDIT_LIMIT", 3): ("hour",   1),
    ("CREDIT_LIMIT", 6): ("weekly", 168),
    ("CREDIT_LIMIT", 5): ("monthly", 720),
}


def _parse_limit_entry(entry: dict) -> dict | None:
    """Parse a single ``limits[]`` entry from the z.ai quota API into a window dict.

    Returns ``{name, type, used_pct, resets_at, window_hours}`` or *None* if the
    entry is unrecognised (skipped, not counted as an error).
    """
    entry_type = entry.get("type", "")
    unit   = entry.get("unit", 0)
    number = entry.get("number", 0)
    pct    = int(entry.get("percentage", 0))
    reset_ms = entry.get("nextResetTime", 0)
    resets_at = int(reset_ms / 1000) if reset_ms else 0

    meta = _UNIT_META.get((entry_type, unit))
    if meta is None:
        return None                      # unknown window type — skip
    label, hours_per_unit = meta
    window_hours = number * hours_per_unit

    # Friendly names for the common single-unit windows
    if entry_type in ("TOKENS_LIMIT", "CREDIT_LIMIT") and unit == 3 and number == 5:
        name = "5-hour"
    elif number == 1:
        name = label if label not in ("hour",) else f"{number}-hour"
    else:
        name = f"{number}{label[0]}" if label != "hour" else f"{number}-hour"

    return {"name": name, "type": entry_type, "used_pct": pct,
            "resets_at": resets_at, "window_hours": window_hours}


def _fetch_quota_windows(key: str) -> list[dict]:
    """Fetch **all** quota windows for *key* from the z.ai monitoring API.

    Returns a list of window dicts (see :func:`_parse_limit_entry`).
    On network / parse error returns a single sentinel window with
    ``used_pct=999`` so the caller treats the key as locked.
    """
    try:
        req = urllib.request.Request(QUOTA_URL, headers={"Authorization": f"Bearer {key}", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        limits = data.get("data", {}).get("limits", [])
        windows = [w for w in (_parse_limit_entry(L) for L in limits) if w]
        return windows if windows else [
            {"name": "unknown", "type": "TOKENS_LIMIT",
             "used_pct": 0, "resets_at": 0, "window_hours": 0}]
    except Exception:
        return [{"name": "error", "type": "TOKENS_LIMIT",
                 "used_pct": 999, "resets_at": 0, "window_hours": 0}]


def _max_pct(windows: list[dict]) -> int:
    """Max ``used_pct`` across *windows* (backward-compat with lock logic)."""
    if not windows:
        return 0
    return max(w.get("used_pct", 0) for w in windows)


def is_key_locked(key_name: str, windows: list[dict]):
    """A key is locked if ANY window exceeds its fixed threshold.

    Proportional overage is handled as a cost penalty in the Kalman router
    (burn_predictor.py), NOT as a hard lock here. This lets the system keep
    working when both keys are slightly ahead of schedule.

    Returns (locked, window_name, used_pct, threshold).
    """
    for w in windows:
        name = w.get("name", "")
        pct = w.get("used_pct", 0)
        threshold = LOCK_THRESHOLDS.get(name, {}).get(key_name, 100)
        if pct >= threshold:
            return True, name, pct, threshold
    return False, None, 0, 0


def _calibrate_telnyx_rates():
    """Calibrate Telnyx cost estimates against the real balance API.

    Called every 10 minutes from _refresh_loop(). Queries the Telnyx balance
    API, compares the real balance delta (since last check) with the sum of
    estimated costs recorded in api_calls for the same period, and updates
    _telnyx_calibration_factor to correct for prompt-caching discounts and
    any rate discrepancies.

    Two windows are used:
      * first run after startup: snapshot the current balance and SKIP the
        factor update.  Historically the first run summed ALL historical
        cost_usd against (STARTING_BALANCE - current_balance), but the
        historical cost_usd rows were themselves computed with a stale
        factor, so the denominator was garbage and the factor either
        spiked to the ceiling (inflating future costs 10×) or floored
        (under-reporting).  A clean 10-minute window starts on the next
        tick.
      * steady state: 10-minute balance delta vs 10-minute estimated spend

    The factor floor is 0.001 because Telnyx prompt caching can make the
    effective price 3 orders of magnitude below list (measured 2026-08-20:
    $5.02 real vs $5,925 estimated since Aug 12 ≈ 0.00085).

    The factor CEILING is 1.0 — the calibration can only DISCOUNT the
    rate-derived estimate, never inflate it.  A ceiling above 1.0 creates a
    runaway feedback loop: a bad denominator (garbage historical cost_usd)
    pushes the factor up, which inflates future cost_usd, which raises the
    next denominator further.  This is what produced the 2026-08-20
    $377.14 "burn" on an account whose total balance only dropped $6.08.

    The calibration factor is applied to all future _extract_cost() results
    for telnyx until the next calibration. This gives accurate spend tracking
    without adding latency to individual requests.
    """
    global _telnyx_calibration_factor
    try:
        if not TELNYX_KEY:
            return
        # Query the real balance from Telnyx API
        real_balance = _get_telnyx_balance()
        if real_balance is None:
            return
        first_run = not hasattr(_calibrate_telnyx_rates, "_last_ts")
        # Get the last calibration timestamp
        last_ts = getattr(_calibrate_telnyx_rates, "_last_ts", None)
        now = time.time()
        # First run after startup: snapshot the current balance and skip
        # the factor update.  Summing ALL historical cost_usd (which was
        # computed with a stale/broken factor) against the lifetime balance
        # delta produces a garbage denominator and the factor either spikes
        # or floors.  A clean 10-minute window starts on the next tick.
        if first_run:
            _calibrate_telnyx_rates._last_ts = now
            _calibrate_telnyx_rates._last_balance = real_balance
            print(f"[telnyx] calibration(first-run): snapshot balance=${real_balance:.2f}, "
                  f"factor stays at {_telnyx_calibration_factor:.6f} (clean window starts next tick)",
                  flush=True)
            return
        # Sum estimated costs for telnyx since last calibration.
        try:
            row = _usage_db().execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM api_calls "
                "WHERE key_name = 'telnyx' AND ts >= ?",
                (last_ts,)).fetchone()
            estimated_spend = row[0] if row else 0.0
        except Exception:
            estimated_spend = 0.0
        # Get the balance at last calibration (or starting balance if first run)
        last_balance = getattr(_calibrate_telnyx_rates, "_last_balance", TELNYX_STARTING_BALANCE)
        real_spend = last_balance - real_balance
        if real_spend < 0:
            # Balance went UP — topped up (or refund). Rebasing the
            # baseline is the only sane move; skip factor update.
            _calibrate_telnyx_rates._last_ts = now
            _calibrate_telnyx_rates._last_balance = real_balance
            return
        # Calculate calibration factor
        if estimated_spend > 0 and real_spend > 0:
            factor = real_spend / estimated_spend
            # Clamp to [0.001, 1.0] — the factor can only DISCOUNT the
            # rate-derived estimate, never inflate it.  The ceiling was
            # 10.0 historically, which let a bad denominator (e.g. cached
            # garbage cost_usd rows from a prior broken factor) push the
            # factor to 10.0 and inflate future estimates 10×, creating a
            # runaway feedback loop (2026-08-20: $377.14 "burn" on a $6.08
            # account).  Prompt caching makes the true price up to ~1000×
            # below list rates, so the 0.001 floor is still needed.
            _telnyx_calibration_factor = max(0.001, min(1.0, factor))
            print(f"[telnyx] calibration: "
                  f"real_spend=${real_spend:.4f} "
                  f"estimated=${estimated_spend:.4f} factor={_telnyx_calibration_factor:.6f} "
                  f"balance=${real_balance:.2f}", flush=True)
        # Store state for next calibration
        _calibrate_telnyx_rates._last_ts = now
        _calibrate_telnyx_rates._last_balance = real_balance
    except Exception:
        pass  # calibration must never break the refresh loop


def _build_key_state_overview() -> str:
    """Build a comprehensive situation overview for the sustained-down alert.

    Shows: today's burn per provider, last-1h burn, key states with failure
    details, quota/allowance pressure, effective routing chains, and action hints.
    """
    import datetime as _dt
    lines = []
    now = time.time()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    try:
        db = _usage_db()

        # Today's burn
        spend_rows = db.execute(
            "SELECT tier, spend_usd, call_count, token_count "
            "FROM daily_spend WHERE date=?", (today,)).fetchall()
        total_spend = sum(r[1] for r in spend_rows) if spend_rows else 0

        # Last 1h burn
        h1_ago = now - 3600
        burn_rows = db.execute(
            "SELECT key_name, COUNT(*), SUM(total_tokens), "
            "AVG(duration_ms), MAX(duration_ms) "
            "FROM api_calls WHERE ts>? GROUP BY key_name", (h1_ago,)).fetchall()
    except Exception:
        spend_rows, burn_rows, total_spend = [], [], 0

    # Key states
    health = _snapshot_health()
    quota = _snapshot_quota()
    all_keys = ["ours", "friend", "ollama_cloud", "ollama_cloud_2", "ollama_cloud_3", "opencode_go",
                "neuralwatt", "deepinfra", "telnyx", "ppq", "openrouter",
                "routstr", "routstrd"]

    lines.append("")
    lines.append("═══ TODAY'S BURN ═══")
    for r in sorted(spend_rows, key=lambda x: -x[1]) if spend_rows else []:
        lines.append(f"  {r[0]:20s} ${r[1]:8.2f}  ({r[2]:5d} calls, {r[3]/1e6:.1f}M tokens)")
    lines.append(f"  {'Total':20s} ${total_spend:8.2f}")

    lines.append("")
    lines.append("═══ KEY STATES ═══")
    for name in all_keys:
        h = health.get(name, None)
        qh = _zai_key_health.get(name, {})
        qp = quota.get(name, {})
        key_present = bool(_EXTERNAL_KEYS.get(name) or KEYS.get(name))

        if not key_present and name not in ("ours", "friend"):
            status = "MISSING — no key configured"
        elif h is False:
            fail_count = qh.get("consecutive_failures", 0)
            err_type = qh.get("last_error_type", "?")
            backoff = qh.get("backoff_seconds", 0)
            status = f"UNHEALTHY — {err_type}, {backoff}s backoff, {fail_count} failures"
        elif h is True:
            quota_pct = qp.get("used_pct", 0) if isinstance(qp, dict) else 0
            regime = qp.get("regime", "") if isinstance(qp, dict) else ""
            if isinstance(quota_pct, (int, float)) and quota_pct >= 100:
                status = f"HEALTHY but quota locked ({quota_pct:.0f}% used)"
            else:
                status = "HEALTHY"
        else:
            status = "UNKNOWN"

        # Add quota/allowance info
        if name == "opencode_go":
            allow = _opencode_go_allowance.get("remaining_usd")
            if allow is not None:
                status += f" (allowance ${allow:.2f} remaining)"
        elif name in ("ollama_cloud", "ollama_cloud_2", "ollama_cloud_3"):
            sess = qp.get("session_used_pct", 0) if isinstance(qp, dict) else 0
            wkly = qp.get("weekly_used_pct", 0) if isinstance(qp, dict) else 0
            moly = qp.get("monthly_used_pct", 0) if isinstance(qp, dict) else 0
            if isinstance(sess, (int, float)):
                status += f" (session {sess:.0f}%, weekly {wkly:.0f}%, monthly {moly:.0f}%)"

        lines.append(f"  {name:20s} {status}")

    # Effective routing for common models
    lines.append("")
    lines.append("═══ EFFECTIVE ROUTING ═══")
    try:
        from flat_router import select_provider as _sp
        for model in ["glm-5.2", "deepseek/deepseek-v4-flash", "kimi-k3"]:
            candidates = _sp(model=model)
            chain = " → ".join(c.name for c in candidates[:6] if c.name != "fallback")
            first_healthy = next((c.name for c in candidates if c.name != "fallback"
                                  and health.get(c.name, True)), "NONE")
            lines.append(f"  {model:35s} chain: {chain}")
            lines.append(f"  {'':35s} first healthy: {first_healthy}")
    except Exception:
        lines.append("  (flat router unavailable)")

    # Last 1h burn
    lines.append("")
    lines.append("═══ LAST 1H BURN ═══")
    for r in sorted(burn_rows, key=lambda x: -x[2]) if burn_rows else []:
        lines.append(f"  {r[0]:20s} {r[1]:4d} calls, {r[2]/1e6:6.1f}M tokens, "
                     f"avg {r[3]:.0f}ms, max {r[4]}ms")

    lines.append("")
    lines.append("═══ ACTION NEEDED ═══")
    for name in all_keys:
        if health.get(name) is False and name in _key_down_since:
            down_min = (now - _key_down_since[name]) / 60
            qh = _zai_key_health.get(name, {})
            err = qh.get("last_error_type", "?")
            if err == "dead":
                lines.append(f"  {name}: DEAD key (401/403) — rotate the API key")
            elif err == "dispatch_fail":
                lines.append(f"  {name}: transient dispatch failures ({down_min:.0f}min) — check network/provider status")
            elif err == "exhausted":
                lines.append(f"  {name}: quota exhausted — wait for reset or use a different provider")

    return "\n".join(lines)


def _check_sustained_down():
    """Check for keys that have been unavailable for ≥15 min and fire alerts.

    Called from _refresh_loop every CACHE_TTL (5 min). Fires ONCE per down
    period (tracked by _key_alerted). Writes a CRITICAL anomaly + journald
    print + espeak-ng voice notice.
    """
    try:
        now = time.time()
        for name, down_since in list(_key_down_since.items()):
            if _key_alerted.get(name):
                continue
            down_duration = now - down_since
            if down_duration >= _KEY_SUSTAINED_DOWN_THRESHOLD:
                _key_alerted[name] = True
                overview = _build_key_state_overview()
                alert_text = (
                    f"KEY SUSTAINED DOWN: \"{name}\" unavailable "
                    f"{down_duration/60:.0f}min (since {time.ctime(down_since)})"
                    f"{overview}"
                )
                # 1. Anomaly table (surfaced by anomaly-notify.sh cron)
                _log_anomaly("CRITICAL", "KEY_SUSTAINED_DOWN",
                             f"{name} unavailable {down_duration/60:.0f}min",
                             alert_text, key_name=name)
                # 2. Journald (immediate visibility)
                print(f"\n{'='*60}", flush=True)
                print(alert_text, flush=True)
                print(f"{'='*60}\n", flush=True)
                # 3. Voice notification (natural Piper voice via say.sh;
                #    espeak-ng fallback inside the wrapper guarantees audibility)
                try:
                    import subprocess as _sp
                    _sp.Popen(["/home/c03rad0r/.hermes/voices/say.sh",
                               f"Alert: key {name} has been unavailable for "
                               f"{down_duration/60:.0f} minutes"],
                              stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                except Exception:
                    pass
    except Exception:
        pass


def _refresh_loop():
    _refresh_iteration = 0
    while True:
        _refresh_iteration += 1
        with lock:
            for name, key in KEYS.items():
                quota_cache[name] = (_fetch_quota_windows(key), time.time())
            STATE_FILE.write_text(json.dumps(
                {n: {"max_pct": _max_pct(v[0]), "windows": v[0],
                     "age_s": int(time.time() - v[1])}
                 for n, v in quota_cache.items()}
                | {"active": _best_unlocked()[0]}, indent=2))
        # B2 (provider-clock-alignment): persist nextResetTime into
        # quota_clock registry so downstream consumers (price_viz ASCII /
        # heatmaps) can show "resets in N hours" columns. Runs every refresh
        # cycle (5 min) for every z.ai key. Idempotent + best-effort.
        try:
            import sys as _sys
            _scrp = str(Path(__file__).resolve().parent / "src")
            if _scrp not in _sys.path:
                _sys.path.insert(0, _scrp)
            import quota_clock as _qc
            for name, key in KEYS.items():
                _qc.refresh_zai_anchors(key, name)
        except Exception:
            pass
        # Refresh burn predictions (OUTSIDE lock — predict_exhaustion does a
        # safe self-HTTP GET to /quota which itself acquires lock).
        for name in KEYS:
            try:
                _get_predictions(name)
            except Exception:
                pass
        # ── Phase 2.4: Compute pace windows for LiveRouter ───────────────
        # After quota refresh, compute pace_factor input tuples from
        # quota_cache + LiveRouter's ConsumptionKalman burn rates. Stored
        # in _pace_windows for best_key() to pass to select_failover().
        # NEVER blocks quota refresh — wrapped in try/except.
        try:
            global _pace_windows
            if _LIVE_ROUTER is not None:
                with lock:
                    pw = _LIVE_ROUTER.compute_pace_windows(dict(quota_cache))
                if pw:
                    with lock:
                        _pace_windows = pw
        except Exception:
            pass  # pace window computation must never block refresh
        # ── Telnyx balance calibration (every 10 min = every 2nd iteration) ──
        # Compares real Telnyx balance API with estimated costs to compute a
        # calibration factor that corrects for prompt-caching discounts.
        if _refresh_iteration % 2 == 0:
            _calibrate_telnyx_rates()
        # Check for keys that have been unavailable for ≥15 min
        _check_sustained_down()
        time.sleep(CACHE_TTL)


def _best_unlocked():
    """Choose the best key using **per-window** lock thresholds.

    A key is "locked" when *any* of its windows meets/exceeds its threshold in
    :data:`LOCK_THRESHOLDS`.

    Returns ``(chosen, reason, ours_pct, friend_pct, ours_available,
    friend_available)`` — same signature as before so all callers stay
    compatible.

    Selection logic:
      * both locked   → least bad (lowest max_pct); reason ``fallback``
      * exactly one locked → use the other; reason embeds the locked window,
        e.g. ``only_available_friend_locked_weekly_80pct``
      * neither locked → lowest **weekly** percentage (prefer preserving quota);
        reason ``lowest_quota``
      * empty cache   → ``empty_cache`` (defaults to ours)
    """
    if not quota_cache:
        return ("ours", "empty_cache", 0, 0, 0, 0)

    ours_windows   = quota_cache.get("ours",   ([], 0.0))[0]
    friend_windows = quota_cache.get("friend", ([], 0.0))[0]

    op = _max_pct(ours_windows)
    fp = _max_pct(friend_windows)

    o_locked, o_lwin, o_lpct, o_lthr = is_key_locked("ours",   ours_windows)
    f_locked, f_lwin, f_lpct, f_lthr = is_key_locked("friend", friend_windows)

    oa = 0 if o_locked else 1
    fa = 0 if f_locked else 1

    # both locked → least bad (lowest max_pct); tie → ours (preferred)
    if o_locked and f_locked:
        chosen = "ours" if op <= fp else "friend"
        reason = (f"fallback_both_locked_"
                  f"ours_{o_lwin}_{o_lpct}pct_friend_{f_lwin}_{f_lpct}pct")
        return (chosen, reason, op, fp, 0, 0)

    # exactly one locked → use the other; note which window triggered the lock
    if o_locked:
        reason = f"only_available_ours_locked_{o_lwin}_{o_lpct}pct"
        return ("friend", reason, op, fp, 0, 1)
    if f_locked:
        reason = f"only_available_friend_locked_{f_lwin}_{f_lpct}pct"
        return ("ours", reason, op, fp, 1, 0)

    # neither locked → prefer the CHEAPER key (cost-aware tie-break).
    # Per _KEY_COST_MULTIPLIER, ours (1.0) is cheaper than friend (1.21), so
    # we default to ours. We own it; friend's key is a courtesy fallback.
    # If ours has been manually disabled or is mid-backoff, _is_key_healthy
    # catches that in Phase 4 of best_key() and switches to friend there.
    ours_cost  = _KEY_COST_MULTIPLIER.get("ours",   1.0)
    friend_cost = _KEY_COST_MULTIPLIER.get("friend", 1.0)
    if friend_cost < ours_cost:
        chosen, reason = "friend", (f"cost_aware_friend_{friend_cost}_cheaper_"
                                    f"ours_{ours_cost}_o{op}pct_f{fp}pct")
    else:
        chosen, reason = "ours", (f"cost_aware_prefer_ours_both_unlocked_"
                                  f"ours_{ours_cost}_friend_{friend_cost}_"
                                  f"o{op}pct_f{fp}pct")
    return (chosen, reason, op, fp, 1, 1)


def best_key() -> str:
    """LEGACY binary key-pick ("ours" | "friend").

    Legacy status (marked 2026-08-16, t_1bd8747e, from the 2026-08-15
    code-read): this is the original two-key selector that predates the
    price-argmin selection path now provided by the RoutingAdvisor /
    routing_optimizer (Phase 2.1, ADR-014).  It is NOT removed because it
    still has live callers and remains the guaranteed fallback whenever
    the advisor is disabled, unavailable, or fails to produce a pick.

    Live callers (all in this module):
      * _best_key_adapter()        — wraps best_key() for the RoutingAdvisor's
                                     fallback path (advisor mode).
      * request-path advisor       — `if chosen is None: chosen = best_key()`
                                     after the advisor attempt fails.
      * original cascade (flag off)— `chosen = best_key()` when the advisor
                                     feature flag is OFF or its module is
                                     unavailable.
      * /tier endpoint             — `chosen = best_key()` for tier queries.

    Selection: PROACTIVE prediction first — Kalman burn-rate predictions
    pick the key least likely to exhaust before its window resets.
    Predictions are fetched OUTSIDE the quota lock (the predictor does a
    safe self-HTTP GET to /quota).  Reactive (fallback): when predictions
    are unavailable (cold start, no data), fall back to per-window lock
    thresholds in _best_unlocked().

    Safety: a predictor failure never breaks key selection — every path is
    wrapped so the proxy always returns a valid key.
    """
    # Phase 1 — PROACTIVE: use Kalman predictions as the primary signal -------
    chosen = None
    reason = ""
    try:
        our_preds = _get_predictions("ours")
        friend_preds = _get_predictions("friend")
        our_exhaust = _will_exhaust(our_preds)
        friend_exhaust = _will_exhaust(friend_preds)

        if our_exhaust is not None and friend_exhaust is None:
            # Our key predicted to exhaust, friend is safe
            chosen = "friend"
            reason = (f"proactive_ours_exhausts_{our_exhaust.get('window','?')}"
                      f"_friend_safe")
        elif friend_exhaust is not None and our_exhaust is None:
            # Friend predicted to exhaust, our key is safe
            chosen = "ours"
            reason = (f"proactive_friend_exhausts_{friend_exhaust.get('window','?')}"
                      f"_ours_safe")
        elif our_exhaust is not None and friend_exhaust is not None:
            # Both exhausting — pick the one that lasts longer
            our_hours = our_exhaust.get("exhausts_in_hours") or 0
            friend_hours = friend_exhaust.get("exhausts_in_hours") or 0
            if friend_hours > our_hours:
                chosen = "friend"
                reason = ("proactive_both_exhausting_prefer_friend_longer_"
                          f"{friend_hours:.1f}h_ours_{our_hours:.1f}h")
            else:
                chosen = "ours"
                reason = ("proactive_both_exhausting_prefer_ours_longer_"
                          f"{our_hours:.1f}h_friend_{friend_hours:.1f}h")
    except Exception:
        pass  # predictor failure → fall through to reactive

    # Also record quota percentages for the log (read outside lock if possible)
    op = fp = 0
    try:
        with lock:
            op = _max_pct(quota_cache.get("ours", ([], 0.0))[0])
            fp = _max_pct(quota_cache.get("friend", ([], 0.0))[0])
    except Exception:
        pass

    # Phase 2 — REACTIVE fallback (when predictions not available) ------------
    if chosen is None:
        with lock:
            chosen, reason, op, fp, oa, fa = _best_unlocked()
    else:
        # Proactive gave us a choice — still determine availability flags
        # from reactive thresholds for the log
        with lock:
            ours_w = quota_cache.get("ours", ([], 0.0))[0]
            friend_w = quota_cache.get("friend", ([], 0.0))[0]
            o_locked, *_ = is_key_locked("ours", ours_w)
            f_locked, *_ = is_key_locked("friend", friend_w)
            oa = 0 if o_locked else 1
            fa = 0 if f_locked else 1

    # Phase 3 — RECOVER: if the non-chosen (previously locked) key has recovered
    # below threshold, prefer it without waiting for next 5-min refresh.  This
    # runs regardless of whether we used proactive or reactive selection.
    try:
        locked_key = "friend" if chosen == "ours" else "ours"
        locked_windows = quota_cache.get(locked_key, ([], 0.0))[0]
        locked_now, *_ = is_key_locked(locked_key, locked_windows)
        if not locked_now:
            # Locked key has recovered — re-evaluate (but only from reactive,
            # to avoid oscillation from stale predictions)
            with lock:
                reactive_choice, reactive_reason, _, _, _, _ = _best_unlocked()
            if reactive_choice != chosen:
                chosen = reactive_choice
                reason = f"proactive_recover_{locked_key}_unlocked"
    except Exception:
        pass  # NEVER break key selection

    # Phase 4 — HEALTH CHECK: skip exhausted keys (empty response / 429)
    if chosen and not _is_key_healthy(chosen):
        other = "friend" if chosen == "ours" else "ours"
        if _is_key_healthy(other):
            chosen = other
            reason = f"health_switch_{other}_other_exhausted"
        else:
            chosen = None
            reason = "both_keys_exhausted"

    # Phase 5 — LIVE ROUTER FAILOVER (Phase 1.2) ─────────────────────────
    # ONLY fires when both z.ai keys are exhausted (chosen is None after
    # Phase 4 health check).  Asks LiveRouter for the cheapest viable
    # external provider via Kalman-converged pricing.  Kill switch:
    # ~/.hermes/bot/.enable_live_routing must exist.  Every call wrapped
    # in try/except — on any failure, falls through to None and the
    # hardcoded ollama → ppq → openrouter chain in _proxy() runs.
    # Phase 5 — LIVE ROUTER FAILOVER (Phase 1.2) ─────────────────────────
    # Fires when best_key()'s INITIAL health check already sees both z.ai
    # keys exhausted (chosen is None). Asks LiveRouter for the cheapest
    # viable external provider via Kalman-converged pricing. Kill switch
    # (.enable_live_routing) + safe fallthrough live inside _consult_live_router.
    #
    # NOTE: this is the LESS common path. In production best_key() usually
    # returns a key whose health cache lags the real 429; that key 429s
    # mid-request and the request-handler retry loop exhausts both keys
    # DURING the loop. That retry-loop terminal fallback now also calls
    # _consult_live_router() (P3.4 Fix 1) — previously it bypassed
    # LiveRouter entirely (841 dual-exhaustion events/2h, 0 live events).
    if chosen is None:
        _pick, _pick_model, _fb, _fb_model = _consult_live_router()
        if _pick:
            chosen = _pick
            reason = f"live_kalman_failover_{_pick}"
            _log_key_decision(chosen_key=chosen, reason=reason,
                              ours_pct=op, friend_pct=fp,
                              ours_available=oa, friend_available=fa)
            return chosen

    _log_key_decision(chosen_key=chosen, reason=reason, ours_pct=op,
                      friend_pct=fp, ours_available=oa, friend_available=fa)
    return chosen


# Constants for retry logic
TRANSIENT_ERRORS = {404, 429, 500, 502, 503, 504}
RETRYABLE_EXCEPTIONS = (
    "Broken pipe",
    "Connection reset",
    "Connection timed out",
    "Remote end closed connection without response",
)

def _is_retryable_error(error):
    """Check if an error should trigger a retry."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in TRANSIENT_ERRORS
    error_str = str(error)
    return any(err in error_str for err in RETRYABLE_EXCEPTIONS)

def _attempt_retry(e, attempt, name, t0, key_order):
    """Retry with binary exponential backoff.

    Between key switches: short jittered delay (prevents hammering endpoint).
    Full cycle (all keys tried): exponential backoff with Kalman override.
    """
    import random

    if attempt >= len(key_order) - 1:
        # All keys exhausted — full backoff cycle
        _log_rate_limit(key_used=name, attempt=attempt, duration_ms=int((time.time() - t0) * 1000))
        retry_num = attempt - len(key_order) + 1
        if retry_num >= 50:
            return False  # Safety cap exhausted
        elif _rate_limit_predictor is not None:
            _rate_limit_predictor.record_429()
            wait = _rate_limit_predictor.predict_retry_at()
            time.sleep(wait)
            return True
        else:
            # Binary exponential: 2s, 4s, 8s, 16s, 32s, 60s cap
            wait = min(2 ** (retry_num + 1), 60)
            wait *= (0.75 + random.random() * 0.5)
            time.sleep(wait)
            return True
    else:
        # Between key switches — brief delay to let endpoint recover
        time.sleep(1 + random.random())  # 1-2s jitter
        return True

# ── proxy handler ───────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _pressure_headers(self, original_model: str | None,
                          served_model: str | None,
                          model_tier_info: dict | None) -> list[tuple[str, str]]:
        """Observability headers for the silent model rewrite (S2b, t_4dfaf0d5).

        Emitted ONLY when the rewrite actually changed the model — the
        rewrite behavior itself is untouched (shadow-first). Headers:
          X-Served-Model:    the model actually served
          X-Downgrade-Reason: tier reason + pressure shadow note (if any)
        Pure function of its args + self._pressure_decision; NEVER raises.
        """
        try:
            if not original_model or not served_model \
                    or served_model == original_model:
                return []
            parts = []
            if model_tier_info and model_tier_info.get("reason"):
                parts.append(str(model_tier_info["reason"])[:100])
            else:
                parts.append("tier_rewrite")
            pd = getattr(self, "_pressure_decision", None)
            if pd is not None and getattr(pd, "reason", None):
                parts.append(f"shadow:{pd.reason}")
            return [("X-Served-Model", str(served_model)),
                    ("X-Downgrade-Reason", "; ".join(parts)[:200])]
        except Exception:
            return []

    def _pressure_tracker_snapshot(self, limit: int = 20) -> dict:
        """Snapshot for GET /pressure (S2b, t_4dfaf0d5). NEVER raises."""
        if _pressure_tracker is None:
            return {"enabled": False, "mode": "unavailable",
                    "hint": "pressure_fsm module not loaded"}
        try:
            return _pressure_tracker.snapshot(limit=limit)
        except Exception as e:
            return {"enabled": False, "mode": "error", "error": str(e)}

    def _try_ollama_cloud(self, body: bytes, model: str | None,
                           response_buffer: bytearray, t0: float,
                           reason: str | None = None,
                           key_name: str = "ollama_cloud",
                           api_key: str | None = None) -> bool:
        """Forward request to Ollama Cloud API (primary provider, not failover).

        Ollama Cloud is a $20/mo flat-rate subscription with no per-token cost.
        During z.ai peak hours (UTC 6-10), z.ai burns 3x quota — Ollama has no
        peak pricing, making it the preferred provider during peak.

        reason: optional override for the key-decision log (used by the S2c
        pressure enforce hook); None keeps the historical peak/exhausted
        reasons so existing call sites are unchanged.

        key_name / api_key: which Ollama Cloud key to use. Defaults to the
        original key #1 ("ollama_cloud" / OLLAMA_CLOUD_KEY) for backward
        compatibility. Pass key_name="ollama_cloud_2" + the second key to
        use the second subscription account.

        Returns True on success (response already sent),
        False on failure (caller should try next provider).
        """
        _api_key = api_key if api_key is not None else OLLAMA_CLOUD_KEY
        if not _api_key:
            return False
        if not _is_key_healthy(key_name):
            return False

        # Map model names: z.ai names work directly on Ollama Cloud API
        # (glm-5.2 → glm-5.2, no :cloud suffix needed for direct API).
        # glm-5.3 / glm-5.3-flash pass through verbatim — Ollama Cloud serves
        # them natively (2026-08-29 live verification). For deepseek models,
        # translate to Ollama's tagged IDs via _PROVIDER_MODEL_NAMES
        # (e.g. deepseek/deepseek-v4-flash → deepseek-v4-flash:0731).
        ollama_model = model or "glm-5.2"
        _oc_map = _PROVIDER_MODEL_NAMES.get(key_name, {})
        ollama_model = _oc_map.get(ollama_model, ollama_model)

        try:
            body_json = json.loads(body) if body else {}
            body_json["model"] = ollama_model
            fwd_body = json.dumps(body_json).encode()

            url = OLLAMA_CLOUD_BASE + "/chat/completions"
            hdrs = {
                "Authorization": f"Bearer {_api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            }

            req = urllib.request.Request(url, data=fwd_body, method="POST", headers=hdrs)
            with urllib.request.urlopen(req, timeout=180) as resp:
                self.send_response(resp.status)
                self._response_started = True  # response bytes committed — no retry
                for h, v in resp.headers.items():
                    if h.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(h, v)
                self.send_header("X-Provider", key_name)
                # FIX-1b (2026-09-02): ollama reasoning models (glm-5.3,
                # qwen3.5:397b, ...) can spend the entire completion budget
                # in the separate "reasoning" field and return content="" —
                # clients that only read message.content then store empty
                # assistant turns (the degenerate-delegate mechanism from
                # tonight's incidents). For NON-streaming requests, buffer
                # the body and inject reasoning into content before
                # forwarding (same treatment _try_zai_key gives
                # "reasoning_content"). Streaming requests forward unchanged.
                if not body_json.get("stream"):
                    full_body = resp.read()
                    try:
                        resp_json = json.loads(full_body.decode("utf-8", errors="ignore"))
                        _msg = (resp_json.get("choices") or [{}])[0].get("message", {})
                        _content = _msg.get("content", "")
                        if not _content or not _content.strip():
                            _reason = (_msg.get("reasoning_content")
                                      or _msg.get("reasoning") or "")
                            if isinstance(_reason, str) and _reason.strip():
                                _msg["content"] = _reason
                                full_body = json.dumps(resp_json).encode()
                    except Exception:
                        pass
                    response_buffer.extend(full_body)
                    self.send_header("Content-Length", str(len(full_body)))
                    self.end_headers()
                    self.wfile.write(full_body)
                    self.wfile.flush()
                else:
                    self.end_headers()
                    while True:
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        response_buffer.extend(chunk)
                        self.wfile.write(chunk)
                        self.wfile.flush()

                # Parse usage for spend tracking
                ollama_usage = _parse_usage(bytes(response_buffer))
                ollama_tokens = int(ollama_usage.get("total_tokens") or 0)
                _record_spend(key_name, ollama_model, ollama_tokens)
                self._spend_recorded = True
                _mark_key_healthy(key_name)
                if _LIVE_ROUTER is not None:
                    try:
                        _LIVE_ROUTER.record_request(provider=key_name, tokens=ollama_tokens)
                    except Exception:
                        pass
                # RP-2: extract real cost (estimated from regime rate × tokens)
                _oc_cost, _oc_cost_src = _extract_cost(
                    key_name, bytes(response_buffer), ollama_tokens)
                _log_api_call(
                    key_name=key_name, key_suffix=_api_key[-4:],
                    model=ollama_model,
                    prompt_tokens=int(ollama_usage.get("prompt_tokens") or 0),
                    completion_tokens=int(ollama_usage.get("completion_tokens") or 0),
                    total_tokens=ollama_tokens,
                    tier=key_name, status_code=resp.status, error=None,
                    duration_ms=int((time.time() - t0) * 1000),
                    cost_usd=_oc_cost, cost_source=_oc_cost_src,
                    session_id=getattr(self, "_session_id", None),
                    task_type=getattr(self, "_task_type", None),
                )
                # Log key decision so dashboard shows the switch to ollama_cloud
                _log_key_decision(
                    chosen_key=key_name,
                    reason=reason or ("peak_hour_ollama_primary" if _is_peak_hour() else "zai_both_keys_exhausted_ollama_fallback"),
                )
                return True

        except urllib.error.HTTPError as he:
            if he.code == 429:
                _mark_key_exhausted(key_name)
            elif he.code == 403:
                # G1: quota-exhaustion on the largest plan surfaces as
                # 403 "requires a subscription" (not 429). Read the body to
                # distinguish a real paywall from an auth/ACL problem, then
                # arm the persisted until-Monday flag so quota_pressure sends
                # the price to +inf (routing avoids Ollama on PRICE, not just
                # health) and the health tracker skips it too.
                try:
                    body403 = he.read(4096).decode(errors="ignore").lower()
                except Exception:
                    body403 = ""
                if "subscription" in body403 or "upgrade" in body403:
                    _mark_key_exhausted(key_name)
                    _arm_ollama_paywall_flag(key_name)
                    print(f"[ollama] 403 paywall — armed paywall flag for {key_name} "
                          f"(price → +inf until Monday reset)", flush=True)
            return False
        except Exception:
            return False

    # ── z.ai key dispatch (Phase 3 flat router) ───────────────────────────
    # Extracted from the old inline retry loop in _proxy(). This method
    # proxies a single request to the z.ai upstream using a specific key.
    # It handles: path stripping, auth, response buffering, empty/error
    # detection, reasoning-content injection, and key health marking.
    # Returns True on success (response sent to client), False on failure.

    def _try_zai_key(self, key_name: str, body: bytes, model: str | None,
                     response_buffer: bytearray, t0: float) -> bool:
        """Proxy a single request to z.ai upstream using the named key.

        Args:
            key_name: "ours" or "friend"
            body: request body bytes
            model: model name (for logging, may be rewritten by tier routing)
            response_buffer: bytearray to extend with response bytes
            t0: request start time

        Returns True on success (response already sent to client),
        False on failure (caller should try next candidate).
        """
        key = KEYS.get(key_name)
        if not key:
            return False
        try:
            path = self.path
            if path.startswith("/v1/"):
                path = path[3:]
            if not path.endswith("/chat/completions"):
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"only /chat/completions is proxied"}')
                return True  # response sent (404), not a failure to retry
            url = UPSTREAM + path
            hdrs = {k: v for k, v in self.headers.items()
                    if k.lower() not in ("host", "authorization", "connection", "content-length")}
            hdrs["Authorization"] = f"Bearer {key}"
            hdrs["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=body, method=self.command, headers=hdrs)
            with urllib.request.urlopen(req, timeout=180) as resp:
                full_body = resp.read()

                # Check for empty or error response
                resp_text = full_body.decode('utf-8', errors='ignore').strip()
                is_empty = (not resp_text or resp_text == "data: [DONE]")

                is_error_response = False
                is_truncated = False
                if not is_empty:
                    try:
                        resp_json = json.loads(resp_text)
                        if "error" in resp_json and "choices" not in resp_json:
                            is_error_response = True
                        else:
                            choices = resp_json.get("choices", [])
                            if choices:
                                msg_obj = choices[0].get("message", {})
                                content = msg_obj.get("content", "")
                                finish_reason = choices[0].get("finish_reason", "")
                                if finish_reason == "length":
                                    is_truncated = True
                                if not content or not content.strip():
                                    # FIX-1 (2026-09-02): z.ai names the field
                                    # "reasoning_content"; ollama + neuralwatt
                                    # name it "reasoning". Accept either so
                                    # ollama-routed glm responses with
                                    # content="" are not treated as empty.
                                    reasoning = (msg_obj.get("reasoning_content")
                                                 or msg_obj.get("reasoning") or "")
                                    if isinstance(reasoning, str) and reasoning.strip():
                                        msg_obj["content"] = reasoning
                                        full_body = json.dumps(resp_json).encode()
                                        is_empty = False
                                    else:
                                        is_empty = True
                    except Exception:
                        pass

                if is_error_response:
                    # Transient error — don't mark key exhausted, just failover
                    return False

                if is_empty:
                    # Key produced nothing — don't mark exhausted, just failover
                    return False

                # Non-empty response — send to client
                _mark_key_healthy(key_name)
                try:
                    self.send_response(resp.status)
                    self._response_started = True  # response bytes committed — no retry
                    for h, v in resp.headers.items():
                        if h.lower() not in ("transfer-encoding", "connection"):
                            self.send_header(h, v)
                    self.send_header("X-Provider", f"zai:{key_name}")
                    if is_truncated:
                        self.send_header("X-Response-Truncated", "true")
                    self.end_headers()
                    response_buffer.extend(full_body)
                    self.wfile.write(full_body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # Client vanished — upstream succeeded, key stays healthy
                    pass
                # Success — reset rate limit predictor
                if _rate_limit_predictor is not None:
                    _rate_limit_predictor.record_success()
                return True

        except urllib.error.HTTPError as e:
            if e.code == 429:
                _mark_key_exhausted(key_name)
            elif e.code in (401, 403):
                _body_403 = b""
                try:
                    _body_403 = e.read()
                except Exception:
                    pass
                _body_403_lower = _body_403.decode(errors="ignore").lower()
                if any(_kw in _body_403_lower for _kw in
                       ("quota", "exhaust", "limit", "rate", "exceed",
                        "insufficient", "weekly", "session")):
                    _mark_key_exhausted(key_name)
                else:
                    _mark_key_dead(key_name)
            elif e.code in (500, 502, 503, 504):
                _mark_key_server_error(key_name)
            return False

        except Exception:
            _mark_key_server_error(key_name)
            return False

    # ── Ollama Cloud multi-key dispatcher (2026-08-23) ─────────────────────
    # Tries each registered Ollama Cloud key in order. Key #1 (existing) is
    # tried first; if it's paywalled/unhealthy, key #2 (new subscription)
    # picks up. Each key has its own Kalman filter, quota tracker, and
    # paywall flag — the market picks whichever has more quota remaining.

    def _try_ollama_cloud_any(self, body: bytes, model: str | None,
                               response_buffer: bytearray, t0: float,
                               reason: str | None = None) -> bool:
        """Try all registered Ollama Cloud keys until one succeeds.

        Iterates over (key_name, api_key) pairs in _ollama_cloud_key_order()
        — pool-weighted (most remaining quota first) since 2026-09-02 (plan
        B1). The previous static [oc, oc2, oc3-LAST] order drained oc at
        2.3× its weekly pool (1,143M tokens/7d vs ~500M/wk nominal) while
        oc2/oc3 sat nearly idle — a root cause of the ollama-burst flapping
        that spilled metered traffic into routstrd. Remaining-pool ordering
        equalizes burn across subscriptions (~800M tokens/wk of paid-but-
        unused combined capacity recovered) without touching the per-key
        scarcity guards (oc3 monthly gate, .key_disabled_* flags, per-key
        disable) — those still apply inside _try_ollama_cloud per attempt.
        Returns True on the first success, False if all keys fail.
        """
        for _kn, _kk in _ollama_cloud_key_order():
            if not _kk:
                continue
            if self._try_ollama_cloud(body, model, response_buffer, t0,
                                      reason=reason, key_name=_kn, api_key=_kk):
                return True
        return False

    def _try_opencode_go(self, body: bytes, model: str | None,
                         response_buffer: bytearray, t0: float,
                         reason: str | None = None) -> bool:
        """Forward request to OpenCode Go API (flat-rate $10/mo subscription).

        OpenCode Go serves native glm-5.3 (as does ollama_cloud since
        2026-08-29), has prompt caching, and 29 models including kimi-k3,
        deepseek-v4.
        No 403 paywall behavior observed — no Monday-reset flag needed.

        Returns True on success (response already sent),
        False on failure (caller should try next provider).
        """
        if not OPENCODE_GO_KEY:
            return False
        if not _is_key_healthy("opencode_go"):
            return False

        # OpenCode Go uses bare model IDs (glm-5.2, glm-5.3, kimi-k3, etc.)
        og_model = model or "glm-5.2"
        # MODEL-TRANSLATION: Translate prefixed model names to bare IDs that
        # opencode.ai expects. The early-exit path at line ~4939 passes the raw
        # model name (e.g. "deepseek/deepseek-v4-flash") but opencode.ai only
        # accepts the bare form ("deepseek-v4-flash"). Without this translation,
        # opencode.ai rejects the request and it falls through to NeuralWatt
        # ($1.43/M) instead of opencode_go ($0 marginal). The flat router's
        # _resolve_model_for_provider() already does this correctly, but the
        # early-exit path bypasses it. See design doc §9.
        _oc_model_map = _PROVIDER_MODEL_NAMES.get("opencode_go", {})
        og_model = _oc_model_map.get(og_model, og_model)
        # glm-5.3 stays as glm-5.3 — OpenCode Go serves it natively.

        try:
            body_json = json.loads(body) if body else {}
            body_json["model"] = og_model
            fwd_body = json.dumps(body_json).encode()

            url = OPENCODE_GO_BASE + "/chat/completions"
            hdrs = {
                "Authorization": f"Bearer {OPENCODE_GO_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            }

            req = urllib.request.Request(url, data=fwd_body, method="POST", headers=hdrs)
            with urllib.request.urlopen(req, timeout=180) as resp:
                self.send_response(resp.status)
                self._response_started = True  # response bytes committed — no retry
                for h, v in resp.headers.items():
                    if h.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(h, v)
                self.send_header("X-Provider", "opencode_go")
                self.end_headers()
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    response_buffer.extend(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()

                og_usage = _parse_usage(bytes(response_buffer))
                og_tokens = int(og_usage.get("total_tokens") or 0)
                _record_spend("opencode_go", og_model, og_tokens)
                self._spend_recorded = True
                _mark_key_healthy("opencode_go")

                # Parse allowance_remaining_usd from Go response body for
                # per-model pressure routing. The field is nested under
                # cost.allowance_remaining_usd in the JSON response.
                try:
                    _og_resp = json.loads(bytes(response_buffer))
                    _og_allow = _og_resp.get("cost", {}).get("allowance_remaining_usd")
                    if _og_allow is not None:
                        _og_allowance_val = float(_og_allow)
                        _opencode_go_allowance["remaining_usd"] = _og_allowance_val
                        _opencode_go_allowance["ts"] = time.time()
                        # B3 (provider-clock-alignment): append (ts, value) to the
                        # allowance log for later cycle-anchor inference. Bounded to
                        # ~1000 lines; rotation keeps the file ~30KB worst case.
                        try:
                            _allow_log_path = Path.home() / ".hermes" / "bot" / "opencode_allowance_log.jsonl"
                            _allow_log_path.parent.mkdir(parents=True, exist_ok=True)
                            _allow_line = json.dumps({"ts": _opencode_go_allowance["ts"], "remaining_usd": _og_allowance_val}) + "\n"
                            with open(_allow_log_path, "a") as _alf:
                                _alf.write(_allow_line)
                                # Rotate if file grew beyond ~30KB (≈1000 lines)
                                _alf.flush()
                            if _allow_log_path.stat().st_size > 30_000:
                                lines = _allow_log_path.read_text().splitlines()
                                _allow_log_path.write_text("\n".join(lines[-500:]) + "\n")
                        except Exception:
                            pass
                except Exception:
                    pass
                if _LIVE_ROUTER is not None:
                    try:
                        _LIVE_ROUTER.record_request(provider="opencode_go", tokens=og_tokens)
                    except Exception:
                        pass
                # Flat-rate subscription → marginal $0 cost (like ollama_cloud included)
                _og_cost, _og_cost_src = _extract_cost(
                    "opencode_go", bytes(response_buffer), og_tokens)
                _log_api_call(
                    key_name="opencode_go", key_suffix=OPENCODE_GO_KEY[-4:],
                    model=og_model,
                    prompt_tokens=int(og_usage.get("prompt_tokens") or 0),
                    completion_tokens=int(og_usage.get("completion_tokens") or 0),
                    total_tokens=og_tokens,
                    tier="opencode_go", status_code=resp.status, error=None,
                    duration_ms=int((time.time() - t0) * 1000),
                    cost_usd=_og_cost, cost_source=_og_cost_src,
                    session_id=getattr(self, "_session_id", None),
                    task_type=getattr(self, "_task_type", None),
                )
                _log_key_decision(
                    chosen_key="opencode_go",
                    reason=reason or "opencode_go_flat_rate_primary",
                )
                return True

        except urllib.error.HTTPError as he:
            # Read the error body once — needed for RegionError detection
            # (model-scoped 403) and 429 reset-time parsing.
            _og_err_body = b""
            try:
                _og_err_body = he.read() or b""
            except Exception:
                pass
            try:
                _og_err_text = _og_err_body.decode("utf-8", "replace")
            except Exception:
                _og_err_text = ""
            if he.code == 429:
                # Quota cap — mark exhausted, but use the upstream
                # "Resets in N days/hours" hint for the backoff when present
                # instead of the exponential default.
                _og_reset_s = _parse_opencode_reset_seconds(_og_err_text)
                _mark_key_failure("opencode_go", "exhausted",
                                  retry_after_seconds=_og_reset_s)
            elif he.code in (401, 403):
                # Model-scoped 403 (RegionError): the MODEL is unavailable in
                # the key's region, but the KEY is fine — glm-5.3, kimi-k3
                # and everything else still route here. Do NOT poison the
                # key. (Incident 2026-08-25: RegionError marked the key
                # exhausted → glm-5.3 dead/exhausted cycled for 72h.)
                if he.code == 403 and "regionerror" in _og_err_text.lower():
                    print(f"[opencode_go] 403 RegionError (model-scoped) "
                          f"model={og_model} — key kept healthy, skipping "
                          f"this model on this provider", flush=True)
                    try:
                        _log_anomaly("INFO", "model_scoped_skip",
                                     "opencode_go 403 RegionError — model skipped, key kept",
                                     f"model={og_model}; body={_og_err_text[:200]}",
                                     key_name="opencode_go")
                    except Exception:
                        pass
                    return False
                # Don't immediately mark "dead" on first 401 — the key may be
                # valid but the endpoint returned a transient auth error.
                # Use "exhausted" backoff (shorter) instead of "dead" (1h).
                # After _KEY_DEAD_THRESHOLD consecutive 401s, _mark_key_failure
                # will escalate to KEY_DEAD via the threshold logic.
                _og_failures = _zai_key_health.get("opencode_go", {}).get("consecutive_failures", 0)
                if OPENCODE_GO_KEY and _og_failures < _KEY_DEAD_THRESHOLD:
                    _mark_key_failure("opencode_go", "exhausted")
                else:
                    _mark_key_failure("opencode_go", "dead")
            return False
        except Exception:
            return False

    def _pressure_enforce(self, decision, body: bytes, t0: float) -> bool:
        """S2c (t_b82e5665): apply an enforce-mode pressure decision.

        Acts ONLY when the tracker runs mode=enforce AND the decision is
        an Ollama downgrade (reason bg_downgraded_ollama[_extra] — the
        AMBER/RED background-glm-5.3 rows of the decision matrix): serve
        glm-5.2 via ollama_cloud (flat-rate, protects the friend key).
        Interactive, friend-path and last-resort decisions return False
        untouched; a failed Ollama attempt also returns False so the
        normal cascade serves the request (bg_last_resort semantics —
        enforcement can redirect pressure traffic, never block it).
        Never raises.
        """
        try:
            if (decision is None
                    or _pressure_tracker is None
                    or not _pressure_tracker.enabled()
                    or _pressure_tracker.mode() != "enforce"):
                return False
            if decision.reason not in ("bg_downgraded_ollama",
                                       "bg_downgraded_ollama_extra"):
                return False
            served_model = decision.would_serve_model or "glm-5.2"
            response_buffer = bytearray()
            served = self._try_ollama_cloud_any(
                body, served_model, response_buffer, t0,
                reason=f"pressure_enforce_{decision.state.lower()}")
            if served:
                return True
            print(f"[pressure] enforce: ollama_cloud unavailable — "
                  f"falling back to normal cascade ({decision.reason})",
                  flush=True)
            return False
        except Exception as e:
            print(f"[pressure] enforce error ({type(e).__name__}: {e}) — "
                  f"normal cascade", flush=True)
            return False

    def _try_telnyx(self, body: bytes, model: str | None,
                     response_buffer: bytearray, t0: float) -> bool:
        """Forward request to Telnyx as failover for Kimi models.

        Uses the demo endpoint (https://telnyx.com/api/inference) which
        requires no API key — only browser-like Origin/Referer headers.
        Rate-limited to 10 req/min per IP. Returns SSE stream.

        If a production API key (TELNYX_KEY) is available, uses the
        production endpoint instead (no rate limit).

        Returns True on success (response already sent),
        False on failure (caller should send 503).
        """
        # Skip if Telnyx was recently rate-limited (circuit breaker)
        if not _is_key_healthy("telnyx"):
            return False

        # Map model name to Telnyx model ID
        telnyx_model = _PROVIDER_MODEL_NAMES.get("telnyx", {}).get(
            model or "", model or "")
        if not telnyx_model:
            return False

        try:
            body_json = json.loads(body) if body else {}
            body_json["model"] = telnyx_model
            fwd_body = json.dumps(body_json).encode()

            # Use production API if key available, else demo endpoint
            if TELNYX_KEY:
                url = TELNYX_BASE + "/chat/completions"
                hdrs = {
                    "Authorization": f"Bearer {TELNYX_KEY}",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                }
            else:
                url = TELNYX_DEMO_URL
                hdrs = {
                    "Content-Type": "application/json",
                    "Origin": "https://telnyx.com",
                    "Referer": "https://telnyx.com/products/inference",
                    "User-Agent": "Mozilla/5.0",
                }

            req = urllib.request.Request(url, data=fwd_body, method="POST", headers=hdrs)
            with urllib.request.urlopen(req, timeout=180) as resp:
                self.send_response(resp.status)
                self._response_started = True  # response bytes committed — no retry
                for h, v in resp.headers.items():
                    if h.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(h, v)
                self.send_header("X-Provider", "telnyx")
                self.end_headers()
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    response_buffer.extend(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()

                # Parse usage for spend tracking
                telnyx_usage = _parse_usage(bytes(response_buffer))
                telnyx_tokens = int(telnyx_usage.get("total_tokens") or 0)
                telnyx_cost, telnyx_cost_src = _extract_cost(
                    "telnyx", bytes(response_buffer), telnyx_tokens)
                _record_spend("telnyx", telnyx_model, telnyx_tokens,
                              actual_cost=telnyx_cost)
                self._spend_recorded = True
                _mark_key_healthy("telnyx")
                if _LIVE_ROUTER is not None:
                    try:
                        _LIVE_ROUTER.record_request(provider="telnyx", tokens=telnyx_tokens)
                    except Exception:
                        pass
                # Capture cached_tokens for the rolling cache-hit ratio
                # (used by _get_provider_cost to rank Telnyx fairly).
                _telnyx_ptd = telnyx_usage.get("prompt_tokens_details") or {}
                _telnyx_cached = int(_telnyx_ptd.get("cached_tokens") or 0) if isinstance(_telnyx_ptd, dict) else 0
                _log_api_call(
                    key_name="telnyx",
                    key_suffix=TELNYX_KEY[-4:] if TELNYX_KEY else "demo",
                    model=telnyx_model,
                    prompt_tokens=int(telnyx_usage.get("prompt_tokens") or 0),
                    completion_tokens=int(telnyx_usage.get("completion_tokens") or 0),
                    total_tokens=telnyx_tokens,
                    tier="telnyx", status_code=resp.status, error=None,
                    duration_ms=int((time.time() - t0) * 1000),
                    cost_usd=telnyx_cost, cost_source=telnyx_cost_src,
                    cache_hit=_telnyx_cached,
                    session_id=getattr(self, "_session_id", None),
                    task_type=getattr(self, "_task_type", None),
                )
                _log_key_decision(
                    chosen_key="telnyx",
                    reason="telnyx_direct" if model in _TELNYX_DIRECT_MODELS else "ollama_cloud_failed_telnyx_fallback",
                )
                return True

        except urllib.error.HTTPError as he:
            if he.code == 429:
                # Rate limited — mark telnyx as temporarily unhealthy
                _mark_key_failure("telnyx", error_type="exhausted")
            elif he.code == 402:
                # Out of credits — mark unfunded for 1 hour
                _mark_unfunded("telnyx")
            return False
        except Exception:
            return False

    def _try_external_single(self, provider_name: str, body: bytes,
                              model: str | None,
                              response_buffer: bytearray, t0: float) -> bool:
        """Try a SINGLE external provider. Returns True on success (response
        sent), False on failure (caller tries next candidate).

        Unlike _try_external_failover which iterates ALL external providers,
        this method contacts exactly ONE provider. Used by the flat router's
        _dispatch_external so candidate ordering is respected.

        On 401/403: marks the key as dead (1h backoff) — the key is likely
        revoked. On 402: marks unfunded. Other errors: just return False.
        """
        prov = EXTERNAL_PROVIDERS.get(provider_name)
        if not prov or not prov.get("key"):
            return False

        # Determine the model name for this provider
        ext_model = model or "glm-5.2"
        actual_model = _PROVIDER_MODEL_NAMES.get(provider_name, {}).get(ext_model, ext_model)

        try:
            body_json = json.loads(body) if body else {}
            body_json["model"] = actual_model
            if provider_name == "neuralwatt":
                for _sk in ("reasoning", "task_type", "tier_hint",
                            "X-Model-Tier", "X-Task-Type"):
                    body_json.pop(_sk, None)
            fwd_body = json.dumps(body_json).encode()

            url = prov["base_url"] + "/chat/completions"
            hdrs = {
                "Authorization": f"Bearer {prov['key']}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            }
            if provider_name == "openrouter":
                hdrs["HTTP-Referer"] = "https://hermes.local"
                hdrs["X-Title"] = "Hermes Agent"

            print(f"[flat-router] trying {provider_name} model={actual_model}", flush=True)
            req = urllib.request.Request(url, data=fwd_body, method="POST", headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    self.send_response(resp.status)
                    self._response_started = True  # response bytes committed — no retry
                    for h, v in resp.headers.items():
                        if h.lower() not in ("transfer-encoding", "connection"):
                            self.send_header(h, v)
                    self.send_header("X-Failover-Provider", provider_name)
                    self.end_headers()
                    while True:
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        response_buffer.extend(chunk)
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    _mark_funded(provider_name)
                    _mark_key_healthy(provider_name)
                    ext_usage = _parse_usage(bytes(response_buffer))
                    ext_tokens = int(ext_usage.get("total_tokens") or 0)
                    ext_cost, ext_src = _extract_cost(
                        provider_name, bytes(response_buffer), ext_tokens)
                    _record_spend(provider_name, actual_model, ext_tokens,
                                  actual_cost=ext_cost,
                                  prompt_tokens=int(ext_usage.get("prompt_tokens") or 0),
                                  completion_tokens=int(ext_usage.get("completion_tokens") or 0))
                    if _LIVE_ROUTER is not None:
                        try:
                            _LIVE_ROUTER.record_request(provider=provider_name, tokens=ext_tokens)
                        except Exception:
                            pass
                    self._spend_recorded = True
                    _log_api_call(
                        key_name=provider_name, key_suffix=prov["key"][-4:],
                        model=actual_model,
                        prompt_tokens=int(ext_usage.get("prompt_tokens") or 0),
                        completion_tokens=int(ext_usage.get("completion_tokens") or 0),
                        total_tokens=ext_tokens,
                        tier="flat_router", status_code=resp.status, error=None,
                        duration_ms=int((time.time() - t0) * 1000),
                        cost_usd=ext_cost, cost_source=ext_src,
                        session_id=getattr(self, "_session_id", None),
                        task_type=getattr(self, "_task_type", None),
                    )
                    # Garbage circuit-breaker: a 200 with degenerate oversized
                    # output demotes THIS (provider, model) pair for 24h.
                    try:
                        _check_garbage_cb(
                            provider_name, actual_model or "",
                            int(ext_usage.get("completion_tokens") or 0),
                            duration_ms=int((time.time() - t0) * 1000),
                        )
                    except Exception:
                        pass
                    return True
            except urllib.error.HTTPError as he:
                if he.code == 401 or he.code == 403:
                    print(f"[flat-router] {provider_name} returned {he.code} — marking DEAD (1h backoff)", flush=True)
                    _mark_key_failure(provider_name, "dead")
                elif he.code == 402:
                    print(f"[flat-router] {provider_name} returned 402 — marking unfunded", flush=True)
                    _mark_unfunded(provider_name)
                elif he.code == 429:
                    print(f"[flat-router] {provider_name} returned 429 — marking exhausted", flush=True)
                    _mark_key_exhausted(provider_name)
                else:
                    print(f"[flat-router] {provider_name} returned HTTP {he.code}", flush=True)
                return False
        except Exception as e:
            print(f"[flat-router] {provider_name} exception: {type(e).__name__}: {e}", flush=True)
            return False

    def _try_external_failover(self, body: bytes, model: str | None,
                                response_buffer: bytearray, t0: float,
                                preferred: str | None = None) -> bool:
        """Try forwarding to the cheapest funded external provider when z.ai fails.

        Dynamically selects the provider with the lowest cost that still has
        credits remaining. On 402 (out of credits), marks that provider
        unfunded for 1 hour and tries the next cheapest.

        Args:
            preferred: Optional provider name (e.g. LiveRouter's pick) to try
                FIRST, ahead of the cost-sorted order. If it is funded + keyed
                it is attempted before the rest; on failure the remaining
                candidates are tried cheapest-first as normal. Honours the
                LiveRouter pick (P3.4 Fix 1) without weakening the safe
                cost-ordered fallback.

        Returns True on success (response already sent),
        False on failure (caller should send error response).
        """

        # ── FIX: Silent model substitution bug (2026-08-25) ──────────────────
        # Previously, ANY non-glm-5.2/5.3 model was silently replaced with
        # WORKER_FALLBACK_MODEL ("deepseek/deepseek-v4-flash") and returned
        # HTTP 200 — customers got a cheaper model without knowing.
        #
        # New behavior:
        #   1. glm-5.2/glm-5.3 → MANAGER_FALLBACK_MODEL (unchanged, intended)
        #   2. Known external models (in _PROVIDER_MODEL_NAMES for any
        #      provider, or starting with known prefixes like deepseek/,
        #      qwen, minimax, mimo) → passed through verbatim. The provider
        #      will either serve it or return 404, which we propagate.
        #   3. Truly unknown models → return False immediately so the caller
        #      sends a proper 503/404 error. NEVER return 200 with a
        #      different model than what was requested.
        if model in ("glm-5.2", "glm-5.3"):
            ext_model = MANAGER_FALLBACK_MODEL
        elif model is not None and _is_known_external_model(model):
            ext_model = model  # pass through verbatim — provider handles 404
        else:
            # Unknown model — don't silently substitute. Return False so
            # the caller sends a proper error response.
            print(f"[failover] rejecting unknown model={model!r} — "
                  f"no silent substitution", flush=True)
            return False

        # Collect funded providers with their cost
        candidates = []
        for name, prov in EXTERNAL_PROVIDERS.items():
            if not prov.get("key"):
                continue
            if not _is_provider_funded(name):
                continue
            # Telnyx Kimi-only guard (2026-08-20): skip Telnyx in generic
            # failover when the model has no explicit telnyx translation.
            # Prevents glm-5.2-class traffic (MANAGER_FALLBACK_MODEL) from
            # reaching the ~$12/M per-token route.
            if name == "telnyx" and ext_model not in _PROVIDER_MODEL_NAMES.get("telnyx", {}):
                continue
            # D6: PPQ good-use policy — daily cap / hourly cap / retry-storm
            # gate, enforced before PPQ can even join the candidate list.
            if name == "ppq":
                ok, why = _ppq_gate_ok(_ppq_hash_body(body))
                if not ok:
                    print(f"[ppq-policy] skipping PPQ — {why}", flush=True)
                    continue
            # Cashu-routed providers must pass an endpoint liveness probe:
            # a TCP-accepting-but-starved endpoint (VPS2 memory pressure /
            # dead mint) would otherwise stall the chain for the full timeout.
            if name in ("routstr", "routstrd") and not _endpoint_alive(prov["base_url"]):
                print(f"[failover] skipping {name} — endpoint probe failed ({prov['base_url']})", flush=True)
                continue
            # Balance gate for routstrd (race-fix 2026-08-23): use the
            # 420s-cache + last-known-good snapshot instead of fail-closed.
            # When the endpoint is alive (checked above) but the balance
            # cache is stale, the last-known-good entry is used so a
            # probe hiccup no longer skips a funded routstrd wallet.
            if name == "routstrd":
                _rd_bal = _routstrd_balance_snapshot()
                _rd_used_pct = float(_rd_bal.get("used_pct", 0.0))
                _rd_remaining = float(_rd_bal.get("remaining", 0.0))
                if _rd_used_pct >= 100.0 or _rd_remaining <= 0.0:
                    print(f"[failover] skipping {name} — wallet exhausted "
                          f"(used={_rd_used_pct:.1f}%, rem={_rd_remaining:.0f} sats)", flush=True)
                    continue
            cost = _get_provider_cost(name, ext_model)
            candidates.append((cost, name, prov))

        # Sort cheapest first; ties broken by failure_count (fewer = tried first)
        candidates.sort(key=lambda c: (c[0], _provider_health.get(c[1], {}).get("failure_count", 0)))

        # Honour a LiveRouter-chosen provider (P3.4 Fix 1): if `preferred` is
        # funded + keyed, move it to the front so it is tried FIRST; the rest
        # keep their cost order as the safe fallback. No-op when preferred is
        # absent, unknown, or not a viable candidate.
        if preferred:
            pref = [c for c in candidates if c[1] == preferred]
            if pref:
                candidates = pref + [c for c in candidates if c[1] != preferred]

        if not candidates:
            print(f"[failover] no candidates available for ext_model={ext_model}", flush=True)
            return False

        for cost, provider_name, prov in candidates:
            try:
                body_json = json.loads(body) if body else {}
                # Per-provider model name translation.
                # PPQ/OpenRouter use "deepseek/deepseek-v4-pro" but DeepInfra expects
                # "deepseek-ai/DeepSeek-V4-Pro" (case-sensitive, dotted form).
                actual_model = _PROVIDER_MODEL_NAMES.get(provider_name, {}).get(ext_model, ext_model)
                body_json["model"] = actual_model
                # Strip non-OpenAI fields that some providers reject with 422.
                # neuralwatt is strict — it rejects reasoning/task_type/tier_hint.
                if provider_name == "neuralwatt":
                    for _strip_key in ("reasoning", "task_type", "tier_hint",
                                       "X-Model-Tier", "X-Task-Type"):
                        body_json.pop(_strip_key, None)
                fwd_body = json.dumps(body_json).encode()

                url = prov["base_url"] + "/chat/completions"
                hdrs = {
                    "Authorization": f"Bearer {prov['key']}",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                }
                if provider_name == "openrouter":
                    hdrs["HTTP-Referer"] = "https://hermes.local"
                    hdrs["X-Title"] = "Hermes Agent"

                print(f"[failover] trying {provider_name} model={actual_model} cost=${cost:.4f}/M", flush=True)
                # D6: record the PPQ attempt so a crash-retry loop of this
                # exact prompt is detected and refused on later gate checks.
                if provider_name == "ppq":
                    _ppq_note_attempt(_ppq_hash_body(body))
                req = urllib.request.Request(url, data=fwd_body, method="POST", headers=hdrs)
                try:
                    with urllib.request.urlopen(req, timeout=180) as resp:
                        self.send_response(resp.status)
                        for h, v in resp.headers.items():
                            if h.lower() not in ("transfer-encoding", "connection"):
                                self.send_header(h, v)
                        self.send_header("X-Failover-Provider", provider_name)
                        self.end_headers()
                        while True:
                            chunk = resp.read(4096)
                            if not chunk:
                                break
                            response_buffer.extend(chunk)
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        _mark_funded(provider_name)
                        # Parse usage from the streamed response for spend tracking
                        ext_usage = _parse_usage(bytes(response_buffer))
                        ext_tokens = int(ext_usage.get("total_tokens") or 0)
                        # RP-2: extract real cost from the response body.
                        # Unifies per-provider cost parsing (openrouter usage.cost,
                        # deepinfra usage.estimated_cost, ppq multi-path probe)
                        # into one call. Returns (None, None) when the provider
                        # doesn't return a cost field.
                        ext_cost_usd, ext_cost_source = _extract_cost(
                            provider_name, bytes(response_buffer), ext_tokens)
                        # Use extracted cost for spend tracking (falls back to
                        # _estimate_cost_usd inside _record_spend when None).
                        _record_spend(provider_name, ext_model, ext_tokens,
                                      actual_cost=ext_cost_usd,
                                      prompt_tokens=int(ext_usage.get("prompt_tokens") or 0),
                                      completion_tokens=int(ext_usage.get("completion_tokens") or 0))
                        if _LIVE_ROUTER is not None:
                            try:
                                _LIVE_ROUTER.record_request(provider=provider_name, tokens=ext_tokens)
                            except Exception:
                                pass
                        # D6: PPQ daily-budget tracker (cap / hourly / 80% alert)
                        if provider_name == "ppq":
                            _ppq_record_success(ext_tokens, ext_cost_usd)
                        # Deduct from DeepInfra local credit balance
                        if provider_name == "deepinfra" and ext_cost_usd is not None and ext_cost_usd > 0:
                            remaining = _deduct_deepinfra_balance(ext_cost_usd)
                        self._spend_recorded = True
                        # Capture cached_tokens (Telnyx prompt caching) for the
                        # rolling cache-hit ratio used by _get_provider_cost.
                        _ext_ptd = ext_usage.get("prompt_tokens_details") or {}
                        _ext_cached = int(_ext_ptd.get("cached_tokens") or 0) if isinstance(_ext_ptd, dict) else 0
                        _log_api_call(
                            key_name=provider_name, key_suffix=prov["key"][-4:],
                            model=ext_model,
                            prompt_tokens=int(ext_usage.get("prompt_tokens") or 0),
                            completion_tokens=int(ext_usage.get("completion_tokens") or 0),
                            total_tokens=ext_tokens,
                            tier=provider_name, status_code=resp.status, error=None,
                            duration_ms=int((time.time() - t0) * 1000),
                            cost_usd=ext_cost_usd, cost_source=ext_cost_source,
                            cache_hit=_ext_cached,
                            session_id=getattr(self, "_session_id", None),
                            task_type=getattr(self, "_task_type", None),
                        )
                        # Log key decision so dashboard shows the failover switch
                        _log_key_decision(
                            chosen_key=provider_name,
                            reason=f"zai_exhausted_{provider_name}_failover",
                        )
                        # ProfitTracker: record consumer-mode savings (next-best
                        # alternative minus what we paid). Fire-and-forget.
                        if _PROFIT_TRACKER is not None:
                            try:
                                idx = candidates.index((cost, provider_name, prov))
                                next_cost = candidates[idx + 1][0] if idx + 1 < len(candidates) else None
                            except Exception:
                                next_cost = None
                            _PROFIT_TRACKER.record_decision(
                                provider=provider_name,
                                effective_price=float(cost),
                                next_best_price=float(next_cost) if next_cost is not None else None,
                                tokens=ext_tokens,
                                is_peak=_is_peak_hour(),
                            )
                        return True
                except urllib.error.HTTPError as he:
                    if he.code == 402:
                        print(f"[failover] {provider_name} returned 402 (unfunded) — marking unfunded, trying next", flush=True)
                        _mark_unfunded(provider_name)
                        if provider_name == "routstrd":
                            _routstrd_bal_cache["entry"] = {"used_pct": 100.0, "remaining": 0.0, "balance_sats": 0}
                            _routstrd_bal_cache["ts"] = time.time()
                        continue
                    print(f"[failover] {provider_name} returned HTTP {he.code} — trying next", flush=True)
                    raise
            except Exception as e:
                print(f"[failover] {provider_name} exception: {type(e).__name__}: {e} — trying next", flush=True)
                continue

        return False

    def _proxy(self):
        # We strip Transfer-Encoding from upstream responses (below) yet pass no
        # Content-Length for streamed bodies, so connection-close is the body
        # delimiter. Force it — otherwise HTTP/1.1 keep-alive leaves the socket
        # open and clients hang waiting for body-end (the /quota + BrokenPipe
        # symptoms). Sending the "Connection: close" header alone is NOT enough;
        # BaseHTTPRequestHandler keys off self.close_connection.
        self.close_connection = True
        t0 = time.time()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self._spend_recorded = False  # set True by _try_external_failover on success

        # ── Quota-aware model tier routing (auto-downgrade) ────────────────
        # Step 1: Extract original model + client tier hint
        original_model = _extract_model(body)
        tier_hint = self.headers.get("X-Model-Tier", "")
        # Phase 1 attribution (productivity-gate §1.4): originating agent
        # session, threaded into every _log_api_call below. Loopback trust
        # boundary — same handling as X-Model-Tier (proxy is localhost-only).
        self._session_id = (self.headers.get("X-Hermes-Session", "") or "").strip() or None

        # CG-5 task-type attribution (cost-gate-reform-v2 §CG-5): the
        # caller-DECLARED task type — X-Task-Type header wins over the body
        # task_type field; unset/unknown -> None (never guessed; NULL in
        # api_calls). Threaded into every _log_api_call below, including
        # ollama_cloud / telnyx / external failover hops. Read-only with
        # respect to the body — the forwarded request is untouched.
        self._task_type = _resolve_task_type(self.headers, body)

        # Early bail: model-only probe (no messages field) — return 400
        # immediately. These are health-check/model-availability probes that
        # every provider rejects with 422. Don't waste time cycling providers.
        try:
            _probe = json.loads(body) if body else {}
            if not isinstance(_probe, dict) or not _probe.get("messages"):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "38")
                self.end_headers()
                self.wfile.write(b'{"error":"messages field required"}')
                return
        except Exception:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "33")
            self.end_headers()
            self.wfile.write(b'{"error":"invalid request body"}')
            return

        # ── Pressure FSM shadow hook (S2b, t_4dfaf0d5) ────────────────────
        # Compute + log the pressure decision this request WOULD get.
        # READ-ONLY: `body`, `chosen` and every routing step below are
        # untouched — enforce mode is S2c+. Never raises (helper swallows).
        self._pressure_decision = _pressure_shadow(
            original_model, self._session_id)

        # Step 1b: Global spend cap — circuit breaker for runaway loops
        # Use global sum across ALL tiers (not just "unknown") to prevent a
        # single paid tier from blocking free/cheap providers.
        allowed, current_spend, cap = _check_global_spend_cap()
        if not allowed:
            err = json.dumps({
                "error": f"daily METERED spend cap exceeded (neuralwatt/routstr/routstrd/deepinfra/telnyx/ppq/openrouter)",
                "spend_usd": round(current_spend, 4),
                "cap_usd": cap,
                "hint": "subscription lanes (ours/friend/ollama_cloud*) are NOT blocked — retry with a sub-served model or wait for the metered market pricing to recover",
                "reset_at": "midnight UTC"
            }).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
            return

        # ── Pressure FSM enforce hook (S2c, t_b82e5665) ────────────────
        # Apply the S2b decision when the tracker runs mode=enforce:
        # AMBER/RED background glm-5.3 → ollama_cloud glm-5.2 (flat-rate,
        # friend-key protection). Shadow mode, non-Ollama decisions and
        # Ollama failures all fall through to the normal cascade below.
        # Ordered AFTER the global spend cap so enforcement can never
        # bypass the runaway-loop circuit breaker.
        if self._pressure_enforce(self._pressure_decision, body, t0):
            return

        # ── Phase 3: Flat router full cutover ──────────────────────────────
        # When .disable_flat_router does NOT exist, use select_provider() as
        # the primary router. When it DOES exist, fall through to the old
        # best_key() + failover chain (rollback path, unchanged).
        #
        # The flat router:
        #   1. Calls select_provider(model) → ordered candidate list (cheapest first)
        #   2. Iterates candidates: try each via _dispatch_to_provider()
        #   3. On success: update Kalman filters, log, return
        #   4. On failure: mark key failure, try next candidate
        #   5. If all candidates fail: send 503
        #
        # Special cases: ollama-only models, telnyx-direct models, and
        # non-z.ai models are NOT special-cased above — the flat router
        # handles them naturally via PROVIDER_MODELS. The old best_key()
        # rollback path keeps its own special-case handling.

        _FLAT_ROUTER_DISABLE_FLAG = os.path.expanduser(
            "~/.hermes/bot/.disable_flat_router")

        # ── EMERGENCY KILL-SWITCH (2026-09-04) ─────────────────────────────
        # When ~/.hermes/bot/.emergency_ollama_only exists, dispatch EVERYTHING
        # directly via the _try_ollama_cloud_any() chain (ollama_cloud →
        # ollama_cloud_2 → ollama_cloud_3), bypassing select_provider entirely.
        # This is the fast rollback once .disable_flat_router dies in Phase 1.
        # On total ollama failure we return a clean 503 (never fall through to
        # the legacy cascade). Startup log line is emitted in __main__.
        _EMERGENCY_OLLAMA_FLAG = os.path.expanduser(
            "~/.hermes/bot/.emergency_ollama_only")
        if os.path.exists(_EMERGENCY_OLLAMA_FLAG):
            _resp = bytearray()
            if self._try_ollama_cloud_any(
                    body, original_model, _resp, t0,
                    reason="emergency_ollama_only"):
                return
            _err = json.dumps({
                "error": "emergency ollama-only mode: all ollama keys failed",
                "model": original_model,
            }).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(_err)))
            self.send_header("X-Provider", "none")
            self.end_headers()
            self.wfile.write(_err)
            return

        if not os.path.exists(_FLAT_ROUTER_DISABLE_FLAG):
            # ── FLAT ROUTER PATH (Phase 3) ─────────────────────────────
            try:
                from flat_router import (
                    select_provider as _flat_select_provider,
                    _dispatch_to_provider as _flat_dispatch,
                    _update_kalman_after_request as _flat_kalman_update,
                    shadow_compare as _flat_router_shadow_compare,
                )
            except Exception:
                # Import failed — fall through to old path as safety net
                _flat_select_provider = None

            if _flat_select_provider is not None:
                # Get ordered candidate list from the flat router.
                # GAP D (2026-09-04): select_provider() is wrapped so a RUNTIME
                # exception (corrupt quota cache, sqlite error, etc.) can never
                # kill the request handler. On failure we log the traceback to
                # the journal and return a clean 503 — we do NOT fall through to
                # the legacy cascade (the flag check already passed, so the old
                # path is not a valid fallback here).
                try:
                    _candidates = _flat_select_provider(model=original_model)
                except Exception as _fr_err:
                    import traceback as _tb
                    print("[flat-router] select_provider raised "
                          f"{type(_fr_err).__name__}: {_fr_err} — returning 503",
                          flush=True)
                    print(_tb.format_exc(), flush=True)
                    _err = json.dumps({
                        "error": "router selection failed",
                        "detail": str(_fr_err),
                    }).encode()
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(_err)))
                    self.send_header("X-Provider", "none")
                    self.end_headers()
                    self.wfile.write(_err)
                    return

                # Shadow log: record what the flat router chose
                try:
                    _flat_router_shadow_compare(None, original_model)
                except Exception:
                    pass

                # Log the candidate list for observability
                _cand_names = [c.name for c in _candidates
                               if c.name != "fallback"]
                if _cand_names:
                    _log_key_decision(
                        chosen_key=_cand_names[0] if _cand_names else "",
                        reason=f"flat_router: {' -> '.join(_cand_names[:5])}"[:120])

                _flat_response_buffer = bytearray()
                _flat_key_used: str | None = None
                # Reset the double-response guard for THIS request (the
                # handler instance is reused across keep-alive requests).
                self._response_started = False
                _flat_status_code = None
                _flat_error_text = None
                _flat_served = False

                for _cand in _candidates:
                    if _cand.name == "fallback" or _cand.dispatch_fn is None:
                        continue

                    _flat_key_used = _cand.name

                    # Apply model name translation for this candidate
                    _cand_body = body
                    if _cand.model and _cand.model != original_model:
                        try:
                            _body_json = json.loads(body)
                            _body_json["model"] = _cand.model
                            _cand_body = json.dumps(_body_json).encode()
                        except Exception:
                            pass

                    # Dispatch to this provider
                    _cand_buffer = bytearray()
                    try:
                        _success = _flat_dispatch(
                            self, _cand.name, _cand_body, _cand.model,
                            _cand_buffer, t0)
                    except Exception:
                        _success = False

                    if _success:
                        # Success — update Kalman filters with real cost data
                        _flat_served = True
                        _flat_response_buffer.extend(_cand_buffer)

                        # Extract cost and tokens for Kalman update
                        try:
                            _usage = _parse_usage(bytes(_cand_buffer))
                            _total_tokens = int(_usage.get("total_tokens") or 0)
                            _cost_usd, _cost_src = _extract_cost(
                                _cand.name, bytes(_cand_buffer), _total_tokens)
                            _flat_kalman_update(
                                provider=_cand.name,
                                cost_usd=_cost_usd,
                                total_tokens=_total_tokens,
                            )
                        except Exception:
                            pass

                        # Log the API call — skip if _try_external_failover
                        # already logged it (sets _spend_recorded=True at the
                        # same time it calls _log_api_call internally).
                        # Without this guard, a failover from e.g. routstr→
                        # neuralwatt produces a phantom flat_router row in
                        # addition to the real neuralwatt row.
                        try:
                            if not getattr(self, '_spend_recorded', False):
                                _suffix = None
                                if _cand.name in KEYS and KEYS.get(_cand.name):
                                    _suffix = KEYS[_cand.name][-4:]
                                elif _cand.name in _EXTERNAL_KEYS:
                                    _ext_key = _EXTERNAL_KEYS.get(_cand.name, {})
                                    _ext_k = _ext_key.get("key", "") if isinstance(_ext_key, dict) else ""
                                    if _ext_k:
                                        _suffix = _ext_k[-4:]
                                _latency_ms = int((time.time() - t0) * 1000)
                                _log_api_call(
                                    key_name=_cand.name,
                                    key_suffix=_suffix,
                                    model=_cand.model or original_model,
                                    prompt_tokens=int(_usage.get("prompt_tokens") or 0) if '_usage' in dir() else 0,
                                    completion_tokens=int(_usage.get("completion_tokens") or 0) if '_usage' in dir() else 0,
                                    total_tokens=_total_tokens if '_total_tokens' in dir() else 0,
                                    tier="flat_router",
                                    status_code=200,
                                    error=None,
                                    duration_ms=_latency_ms,
                                    cost_usd=_cost_usd if '_cost_usd' in dir() else None,
                                    cost_source=_cost_src if '_cost_src' in dir() else None,
                                    session_id=getattr(self, "_session_id", None),
                                    task_type=getattr(self, "_task_type", None),
                                )
                        except Exception:
                            pass

                        # Record spend
                        try:
                            if not getattr(self, '_spend_recorded', False):
                                _record_spend(_cand.name, _cand.model or original_model,
                                              _total_tokens if '_total_tokens' in dir() else 0)
                        except Exception:
                            pass

                        # Garbage circuit-breaker (per-(provider, model)): check
                        # a SUCCESSFUL completion for degenerate oversized output
                        # and demote (provider, model) for 24h if tripped. Never
                        # raises — a detector error must not break the response.
                        try:
                            _cb_model = (_cand.model or original_model or "")
                            _cb_comp = int(_usage.get("completion_tokens") or 0) \
                                if '_usage' in dir() else 0
                            _check_garbage_cb(
                                _cand.name, _cb_model, _cb_comp,
                                duration_ms=int((time.time() - t0) * 1000),
                            )
                        except Exception:
                            pass

                        # LiveRouter consumption tracking
                        if _LIVE_ROUTER is not None:
                            try:
                                _LIVE_ROUTER.record_request(
                                    provider=_cand.name,
                                    tokens=_total_tokens if '_total_tokens' in dir() else 0,
                                )
                            except Exception:
                                pass

                        return
                    else:
                        # Double-response guard: if this candidate already
                        # sent status/headers/body bytes to the client, we
                        # CANNOT try the next candidate (the client would get
                        # two concatenated responses) and CANNOT send a 503
                        # either. Treat a started response as terminal.
                        if getattr(self, "_response_started", False):
                            print("[flat-router] response already started — "
                                  "aborting candidate iteration "
                                  f"(failed at {_cand.name})", flush=True)
                            return
                        # Failure — mark key and try next candidate.
                        # Guard: only increment failure count if the key was
                        # actually healthy (i.e. the dispatch was attempted).
                        # If the key was already in backoff, the dispatch
                        # returned False without contacting the API — don't
                        # increment the failure count (prevents death spiral
                        # where every request increments failures while the
                        # key is in backoff, making it never recover).
                        try:
                            if _is_key_healthy(_cand.name):
                                _mark_key_failure(_cand.name, "dispatch_fail")
                        except Exception:
                            pass
                        continue

                # All candidates failed — send 503
                # (skip if a response was already started — see guard above)
                if not _flat_served and not getattr(self, "_response_started", False):
                    _err = json.dumps({
                        "error": "all providers exhausted (flat router)",
                        "model": original_model,
                        "candidates_tried": _cand_names,
                    }).encode()
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(_err)))
                    self.send_header("X-Provider", "none")
                    self.end_headers()
                    self.wfile.write(_err)
                    return

        # ── OLD PATH (rollback when .disable_flat_router exists) ────────
        # The code below is the ORIGINAL routing path, unchanged.
        # It runs when .disable_flat_router flag file exists.

        # Step 1d + Step 2 — choose a routing key.
        #
        # ADVISOR MODE (Phase 2.2, hot-swappable): when the feature flag is ON
        # (touch ~/.hermes/bot/.optimizer_advisor_mode  OR
        #  ROUTING_ADVISOR_ENABLED=1) the price-first RoutingOptimizer is
        # consulted FIRST; best_key() is the fallback on any failure. Peak-hour
        # pricing is now the optimizer's job — during z.ai peak (UTC 6-10) it
        # charges z.ai 3x, making ollama_cloud cheaper, so it routes there
        # automatically. best_key() is NEVER removed — it is the fallback on
        # ANY optimizer exception or "no viable provider" result.
        #
        # Flag OFF → behaviour is UNCHANGED: the original peak-hour Ollama
        # pre-check + best_key() cascade runs exactly as before.
        # `peak` is computed once here so downstream (failover chain, logging)
        # always sees a defined value regardless of which branch ran.
        peak = _is_peak_hour()
        if _routing_advisor is not None and _routing_advisor.enabled():
            chosen = None
            try:
                _adv = _routing_advisor.decide(
                    difficulty="medium", estimated_tokens=0)
                # Optimizer may route directly to ollama_cloud (self-hosted,
                # bypasses z.ai). If it does and ollama fails, fall through to
                # best_key() (chosen stays None → failover chain below).
                if _adv.routed_directly_to_ollama and OLLAMA_CLOUD_KEY:
                    response_buffer = bytearray()
                    if self._try_ollama_cloud_any(
                            body, original_model, response_buffer, t0):
                        return
                chosen = _adv.key
                _log_key_decision(
                    chosen_key=chosen or "",
                    reason=("optimizer_advisor:"
                            + (_adv.reason or "price_optimal"))[:120])
            except Exception:
                pass  # advisor must never break routing
            if chosen is None:
                chosen = best_key()
        else:
            # ORIGINAL CASCADE — flag off or advisor module unavailable.
            # Step 1d: Peak-hour routing — consult LiveRouter first (P3.4-fix),
            # then fall through to peak-hour Ollama pre-check.
            if peak:
                _pick, _pick_model, _fb, _fb_model = _consult_live_router(
                    model=original_model,
                    task_type=_tier_to_task_type(tier_hint),
                )
                if _pick:
                    _log_key_decision(
                        chosen_key=_pick,
                        reason=f"live_kalman_failover_{_pick}")
                    response_buffer = bytearray()
                    if _pick in ("ollama_cloud", "ollama_cloud_2", "ollama_cloud_3") and (OLLAMA_CLOUD_KEY or OLLAMA_CLOUD_KEY_2 or OLLAMA_CLOUD_KEY_3):
                        if self._try_ollama_cloud_any(
                                body, original_model, response_buffer, t0):
                            return
                    elif _pick in EXTERNAL_PROVIDERS or _pick == "deepinfra":
                        if self._try_external_failover(body, original_model,
                                                       response_buffer, t0,
                                                       preferred=_pick):
                            return
                    # LiveRouter pick failed — fall through
            # Peak-hour Ollama pre-check (fallback if LiveRouter disabled/no pick)
            if peak and OLLAMA_CLOUD_KEY:
                response_buffer = bytearray()
                if self._try_ollama_cloud_any(
                        body, original_model, response_buffer, t0):
                    return
            # Step 2: Choose key.
            chosen = best_key()

        # Shadow mode (Phase 2.1, ADR-014): record what the price-first
        # optimizer WOULD have chosen, alongside the live pick. READ-ONLY —
        # never changes `chosen`. In advisor mode the live pick already came
        # from the optimizer, so this logs the agreement (useful monitoring);
        # any failure is swallowed so production is unaffected.
        #
        # T7 / C1 fix (docs/shadow-7d-report.md §3/§6): ALSO compute the
        # LiveRouter's pressure-routing pick so the P6 divergence and 429
        # exit-gate columns get genuine data.  Before this fix the live path
        # called log_decision() (legacy API) which left pressure_provider,
        # actual_cost, divergence, is_429 all NULL — making the exit gate
        # degenerate (passed trivially on empty inputs).  The pressure pick
        # bypasses the kill switch intentionally — that's the point of SHADOW
        # mode (log, don't route).
        if _shadow_logger and _shadow_optimizer and chosen:
            try:
                _sd = _shadow_optimizer.route(difficulty="medium", estimated_tokens=0)
                if _sd:
                    # ── C1: compute LiveRouter pressure pick (best-effort) ──
                    _pr_prov = _pr_mod = _pr_cost = _act_cost = None
                    if _LIVE_ROUTER is not None:
                        try:
                            _pw_s = None
                            with lock:
                                _pw_s = dict(_pace_windows) if _pace_windows else None
                            (_pr_prov, _pr_mod), _ = _LIVE_ROUTER.select_failover(
                                quota_state=_snapshot_quota(),
                                health_state=_snapshot_health(),
                                peak=_is_peak_hour(),
                                pace_windows=_pw_s,
                            )
                            _rates = _converged_rates or {}
                            if _pr_prov:
                                _pr_cost = (_rates.get(_pr_prov)
                                            or _rates.get(str(_pr_prov).replace("zai_", "")))
                            _ll = _shadow_live_label(chosen)
                            _act_cost = (_rates.get(_ll)
                                         or _rates.get(chosen)
                                         or _rates.get(_ll.replace("zai_", "")))
                        except Exception:
                            pass  # pressure pick is best-effort only
                    # ── Log with pressure dimension if available ──
                    if hasattr(_shadow_logger, 'log_decision_with_pressure'):
                        _shadow_logger.log_decision_with_pressure(
                            ts=time.time(),
                            live_provider=_shadow_live_label(chosen),
                            live_model=original_model,
                            shadow_provider=_sd.get("chosen_provider"),
                            shadow_model=_sd.get("chosen_model"),
                            shadow_cost=_sd.get("effective_cost_per_1m"),
                            tokens=0,
                            reason=(_sd.get("reason") or "")[:200],
                            live_cost=_act_cost,
                            quota_regime=_get_ollama_quota_status().get("regime"),
                            pressure_provider=_pr_prov,
                            pressure_model=_pr_mod,
                            pressure_cost=_pr_cost,
                            actual_cost=_act_cost,
                        )
                    else:
                        _shadow_logger.log_decision(
                            ts=time.time(),
                            live_provider=_shadow_live_label(chosen),
                            live_model=original_model,
                            shadow_provider=_sd.get("chosen_provider"),
                            shadow_model=_sd.get("chosen_model"),
                            shadow_cost=_sd.get("effective_cost_per_1m"),
                            tokens=0,
                            reason=(_sd.get("reason") or "")[:200],
                            live_cost=None,
                            quota_regime=_get_ollama_quota_status().get("regime"),
                        )
            except Exception:
                pass  # Shadow mode never blocks production

        # ── Flat router shadow comparison (Phase 1) ──────────────────────
        # Run select_provider() in shadow and log what it WOULD have chosen
        # alongside the best_key() pick. NO routing change — best_key() still
        # drives all routing. This is observability only.
        try:
            from flat_router import shadow_compare as _flat_router_shadow_compare
            _flat_router_shadow_compare(chosen, original_model)
        except Exception:
            pass  # Flat router shadow never blocks production

        # If both z.ai keys exhausted, consult LiveRouter first (P3.4-fix),
        # then fall through to Ollama Cloud / PPQ hardcoded chain.
        if chosen is None:
            # LiveRouter consultation (kill switch checked inside)
            _pick, _pick_model, _fb, _fb_model = _consult_live_router(
                model=original_model,
                task_type=_tier_to_task_type(tier_hint),
            )
            if _pick:
                _log_key_decision(
                    chosen_key=_pick,
                    reason=f"live_kalman_failover_{_pick}")
                response_buffer = bytearray()
                if _pick in ("ollama_cloud", "ollama_cloud_2", "ollama_cloud_3") and (OLLAMA_CLOUD_KEY or OLLAMA_CLOUD_KEY_2 or OLLAMA_CLOUD_KEY_3):
                    if self._try_ollama_cloud_any(body, original_model, response_buffer, t0):
                        return
                elif _pick in EXTERNAL_PROVIDERS or _pick == "deepinfra":
                    if self._try_external_failover(body, original_model,
                                                   response_buffer, t0,
                                                   preferred=_pick):
                        return
                # LiveRouter pick failed — fall through to hardcoded chain
            response_buffer = bytearray()
            if self._try_ollama_cloud_any(body, original_model, response_buffer, t0):
                return
            # OpenCode Go — flat-rate $10/mo, native glm-5.3, try before per-token
            if OPENCODE_GO_KEY and self._try_opencode_go(body, original_model, response_buffer, t0):
                return
            if self._try_external_failover(body, original_model, response_buffer, t0):
                return
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"all providers exhausted, retry later"}')
            return

        # Phase 1.2: LiveRouter returned an external provider (not a z.ai
        # key).  Route to the appropriate external handler.  This ONLY
        # happens when both z.ai keys are exhausted AND the kill switch
        # (.enable_live_routing) is active.  If the external provider fails,
        # fall through to the hardcoded failover chain below.
        if chosen not in KEYS:
            response_buffer = bytearray()
            if chosen in ("ollama_cloud", "ollama_cloud_2", "ollama_cloud_3") and (OLLAMA_CLOUD_KEY or OLLAMA_CLOUD_KEY_2 or OLLAMA_CLOUD_KEY_3):
                if self._try_ollama_cloud_any(body, original_model, response_buffer, t0):
                    return
            elif chosen in EXTERNAL_PROVIDERS or chosen == "deepinfra":
                # Try the LiveRouter-chosen provider first, then the rest
                if self._try_external_failover(body, original_model,
                                               response_buffer, t0):
                    return
            # LiveRouter provider failed — fall through to hardcoded chain
            response_buffer = bytearray()
            if self._try_ollama_cloud_any(body, original_model, response_buffer, t0):
                return
            if OPENCODE_GO_KEY and self._try_opencode_go(body, original_model, response_buffer, t0):
                return
            if self._try_external_failover(body, original_model, response_buffer, t0):
                return
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"all providers exhausted, retry later"}')
            return

        # Step 3: Compute tier for chosen key from Kalman + peak hours + client hint
        model_tier_info = None
        if _select_model_tier is not None and body:
            try:
                model_tier_info = _select_model_tier(chosen, tier_hint if tier_hint else None)
                new_model = model_tier_info.get("model")
                if original_model and new_model and new_model != original_model:
                    body_json = json.loads(body)
                    body_json["model"] = new_model
                    body = json.dumps(body_json).encode()
                    self.headers["Content-Length"] = str(len(body))
            except Exception:
                pass

        # Step 3b: Compression model selection (parallel to tier routing)
        # When the request is a compression call (X-Task-Type: compression or
        # model == "__compress__" sentinel), select the cheapest capable
        # summarizer model based on cost × pressure × benchmarks.
        compression_info = None
        if _select_compression_model is not None and _is_compression_request(self._task_type, original_model):
            try:
                session_ctx = 131072
                compression_info = _select_compression_model(
                    session_id=self._session_id,
                    session_context_length=session_ctx,
                )
                new_model = compression_info.get("model")
                if new_model:
                    body_json = json.loads(body)
                    body_json["model"] = new_model
                    body = json.dumps(body_json).encode()
                    self.headers["Content-Length"] = str(len(body))
            except Exception:
                compression_info = None

        # Step 4: Extract final model (may have been rewritten)
        model = _extract_model(body)

        # Step 5: Log the model decision
        if model_tier_info:
            _log_model_decision(
                key_name=chosen,
                model=model,
                original_model=original_model,
                tier=model_tier_info.get("tier"),
                base_tier=model_tier_info.get("base_tier"),
                hint=tier_hint if tier_hint else None,
                reason=model_tier_info.get("reason"),
                peak=1 if model_tier_info.get("peak") else 0,
                hours_left=model_tier_info.get("hours_left"),
                active_key=chosen,
            )
        elif original_model != model:
            _log_model_decision(
                key_name=chosen,
                model=model,
                original_model=original_model,
                tier="client",
                base_tier="client",
                hint=tier_hint if tier_hint else None,
                reason=f"client X-Model-Tier={tier_hint}",
                peak=0,
                active_key=chosen,
            )

        if compression_info:
            _log_model_decision(
                key_name=chosen,
                model=model,
                original_model=original_model,
                tier="compression",
                base_tier="compression",
                hint=None,
                reason=compression_info.get("reason"),
                peak=0,
                active_key=chosen,
            )

        order = [chosen] + [n for n in KEYS if n != chosen]
        # Never try manually-disabled keys (operator touched
        # ~/.hermes/bot/.key_disabled_<name>). best_key() Phase 4 already steers
        # the *initial* choice away from them via _is_key_healthy; this filter
        # also drops them from the retry fallback list so the loop skips them
        # entirely. If every key is disabled, `order` empties and the request
        # falls through to Ollama Cloud / external failover (correct behaviour).
        order = [n for n in order if not _is_manually_disabled(n)]

        response_buffer = bytearray()
        key_used: str | None = None
        status_code = None
        error_text = None
        try:
            for attempt, name in enumerate(order):
                key_used = name
                key = KEYS[name]
                try:
                    path = self.path
                    # Strip /v1 prefix (OpenAI SDK sends /v1/chat/completions but
                    # the z.ai v4 base URL already contains the API version).
                    if path.startswith("/v1/"):
                        path = path[3:]
                    # Only proxy /chat/completions to z.ai.  Non-chat paths
                    # (model listings, Ollama API probes, version checks) get
                    # a fast local 404 — sending them to z.ai wastes quota
                    # and triggers Hermes fallback retries that burn PPQ.
                    if not path.endswith("/chat/completions"):
                        self.send_response(404)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error":"only /chat/completions is proxied"}')
                        return
                    url = UPSTREAM + path
                    hdrs = {k: v for k, v in self.headers.items()
                            if k.lower() not in ("host", "authorization", "connection", "content-length")}
                    hdrs["Authorization"] = f"Bearer {key}"
                    hdrs["Content-Type"] = "application/json"
                    req = urllib.request.Request(url, data=body, method=self.command, headers=hdrs)
                    with urllib.request.urlopen(req, timeout=180) as resp:
                        status_code = resp.status
                        # Buffer full response before sending — allows
                        # empty-response detection for key health tracking.
                        full_body = resp.read()

                        # Check for empty or error response
                        resp_text = full_body.decode('utf-8', errors='ignore').strip()
                        is_empty = (
                            not resp_text
                            or resp_text == "data: [DONE]"
                        )

                        # Parse JSON to check content field
                        is_error_response = False
                        is_truncated = False  # finish_reason=length (ran out of tokens)
                        if not is_empty:
                            try:
                                resp_json = json.loads(resp_text)
                                # Check for error response (quota exhausted, etc.)
                                if "error" in resp_json and "choices" not in resp_json:
                                    is_error_response = True
                                else:
                                    choices = resp_json.get("choices", [])
                                    if choices:
                                        msg_obj = choices[0].get("message", {})
                                        content = msg_obj.get("content", "")
                                        finish_reason = choices[0].get("finish_reason", "")
                                        if finish_reason == "length":
                                            is_truncated = True
                                        if not content or not content.strip():
                                            # Content is empty — check if reasoning
                                            # has value we can use instead
                                            # FIX-1 (2026-09-02): also accept the
                                            # ollama/neuralwatt field name "reasoning"
                                            # (z.ai uses "reasoning_content").
                                            reasoning = (msg_obj.get("reasoning_content")
                                                         or msg_obj.get("reasoning") or "")
                                            if isinstance(reasoning, str) and reasoning.strip():
                                                # Inject reasoning as content so
                                                # the tokens aren't wasted
                                                msg_obj["content"] = reasoning
                                                full_body = json.dumps(resp_json).encode()
                                                is_empty = False
                                            else:
                                                is_empty = True
                            except Exception:
                                pass

                        if is_error_response:
                            # Error responses are transient (model overload,
                            # internal errors) — NOT quota issues. Only 429
                            # should block a key. Failover this request only.
                            continue

                        if is_empty:
                            # Content AND reasoning both empty — key produced nothing.
                            # Try external failover for THIS request only.
                            # Do NOT mark key as exhausted (it might work next time).
                            if self._try_external_failover(body, model, response_buffer, t0):
                                return
                            continue  # try next key

                        # Non-empty response — send to client
                        _mark_key_healthy(name)
                        status_code = resp.status
                        try:
                            self.send_response(resp.status)
                            for h, v in resp.headers.items():
                                if h.lower() not in ("transfer-encoding", "connection"):
                                    self.send_header(h, v)
                            # S2b (t_4dfaf0d5): surface the silent rewrite —
                            # shadow observability, rewrite behavior unchanged.
                            for _ph, _pv in self._pressure_headers(
                                    original_model, model, model_tier_info):
                                self.send_header(_ph, _pv)
                            if is_truncated:
                                self.send_header("X-Response-Truncated", "true")
                            self.end_headers()
                            response_buffer.extend(full_body)
                            self.wfile.write(full_body)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError) as _ce:
                            # Client vanished mid-stream — the upstream
                            # succeeded, so the key stays healthy and must
                            # NOT be marked failed (nor retried elsewhere).
                            # Truthful api_calls row: upstream status + a
                            # client-disconnect note instead of a fake 502.
                            error_text = f"client disconnect: {_ce.__class__.__name__}"
                            return
                        # Success — reset the Kalman consecutive-429 streak.
                        if _rate_limit_predictor is not None:
                            _rate_limit_predictor.record_success()
                        return
                except urllib.error.HTTPError as e:
                    # Classify the failure by HTTP status to arm the correct
                    # circuit-breaker backoff (req 2 — dead-key detection):
                    #   429              → exhausted (exponential 2→60s)
                    #   401/403          → dead key  (flat 1h)
                    #   500/502/503/504  → server err (flat 30s)
                    _body_403 = b""
                    if e.code == 429:
                        _mark_key_exhausted(name)
                    elif e.code in (401, 403):
                        # Distinguish 403-quota-exhaustion from 403-auth-revocation.
                        # z.ai returns 403 (not 429) when the weekly quota is
                        # exhausted — this is TRANSIENT (recovers on window
                        # reset), not a dead key. Parse the body for keywords.
                        _body_403 = b""
                        try:
                            _body_403 = e.read()
                        except Exception:
                            pass
                        _body_403_lower = _body_403.decode(errors="ignore").lower()
                        if any(_kw in _body_403_lower for _kw in
                               ("quota", "exhaust", "limit", "rate", "exceed",
                                "insufficient", "weekly", "session")):
                            _mark_key_exhausted(name)
                        else:
                            _mark_key_dead(name)
                    elif e.code in (500, 502, 503, 504):
                        _mark_key_server_error(name)
                    if _is_retryable_error(e):
                        if _attempt_retry(e, attempt, name, t0, order):
                            continue
                    # z.ai failure — try external failover before giving up
                    # Include 429 (rate limit) since z.ai returns 429 when exhausted
                    if e.code in (401, 403, 429) and self._try_external_failover(body, model, response_buffer, t0):
                        return
                    # Non-retryable error
                    status_code = e.code
                    error_text = f"HTTPError {e.code}"
                    # 403 body was already read during classification above;
                    # reuse it instead of calling e.read() again (which returns b"").
                    if e.code in (401, 403) and _body_403:
                        body_err = _body_403
                    else:
                        body_err = e.read()
                    response_buffer.extend(body_err)
                    self.send_response(e.code)
                    self.end_headers()
                    self.wfile.write(body_err)
                    return
                except Exception as e:
                    if _is_retryable_error(e):
                        if _attempt_retry(e, attempt, name, t0, order):
                            continue
                    # Non-retryable error. A timeout / connection failure IS
                    # an upstream server-side failure: mark the key so the
                    # anomaly trail (and the recent_503 gate) sees the burst
                    # class that previously surfaced only as client 502s with
                    # zero anomaly rows (t_8e2673cd, 2026-08-15 read timeouts).
                    _mark_key_server_error(name)
                    status_code = 502
                    error_text = f"proxy error: {e}"
                    msg = f"proxy error: {e}".encode()
                    response_buffer.extend(msg)
                    self.send_response(status_code)
                    self.end_headers()
                    self.wfile.write(msg)
                    return

            # Phase 1.2 LIVE ROUTER — retry-loop terminal fallback (P3.4 Fix 1).
            # When BOTH z.ai keys 429-exhaust DURING the retry loop, best_key()'s
            # initial pick already returned a key (its health cache lagged the
            # real 429), so the Phase 5 gate inside best_key() never fired. This
            # is the production path that previously bypassed LiveRouter (841
            # dual-exhaustion events/2h, 0 live events). Consult LiveRouter HERE
            # and route its pick before the hardcoded ollama->external chain.
            # Kill switch + safe fallthrough live inside _consult_live_router;
            # on any failure / no pick we fall through to the chain below.
            _pick, _pick_model, _fb, _fb_model = _consult_live_router(
                model=original_model,
                task_type=_tier_to_task_type(tier_hint),
            )
            if _pick:
                _log_key_decision(
                    chosen_key=_pick,
                    reason=f"live_kalman_failover_{_pick}")
                response_buffer = bytearray()
                if _pick in ("ollama_cloud", "ollama_cloud_2", "ollama_cloud_3") and (OLLAMA_CLOUD_KEY or OLLAMA_CLOUD_KEY_2 or OLLAMA_CLOUD_KEY_3):
                    if self._try_ollama_cloud_any(body, model, response_buffer, t0):
                        return
                elif _pick in EXTERNAL_PROVIDERS:
                    if self._try_external_failover(body, model,
                                                   response_buffer, t0,
                                                   preferred=_pick):
                        return
                # LiveRouter pick failed (or was a z.ai key) — fall through to
                # the hardcoded chain below (safe fallback, criterion 4).

            # All z.ai keys exhausted — try Ollama Cloud (primary, not failover)
            if not peak and OLLAMA_CLOUD_KEY:
                if self._try_ollama_cloud_any(body, model, response_buffer, t0):
                    return

            # All primary providers exhausted — try paid failover (PPQ/OpenRouter)
            if self._try_external_failover(body, model, response_buffer, t0):
                return

            # All providers failed
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"all providers exhausted, retry later"}')
            return
        finally:
            usage = _parse_usage(bytes(response_buffer))
            suffix = None
            if key_used and KEYS.get(key_used):
                suffix = KEYS[key_used][-4:]
            # RP-2: extract real cost (flat-rate $0 for ours/friend z.ai keys)
            _zai_tokens = int(usage.get("total_tokens") or 0)
            _zai_cost, _zai_cost_src = _extract_cost(
                key_used, bytes(response_buffer), _zai_tokens)
            _log_api_call(
                key_name=key_used, key_suffix=suffix, model=model,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=_zai_tokens,
                tier="zai", status_code=status_code, error=error_text,
                duration_ms=int((time.time() - t0) * 1000),
                cost_usd=_zai_cost, cost_source=_zai_cost_src,
                session_id=getattr(self, "_session_id", None),
                task_type=getattr(self, "_task_type", None),
            )
            if not getattr(self, '_spend_recorded', False):
                _record_spend(key_used, model, _zai_tokens)

            # ── Provider telemetry (Phase 2.5.1) ─────────────────────────────
            # One row per request: success/fail, latency, token-mismatch.
            # NEVER raises — telemetry failure is silent and must never break
            # request handling.  Wrapped in its own try/except (the function
            # itself also swallows errors, so this is belt-and-suspenders).
            try:
                _latency_ms = int((time.time() - t0) * 1000)
                # Use completion_tokens — NOT total_tokens — for the audit.
                # `actual_tokens` is estimated from len(response_buffer)//4,
                # and the response buffer contains ONLY the completion content
                # (the prompt is never echoed back).  Comparing total_tokens
                # (prompt+completion) against a completion-only estimate always
                # looks like a >20% over-billing gap whenever the prompt is
                # non-trivial, producing false-positive billing-mismatch alerts.
                # (Phase 2.5.4 false-positive fix.)
                _billed = int(usage.get("completion_tokens") or 0)
                _resp_buf = bytes(response_buffer)
                # Classify the upstream response for telemetry. Extracted to
                # _classify_response() (testable; mirrors _parse_usage's SSE
                # handling) so genuine HTTP/network errors are reported with
                # their real error_text instead of a generic 'parse_error'.
                _resp_received, _resp_valid, _err_type = _classify_response(
                    _resp_buf, error_text)
                # Estimate actual tokens + detect billing mismatch (Phase 2.5.4).
                # Uses the unit-tested audit_token_count from src/token_audit.py
                # (with a never-raising fallback stub if the import failed).
                # Token audit NEVER blocks request handling — _audit_token_count
                # swallows all errors internally.
                _actual, _mismatch, _mm_rate = _audit_token_count(_billed, _resp_buf)
                if _mismatch:
                    # Quality signal: feed mismatch_rate into CPVO via the
                    # token_mismatch telemetry column. Warn loudly — a large
                    # billed-vs-actual gap on the COMPLETION content is a
                    # billing-fraud / silent-downgrade signal worth
                    # investigating.  (Note: total_tokens is intentionally NOT
                    # used here — see the comment at the _billed assignment
                    # above — because the response buffer holds completion text
                    # only, so completion_tokens is the correct comparison basis.)
                    print(
                        f"[telemetry] token billing mismatch (completion): "
                        f"provider={key_used or 'unknown'} "
                        f"billed={_billed} actual~={_actual} "
                        f"gap={_mm_rate:.0%}",
                        flush=True,
                    )
                _log_provider_telemetry(
                    conn=_usage_db(),
                    provider=key_used or "unknown",
                    response_received=_resp_received,
                    response_valid=_resp_valid,
                    latency_ms=_latency_ms,
                    error_type=_err_type,
                    billed_tokens=_billed,
                    actual_tokens=_actual,
                    token_mismatch=_mismatch,
                    model=model,
                )
            except Exception:
                pass

            # ── Shadow mode: log optimizer decision alongside live pick ────
            # Read-only comparison. NEVER affects routing. Wrapped so any
            # shadow failure cannot break the proxied request.
            if _shadow_hook is not None:
                try:
                    _shadow_hook.compare(
                        live_provider=key_used,
                        live_model=model,
                        tokens=int(usage.get("total_tokens") or 0),
                        quota_state=_snapshot_quota(),
                        health_state=_snapshot_health(),
                        peak=peak if 'peak' in dir() else False,
                    )
                except Exception:
                    pass

            # ── Phase 2.3: Live consumption tracking ──────────────────────
            # Feed completed request token count to LiveRouter's Kalman
            # filters. Wrapped in try/except — NEVER breaks request handling.
            # record_request updates the ConsumptionKalman for the provider
            # that served this request, keeping burn-rate predictions fresh.
            if _LIVE_ROUTER is not None:
                try:
                    total_tokens = int(usage.get("total_tokens") or 0)
                    _LIVE_ROUTER.record_request(
                        provider=key_used if key_used else "unknown",
                        tokens=total_tokens,
                    )
                except Exception:
                    pass  # recording must never break production

    def do_POST(self): self._proxy()
    def do_PUT(self):  self._proxy()
    def do_GET(self):
        if self.path == "/v1/pricing" or self.path.startswith("/v1/pricing?"):
            # CG-2 price-exposure feed (plan v2.1 §2.1). Read-only snapshot.
            # ?model= filters flat-tier catalog rates; ?horizon_min= appends a
            # forecast horizon for task-duration pricing.
            self.close_connection = True
            try:
                from urllib.parse import urlsplit, parse_qs
                qs = parse_qs(urlsplit(self.path).query)
                _model = qs.get("model", [None])[0]
                _h = qs.get("horizon_min", [None])[0]
                _horizon = int(_h) if _h is not None and str(_h).lstrip("-").isdigit() else None
                result = _build_v1_pricing(model=_model, horizon_min=_horizon)
                payload = json.dumps(result, indent=2).encode()
            except Exception as e:
                payload = json.dumps(
                    {"error": str(e), "hint": "v1/pricing endpoint failed"},
                    indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/quota":
            with lock:
                data = {}
                for n, v in quota_cache.items():
                    wins = v[0]
                    lckd, lwin, lpct, lthr = is_key_locked(n, wins)
                    data[n] = {
                        "windows": wins,
                        "locked": lckd,
                        "locked_window": lwin,
                        "locked_pct": lpct,
                        "locked_threshold": lthr,
                        "max_pct": _max_pct(wins),
                        "age_s": int(time.time() - v[1]),
                    }
                data["active"] = _best_unlocked()[0]
                data["proactive_cooldown"] = {
                    "switched_to": _proactive_switch_state["key"],
                    "active": time.time() < _proactive_switch_state["until"],
                    "expires_in_s": max(0, int(_proactive_switch_state["until"] - time.time())),
                }
            # Predictions: cache-ONLY (never triggers a fetch → no self-HTTP
            # recursion deadlock).  The background _refresh_loop keeps these warm.
            for n in KEYS:
                if n in data:
                    data[n]["predictions"] = _get_cached_predictions(n)
            # Ollama Cloud quota from tracker (EUv2-5)
            _sq = _snapshot_quota()
            data["ollama_cloud"] = _sq.get("ollama_cloud", {})
            data["ollama_cloud_2"] = _sq.get("ollama_cloud_2", {})
            data["ollama_cloud_3"] = _sq.get("ollama_cloud_3", {})
            data["opencode_go"] = _sq.get("opencode_go", {})
            data["neuralwatt"] = _sq.get("neuralwatt", {})
            payload = json.dumps(data, indent=2).encode()
            self.close_connection = True   # honor the Connection: close header below
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/pressure" or self.path.startswith("/pressure?"):
            # Pressure FSM observability (S2b, t_4dfaf0d5) — band state,
            # mode, kill-switch status and last shadow decisions.
            # ?limit=N clamps to [1,100]; anything invalid -> 20.
            self.close_connection = True
            limit = 20
            try:
                from urllib.parse import urlsplit, parse_qs
                _q = parse_qs(urlsplit(self.path).query)
                _n = int(_q.get("limit", [""])[0])
                limit = 20 if _n < 1 else (100 if _n > 100 else _n)
            except Exception:
                pass
            try:
                payload = json.dumps(
                    self._pressure_tracker_snapshot(limit=limit),
                    indent=2).encode()
            except Exception as e:
                payload = json.dumps(
                    {"error": str(e), "hint": "pressure FSM snapshot failed"}
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/health":
            self.close_connection = True   # honor the Connection: close header below
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == "/kalman-pricing":
            # Kalman-aware pricing feed for routstrd (read-only).
            # Returns effective prices per z.ai key + PPQ, computed from
            # the same Kalman state the dispatch_gate uses.  See
            # IMPL-SPEC-kalman-pricing-feed.md.
            #
            # Uses the shared _build_kalman_pricing_json() function so the
            # endpoint and the Nostr publisher always emit identical data.
            self.close_connection = True
            try:
                result = _build_kalman_pricing_json()
                payload = json.dumps(result, indent=2).encode()
            except Exception as e:
                payload = json.dumps(
                    {"error": str(e), "hint": "kalman-pricing endpoint failed"},
                    indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/tier":
            # Current recommended model tier (for dispatch gate queries)
            # Supports ?urgency=urgent|standard|background query parameter
            self.close_connection = True
            try:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                urgency = qs.get("urgency", ["standard"])[0]
                # GAP C (2026-09-04): re-point /tier off best_key() to the flat
                # router's top candidate. select_provider() picks the cheapest
                # healthy provider for the representative model (glm-5.2, the
                # manager default); its top candidate becomes active_key. The
                # response shape (active_key / quota_pct) is unchanged. best_key()
                # remains as the fallback when the flat router is unavailable or
                # raises (it is removed in Phase 1).
                chosen = None
                try:
                    from flat_router import select_provider as _tier_sp
                    _tier_cands = _tier_sp(model="glm-5.2")
                    _tier_top = next(
                        (c for c in _tier_cands if c.name != "fallback"), None)
                    if _tier_top is not None:
                        chosen = _tier_top.name
                except Exception:
                    chosen = None
                if chosen is None:
                    chosen = best_key()
                if _select_model_tier is not None:
                    info = _select_model_tier(chosen, None, urgency)
                else:
                    # Tier router is disabled — model selection is profile-level
                    # (each profile sets its own model in config.yaml). The proxy
                    # passes through whatever model the profile requests.
                    info = {"tier": "disabled", "model": "profile-level",
                            "reason": "model selection is profile-level, proxy passes through"}
                info["active_key"] = chosen
                info["quota_pct"] = {n: _max_pct(v[0]) for n, v in quota_cache.items()}
            except Exception as e:
                info = {"tier": "error", "reason": str(e)}
            payload = json.dumps(info, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path.startswith("/route"):
            # Full routing decision endpoint (Kalman + costs + difficulty)
            self.close_connection = True
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            tokens = int(qs.get("tokens", ["0"])[0])
            difficulty = qs.get("difficulty", ["medium"])[0]
            try:
                sys.path.insert(0, os.path.dirname(__file__))
                from burn_predictor import route_request
                decision = route_request(estimated_tokens=tokens, difficulty=difficulty)
            except Exception as e:
                decision = {"error": str(e)}
            payload = json.dumps(decision, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path.startswith("/v1/dispatch_gate"):
            # Dispatch gate — should this job run now?
            # Three-dimension Kalman-gated decision (no SQLite reads):
            #   D1 hardware availability (binary) → D2 quota sufficiency
            #   (hardware-scaled safety margin + flash downgrade) → D3 price
            #   (scarcity override when hardware present).  See
            #   IMPL-SPEC-kalman-dispatch-gate.md (v2) + src/dispatch_gate.py.
            self.close_connection = True
            from urllib.parse import urlparse, parse_qs
            # NOTE: do NOT `from datetime import datetime, timezone` here.
            # A local import anywhere in do_GET makes `datetime` a local name
            # for the WHOLE method scope, so branches that use it without
            # binding it first (e.g. /spend at datetime.now(timezone.utc))
            # raise UnboundLocalError → HTTP 500. The module-level import
            # (top of file) already provides datetime/timezone.
            try:
                qs = parse_qs(urlparse(self.path).query)
                estimated_tokens = int(qs.get("estimated_tokens", ["0"])[0])
                task_type = qs.get("task_type", ["coding"])[0]
                urgency = qs.get("urgency", ["standard"])[0]
                hardware_req = qs.get("hardware_req", ["none"])[0]
                task_subtype = qs.get("task_subtype", [None])[0]

                peak = _is_peak_hour()
                peak_mult = 3.0 if peak else 1.0
                quota_snap = _snapshot_quota()
                health_snap = _snapshot_health()

                # 1. Gather primary candidates (ours/friend) for BOTH the gate
                #    and the legacy recommended_provider / downgrade_chain fields.
                candidates = []
                gate_quota = {}
                for key in ("ours", "friend"):
                    if not health_snap.get(key):
                        gate_quota[key] = {"used_pct": 100.0, "remaining": 0.0, "healthy": False}
                        continue
                    q = quota_snap.get(key, {})
                    remaining = q.get("remaining", 0)
                    gate_quota[key] = {
                        "used_pct": q.get("used_pct", 0.0),
                        "remaining": remaining,
                        "healthy": True,
                    }
                    if remaining >= estimated_tokens:
                        base = (_converged_rates or {}).get(key)
                        if base is None:
                            base = _rpt_rate(key)
                        eff = base * _KEY_COST_MULTIPLIER.get(key, 1.0) * peak_mult
                        candidates.append({
                            "provider": key,
                            "price_per_m": eff,
                            "remaining": remaining,
                            "used_pct": q.get("used_pct", 0.0),
                        })
                # Ollama Cloud — flat rate, BYPASSES the quota margin gate (no
                # exhaustion risk).  Tracked separately so it can act as a
                # fallback when the primary-key gate holds.
                flat_candidates = []
                if health_snap.get("ollama_cloud") and OLLAMA_CLOUD_KEY:
                    base = (_converged_rates or {}).get("ollama_cloud")
                    if base is None:
                        base = _rpt_rate("ollama_cloud")
                    eff = base * _KEY_COST_MULTIPLIER.get("ollama_cloud", 1.0) * peak_mult
                    flat_candidates.append({
                        "provider": "ollama_cloud",
                        "price_per_m": eff,
                        "remaining": quota_snap.get("ollama_cloud", {}).get("remaining", 999999999),
                        "used_pct": 0.0,
                    })
                candidates.sort(key=lambda c: c["price_per_m"])
                flat_candidates.sort(key=lambda c: c["price_per_m"])
                all_candidates = candidates + flat_candidates

                # 2. Cached burn-rate predictions (cache-only → never fetches).
                burn_rate = {}
                hours_until = {}
                for key in ("ours", "friend"):
                    preds = _get_cached_predictions(key)
                    exhaust = _will_exhaust(preds)
                    burn_rate[key] = (exhaust or {}).get("burn_rate_pct_per_hour", 0.0) or 0.0
                    hours_until[key] = (exhaust or {}).get("exhausts_in_hours", 999)

                # 3. Hardware probe (Dimension 1) — only when hardware_req != none.
                hw_state = _probe_hardware(hardware_req)

                # 4. Run the three-dimension gate (src/dispatch_gate.py).
                if _evaluate_dispatch is not None:
                    gate = _evaluate_dispatch(
                        estimated_tokens=estimated_tokens,
                        task_type=task_type,
                        hardware_req=hardware_req,
                        task_subtype=task_subtype,
                        quota=gate_quota,
                        burn_rate_pct_per_hour=burn_rate,
                        converged_rates=(_converged_rates or {"ours": 0.001, "friend": 0.001}),
                        is_peak=peak,
                        peak_mult=peak_mult,
                        hardware_state=hw_state,
                    )
                else:
                    # Module unavailable — coarse decision, but the HARDWARE
                    # GATE (D1) must stay FAIL-CLOSED.  Without the real
                    # module we cannot safely confirm a board/DQ05, so default
                    # to *unavailable* unless the probed hw_state actually
                    # confirms presence+free.  This mirrors
                    # src/dispatch_gate._hardware_available so a board-required
                    # task can never dispatch on ollama_cloud (flat-rate path
                    # below also checks gate["hardware"]["available"]).
                    _hws = hw_state or {}
                    _lock_free = _hws.get("lock_status") == "free"
                    _hw_avail = (
                        hardware_req == "none"
                        or (hardware_req == "board"
                            and _hws.get("board_present") and _lock_free)
                        or (hardware_req == "dual_board"
                            and _hws.get("board_count", 0) >= 2 and _lock_free)
                        or (hardware_req == "dq05"
                            and _hws.get("dq05_reachable"))
                    )
                    gate = {
                        "can_dispatch": bool(candidates) and _hw_avail,
                        "reason": "dispatch_gate module unavailable; coarse check",
                        "recommended_model": (candidates[0] and "glm-5.2") if candidates else None,
                        "effective_price_per_m": round(candidates[0]["price_per_m"], 6) if candidates else None,
                        "predicted_cost": None,
                        "hours_until_exhaustion": {k: hours_until[k] for k in ("ours", "friend")},
                        "quota_used_pct": {k: round(gate_quota[k]["used_pct"], 1) for k in ("ours", "friend")},
                        "burn_rate_pct_per_hour": {k: round(burn_rate[k], 1) for k in ("ours", "friend")},
                        "is_peak_hour": peak, "peak_multiplier": peak_mult,
                        "scarcity_factor": 1.0, "downgraded": False,
                        "scarcity_override": False,
                        "hardware": {"required": hardware_req, "available": _hw_avail},
                        "task_budget": estimated_tokens, "safety_margin": 2.0,
                    }

                # 5. Flat-rate fallback: gate held on QUOTA (primary keys tight)
                #    but a flat-rate provider is available → dispatch anyway (no
                #    quota risk).  Does NOT apply to hardware holds — a task that
                #    needs a board/DQ05 cannot run on a flat-rate LLM provider.
                recommended_provider = all_candidates[0]["provider"] if all_candidates else None
                hw_avail = gate.get("hardware", {}).get("available", True)
                if not gate["can_dispatch"] and flat_candidates and hw_avail:
                    fc = flat_candidates[0]
                    gate["can_dispatch"] = True
                    gate["downgraded"] = True
                    gate["recommended_model"] = "llama3.3-70b"
                    gate["reason"] = ("primary keys tight (gate hold); dispatching "
                                      "on flat-rate " + fc["provider"])
                    gate["effective_price_per_m"] = round(fc["price_per_m"], 6)
                    recommended_provider = fc["provider"]

                # 6. Legacy fields (kept for backward compatibility — ADDITIVE).
                # Urgency-tier model selection incl. the new spec task types.
                TASK_MODELS = {
                    "coding":     {"high": "glm-5.3",        "standard": "glm-4.5-air",   "low": "glm-4.5-flash"},
                    "reasoning":  {"high": "glm-4.5",        "standard": "glm-4.5-air",   "low": "glm-4.5-flash"},
                    "chat":       {"high": "glm-4.5-air",    "standard": "glm-4.5-air",   "low": "glm-4.5-flash"},
                    "simple":     {"high": "glm-4.5-flash",  "standard": "glm-4.5-flash", "low": "glm-4.5-flash"},
                    "mechanical": {"high": "glm-4.5-flash",  "standard": "glm-4.5-flash", "low": "glm-4.5-flash"},
                    "research":   {"high": "glm-5.2",        "standard": "glm-5.2",        "low": "glm-4.5-flash"},
                    "review":     {"high": "glm-5.3",        "standard": "glm-4.5-air",   "low": "glm-4.5-flash"},
                    "docs":       {"high": "glm-4.5-flash",  "standard": "glm-4.5-flash", "low": "glm-4.5-flash"},
                }
                tt = task_type if task_type in TASK_MODELS else "coding"
                chain = []
                for tier in ("high", "standard", "low"):
                    m = TASK_MODELS[tt][tier]
                    viable = bool(all_candidates)
                    provider = all_candidates[0]["provider"] if all_candidates else "none"
                    chain.append({"model": m, "tier": tier, "provider": provider, "viable": viable})

                # Defer suggestion (suppressed when scarcity override is active).
                defer = None
                if peak and urgency == "background" and not gate.get("scarcity_override"):
                    defer = {"reason": "peak_hours_3x_cost", "wait_until_utc_hour": 11, "savings_factor": 3.0}

                # Quota state snapshot (thread-safe).
                quota_state = {}
                with lock:
                    for key in ("ours", "friend"):
                        wins = quota_cache.get(key, ([], 0.0))[0]
                        pct = _max_pct(wins)
                        lckd, _lwin, _lpct, _lthr = is_key_locked(key, wins)
                        quota_state[key] = {
                            "used_pct": pct,
                            "remaining_tokens": int(max(0.0, 2_000_000 * (1.0 - pct / 100.0))),
                            "locked": lckd,
                        }

                # Peak timing.
                now_utc = datetime.now(timezone.utc)
                peak_ends_in = max(0, 11 - now_utc.hour) if peak else None

                # 7. Build response — gate fields (authoritative) + legacy fields.
                est_cost = None
                if gate.get("effective_price_per_m") is not None:
                    est_cost = round(gate["effective_price_per_m"] * estimated_tokens / 1e6, 6)
                info = dict(gate)
                info.update({
                    "recommended_provider": recommended_provider,
                    "estimated_cost_usd": est_cost,
                    "hours_until_exhaust": hours_until,
                    "peak_active": peak,
                    "peak_ends_in_hours": peak_ends_in,
                    "defer_suggestion": defer,
                    "downgrade_chain": chain,
                    "quota_state": quota_state,
                    "timestamp": int(time.time()),
                })
            except Exception as e:
                info = {"can_dispatch": False, "reason": "error: " + str(e)}
            payload = json.dumps(info, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/v1/models" or self.path == "/models":
            # Model listing with Kalman-driven dynamic sats_pricing.
            # Reads ~/.hermes/bot/kalman_pricing.json (written by exhaustion-gate.py
            # every 5 min). Price per model is determined by its backing pool's
            # effective_rate_per_m. Delisted pools → model omitted from listing.
            self.close_connection = True
            now = int(time.time())

            # Load dynamic pricing state
            _kp = {}
            try:
                _kp_path = Path.home() / ".hermes" / "bot" / "kalman_pricing.json"
                if _kp_path.exists():
                    _kp = json.loads(_kp_path.read_text()).get("providers", {})
            except Exception:
                pass

            # Model → backing pool mapping (which quota pool serves this model)
            _MODEL_POOL = {
                "glm-5.3":          "ours",
                "glm-5.2":           "ollama_cloud",
                "glm-4.5-flash":    "ollama_cloud",
                "glm-4.5-air":      "ollama_cloud",
                "kimi-k2.7-code":   "ollama_cloud",
                "kimi-k3:cloud":    "ollama_cloud",
                "kimi-k3":          "telnyx",
                "minimax-m3:cloud": "ollama_cloud",
            }

            # Default near-zero pricing (internal use — our own agents pay ~$0)
            _DEFAULT_SP = {
                "prompt": 0.000001, "completion": 0.000001, "request": 1,
                "image": 0, "web_search": 0, "internal_reasoning": 0,
                "max_completion_cost": 2, "max_prompt_cost": 2, "max_cost": 3,
            }

            def _m(mid, owner, ctx=1048576):
                pool = _MODEL_POOL.get(mid, "ollama_cloud")
                kp_entry = _kp.get(pool, {})
                if kp_entry.get("delisted"):
                    return None
                sat_per_m = kp_entry.get("sat_per_m")
                if sat_per_m and sat_per_m > 0:
                    sat_per_token = sat_per_m / 1e6
                    sp = {
                        "prompt": sat_per_token,
                        "completion": sat_per_token,
                        "request": 1, "image": 0, "web_search": 0,
                        "internal_reasoning": 0,
                        "max_completion_cost": int(sat_per_m * 2),
                        "max_prompt_cost": int(sat_per_m),
                        "max_cost": int(sat_per_m * 3),
                    }
                else:
                    sp = dict(_DEFAULT_SP)
                return {"id": mid, "object": "model", "created": now,
                        "owned_by": owner, "context_window": ctx,
                        "sats_pricing": sp}

            _all_models = [
                _m("glm-5.3", "zai"),
                _m("glm-5.2", "zai"),
                _m("glm-4.5-flash", "zai"),
                _m("glm-4.5-air", "zai"),
                _m("kimi-k2.7-code", "ollama", 262144),
                _m("kimi-k3:cloud", "ollama", 262144),
                _m("kimi-k3", "telnyx", 262144),
                _m("minimax-m3:cloud", "ollama", 1048576),
            ]
            models_data = {
                "object": "list",
                "data": [m for m in _all_models if m is not None],
            }
            payload = json.dumps(models_data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/spend":
            # Daily spend tracker — shows current spend vs caps
            self.close_connection = True
            try:
                today = datetime.now(timezone.utc).strftime('%Y-%m-%d')  # UTC, not local IST
                rows = _usage_db().execute(
                    "SELECT tier, spend_usd, call_count, token_count "
                    "FROM daily_spend WHERE date=?", (today,)).fetchall()
                data = {
                    "date": today,
                    "caps": {"manager": _SPEND_CAP_MANAGER, "worker": _SPEND_CAP_WORKER},
                    "tiers": {},
                }
                # D6: PPQ good-use policy state (daily cap / hourly / storms)
                try:
                    pol = _ppq_policy()
                    ppq_row = _ppq_usage_row()
                    data["ppq_policy"] = {
                        "enabled": pol.get("enabled", True),
                        "daily_cap_usd": pol["daily_cap_usd"],
                        "max_requests_per_hour": pol["max_requests_per_hour"],
                        "spend_usd": round(ppq_row["spend_usd"], 4),
                        "pct_of_cap": round(
                            ppq_row["spend_usd"] / pol["daily_cap_usd"] * 100, 1
                        ) if pol["daily_cap_usd"] > 0 else 0,
                        "requests_today": ppq_row["requests"],
                        "requests_this_hour": int(
                            json.loads(ppq_row["hour_requests"] or "{}").get(
                                _ppq_hour_bucket(), 0)),
                        "tokens_today": ppq_row["tokens"],
                        "storm_blocked_today": ppq_row["storm_blocked"],
                    }
                except Exception as _ppq_e:
                    data["ppq_policy"] = {"error": str(_ppq_e)}
                for tier, spend, calls, tokens in rows:
                    # Map key-based tier names to the correct cap for display.
                    # "ours" is the z.ai subscription — serves manager models
                    # (glm-5.2/glm-5.3) so gets manager cap.
                    _MANAGER_DISPLAY_TIERS = {"ours", "ollama_cloud", "friend",
                                              "deepinfra", "telnyx", "ppq",
                                              "openrouter", "routstr"}
                    cap = _SPEND_CAP_MANAGER if tier in _MANAGER_DISPLAY_TIERS else _SPEND_CAP_WORKER
                    data["tiers"][tier] = {
                        "spend_usd": round(spend, 4),
                        "cap_usd": cap,
                        "pct_of_cap": round(spend / cap * 100, 1) if cap > 0 else 0,
                        "call_count": calls,
                        "token_count": tokens,
                    }
            except Exception as e:
                data = {"error": str(e)}
            payload = json.dumps(data, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path.startswith("/viz/"):
            # Price/quota visualization static handler (price_viz.py)
            self.close_connection = True
            viz_dir = os.path.expanduser("~/.hermes/viz")
            requested = self.path[len("/viz/"):]
            # Prevent path traversal
            safe_name = os.path.basename(requested)
            if not safe_name or ".." in safe_name:
                self.send_error(404)
                return
            filepath = os.path.join(viz_dir, safe_name)
            if not os.path.isfile(filepath):
                self.send_error(404)
                return
            ext = os.path.splitext(safe_name)[1].lower()
            ct = {"png": "image/png", "txt": "text/plain", "json": "application/json"}.get(ext.lstrip("."), "application/octet-stream")
            try:
                with open(filepath, "rb") as f:
                    payload = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
            except Exception:
                self.send_error(500)
        else:
            self._proxy()

    def log_message(self, *a):
        pass


# ── Nostr kind-30315 Kalman pricing publisher ────────────────────────────────
# Background thread that publishes the /kalman-pricing endpoint data as a
# public kind-30315 replaceable Nostr event every 30 seconds.  This replaces
# the old reverse SSH tunnel — Nostr relays are the transport now, so any
# machine on any network can subscribe.
#
# The publisher uses the `nak` CLI (available at ~/.local/bin/nak) to sign
# and publish events.  Falls back gracefully: all exceptions are caught and
# logged, never crashing the main proxy.
_NOSTR_SEC_PATH = Path.home() / ".hermes" / "bot" / "kalman_npub.nsec"
_NOSTR_RELAYS = [
    "wss://relay.primal.net",
    "wss://nos.lol",
    "wss://relay.damus.io",
]
_NOSTR_PUBLISH_INTERVAL = 30  # seconds
_NOSTR_PUBLISHER_NPUB = "npub1q2pk0674pg7yn5et8vhxxp3pe6s74grwpy30qj3wja7dysduqtms0ef294"


def _load_nostr_sec() -> str | None:
    """Load the Nostr private key from disk.  Returns hex sec or None."""
    try:
        if _NOSTR_SEC_PATH.exists():
            sec = _NOSTR_SEC_PATH.read_text().strip()
            if len(sec) == 64:
                return sec
    except Exception:
        pass
    return None


def _build_kalman_pricing_json() -> dict:
    """Build the same JSON that /kalman-pricing returns, plus source field.
    Extracted so the publisher thread can call it without HTTP self-request.
    """
    peak = _is_peak_hour()
    peak_mult = 3.0 if peak else 1.0
    quota_snap = _snapshot_quota()
    health_snap = _snapshot_health()

    zai_providers = {}
    available_zai = []
    for key in ("ours", "friend"):
        healthy = health_snap.get(key, False)
        with lock:
            wins = quota_cache.get(key, ([], 0.0))[0]
        locked, lwin, lpct, lthr = is_key_locked(key, wins)
        pct = _max_pct(wins)

        win_pcts = {}
        for w in wins:
            wname = w.get("name", "unknown")
            win_pcts[wname] = w.get("used_pct", 0)

        base = (_converged_rates or {}).get(key)
        if base is None:
            base = _rpt_rate(key)

        cost_mult = _KEY_COST_MULTIPLIER.get(key, 1.0)
        scarcity_mult = 1.0
        if pct >= 80:
            scarcity_mult = 1.0 + (pct - 80) / 20.0

        health_mult = 1.0 if healthy else 10.0

        preds = _get_cached_predictions(key)
        exhaust = _will_exhaust(preds)
        burn_rate = (exhaust or {}).get("burn_rate_pct_per_hour", 0.0) or 0.0
        hours_until = (exhaust or {}).get("exhausts_in_hours", None)
        will_exhaust = bool(exhaust)
        pace_mult = 1.0
        if will_exhaust and hours_until is not None and hours_until < 6:
            pace_mult = 1.0 + (6.0 - hours_until) / 6.0

        effective = base * cost_mult * peak_mult * scarcity_mult * health_mult * pace_mult

        available = healthy and not locked

        # Safety: if there are NO quota windows at all (empty list from the
        # API or cache miss), we are blind — do NOT publish "available" on
        # the false 0% that _max_pct([]) returns. Also guard the "unknown"
        # sentinel case where the API returned no parseable limits.
        quota_data_unavailable = bool(
            not wins or all(w.get("name") == "unknown" for w in wins)
        )
        if quota_data_unavailable:
            available = False
            locked = True
            lwin = "no_quota_data" if not wins else "unknown_quota_data"

        zai_providers[f"zai_{key}"] = {
            "base_rate_usd_per_m": round(base, 6),
            "effective_price_usd_per_m": round(effective, 6),
            "peak_multiplier": peak_mult,
            "scarcity_multiplier": round(scarcity_mult, 3),
            "health_multiplier": health_mult,
            "pace_multiplier": round(pace_mult, 3),
            "quota_used_pct": win_pcts,
            "locked": locked,
            "locked_window": lwin,
            "locked_threshold": lthr,
            "will_exhaust": will_exhaust,
            "hours_until_exhaustion": round(hours_until, 1) if hours_until else None,
            "burn_rate_tph": round(burn_rate, 1),
            "available": available,
            "quota_data_unavailable": quota_data_unavailable,
        }
        if available:
            available_zai.append((key, effective))

    ppq_base = 0.28
    ppq_snap = quota_snap.get("ppq", {})
    ppq_available = ppq_snap.get("used_pct", 0.0) < 100.0
    zai_providers["ppq"] = {
        "base_rate_usd_per_m": ppq_base,
        "effective_price_usd_per_m": ppq_base,
        "available": ppq_available,
    }

    if available_zai:
        available_zai.sort(key=lambda x: x[1])
        zai_eff_price = available_zai[0][1]
        zai_available = True
        zai_locked_reason = None
    else:
        zai_eff_price = None
        zai_available = False
        reasons = []
        for key in ("ours", "friend"):
            p = zai_providers.get(f"zai_{key}", {})
            if p.get("locked"):
                reasons.append(f"{key}:locked({p.get('locked_window')})")
            elif not p.get("available"):
                reasons.append(f"{key}:unhealthy")
        zai_locked_reason = "; ".join(reasons) if reasons else "no keys available"

    return {
        "timestamp": int(time.time()),
        "source": "T470",
        "providers": zai_providers,
        "zai_effective_price_usd_per_m": round(zai_eff_price, 6) if zai_eff_price else None,
        "zai_available": zai_available,
        "zai_locked_reason": zai_locked_reason,
        "is_peak_hour": peak,
    }


def _nostr_publish_kalman():
    """Background thread: publish kalman pricing as kind-30315 every 30s."""
    import subprocess as _sp

    sec = _load_nostr_sec()
    if not sec:
        print("[nostr] No private key found at ~/.hermes/bot/kalman_npub.nsec — publisher disabled",
              flush=True)
        return

    nak_bin = None
    for candidate in [os.path.expanduser("~/.local/bin/nak"), "/usr/local/bin/nak", "nak"]:
        try:
            _r = _sp.run(["which", candidate], capture_output=True, text=True, timeout=3)
            if _r.returncode == 0 or os.path.exists(candidate):
                nak_bin = candidate if os.path.exists(candidate) else _r.stdout.strip()
                break
        except Exception:
            pass
    if not nak_bin:
        print("[nostr] nak CLI not found — publisher disabled", flush=True)
        return

    print(f"[nostr] Kalman publisher thread started — npub={_NOSTR_PUBLISHER_NPUB}", flush=True)

    while True:
        try:
            pricing = _build_kalman_pricing_json()
            content = json.dumps(pricing, separators=(",", ":"))

            # Build nak event command — publish to all relays in one call
            # NOTE: pass the secret key via NOSTR_SECRET_KEY env var, NOT --sec
            # CLI arg, so it doesn't appear in ps aux / /proc/*/cmdline.
            env = os.environ.copy()
            env["NOSTR_SECRET_KEY"] = sec
            cmd = [
                nak_bin, "event",
                "--kind", "30315",
                "--tag", "d=kalman-pricing",
                "--tag", "t=routstr",
                "--content", content,
            ] + _NOSTR_RELAYS

            result = _sp.run(cmd, capture_output=True, text=True, timeout=20, env=env)
            if result.returncode == 0:
                # Log a compact summary
                zai_avail = pricing.get("zai_available", False)
                zai_price = pricing.get("zai_effective_price_usd_per_m")
                print(f"[nostr] Published kind-30315 — zai_available={zai_avail} "
                      f"price={zai_price} ts={pricing.get('timestamp')}", flush=True)
            else:
                print(f"[nostr] nak event failed (rc={result.returncode}): "
                      f"{result.stderr[:200]}", flush=True)
        except _sp.TimeoutExpired:
            print("[nostr] nak event timed out — will retry next cycle", flush=True)
        except Exception as e:
            print(f"[nostr] publisher error: {e}", flush=True)

        time.sleep(_NOSTR_PUBLISH_INTERVAL)


if __name__ == "__main__":
    t = threading.Thread(target=_refresh_loop, daemon=True)
    t.start()
    # Nostr kind-30315 Kalman pricing publisher
    _nostr_thread = threading.Thread(target=_nostr_publish_kalman, daemon=True)
    _nostr_thread.start()
    time.sleep(3)  # let first quota fetch complete
    print(f"zai_proxy on :{PORT}  quotas={ {n: _max_pct(v[0]) for n, v in quota_cache.items()} }")
    # Emergency kill-switch startup notice (2026-09-04)
    if os.path.exists(os.path.expanduser("~/.hermes/bot/.emergency_ollama_only")):
        print("[emergency] .emergency_ollama_only ACTIVE — all requests routed "
              "directly to ollama_cloud chain, select_provider bypassed", flush=True)
    # Allow socket reuse to prevent "Address already in use" on restart
    from socketserver import TCPServer
    TCPServer.allow_reuse_address = True
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
