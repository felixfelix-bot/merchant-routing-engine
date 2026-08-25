# Free-Tier Guard: Implementation Specification

> **Date:** 2026-08-22  
> **Status:** Spec — ready for implementation  
> **Author:** Derived from analysis of `promo_tier.py`, `free-tier-pricing-analysis.md`, `free-tier-integration-analysis.md`, `pricing_engine.py`, `routing_optimizer.py`, and `zai_proxy.py`  
> **Decision context:** Both analysis docs recommend **Approach B (Pre-Proxy Filter)** over full Kalman integration. This spec generalizes the existing `PromoTierGuard` pattern into a multi-endpoint `FreeTierGuard`.

---

## 0. Executive Summary

The existing `PromoTierGuard` (`src/promo_tier.py`, 407 lines) handles exactly one free endpoint (`oxalpha`). This spec defines a **generalized `FreeTierGuard`** class that handles **N free endpoints** via config-driven registration, with the same binary-cliff + circuit-breaker + spend-guard pattern already validated for `oxalpha`.

**Key design decisions (from analysis docs):**
1. **Binary cliff, not Kalman pricing** — the 14.3× price spread makes the pressure curve inert (pitfall #24). The free tier stays cheaper than z.ai until 99.28% of its daily limit. Kalman integration saves ~$0.10/year vs binary cliff (§5 of pricing analysis).
2. **Pre-proxy filter, not in-Kalman** — the Kalman system optimizes for dynamic cost; free tiers have constant $0 cost. Integrating a constant into a smoothing filter is architecturally wrong (§recommendation of integration analysis).
3. **Config-driven** — adding a new free endpoint = adding a YAML entry, not writing code.
4. **Pure module** — no DB, no network, no filesystem, no clock reads at import (same purity contract as `promo_tier.py`).

**Estimated effort:** ~6-8 hours, ~350-450 LOC (guard module + config + proxy hook + tests).

---

## 1. Architecture

### 1.1 Position in the Request Flow

The guard sits **before** the Kalman routing pipeline, as a pre-proxy filter:

```
Request arrives at _proxy()
    │
    ├─ Step 0: Extract model, task_type, estimate tokens
    │
    ├─ Step 0b (NEW): FreeTierGuard.try_route()     ← THIS SPEC
    │   │   Checks: context cap, daily limit, model match,
    │   │           circuit breaker, spend guard
    │   │
    │   ├─ eligible + available → forward to free endpoint
    │   │       success → record_success(), return response
    │   │       failure (429/5xx/timeout) → record_failure(),
    │   │       fall through to normal routing
    │   │
    │   └─ not eligible/unavailable → None (fall through)
    │
    ├─ Step 1: Global spend cap check (existing)
    ├─ Step 1c: Ollama-only models (existing)
    ├─ Step 1d + Step 2: best_key() / RoutingAdvisor (existing)
    ├─ Step 3+: Model tier, retry loop, failover chain (existing)
    │
    └─ Response returned
```

**Integration point:** After model/task_type extraction (line ~4182), before the global spend cap check (line ~4194). This means:
- Free-tier requests bypass the spend cap (they cost $0 — the cap is for runaway paid spend).
- Free-tier requests bypass the Kalman routing pipeline entirely (no `best_key()`, no `RoutingAdvisor`).
- On failure, the request falls through to the **entire existing cascade** unchanged.

### 1.2 Module Layout

```
src/
  free_tier_guard.py     ← NEW: FreeTierGuard class + FreeTierEndpoint config
  promo_tier.py           ← EXISTING: kept for backward compat (oxalpha migrates to free_tier_guard)
config/
  providers.yaml          ← MODIFIED: add `free_tier:` section
tests/
  test_free_tier_guard.py ← NEW: TDD tests
```

### 1.3 Relationship to PromoTierGuard

`PromoTierGuard` is the existing single-endpoint guard for `oxalpha`. Two migration options:

| Option | Description | Effort |
|--------|-------------|--------|
| **A. Generalize in-place** | Extend `PromoTierGuard` to handle multiple endpoints | High refactor risk — 407 lines of carefully tested code |
| **B. New module, migrate later** | Create `FreeTierGuard` alongside; `oxalpha` config moves to the new section; `promo_tier.py` deprecated | Low risk — additive, no existing code touched |

**Recommendation: Option B.** Create `free_tier_guard.py` as a new module. The `oxalpha` entry migrates to the `free_tier:` config section. `promo_tier.py` remains importable for any existing callers but is deprecated.

---

## 2. Config Schema

### 2.1 New `free_tier:` Section in `providers.yaml`

```yaml
# ── Free-tier endpoints (generalized guard) ─────────────────────────────────
# Add a new free endpoint by adding an entry under `endpoints:` — no code changes.
# Each entry specifies everything the FreeTierGuard needs to decide eligibility
# and format the request. Delete an entry to remove that endpoint.
free_tier:
  enabled: true  # master kill switch — set false to bypass all free-tier routing
  
  endpoints:
    # ── oxalpha (migrated from the oxalpha: block) ──────────────────────────
    - name: "oxalpha"
      base_url: "https://openrouter.ai/api/v1"
      key_env: "OPENROUTER_OXALPHA_KEY"
      headers:
        HTTP-Referer: "https://hermes.local"
        X-Title: "Hermes Agent"
      
      # Model served by this free endpoint
      # provider_model is what gets sent in the request body's "model" field
      # substitutes_for is the paid model it replaces (for price matching/logging)
      provider_model: "stealth/ox-alpha"
      substitutes_for: "glm-5.2"      # paid model this free tier replaces
      
      # Context window cap (HARD constraint — request too large = skip)
      context_window_cap: 256000      # tokens
      
      # Daily request limit (binary cliff: drain until limit, then switch)
      daily_request_limit: 50         # requests per UTC day
      
      # Task-type allowlist (data sensitivity — only certain tasks may use free tier)
      allowed_task_types: [vision, bulk_summarize, shadow_eval]
      
      # Spend guard (anti-routstrd: ANY nonzero charge → disable)
      budget_usd: 0                   # 0 = any charge kills the endpoint
      
      # Promo-specific (optional — omit for permanent free tiers)
      promo:
        expires_at: "2026-08-28T00:00:00Z"
        post_promo_pessimistic_per_m: { input: 10.0, output: 30.0 }
      
      # Rate-limit backoff (429 handling)
      rate_limit_backoff_s: [60, 120, 300]
      
      # Circuit breaker
      circuit_breaker_threshold: 5
      circuit_breaker_cooldown_s: 300
    
    # ── Example: future OpenRouter free GLM-5.2 ─────────────────────────────
    # Uncomment when this endpoint becomes available:
    # - name: "openrouter_free_glm52"
    #   base_url: "https://openrouter.ai/api/v1"
    #   key_env: "OPENROUTER_API_KEY"
    #   headers:
    #     HTTP-Referer: "https://hermes.local"
    #     X-Title: "Hermes Agent"
    #   provider_model: "zai-org/GLM-5.2:free"
    #   substitutes_for: "glm-5.2"
    #   context_window_cap: 256000
    #   daily_request_limit: 50
    #   allowed_task_types: [chat, simple, bulk_summarize]
    #   budget_usd: 0
    #   rate_limit_backoff_s: [60, 120, 300]
    #   circuit_breaker_threshold: 5
    #   circuit_breaker_cooldown_s: 300
```

### 2.2 Config Field Reference

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | str | yes | — | Unique endpoint identifier (used in logs, state tracking) |
| `base_url` | str | yes | — | API base URL for the free endpoint |
| `key_env` | str | yes | — | Environment variable name holding the API key |
| `headers` | dict | no | `{}` | Extra HTTP headers to send with requests |
| `provider_model` | str | yes | — | Model name to put in request body's `model` field |
| `substitutes_for` | str | yes | — | Paid model name this free tier replaces (for model matching) |
| `context_window_cap` | int | yes | — | Max total tokens (input+output) the endpoint can handle |
| `daily_request_limit` | int | yes | — | Max requests per UTC day (binary cliff threshold) |
| `allowed_task_types` | list[str] | no | `[]` (all allowed) | Task types permitted to use this endpoint |
| `budget_usd` | float | no | `0.0` | Max spend before disable; 0 = any charge kills it |
| `promo.expires_at` | str | no | — | ISO-8601 deadline; after this, endpoint auto-disables |
| `promo.post_promo_pessimistic_per_m` | dict | no | `{input: 10, output: 30}` | Post-promo pessimistic pricing (priced OUT) |
| `rate_limit_backoff_s` | list[float] | no | `[60, 120, 300]` | 429 backoff sequence |
| `circuit_breaker_threshold` | int | no | `5` | Consecutive failures before breaker trips |
| `circuit_breaker_cooldown_s` | float | no | `300` | Breaker cooldown before retry |

---

## 3. FreeTierGuard Class Design

### 3.1 Module Structure

```python
# src/free_tier_guard.py
"""Generalized free-tier guard for N free endpoints.

Config-driven: add a new free endpoint by adding an entry to the `free_tier:`
section of config/providers.yaml. No code changes needed.

PURITY CONTRACT: no DB, no network, no filesystem, no clock reads at import.
All now-times are injected; anomaly events are RETURNED as dicts and collected
on the guard — the caller performs inserts (same pattern as promo_tier.py).

The module is inert without config: delete the `free_tier:` block and this
code never runs.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any
```

### 3.2 FreeTierEndpoint (Config Dataclass)

```python
@dataclass
class FreeTierEndpoint:
    """Configuration for one free-tier endpoint (from providers.yaml)."""
    
    name: str                           # "oxalpha", "openrouter_free_glm52"
    base_url: str                       # "https://openrouter.ai/api/v1"
    key_env: str                        # "OPENROUTER_OXALPHA_KEY"
    headers: dict = field(default_factory=dict)
    
    # Model mapping
    provider_model: str = ""            # "stealth/ox-alpha" (sent in request body)
    substitutes_for: str = ""           # "glm-5.2" (paid model it replaces)
    
    # Hard constraints
    context_window_cap: int = 256_000   # tokens — request too large = skip
    daily_request_limit: int = 50       # requests per UTC day
    
    # Eligibility filter
    allowed_task_types: frozenset = field(default_factory=frozenset)
    
    # Spend guard
    budget_usd: float = 0.0             # 0 = any nonzero charge → disable
    
    # Promo-specific (optional)
    promo_expires_at: datetime | None = None
    post_promo_per_m: dict = field(
        default_factory=lambda: {"input": 10.0, "output": 30.0})
    
    # Rate-limit / circuit breaker
    rate_limit_backoff_s: tuple = (60.0, 120.0, 300.0)
    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown_s: float = 300.0
    
    @classmethod
    def from_config(cls, raw: dict) -> "FreeTierEndpoint":
        """Build from one entry in providers.yaml → free_tier.endpoints[]."""
        promo = raw.get("promo") or {}
        expires = promo.get("expires_at")
        return cls(
            name=raw["name"],
            base_url=raw["base_url"],
            key_env=raw["key_env"],
            headers=dict(raw.get("headers") or {}),
            provider_model=raw["provider_model"],
            substitutes_for=raw["substitutes_for"],
            context_window_cap=int(raw["context_window_cap"]),
            daily_request_limit=int(raw["daily_request_limit"]),
            allowed_task_types=frozenset(raw.get("allowed_task_types") or []),
            budget_usd=float(raw.get("budget_usd", 0.0)),
            promo_expires_at=_parse_iso_utc(expires) if expires else None,
            post_promo_per_m=dict(
                promo.get("post_promo_pessimistic_per_m")
                or {"input": 10.0, "output": 30.0}),
            rate_limit_backoff_s=tuple(raw.get("rate_limit_backoff_s") or (60, 120, 300)),
            circuit_breaker_threshold=int(raw.get("circuit_breaker_threshold", 5)),
            circuit_breaker_cooldown_s=float(raw.get("circuit_breaker_cooldown_s", 300)),
        )
```

### 3.3 FreeTierEndpointState (Runtime State)

```python
@dataclass
class FreeTierEndpointState:
    """Mutable runtime state for one free-tier endpoint (NOT config).
    
    Tracks: daily request count, circuit breaker, spend kills, backoff.
    All state is in-memory. Thread safety: caller holds a lock around
    try_route() and record_result().
    """
    
    # Daily request counter (UTC midnight reset)
    request_count_today: int = 0
    request_count_date: str = ""        # UTC date string "YYYY-MM-DD"
    
    # Circuit breaker
    consecutive_failures: int = 0
    breaker_tripped: bool = False
    breaker_tripped_at: float | None = None  # unix timestamp
    
    # Backoff (429-specific)
    consecutive_429s: int = 0
    backoff_until: float = 0.0          # unix timestamp; 0 = no backoff
    
    # Spend guard kills (permanent for process lifetime)
    disabled_reason: str | None = None  # "nonzero_charge", "promo_expired", "http_402"
    _nonzero_kill_fired: bool = False
    _402_fired: bool = False
    
    # Audit trail (caller inserts into anomaly_events table)
    anomaly_events: list = field(default_factory=list)
    
    def _reset_daily_if_needed(self, now_dt: datetime) -> None:
        """Reset daily counter at UTC midnight."""
        today = now_dt.strftime("%Y-%m-%d")
        if self.request_count_date != today:
            self.request_count_today = 0
            self.request_count_date = today
    
    def daily_limit_remaining(self, config: FreeTierEndpoint,
                              now_dt: datetime) -> int:
        """How many requests remain in today's daily quota."""
        self._reset_daily_if_needed(now_dt)
        return max(0, config.daily_request_limit - self.request_count_today)
    
    def is_in_backoff(self, now_ts: float) -> bool:
        """True if currently in a 429 backoff window."""
        return now_ts < self.backoff_until
    
    def is_breaker_open(self, config: FreeTierEndpoint,
                        now_ts: float) -> bool:
        """Check circuit breaker; auto-reset after cooldown."""
        if not self.breaker_tripped:
            return False
        if self.breaker_tripped_at is None:
            return False
        if now_ts - self.breaker_tripped_at > config.circuit_breaker_cooldown_s:
            # Cooldown elapsed — reset breaker
            self.breaker_tripped = False
            self.consecutive_failures = 0
            return False
        return True
    
    def is_disabled(self) -> bool:
        """True if endpoint has been permanently killed (spend guard)."""
        return self.disabled_reason is not None
```

### 3.4 FreeTierGuard (Main Class)

```python
class FreeTierGuard:
    """Pre-proxy guard for multiple free-tier endpoints.
    
    Lifecycle:
        guard = FreeTierGuard.from_config(yaml_config)
        route = guard.try_route(model, task_type, estimated_tokens)
        if route:
            try:
                response = forward_to_free_endpoint(route, body)
                guard.record_result(route["name"], success=True, 
                                    cost_usd=response_cost)
            except Exception:
                guard.record_result(route["name"], success=False)
                # Fall through to normal Kalman routing
        else:
            # Fall through to normal Kalman routing
    
    Thread safety: callers should hold a lock around try_route() and 
    record_result(). The guard itself has no internal locking.
    """
    
    def __init__(self, endpoints: list[FreeTierEndpoint] | None = None,
                 enabled: bool = True) -> None:
        self._configs: list[FreeTierEndpoint] = endpoints or []
        self._states: dict[str, FreeTierEndpointState] = {
            e.name: FreeTierEndpointState() for e in self._configs
        }
        self._enabled = enabled
    
    # ── Construction from providers.yaml ──
    
    @classmethod
    def from_config(cls, cfg: dict | None) -> "FreeTierGuard":
        """Build from the `free_tier:` block of providers.yaml.
        
        Returns an empty (inert) guard if cfg is None or has no endpoints.
        """
        cfg = cfg or {}
        raw_endpoints = cfg.get("endpoints") or []
        endpoints = [FreeTierEndpoint.from_config(raw) for raw in raw_endpoints]
        return cls(endpoints=endpoints, enabled=cfg.get("enabled", True))
    
    # ── Core routing decision ──
    
    def try_route(
        self,
        model: str | None,
        task_type: str | None,
        estimated_tokens: int,
        now_dt: datetime | None = None,
    ) -> dict | None:
        """Try to route a request to a free-tier endpoint.
        
        Returns a routing dict if an endpoint can serve this request:
            {
                "name": str,              # endpoint name
                "base_url": str,          # API base URL
                "key_env": str,           # env var name for API key
                "headers": dict,          # HTTP headers
                "provider_model": str,    # model to put in request body
                "substitutes_for": str,    # paid model being replaced
            }
        
        Returns None if no free endpoint can serve this request (caller
        falls through to normal Kalman routing).
        
        Gate logic (checked in order, first match wins):
            1. Master enabled check
            2. Model match (substitutes_for == requested model)
            3. Task type allowlist
            4. Context window cap
            5. Daily request limit
            6. Spend guard (disabled check)
            7. Promo expiry check
            8. Circuit breaker
            9. 429 backoff
        """
        if not self._enabled or not self._configs:
            return None
        
        now_dt = _coerce_utc(now_dt) if now_dt is not None else _utcnow()
        now_ts = now_dt.timestamp()
        
        for config in self._configs:
            state = self._states[config.name]
            
            # 1. Spend guard — permanently disabled?
            if state.is_disabled():
                continue
            
            # 2. Promo expiry check
            if config.promo_expires_at is not None:
                if now_dt >= config.promo_expires_at:
                    state.disabled_reason = "promo_expired"
                    continue
            
            # 3. Model match — only substitute for the paid model this
            #    endpoint serves. GLM-5.2 free substitutes for GLM-5.2
            #    paid, NOT for GLM-5.3.
            if model and model != config.substitutes_for:
                continue
            
            # 4. Task type allowlist
            if config.allowed_task_types:
                if not (isinstance(task_type, str)
                        and task_type in config.allowed_task_types):
                    continue
            
            # 5. Context window cap (HARD constraint)
            if estimated_tokens > config.context_window_cap:
                continue
            
            # 6. Daily request limit
            if state.daily_limit_remaining(config, now_dt) <= 0:
                continue
            
            # 7. Circuit breaker
            if state.is_breaker_open(config, now_ts):
                continue
            
            # 8. 429 backoff
            if state.is_in_backoff(now_ts):
                continue
            
            # All gates passed — route to this endpoint
            state.request_count_today += 1
            return {
                "name": config.name,
                "base_url": config.base_url,
                "key_env": config.key_env,
                "headers": dict(config.headers),
                "provider_model": config.provider_model,
                "substitutes_for": config.substitutes_for,
            }
        
        return None  # No eligible endpoint — fall through
    
    # ── Result recording ──
    
    def record_result(
        self,
        endpoint_name: str,
        success: bool,
        now_dt: datetime | None = None,
        cost_usd: float | None = None,
        http_status: int | None = None,
    ) -> None:
        """Feed the result of a free-tier request back into the guard.
        
        Updates circuit breaker, backoff, and spend guard state.
        Generates anomaly events (collected on state; caller inserts).
        """
        now_dt = _coerce_utc(now_dt) if now_dt is not None else _utcnow()
        now_ts = now_dt.timestamp()
        
        state = self._states.get(endpoint_name)
        if state is None:
            return
        
        config = next((c for c in self._configs if c.name == endpoint_name), None)
        if config is None:
            return
        
        # ── Spend guard (check first — even on "success") ──
        if cost_usd is not None and float(cost_usd) > 0:
            self._kill_nonzero(state, config, float(cost_usd), now_dt)
            return
        
        # ── HTTP 402 → disable (promo terms changed) ──
        if http_status == 402:
            self._kill_402(state, config, now_dt)
            return
        
        if success:
            state.consecutive_failures = 0
            state.consecutive_429s = 0
            state.breaker_tripped = False
            state.backoff_until = 0.0
        else:
            state.consecutive_failures += 1
            
            # 429-specific backoff
            if http_status == 429:
                state.consecutive_429s += 1
                backoff_s = self._compute_backoff(
                    state.consecutive_429s, config.rate_limit_backoff_s)
                state.backoff_until = now_ts + backoff_s
            else:
                # Non-429 failure (timeout, 5xx) — reset 429 counter
                state.consecutive_429s = 0
            
            # Circuit breaker
            if state.consecutive_failures >= config.circuit_breaker_threshold:
                state.breaker_tripped = True
                state.breaker_tripped_at = now_ts
    
    # ── Spend guard kills ──
    
    def _kill_nonzero(self, state: FreeTierEndpointState,
                      config: FreeTierEndpoint, cost: float,
                      now_dt: datetime) -> None:
        """Disable endpoint on any nonzero charge (anti-routstrd guard).
        Fires exactly once per endpoint per process lifetime."""
        if state._nonzero_kill_fired:
            return
        state._nonzero_kill_fired = True
        state.disabled_reason = "nonzero_charge"
        event = {
            "ts": now_dt.timestamp(),
            "severity": "critical",
            "category": "free_tier_spend",
            "title": f"{config.name} free-tier endpoint charged — auto-disabled",
            "detail": json.dumps({
                "detail": (
                    f"budget is ${config.budget_usd:.2f}; observed nonzero "
                    f"spend on a $0 free tier. Endpoint disabled; re-enable "
                    f"requires human action (config + restart)."),
                "endpoint": config.name,
                "reason": "nonzero_charge",
                "cost_usd": cost,
                "budget_usd": config.budget_usd,
            }),
        }
        state.anomaly_events.append(event)
    
    def _kill_402(self, state: FreeTierEndpointState,
                  config: FreeTierEndpoint, now_dt: datetime) -> None:
        """Disable endpoint on HTTP 402 (free model demanding credits)."""
        if state._402_fired:
            state.disabled_reason = "http_402"
            return
        state._402_fired = True
        state.disabled_reason = "http_402"
        event = {
            "ts": now_dt.timestamp(),
            "severity": "warning",
            "category": "free_tier",
            "title": f"{config.name}: HTTP 402 — disabled",
            "detail": json.dumps({
                "detail": "Free model demanding credits = terms changed.",
                "endpoint": config.name,
                "reason": "http_402",
                "status_code": 402,
            }),
        }
        state.anomaly_events.append(event)
    
    @staticmethod
    def _compute_backoff(consecutive_429s: int,
                         backoff_sequence: tuple) -> float:
        """Backoff seconds after N consecutive 429s.
        
        Uses the endpoint's configured sequence (default 60→120→300).
        Caps at the last value in the sequence.
        """
        n = int(consecutive_429s)
        if n <= 0 or not backoff_sequence:
            return 0.0
        return float(backoff_sequence[min(n - 1, len(backoff_sequence) - 1)])
    
    # ── Observability ──
    
    def status(self, now_dt: datetime | None = None) -> dict:
        """Full status report for all endpoints (observability/logging)."""
        now_dt = _coerce_utc(now_dt) if now_dt is not None else _utcnow()
        now_ts = now_dt.timestamp()
        return {
            "enabled": self._enabled,
            "endpoint_count": len(self._configs),
            "endpoints": [
                {
                    "name": c.name,
                    "provider_model": c.provider_model,
                    "substitutes_for": c.substitutes_for,
                    "disabled": s.is_disabled(),
                    "disable_reason": s.disabled_reason,
                    "daily_remaining": s.daily_limit_remaining(c, now_dt),
                    "breaker_open": s.is_breaker_open(c, now_ts),
                    "in_backoff": s.is_in_backoff(now_ts),
                    "consecutive_failures": s.consecutive_failures,
                    "consecutive_429s": s.consecutive_429s,
                    "anomaly_events": len(s.anomaly_events),
                }
                for c, s in (
                    (c, self._states[c.name])
                    for c in self._configs
                )
            ],
        }
    
    def drain_anomaly_events(self) -> list[dict]:
        """Pop all accumulated anomaly events (caller inserts into DB).
        
        Returns a flat list across all endpoints. After this call, each
        endpoint's anomaly_events list is empty.
        """
        events = []
        for state in self._states.values():
            events.extend(state.anomaly_events)
            state.anomaly_events.clear()
        return events
    
    # ── Singleton access (for proxy integration) ──
    
    _instance: "FreeTierGuard | None" = None
    
    @classmethod
    def get_instance(cls) -> "FreeTierGuard":
        """Singleton accessor. Returns an inert guard if not initialized."""
        if cls._instance is None:
            cls._instance = cls()  # inert — no endpoints
        return cls._instance
    
    @classmethod
    def initialize(cls, cfg: dict | None) -> "FreeTierGuard":
        """Initialize the singleton from config (called at proxy startup)."""
        cls._instance = cls.from_config(cfg)
        return cls._instance
```

### 3.5 Helper Functions

```python
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _coerce_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _parse_iso_utc(value: str) -> datetime:
    """Parse ISO-8601 string into UTC datetime (accepts trailing Z)."""
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return _coerce_utc(dt)
```

---

## 4. Gate Logic (Detailed)

### 4.1 Gate Pipeline (checked in order)

Each gate is a **filter** — if it fails, the endpoint is skipped (try the next one). If all endpoints fail all gates, `try_route()` returns `None` and the request falls through to normal Kalman routing.

| # | Gate | Check | Skip condition | Rationale |
|---|------|-------|----------------|-----------|
| 1 | Master enabled | `self._enabled` | Guard disabled | Kill switch |
| 2 | Spend guard | `state.is_disabled()` | Endpoint killed by nonzero charge / 402 / promo expired | Anti-routstrd (§2.4 of promo_tier.py) |
| 3 | Promo expiry | `now >= promo_expires_at` | Past hard deadline | Time-limited promos |
| 4 | Model match | `model == substitutes_for` | Wrong model family | GLM-5.2 free ≠ GLM-5.3 paid |
| 5 | Task type | `task_type in allowed_task_types` | Disallowed task type | Data sensitivity (§2.5) |
| 6 | Context cap | `estimated_tokens <= context_window_cap` | Request too large | HARD capacity constraint (§3 of pricing analysis) |
| 7 | Daily limit | `daily_remaining > 0` | Daily quota exhausted | Binary cliff (§5 of pricing analysis) |
| 8 | Circuit breaker | `not is_breaker_open()` | Breaker tripped | 5 consecutive failures → 300s cooldown |
| 9 | 429 backoff | `not is_in_backoff()` | In 429 backoff window | 60→120→300s sequence |

### 4.2 Model Match Logic

The `substitutes_for` field is the **paid model name** that this free endpoint replaces. The check is exact match:

```python
# GLM-5.2 free substitutes for GLM-5.2 paid — eligible
model = "glm-5.2"  # requested by manager
config.substitutes_for = "glm-5.2"  # free endpoint serves this
# → MATCH

# GLM-5.2 free does NOT substitute for GLM-5.3 — skip
model = "glm-5.3"  # requested by manager
config.substitutes_for = "glm-5.2"  # free endpoint serves GLM-5.2, not 5.3
# → SKIP
```

**Future extension:** For endpoints that serve multiple models, `substitutes_for` can be a list. The check becomes `model in config.substitutes_for`. This is noted in §7 but not implemented in v1.

### 4.3 Context Window Estimation

The proxy already has `_extract_model(body)` (line 1741 of `zai_proxy.py`) but does **not** have a token estimator. The guard needs an `estimated_tokens` value. Two options:

| Option | Description | Accuracy | Effort |
|--------|-------------|----------|--------|
| **A. Body size heuristic** | `estimated_tokens = len(body) // 4` (rough: 4 bytes/token) | ±30% | 1 line |
| **B. Tokenizer** | Use `tiktoken` or the request's `max_tokens` field | ±5% | ~20 lines + dependency |

**Recommendation: Option A** for v1. The context cap is a hard gate (256k tokens), and a ±30% estimate is sufficient to prevent oversized requests. The `max_tokens` field in the request body can be added as a lower bound:

```python
def _estimate_tokens(body: bytes) -> int:
    """Rough token estimate for free-tier eligibility check."""
    try:
        obj = json.loads(body)
        max_tokens = int(obj.get("max_tokens", 0))
        # Estimate input tokens from body size (4 bytes/token heuristic)
        # plus the requested max output tokens.
        input_est = len(body) // 4
        return input_est + max_tokens
    except Exception:
        return len(body) // 4
```

### 4.4 Daily Request Counter

The counter uses a **UTC date string** for reset detection:

```python
# State tracks:
request_count_today: int       # requests sent today
request_count_date: str        # "2026-08-22" (UTC date)

# On each try_route() call:
today = now_dt.strftime("%Y-%m-%d")
if state.request_count_date != today:
    state.request_count_today = 0
    state.request_count_date = today
```

This is a **sliding reset** (not a sliding window) — the counter resets at UTC midnight, not 24 hours after the first request. This matches how free-tier rate limits typically work (OpenRouter resets at midnight UTC).

### 4.5 Circuit Breaker

Reuses the pattern from `promo_tier.py` (§2.6) and `providers.yaml` strategy defaults:

- **Threshold:** 5 consecutive failures → breaker trips
- **Cooldown:** 300 seconds → breaker auto-resets
- **Failure types:** 429, 5xx, timeout, connection error (any non-success)
- **Success resets:** `consecutive_failures = 0` on any successful response

The breaker is **per-endpoint**, not global. One endpoint being down doesn't affect others.

### 4.6 429 Backoff

Separate from the circuit breaker. Uses the endpoint's configured backoff sequence (default `[60, 120, 300]` seconds):

```python
# 1st 429: backoff 60s
# 2nd 429: backoff 120s
# 3rd+ 429: backoff 300s (cap)

# Non-429 failures (timeout, 5xx) reset the 429 counter:
state.consecutive_429s = 0
```

### 4.7 Fallback

On **any** failure (429, timeout, 5xx, connection error), the request falls through to the **entire existing cascade**:

```
FreeTierGuard.try_route() → None or route
    │
    route returned → forward to free endpoint
        success → return response
        failure → record_result(), fall through
    │
    None returned → fall through to:
        ├─ Global spend cap check
        ├─ best_key() / RoutingAdvisor
        ├─ Ollama Cloud / external failover chain
        └─ 503 error
```

The fallthrough is **automatic** — the proxy's `_proxy()` method continues with the next step if the free-tier attempt returns `None` or fails.

---

## 5. Integration into zai_proxy.py

### 5.1 Initialization (at module load / startup)

Add near the top of `zai_proxy.py`, alongside the existing shadow mode / LiveRouter imports (around line 34-270):

```python
# ── Free-Tier Guard (pre-proxy filter) ───────────────────────────────────────
# Sits BEFORE the Kalman routing pipeline. If a request is eligible for a
# free-tier endpoint and the endpoint is available, it short-circuits to the
# free endpoint. On failure, falls through to normal routing.
_free_tier_guard = None
try:
    _MRE_PATH = os.path.expanduser("~/merchant-routing-engine")
    if _MRE_PATH not in sys.path:
        sys.path.insert(0, _MRE_PATH)
    from src.free_tier_guard import FreeTierGuard as _FreeTierGuardCls
    
    # Load config from providers.yaml
    import yaml as _yaml
    _providers_yaml_path = os.path.join(_MRE_PATH, "config", "providers.yaml")
    with open(_providers_yaml_path) as _f:
        _providers_cfg = _yaml.safe_load(_f)
    
    _free_tier_guard = _FreeTierGuardCls.initialize(
        _providers_cfg.get("free_tier"))
    if _free_tier_guard and _free_tier_guard._configs:
        print(f"[free-tier] {len(_free_tier_guard._configs)} endpoint(s) loaded",
              flush=True)
except Exception as _e:
    print(f"[free-tier] init failed (free tiers disabled): {_e}", flush=True)
    _free_tier_guard = _FreeTierGuardCls.get_instance()  # inert singleton
```

### 5.2 Hook in `_proxy()` Method

Insert **after** model/task_type extraction (line ~4182) and **before** the global spend cap check (line ~4194):

```python
# ── Step 0b: Free-Tier Guard (pre-proxy filter) ──────────────────────────
# Try to route to a free-tier endpoint BEFORE the spend cap check (free
# requests cost $0) and BEFORE the Kalman routing pipeline.
if _free_tier_guard and _free_tier_guard._enabled:
    _ft_est_tokens = _estimate_tokens(body)
    _ft_route = _free_tier_guard.try_route(
        model=original_model,
        task_type=self._task_type,
        estimated_tokens=_ft_est_tokens,
    )
    if _ft_route:
        # Forward to the free-tier endpoint
        _ft_response_buffer = bytearray()
        _ft_ok, _ft_cost, _ft_http_status = self._try_free_tier(
            body, _ft_route, _ft_response_buffer, t0)
        _free_tier_guard.record_result(
            _ft_route["name"],
            success=_ft_ok,
            cost_usd=_ft_cost,
            http_status=_ft_http_status,
        )
        # Drain anomaly events (insert into anomaly_events table)
        for _ev in _free_tier_guard.drain_anomaly_events():
            try:
                _log_anomaly(_ev)
            except Exception:
                pass
        if _ft_ok:
            return  # response already sent
        # Free tier failed — fall through to normal routing

# Step 1b: Global spend cap (existing, unchanged)
allowed, current_spend, cap = _check_global_spend_cap()
# ... rest of _proxy() unchanged
```

### 5.3 `_try_free_tier()` Method

Add a new method on the request handler class (alongside `_try_ollama_cloud`, `_try_external_failover`, `_try_telnyx`):

```python
def _try_free_tier(self, body: bytes, route: dict,
                   response_buffer: bytearray, t0: float) -> tuple[bool, float | None, int | None]:
    """Forward a request to a free-tier endpoint.
    
    Returns (success, cost_usd, http_status).
    On success, the response is already written to the client.
    On failure, the caller falls through to normal routing.
    """
    import urllib.request
    
    # Get API key from env
    api_key = os.environ.get(route["key_env"], "")
    if not api_key:
        return (False, None, None)
    
    # Rewrite model in request body
    try:
        body_json = json.loads(body)
        body_json["model"] = route["provider_model"]
        body = json.dumps(body_json).encode()
    except Exception:
        return (False, None, None)
    
    url = route["base_url"] + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    headers.update(route["headers"])
    
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            response_data = resp.read()
            status_code = resp.getcode()
            
            # Extract cost from response (OpenRouter returns usage.cost)
            cost = None
            try:
                resp_json = json.loads(response_data)
                cost = resp_json.get("usage", {}).get("cost")
            except Exception:
                pass
            
            # Stream response back to client
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_data)))
            self.end_headers()
            self.wfile.write(response_data)
            
            # Log the API call
            _log_api_call(
                key_name=route["name"],
                model=route["provider_model"],
                tokens_in=0, tokens_out=0,  # extracted from response if available
                cost_usd=cost or 0.0,
                latency_ms=int((time.time() - t0) * 1000),
                task_type=self._task_type,
                session_id=self._session_id,
            )
            
            return (True, cost, status_code)
    
    except urllib.error.HTTPError as e:
        return (False, None, e.code)
    except Exception:
        return (False, None, None)
```

### 5.4 Token Estimation Helper

Add near `_extract_model` (line ~1741):

```python
def _estimate_tokens(body: bytes) -> int:
    """Rough token estimate for free-tier eligibility check.
    
    Uses body size heuristic (4 bytes/token) + max_tokens from request.
    This is intentionally imprecise — the context cap is a hard gate
    (256k tokens), and ±30% is sufficient to prevent oversized requests.
    """
    try:
        obj = json.loads(body)
        max_tokens = int(obj.get("max_tokens", 0))
        input_est = len(body) // 4
        return input_est + max_tokens
    except Exception:
        return len(body) // 4
```

### 5.5 Observability

The guard provides two observability hooks:

1. **Anomaly events** — drained after each free-tier attempt and inserted into the `anomaly_events` table (same shape as `promo_tier.py`):
   ```python
   for ev in _free_tier_guard.drain_anomaly_events():
       _log_anomaly(ev)  # or direct INSERT
   ```

2. **Status report** — available at any time for monitoring endpoints:
   ```python
   _free_tier_guard.status()  # returns dict with all endpoint states
   ```

3. **API call logging** — each free-tier request is logged via `_log_api_call()` with `key_name=route["name"]` and `cost_usd=0.0` (or actual cost if the response includes it). This integrates with the existing cost tracking infrastructure.

---

## 6. Future Extensibility

### 6.1 Adding a New Free Endpoint

**Zero code changes.** Add a YAML entry:

```yaml
free_tier:
  endpoints:
    - name: "new_free_endpoint"
      base_url: "https://new-provider.com/v1"
      key_env: "NEW_PROVIDER_KEY"
      provider_model: "some-model:free"
      substitutes_for: "glm-4.5-flash"
      context_window_cap: 128000
      daily_request_limit: 100
      allowed_task_types: [chat, simple]
      budget_usd: 0
```

Restart the proxy. The endpoint is automatically registered and eligible for routing.

### 6.2 Different Providers

The guard is provider-agnostic. Any OpenAI-compatible endpoint works:
- OpenRouter free tiers (`:free` suffix models)
- Direct provider free tiers (e.g., Google AI Studio free tier)
- Self-hosted free endpoints

The only requirement: the endpoint must accept OpenAI-format `/chat/completions` requests with a `model` field.

### 6.3 Different Rate Limit Types

Currently supports: **daily request limit** (UTC midnight reset).

Future extensions (config-driven, no code changes needed for the guard itself):

| Rate Limit Type | Config Field | Implementation |
|----------------|-------------|----------------|
| Daily (current) | `daily_request_limit` | UTC date string reset |
| Hourly | `hourly_request_limit` | Hour string reset |
| Per-minute (RPM) | `rpm_limit` | Sliding window (60s) |
| Token-based | `daily_token_limit` | Sum response tokens |

**v1 implementation:** Daily request limit only (matches OpenRouter free tiers). Other types can be added by extending `FreeTierEndpointState` with the appropriate counter.

### 6.4 Model Families

The `substitutes_for` field supports model family matching. Future extension: make it a list:

```yaml
substitutes_for: ["glm-5.2", "glm-4.5-air"]  # serves both model families
```

Code change: `model in config.substitutes_for` instead of `model == config.substitutes_for`. ~3 LOC.

### 6.5 Migration from PromoTierGuard

Once `FreeTierGuard` is deployed and tested:
1. Move the `oxalpha:` config block into `free_tier.endpoints[]`
2. Remove `promo_tier.py` (or keep as deprecated import)
3. Remove the `oxalpha:` block from `providers.yaml`

The `is_promo_row()`, `filter_promo_rows()`, and `promo_exclusion_sql()` functions from `promo_tier.py` should be migrated to a standalone module (or kept in `promo_tier.py` as a utility module) since they serve the p20 filter — a separate concern from the guard.

### 6.6 Multiple Free Endpoints with Same Model

If two endpoints both serve `glm-5.2:free` (e.g., OpenRouter free + another provider's free tier), the guard iterates endpoints in config order and picks the first eligible one. Future extension: add a priority field or cost-based selection among free endpoints (but this is YAGNI until >5 free endpoints exist, per the integration analysis).

---

## 7. TDD Test Plan

### 7.1 Test File Structure

```
tests/test_free_tier_guard.py
```

Tests are written **first** (TDD). Each test is a pure unit test — no DB, no network, no filesystem.

### 7.2 Test Cases

#### Test Group 1: Config Parsing

```python
class TestFreeTierEndpointFromConfig:
    """Tests for FreeTierEndpoint.from_config()"""
    
    def test_minimal_config(self):
        """Parse a minimal config entry with required fields only."""
        
    def test_full_config(self):
        """Parse a config entry with all fields including promo."""
        
    def test_missing_required_field_raises(self):
        """Missing name/base_url/key_env/provider_model raises KeyError."""
        
    def test_default_values(self):
        """Unspecified fields get correct defaults."""
        
    def test_promo_expiry_parsing(self):
        """promo.expires_at is parsed into UTC datetime."""
        
    def test_promo_expiry_with_z_suffix(self):
        """ISO-8601 with trailing Z is accepted."""
        
    def test_allowed_task_types_as_frozenset(self):
        """allowed_task_types is converted to frozenset."""
```

#### Test Group 2: Guard Construction

```python
class TestFreeTierGuardConstruction:
    """Tests for FreeTierGuard.from_config()"""
    
    def test_empty_config_returns_inert_guard(self):
        """None/empty config → guard with no endpoints."""
        
    def test_multiple_endpoints(self):
        """Guard with 3 endpoints has 3 states."""
        
    def test_enabled_flag(self):
        """enabled: false → guard is disabled."""
        
    def test_singleton_get_instance(self):
        """get_instance() returns same object."""
        
    def test_singleton_initialize(self):
        """initialize() replaces singleton."""
```

#### Test Group 3: Model Match Gate

```python
class TestModelMatchGate:
    """Tests for the model substitution check."""
    
    def test_exact_model_match(self):
        """model='glm-5.2' matches substitutes_for='glm-5.2' → eligible."""
        
    def test_wrong_model(self):
        """model='glm-5.3' does NOT match substitutes_for='glm-5.2' → skip."""
        
    def test_none_model_skips(self):
        """model=None → skip (can't match without a model)."""
        
    def test_empty_model_skips(self):
        """model='' → skip."""
        
    def test_case_sensitivity(self):
        """Model match is case-sensitive (GLM-5.2 ≠ glm-5.2)."""
```

#### Test Group 4: Context Window Cap

```python
class TestContextWindowCap:
    """Tests for the context window capacity gate."""
    
    def test_under_cap(self):
        """estimated_tokens < cap → eligible."""
        
    def test_at_cap(self):
        """estimated_tokens == cap → eligible (boundary)."""
        
    def test_over_cap(self):
        """estimated_tokens > cap → skip."""
        
    def test_huge_request(self):
        """estimated_tokens = 10M with cap=256k → skip."""
```

#### Test Group 5: Daily Request Limit

```python
class TestDailyRequestLimit:
    """Tests for the daily request counter."""
    
    def test_under_limit(self):
        """5 requests used, limit 50 → eligible."""
        
    def test_at_limit(self):
        """50 requests used, limit 50 → skip."""
        
    def test_over_limit(self):
        """51 requests used, limit 50 → skip."""
        
    def test_midnight_reset(self):
        """Counter resets at UTC midnight."""
        
    def test_counter_increments_on_route(self):
        """try_route() increments request_count_today."""
        
    def test_failed_request_still_counts(self):
        """Request count is incremented even if the request later fails."""
```

#### Test Group 6: Circuit Breaker

```python
class TestCircuitBreaker:
    """Tests for the circuit breaker."""
    
    def test_under_threshold(self):
        """4 failures, threshold 5 → still eligible."""
        
    def test_at_threshold(self):
        """5 failures, threshold 5 → breaker trips → skip."""
        
    def test_cooldown_reset(self):
        """Breaker resets after cooldown_s seconds."""
        
    def test_success_resets_failures(self):
        """A successful response resets consecutive_failures to 0."""
        
    def test_breaker_is_per_endpoint(self):
        """One endpoint's breaker doesn't affect another."""
```

#### Test Group 7: 429 Backoff

```python
class TestBackoff:
    """Tests for the 429 backoff sequence."""
    
    def test_first_429_backoff(self):
        """1st 429 → backoff 60s."""
        
    def test_second_429_backoff(self):
        """2nd 429 → backoff 120s."""
        
    def test_third_plus_429_backoff(self):
        """3rd+ 429 → backoff 300s (cap)."""
        
    def test_non_429_resets_429_counter(self):
        """A timeout (not 429) resets consecutive_429s."""
        
    def test_backoff_expires(self):
        """After backoff_s, endpoint is eligible again."""
```

#### Test Group 8: Spend Guard

```python
class TestSpendGuard:
    """Tests for the anti-routstrd spend guard."""
    
    def test_zero_cost_no_op(self):
        """cost_usd=0 → no disable."""
        
    def test_nonzero_cost_disables(self):
        """cost_usd=0.01 → endpoint disabled."""
        
    def test_kill_fires_once(self):
        """Second nonzero charge doesn't create a second anomaly event."""
        
    def test_disabled_endpoint_skipped(self):
        """Disabled endpoint is skipped in try_route()."""
        
    def test_http_402_disables(self):
        """HTTP 402 → endpoint disabled."""
        
    def test_anomaly_event_shape(self):
        """Anomaly event has correct fields (ts/severity/category/title/detail)."""
```

#### Test Group 9: Promo Expiry

```python
class TestPromoExpiry:
    """Tests for promo expiry gate."""
    
    def test_before_expiry_eligible(self):
        """now < expires_at → eligible."""
        
    def test_after_expiry_disabled(self):
        """now >= expires_at → endpoint disabled."""
        
    def test_no_expiry_permanent(self):
        """No promo.expires_at → endpoint never expires (permanent free tier)."""
```

#### Test Group 10: Multiple Endpoints

```python
class TestMultipleEndpoints:
    """Tests with multiple free-tier endpoints."""
    
    def test_first_eligible_chosen(self):
        """When 2 endpoints are eligible, the first in config order wins."""
        
    def test_first_skipped_second_chosen(self):
        """When first endpoint is exhausted, second is tried."""
        
    def test_all_exhausted_returns_none(self):
        """When all endpoints are exhausted, try_route() returns None."""
        
    def test_different_models_different_endpoints(self):
        """Endpoint A serves glm-5.2, endpoint B serves glm-4.5-flash."""
        
    def test_model_match_routes_to_correct_endpoint(self):
        """Request for glm-5.2 routes to endpoint A, not B."""
```

#### Test Group 11: Fallback

```python
class TestFallback:
    """Tests for the fallback behavior."""
    
    def test_no_endpoints_returns_none(self):
        """Guard with no endpoints → try_route() returns None."""
        
    def test_disabled_guard_returns_none(self):
        """enabled=false → try_route() returns None."""
        
    def test_all_gates_fail_returns_none(self):
        """When all gates fail for all endpoints → None."""
        
    def test_record_result_on_failure(self):
        """record_result(success=False) updates breaker/backoff state."""
        
    def test_record_result_on_success(self):
        """record_result(success=True) resets breaker/backoff."""
```

#### Test Group 12: Observability

```python
class TestObservability:
    """Tests for status() and drain_anomaly_events()."""
    
    def test_status_returns_all_endpoints(self):
        """status() includes all configured endpoints."""
        
    def test_drain_events_empties_list(self):
        """drain_anomaly_events() clears the events list."""
        
    def test_status_shows_disabled(self):
        """status() shows disable_reason for killed endpoints."""
        
    def test_status_shows_daily_remaining(self):
        """status() shows correct daily_limit_remaining."""
```

### 7.3 Test Execution

```bash
# Run all free-tier guard tests
python3 -m pytest tests/test_free_tier_guard.py -v

# Run specific test group
python3 -m pytest tests/test_free_tier_guard.py::TestCircuitBreaker -v

# Run all tests (including existing promo_tier tests)
python3 -m pytest tests/ -v
```

---

## 8. Implementation Estimate

### 8.1 Effort Breakdown

| Task | Files | LOC | Hours |
|------|-------|-----|-------|
| `FreeTierEndpoint` dataclass + `from_config()` | `src/free_tier_guard.py` | ~60 | 0.5 |
| `FreeTierEndpointState` dataclass | `src/free_tier_guard.py` | ~50 | 0.5 |
| `FreeTierGuard` class (try_route, record_result, kills) | `src/free_tier_guard.py` | ~150 | 2.0 |
| Helper functions (_utcnow, _coerce_utc, _parse_iso_utc) | `src/free_tier_guard.py` | ~15 | 0.25 |
| Config schema in `providers.yaml` | `config/providers.yaml` | ~30 | 0.25 |
| Singleton + initialization in `zai_proxy.py` | `~/.hermes/bot/zai_proxy.py` | ~25 | 0.5 |
| `_proxy()` hook (Step 0b) | `~/.hermes/bot/zai_proxy.py` | ~20 | 0.5 |
| `_try_free_tier()` method | `~/.hermes/bot/zai_proxy.py` | ~60 | 1.0 |
| `_estimate_tokens()` helper | `~/.hermes/bot/zai_proxy.py` | ~10 | 0.1 |
| Anomaly event draining + logging | `~/.hermes/bot/zai_proxy.py` | ~10 | 0.25 |
| TDD tests (12 test groups, ~40 test cases) | `tests/test_free_tier_guard.py` | ~300 | 2.0 |
| **Total** | **4 files** | **~730** | **~8.0** |

### 8.2 Implementation Order (TDD)

1. **Write tests first** — all 12 test groups (~2h)
2. **Implement `FreeTierEndpoint` + `from_config()`** — make config tests pass (~0.5h)
3. **Implement `FreeTierEndpointState`** — make state tests pass (~0.5h)
4. **Implement `FreeTierGuard.try_route()`** — make gate tests pass (~1.5h)
5. **Implement `FreeTierGuard.record_result()` + kills** — make breaker/spend tests pass (~0.5h)
6. **Implement `status()` + `drain_anomaly_events()`** — make observability tests pass (~0.25h)
7. **Add config to `providers.yaml`** (~0.25h)
8. **Wire into `zai_proxy.py`** — init + hook + `_try_free_tier()` + `_estimate_tokens()` (~2h)
9. **End-to-end test** — start proxy, send request, verify free-tier routing + fallback (~0.5h)

### 8.3 Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Free-tier response format differs from z.ai | Medium | `_try_free_tier()` handles OpenAI-format responses; add per-endpoint response parsing if needed |
| Token estimate too imprecise | Low | ±30% is fine for a 256k hard gate; upgrade to tokenizer if needed |
| Concurrent request burst exceeds daily limit | Medium | Counter increments before forwarding; over-count by 1-2 in burst is acceptable (binary cliff) |
| Spend guard misses a charge (no usage.cost in response) | Medium | Wallet delta collector (existing in zai_proxy) provides independent detection |
| Migration from promo_tier.py breaks existing callers | Low | promo_tier.py is NOT wired into zai_proxy.py (confirmed: 0 search matches for "promo" or "oxalpha") |

---

## 9. Key Design Decisions (Summary)

| Decision | Choice | Rationale | Source |
|----------|--------|-----------|--------|
| Kalman vs binary cliff | Binary cliff | 14.3× price spread makes pressure curve inert (pitfall #24) | `free-tier-pricing-analysis.md` §5 |
| In-Kalman vs pre-proxy | Pre-proxy filter | Kalman optimizes for dynamic cost; free tiers have constant $0 cost | `free-tier-integration-analysis.md` §recommendation |
| Single vs multi-endpoint | Multi (config-driven) | Generalize from oxalpha to N endpoints; future-proof | This spec |
| Daily counter reset | UTC midnight | Matches OpenRouter free-tier reset behavior | Convention |
| Token estimation | Body size / 4 | ±30% sufficient for 256k hard gate; no dependency | This spec |
| Circuit breaker threshold | 5 failures / 300s cooldown | Matches existing `promo_tier.py` and `providers.yaml` strategy defaults | `promo_tier.py` §2.6 |
| 429 backoff | 60→120→300s | Matches `promo_tier.py` defaults; free-tier 429s mean "try in a minute" | `promo_tier.py` §2.6 |
| Spend guard | budget_usd=0 (any charge kills) | Anti-routstrd: any nonzero charge on a $0 tier is an anomaly | `promo_tier.py` §2.4 |
| Integration point | After model extraction, before spend cap | Free requests are $0 — bypass spend cap; bypass Kalman entirely | This spec |
| Migration strategy | New module, deprecate promo_tier.py | Additive — no existing tested code touched | This spec |

---

## Appendix A: References to Actual Code

| Reference | File | Line(s) | What it proves |
|-----------|------|---------|----------------|
| `PromoTierGuard` pattern | `src/promo_tier.py` | 182-407 | The existing single-endpoint guard to generalize |
| `from_config()` pattern | `src/promo_tier.py` | 222-239 | Config-driven construction from YAML |
| Spend guard kill | `src/promo_tier.py` | 323-348 | Anti-routstrd: any charge → disable + anomaly |
| 429 backoff | `src/promo_tier.py` | 125-134 | Backoff sequence helper |
| Circuit breaker constants | `src/promo_tier.py` | 81-83 | Threshold=5, cooldown=300s |
| `providers.yaml` oxalpha block | `config/providers.yaml` | 117-135 | Config format to generalize |
| `quota_pressure_factor` signature | `src/pricing_engine.py` | 516-596 | Shows the pressure curve is dimensionless (tokens OR requests) |
| `_evaluate_provider` pipeline | `src/routing_optimizer.py` | 267-362 | 5-stage filter pipeline (tier→health→exhaustion→scarcity→price) |
| `free-tier-pricing-analysis.md` | `docs/` | §5 | Binary cliff is optimal for 14.3× price spread |
| `free-tier-integration-analysis.md` | `docs/` | §recommendation | Pre-proxy filter (Approach B) over Kalman integration (Approach A) |
| `zai_proxy._proxy()` | `~/.hermes/bot/zai_proxy.py` | 4154+ | Request flow — insertion point for Step 0b |
| `_extract_model()` | `~/.hermes/bot/zai_proxy.py` | 1741-1751 | Model extraction from request body |
| `_resolve_task_type()` | `~/.hermes/bot/zai_proxy.py` | 1774+ | Task type resolution (X-Task-Type header) |
| `EXTERNAL_PROVIDERS` | `~/.hermes/bot/zai_proxy.py` | 561-590 | External provider registry (pattern for free-tier provider registry) |
| `_try_external_failover()` | `~/.hermes/bot/zai_proxy.py` | 3935+ | External request forwarding pattern (model for `_try_free_tier()`) |
| `best_key()` | `~/.hermes/bot/zai_proxy.py` | 3385-3532 | The Kalman routing entry point (bypassed by free-tier guard) |
| Promo not wired | `~/.hermes/bot/zai_proxy.py` | (search) | 0 matches for "promo"/"oxalpha" — confirms promo_tier.py is repo-side only |

---

## Appendix B: Sequence Diagram

```
Client → zai_proxy._proxy()
         │
         ├─ Extract model (line 4169)
         ├─ Extract task_type (line 4182)
         │
         ├─ [NEW] FreeTierGuard.try_route(model, task_type, est_tokens)
         │   │
         │   ├─ For each endpoint in config order:
         │   │   ├─ Check disabled? → skip
         │   │   ├─ Check promo expired? → skip
         │   │   ├─ Check model match? → skip if wrong model
         │   │   ├─ Check task type allowed? → skip if not allowed
         │   │   ├─ Check context cap? → skip if too large
         │   │   ├─ Check daily limit? → skip if exhausted
         │   │   ├─ Check circuit breaker? → skip if open
         │   │   ├─ Check 429 backoff? → skip if in backoff
         │   │   └─ All pass → return route dict
         │   │
         │   └─ No endpoint eligible → return None
         │
         ├─ route != None?
         │   ├─ YES → _try_free_tier(body, route)
         │   │   ├─ Get API key from env
         │   │   ├─ Rewrite model in body
         │   │   ├<arg_value> POST to free endpoint
         │   │   ├─ Success → write response to client, return
         │   │   └─ Failure → record_result(success=False), fall through
         │   │
         │   └─ NO (or free tier failed) → fall through
         │
         ├─ [EXISTING] Global spend cap check
         ├─ [EXISTING] Ollama-only model routing
         ├─ [EXISTING] best_key() / RoutingAdvisor
         ├─ [EXISTING] Model tier selection
         ├─ [EXISTING] Retry loop (z.ai keys)
         ├─ [EXISTING] LiveRouter failover
         ├─ [EXISTING] Ollama Cloud / external failover chain
         └─ [EXISTING] 503 if all exhausted
```
