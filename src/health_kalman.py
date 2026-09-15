#!/usr/bin/env python3
"""health_kalman.py — per-(endpoint, LLM) predictive health for routing.

ADR-015 / ADR-016 (merchant-routing-engine). Replaces the boolean + streak
health model (ADR-003 / ADR-008 moved health *out* of the filter; that was the
regression behind the recurring "all providers exhausted").

Model
-----
A 1-D Kalman filter over the latent **error probability** ``x`` of a lane
(endpoint, optionally model). Each observation is a Bernoulli outcome:

    * failure (429/5xx/timeout/…)  → z = 1  (bump)
    * success (probe OK)           → z = 0  (decay)

    K  = p / (p + R) * GAIN_SCALE     # GAIN_SCALE models probe unreliability
    x  = clamp(x + K * (z - x), 0, 1)
    p  = (1 - K) * p + Q

Tuning is deliberate: a *single* failure raises ``x`` (and therefore the price)
but does **not** exclude the lane; sustained failures cross ``EXCLUDE_ERR`` and
the lane is priced to ``+inf`` (unreachable). Successes decay ``x`` back down.
This is exactly "bump on error, gradually lower as probes succeed".

The filter drives BOTH:
  * the routing **gate** (``is_unhealthy``), and
  * the routing **price** (``health_multiplier``), per ADR-016.

State is persisted (default ``~/.hermes/bot/health_kalman.json``; override with
``HEALTH_KALMAN_STATE``) with atomic writes. The module never raises on I/O.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# ── Filter parameters ────────────────────────────────────────────────────────
Q = 0.02           # process noise: how fast a true error rate can drift
R = 0.15           # measurement noise of a single Bernoulli observation
GAIN_SCALE = 0.45  # probe outcomes are noisy → damped gain (gradual response)
PRIOR_X = 0.05     # prior error probability for a fresh lane (optimistic)
PRIOR_P = 0.30     # prior variance (uncertain about a fresh lane)

# ── Gate / price thresholds ──────────────────────────────────────────────────
EXCLUDE_ERR = 0.55   # error probability at/above which a lane is unreachable
MIN_SAMPLES = 3      # observations required before the gate may exclude

# ── Price curve (bounded; mirrors the RP-EXP asymptote used elsewhere) ───────
ERR_ONSET = 0.15     # below this error prob, no price penalty (mult = 1.0)
HK_ASYMPTOTE = 4.0   # price multiplier asymptote as error prob → 1

_DEFAULT_STATE = Path.home() / ".hermes" / "bot" / "health_kalman.json"


def _state_path() -> Path:
    override = os.environ.get("HEALTH_KALMAN_STATE")
    return Path(override) if override else _DEFAULT_STATE


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


class HealthKalman:
    """1-D Kalman health filter, keyed by endpoint (optionally endpoint/model)."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        q: float = Q,
        r: float = R,
        gain_scale: float = GAIN_SCALE,
        prior_x: float = PRIOR_X,
        prior_p: float = PRIOR_P,
        exclude_err: float = EXCLUDE_ERR,
        min_samples: int = MIN_SAMPLES,
        onsets: float = ERR_ONSET,
        asymptote: float = HK_ASYMPTOTE,
    ) -> None:
        self._path = Path(path) if path else _state_path()
        self._q = q
        self._r = r
        self._gain = gain_scale
        self._prior_x = prior_x
        self._prior_p = prior_p
        self._exclude = exclude_err
        self._min_samples = min_samples
        self._onset = onsets
        self._asymptote = asymptote
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, Any]] | None = None

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> dict[str, dict[str, Any]]:
        if self._state is not None:
            return self._state
        state: dict[str, dict[str, Any]] = {}
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text())
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(v, dict) and "x" in v:
                            state[k] = v
        except Exception:
            state = {}
        self._state = state
        return state

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps(self._state or {}, indent=1, sort_keys=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(data)
                os.replace(tmp, self._path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except Exception:
            pass

    @staticmethod
    def _key(provider: str, model: str | None = None) -> str:
        return f"{provider}/{model}" if model else provider

    # ── filter ───────────────────────────────────────────────────────────
    def observe(
        self, provider: str, ok: bool, *,
        model: str | None = None, ts: float | None = None,
        error: str = "", http: int | None = None,
    ) -> float:
        """Update the filter for *provider* with one Bernoulli outcome."""
        if not provider:
            return self._prior_x
        key = self._key(provider, model)
        with self._lock:
            state = self._load()
            rec = state.get(key)
            if not rec:
                rec = {"x": self._prior_x, "p": self._prior_p, "n": 0}
            x = float(rec.get("x", self._prior_x))
            p = float(rec.get("p", self._prior_p))
            z = 0.0 if ok else 1.0
            gain = (p / (p + self._r)) * self._gain
            x = _clamp(x + gain * (z - x), 0.0, 1.0)
            p = (1.0 - gain) * p + self._q
            rec.update({
                "x": round(x, 6), "p": round(p, 6),
                "n": int(rec.get("n", 0)) + 1,
                "last_ts": float(ts if ts is not None else time.time()),
                "last_ok": bool(ok),
                "model": model or rec.get("model") or "",
            })
            if error:
                rec["last_error"] = str(error)[:120]
            if http is not None:
                rec["last_http"] = int(http)
            state[key] = rec
            self._save()
            return x

    def ingest_probe(self, rows: dict, now: float | None = None) -> int:
        """Fold a ``provider_probe.json`` snapshot into the filter.

        Each provider row is observed **once per new probe tick** (deduped by the
        row's ``ts``), so re-reading the same snapshot never double-counts. Only
        rows carrying a boolean-ish ``ok`` are used.
        """
        if not isinstance(rows, dict):
            return 0
        n = 0
        with self._lock:
            state = self._load()
            for name, row in rows.items():
                if name.startswith("_") or not isinstance(row, dict):
                    continue
                if "ok" not in row:
                    continue
                try:
                    ts = float(row.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                model = row.get("model") or None
                key = self._key(name, model)
                prev = float((state.get(key) or {}).get("last_ts") or 0)
                if ts and ts <= prev:
                    continue  # already ingested this probe tick
                ok = bool(row.get("ok"))
                # inline update (we already hold the lock)
                rec = state.get(key) or {"x": self._prior_x, "p": self._prior_p, "n": 0}
                x = float(rec.get("x", self._prior_x))
                p = float(rec.get("p", self._prior_p))
                z = 0.0 if ok else 1.0
                gain = (p / (p + self._r)) * self._gain
                x = _clamp(x + gain * (z - x), 0.0, 1.0)
                p = (1.0 - gain) * p + self._q
                rec.update({
                    "x": round(x, 6), "p": round(p, 6),
                    "n": int(rec.get("n", 0)) + 1,
                    "last_ts": ts or time.time(),
                    "last_ok": ok,
                    "model": model or rec.get("model") or "",
                })
                if row.get("error"):
                    rec["last_error"] = str(row.get("error"))[:120]
                if row.get("http") is not None:
                    rec["last_http"] = row.get("http")
                state[key] = rec
                n += 1
            if n:
                self._save()
        return n

    # ── queries ──────────────────────────────────────────────────────────
    def _rec(self, provider: str, model: str | None = None) -> dict | None:
        state = self._load()
        key = self._key(provider, model)
        if key in state:
            return state[key]
        if model is None:
            # no model given → if exactly one model row exists, use it
            pref = f"{provider}/"
            matches = [v for k, v in state.items() if k.startswith(pref)]
            if len(matches) == 1:
                return matches[0]
        return None

    def error_prob(self, provider: str, model: str | None = None) -> float | None:
        rec = self._rec(provider, model)
        return None if rec is None else float(rec.get("x", self._prior_x))

    def health(self, provider: str, model: str | None = None) -> float | None:
        x = self.error_prob(provider, model)
        return None if x is None else 1.0 - x

    def samples(self, provider: str, model: str | None = None) -> int:
        rec = self._rec(provider, model)
        return 0 if rec is None else int(rec.get("n", 0))

    def is_unhealthy(self, provider: str, model: str | None = None) -> bool:
        """True when the filter has enough evidence that the lane is failing."""
        rec = self._rec(provider, model)
        if rec is None:
            return False
        if int(rec.get("n", 0)) < self._min_samples:
            return False
        return float(rec.get("x", 0.0)) >= self._exclude

    def health_multiplier(self, provider: str, model: str | None = None) -> float:
        """Bounded routing-price multiplier from health (ADR-016).

        1.0 at/below ``ERR_ONSET``; rises on the RP-EXP curve to ``HK_ASYMPTOTE``
        as the error probability approaches 1; ``+inf`` once ``is_unhealthy``.
        """
        if self.is_unhealthy(provider, model):
            return math.inf
        x = self.error_prob(provider, model)
        if x is None or x <= self._onset:
            return 1.0
        t = (x - self._onset) / (1.0 - self._onset)
        t = _clamp(t, 0.0, 0.999999)
        return 1.0 + (self._asymptote - 1.0) * t / (1.0 - t)

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._load()))


# ── module singleton + convenience helpers ───────────────────────────────────
_SINGLETON: HealthKalman | None = None
_SINGLETON_LOCK = threading.Lock()


def get_health_kalman() -> HealthKalman:
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = HealthKalman()
    return _SINGLETON


def health_pricing_multiplier(provider: str, model: str | None = None) -> float:
    try:
        return get_health_kalman().health_multiplier(provider, model)
    except Exception:
        return 1.0


def is_unhealthy(provider: str, model: str | None = None) -> bool:
    try:
        return get_health_kalman().is_unhealthy(provider, model)
    except Exception:
        return False


if __name__ == "__main__":  # pragma: no cover — tiny debug surface
    import sys
    hk = get_health_kalman()
    if len(sys.argv) > 1 and sys.argv[1] == "snapshot":
        print(json.dumps(hk.snapshot(), indent=2, sort_keys=True))
    else:
        print(__doc__)
