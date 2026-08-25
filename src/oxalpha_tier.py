"""oxalpha_tier.py — OX-2 proxy-side oxalpha tier logic (pure, testable).

Implements docs/PLAN-oxalpha-promo-2026-08-21.md §5 (OX-2) as refined by the
acceleration plan (docs/PLAN-oxalpha-acceleration-2026-08-22.md §4/§5) and
the EMERGENCY directive of 2026-08-22 (task t_2ed46556):

  EMERGENCY (live immediately): oxalpha participates in the proxy's external
  failover chain as a FREE candidate positioned AFTER the z.ai keys and
  BEFORE any paid provider (paid openrouter/deepinfra last). Guard-wrapped:
  PromoTierGuard.enabled + key present + not rate-limit-suppressed. Any
  error (429/timeout/5xx) falls through to the existing paid chain — zero
  regression. This stops the per-request paid-token bleed at z.ai-429 time.

  ALIAS (acceleration §4, armed-not-live): a pre-chain preferred attempt for
  rung-1 lanes (glm-4.5-flash / bulk_summarize digesters). Enabled ONLY via
  config (preferred_for.enabled) + all guards; ANY failure falls through to
  the untouched zai-first chain. glm-5.2 / glm-5.3 are HARD-excluded from
  the alias (§5.2) — config cannot override that.

Consumed by ~/.hermes/bot/zai_proxy.py as a thin shell: this module owns
every decision (eligibility, backoff, breaker, body mutation, kills); the
proxy only performs I/O (urlopen, streaming, anomaly-table inserts, the
5-minute /api/v1/key usage poll).

PURITY CONTRACT (mirrors OX-1 src/promo_tier.py): no network, no filesystem
reads at import, no .env access — the API key is HANDED IN by the caller so
tests never touch key material. Anomaly events are collected on the guard
and drained by the caller for insertion into the shared anomaly_events
table. The module is inert without a configured tier (absent/placeholder
key -> everything disabled, fail-closed, never loud).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from .promo_tier import (
    MODEL_NAME,
    PROVIDER_NAME,
    PromoTierGuard,
    rate_limit_backoff_s,
)

# ── Provider descriptor (EMERGENCY config contract) ─────────────────────────

PROVIDER_NAME = PROVIDER_NAME  # "oxalpha"
BASE_URL = "https://openrouter.ai/api/v1"
KEY_ENV_NAME = "OPENROUTER_OXALPHA_KEY"
MODEL_NAME = MODEL_NAME  # "stealth/ox-alpha"
REASONING_EFFORT = "low"
MAX_COMPLETION_TOKENS = 8192
UPSTREAM_TIMEOUT_S = 90.0
SINGLE_ATTEMPT = True  # never internally retried; failover falls through

# OpenRouter attribution headers (mirror the paid openrouter entry).
UPSTREAM_HEADERS = {"HTTP-Referer": "https://hermes.local",
                    "X-Title": "Hermes Agent (oxalpha promo)"}

# ── Alias (preferred_for) policy — acceleration plan §4/§5 ──────────────────

# glm-5.2/glm-5.3 are the premium coding/reasoning pair (§5.2): NEVER aliased
# to the promo tier during the promo, regardless of any config mistake.
HARD_EXCLUDED_MODELS = frozenset({"glm-5.2", "glm-5.3"})

# Gated task types are never aliased at rung 1 (§5.2): rung-1 lanes are the
# non-gated flash-lane digesters only.
GATED_TASK_TYPES = frozenset({"coding", "review", "research"})

# Rung-1 preferred_for defaults; live values come from providers.yaml.
PREFERRED_RUNG1_DEFAULT = {"enabled": False,           # OX-3b flips this
                           "models": ["glm-4.5-flash"],
                           "task_types": ["bulk_summarize"]}

# §4.2 kill-switch: present -> alias skipped (failover/opt-in unaffected).
ALIAS_KILLSWITCH_DEFAULT = Path.home() / ".hermes" / "bot" / ".oxalpha_alias_off"


# ── The tier ────────────────────────────────────────────────────────────────

class OxalphaTier:
    """Decision core for the oxalpha promo tier.

    Two eligibility surfaces:

      failover_eligible()  — the EMERGENCY generic-failover candidate.
          No task-type/model gating (it is a catch-all whose whole point is
          to sit in front of the PAID chain); guarded by PromoTierGuard
          (expiry/spend/402), key presence, and 429-backoff/breaker state.

      alias_eligible(...)  — the acceleration-plan pre-chain attempt.
          Narrowly gated: preferred_for.enabled + model/task_type lanes +
          OX-1 allowlist + no images + kill-switch absent + all of the above.

    State transitions (note_429 / note_failure / note_success) implement
    promo_tier §2.6: backoff 60→120→300 s on 429s, circuit breaker after 5
    consecutive failures for 300 s. 429s count toward the breaker. All
    methods are thread-safe (the proxy is a ThreadingHTTPServer).
    """

    def __init__(self, guard: PromoTierGuard, api_key: str = "",
                 *, failover_enabled: bool = True,
                 preferred: dict | None = None,
                 backoff_sequence: tuple = (60.0, 120.0, 300.0),
                 breaker_threshold: int = 5,
                 breaker_cooldown_s: float = 300.0,
                 killswitch_path: Path | None = None,
                 clock=time.monotonic):
        self.guard = guard
        self.api_key = api_key or ""
        self.failover_enabled = bool(failover_enabled)
        pref = dict(PREFERRED_RUNG1_DEFAULT)
        pref.update(preferred or {})
        pref["models"] = [str(m) for m in (pref.get("models") or [])]
        pref["task_types"] = [str(t) for t in (pref.get("task_types") or [])]
        self.preferred = pref
        self.backoff_sequence = tuple(backoff_sequence)
        self.breaker_threshold = int(breaker_threshold)
        self.breaker_cooldown_s = float(breaker_cooldown_s)
        self.killswitch_path = (Path(killswitch_path)
                                if killswitch_path is not None
                                else ALIAS_KILLSWITCH_DEFAULT)
        self._clock = clock
        self._lock = threading.Lock()
        # runtime suppression state (monotonic-clock domain)
        self._n429 = 0
        self._nfails = 0
        self._backoff_until = 0.0
        self._breaker_until = 0.0

    # ── configuration surface ──

    @property
    def configured(self) -> bool:
        """True only when a real (non-empty, non-placeholder) key is set."""
        key = (self.api_key or "").strip()
        return bool(key) and key not in {"...", "…", "REDACTED", "PLACEHOLDER"}

    def descriptor(self) -> dict:
        """Provider descriptor consumed by the proxy wiring + contract tests."""
        return {"name": PROVIDER_NAME,
                "base_url": BASE_URL,
                "model": MODEL_NAME,
                "key_env": KEY_ENV_NAME,
                "reasoning_effort": REASONING_EFFORT,
                "max_completion_tokens": MAX_COMPLETION_TOKENS,
                "upstream_timeout_s": UPSTREAM_TIMEOUT_S,
                "single_attempt": SINGLE_ATTEMPT}

    # ── guard plumbing ──

    def _guard_alive(self, now) -> bool:
        st = self.guard.status(now)  # performs the request-time expiry flip
        return bool(st["enabled"] and st["in_promo"])

    def _suppressed(self) -> bool:
        now = self._clock()
        return now < self._backoff_until or now < self._breaker_until

    def _killswitch_present(self, killswitch_exists) -> bool:
        if killswitch_exists is not None:
            return bool(killswitch_exists)
        try:
            return self.killswitch_path.exists()
        except OSError:
            return True  # unreadable -> treat as present (fail-closed)

    # ── EMERGENCY failover candidate ──

    def failover_eligible(self, now=None) -> bool:
        """May the generic external-failover chain use oxalpha right now?

        Catch-all by design (EMERGENCY directive): no model/task-type gate.
        Requires: key configured, tier enabled by config, guard alive, not
        rate-limit-suppressed / breaker-open.
        """
        if not (self.configured and self.failover_enabled):
            return False
        if not self._guard_alive(now):
            return False
        with self._lock:
            return not self._suppressed()

    # ── alias (preferred_for) eligibility ──

    def alias_eligible(self, model, task_type, has_images=False,
                       now=None, killswitch_exists=None):
        """May the pre-chain preferred attempt fire for this request?

        Returns (ok, reason); reason is always a non-empty string.
        ANY False here means the proxy must run the ordinary zai-first
        chain untouched (byte-identical) — fall-through, never an error.
        """
        if not self.configured:
            return False, "no_key"
        if self._killswitch_present(killswitch_exists):
            return False, "alias_killswitch"
        if not self.preferred.get("enabled"):
            return False, "alias_disabled"
        if not self._guard_alive(now):
            return False, "guard_disabled"
        with self._lock:
            if self._suppressed():
                return False, "suppressed_429_or_breaker"
        if model in HARD_EXCLUDED_MODELS:
            return False, f"model_{model}_never_aliased"
        if has_images:
            return False, "images_not_aliased"
        if model not in self.preferred["models"]:
            return False, "model_not_in_preferred_for"
        if task_type in GATED_TASK_TYPES:
            return False, "gated_task_type"
        if task_type not in self.preferred["task_types"]:
            return False, "task_type_not_in_preferred_for"
        if not self.guard.task_type_allowed(task_type):  # OX-1 helper, D2
            return False, "task_type_not_in_allowlist"
        return True, "ok"

    # ── §2.6 state transitions ──

    def note_429(self) -> float:
        """Register a 429. Applies 60/120/300 backoff, feeds the breaker.

        Returns the backoff seconds applied (caller logs it; this module
        never sleeps). 429 NEVER re-raised or retried by this tier — the
        proxy continues down the chain and the caller's terminal 503 path
        stays the only error surface.
        """
        with self._lock:
            self._n429 += 1
            delay = rate_limit_backoff_s(self._n429)
            self._backoff_until = self._clock() + delay
            self._nfails += 1
            if self._nfails >= self.breaker_threshold:
                self._breaker_until = self._clock() + self.breaker_cooldown_s
            return delay

    def note_failure(self) -> None:
        """Register a non-429 failure (timeout, 5xx, connection error)."""
        with self._lock:
            self._nfails += 1
            if self._nfails >= self.breaker_threshold:
                self._breaker_until = self._clock() + self.breaker_cooldown_s

    def note_success(self) -> None:
        """Register a successful completion — resets backoff + breaker."""
        with self._lock:
            self._n429 = 0
            self._nfails = 0
            self._backoff_until = 0.0
            self._breaker_until = 0.0

    # ── request mutation ──

    def build_request_body(self, body: dict) -> dict:
        """Return a NEW body targeted at the promo model.

        Forces stealth/ox-alpha + reasoning_effort=low, caps
        max_completion_tokens at 8192 (smaller caller asks are respected),
        drops the legacy max_tokens spelling. Everything else is copied
        verbatim; the input dict is NEVER mutated (the ordinary chain must
        see its original body — byte-identical guarantee).
        """
        out = dict(body)
        out["model"] = MODEL_NAME
        out["reasoning_effort"] = REASONING_EFFORT
        requested = out.get("max_completion_tokens") or out.get("max_tokens")
        try:
            cap = min(int(requested), MAX_COMPLETION_TOKENS) if requested \
                else MAX_COMPLETION_TOKENS
        except (TypeError, ValueError):
            cap = MAX_COMPLETION_TOKENS
        out.pop("max_tokens", None)
        out["max_completion_tokens"] = cap
        return out

    # ── observation / kill paths (delegate to the OX-1 guard) ──

    def observe_response_cost(self, cost_usd, now=None):
        """Mid-stream usage.cost guard. A response already streamed stays
        streamed — the kill suppresses every FUTURE attempt (no re-enable)."""
        return self.guard.observe_charge(cost_usd, now)

    def note_http_status(self, status_code, now=None):
        """Feed the upstream status; 402 -> disabled for promo remainder."""
        return self.guard.observe_http_status(status_code, now)

    def decide_usage_kill(self, prev_cumulative, new_cumulative, now=None):
        """§4.3 poller logic: kill on cumulative-usage INCREASE only.

        prev=None -> first sample after (re)start: baseline capture, no kill
        (a restart mid-promo must not false-positive on existing usage).
        Poll transport errors surface as new=None -> no-op (fail-open on the
        poll, never on the spend guards).
        """
        if new_cumulative is None:
            return None
        if prev_cumulative is None:
            return None
        try:
            delta = float(new_cumulative) - float(prev_cumulative)
        except (TypeError, ValueError):
            return None
        if delta > 0:
            return self.guard.observe_charge(delta, now)
        return None

    # ── anomaly-event drain (proxy inserts into anomaly_events) ──

    def drain_anomaly_events(self) -> list:
        """Hand collected guard events to the caller exactly once."""
        with self._lock:
            rows = list(self.guard.anomaly_events)
            self.guard.anomaly_events.clear()
            return rows

    # ── status ──

    def status(self, now=None) -> dict:
        st = dict(self.guard.status(now))
        with self._lock:
            st.update({
                "provider": PROVIDER_NAME,
                "key_configured": self.configured,
                "failover_enabled": self.failover_enabled,
                "failover_eligible": self.failover_eligible(now),
                "alias_enabled": bool(self.preferred.get("enabled")),
                "consecutive_429": self._n429,
                "consecutive_failures": self._nfails,
                "backoff_active": self._clock() < self._backoff_until,
                "breaker_open": self._clock() < self._breaker_until,
            })
        return st


# ── Construction from providers.yaml ────────────────────────────────────────

def load_tier_from_config(cfg: dict | None, strategy_cfg: dict | None,
                          api_key: str, *, clock=time.monotonic,
                          killswitch_path: Path | None = None) -> OxalphaTier:
    """Build a tier from the `oxalpha:` block (+ strategy) of providers.yaml.

    The key is passed in by the caller — this module never reads .env.
    Defaults encode the EMERGENCY directive: failover enabled even when the
    `failover:` sub-block is absent; alias (preferred_for) DISABLED until
    OX-3b's rung-1 flip.
    """
    cfg = cfg or {}
    strategy_cfg = strategy_cfg or {}
    guard = PromoTierGuard.from_config(cfg, strategy_cfg)
    failover_enabled = bool((cfg.get("failover") or {}).get("enabled", True))
    preferred = cfg.get("preferred_for") or {}
    return OxalphaTier(
        guard, api_key,
        failover_enabled=failover_enabled,
        preferred=preferred,
        killswitch_path=killswitch_path,
        clock=clock,
    )
