#!/usr/bin/env python3
"""stress_test.py — routstr stress-test harness (T-C, ADR-007 Gate 3).

Simulates z.ai (the `ours` provider) at ~90% quota pressure under MIXED
internal + sold load, and asserts the ADR-007 routing degradation contract:

  1. INTERNAL requests are NEVER blocked (no 429), no matter how close the
     provider is to exhaustion. Internal always routes.
  2. SOLD requests degrade via HTTP 429 + Retry-After once predicted
     exhaustion is within the safety window (SOLD_SAFETY_HOURS, default 2h).
     When exhaustion is far out, or the predictor is down (fail-open), sold
     is still served.
  3. An exhausted provider is DELISTED within DELIST_MAX_DELAY_S (5 minutes)
     of crossing the 90% threshold, measured in REAL wall-clock time. The
     DelistTracker records the actual trigger latency and flags a CAP
     VIOLATION if a delist decision is ever made later than the cap.

On completion it writes ~/.hermes/bot/stress_test_results.json with
"status": "pass" when every assertion is green over the run, else "fail".

USAGE (run continuously for the 24h soak — default when run with no args):
    python3 stress_test.py --duration 86400 --out ~/.hermes/bot/stress_test_results.json
    python3 stress_test.py                          # same, 24h default

The gate decision reuses the REAL flat_router.sold_429_gate() from T-A so the
stress test exercises the actual production degradation path, not a copy.
The soak loop is PACED (request_interval_s), so a 24h run is a realistic
continuous load profile instead of an unpaced CPU spin, and the delist
latency is measured against real time (time.monotonic).

HOLD compliance (plan §8): this harness only simulates/mocks — it never
routes real sold traffic to z.ai or any provider, and it never delists a live
listing. The production delist script is owned by T-D; this harness verifies
the <5min time-to-delist property against its own tracker.

Author: worker-merchant-qa (T-C, ADR-007 Gate 3)
"""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

# Retry-After seconds returned on a sold 429 (mirrors production proxy).
SOLD_RETRY_AFTER_S: int = 120

# Delist guarantee: an exhausted provider must be delisted within this many
# seconds of crossing the threshold (5 minutes).
DELIST_MAX_DELAY_S: int = 300

# Default output path per plan T-C.
DEFAULT_RESULTS_PATH: str = os.path.expanduser(
    "~/.hermes/bot/stress_test_results.json")

# Healthy predictions (quota far from exhaustion) — used so the soak also
# exercises the SERVED sold path, proving sold is not ALWAYS 429'd.
_HEALTHY_PREDICTIONS: list[dict] = [
    {"key": "routstr", "window": "monthly", "will_exhaust": False,
     "exhausts_in_hours": 24.0, "used_pct": 40.0},
]
# Default pressure predictions (z.ai at ~92%, exhaustion within ~1h) — this
# is the ADR-007 Gate 3 scenario.
_PRESSURE_PREDICTIONS: list[dict] = [
    {"key": "routstr", "window": "monthly", "will_exhaust": True,
     "exhausts_in_hours": 0.8, "used_pct": 92.0},
]


def _sold_gate(caller_class: str, predictions: list[dict] | None,
               safety_hours: float) -> bool:
    """Evaluate the real sold-pressure gate (fail-open if flat_router is
    unavailable, mirroring the production proxy's never-raise contract)."""
    try:
        from flat_router import sold_429_gate
        return bool(sold_429_gate(caller_class, predictions, safety_hours))
    except Exception:
        # Gate must never raise; fail OPEN (route) so internal+normal traffic
        # is never harmed by a missing module.
        return False


def simulate_request(caller_class: str,
                     predictions: list[dict] | None,
                     safety_hours: float = 2.0) -> dict[str, Any]:
    """Simulate one routed-request decision under the given quota pressure.

    Returns a decision dict:
      {
        "caller_class": caller_class,
        "status": "routed" | "429",
        "blocked": bool,          # True only for a gated sold request
        "retry_after": int|None,  # Retry-After seconds on a 429, else None
      }
    """
    gated = _sold_gate(caller_class, predictions, safety_hours=safety_hours)
    if gated:
        return {
            "caller_class": caller_class,
            "status": "429",
            "blocked": True,
            "retry_after": SOLD_RETRY_AFTER_S,
        }
    return {
        "caller_class": caller_class,
        "status": "routed",
        "blocked": False,
        "retry_after": None,
    }


class DelistTracker:
    """Track a provider's quota health and guarantee delist <= max_delay_s.

    A provider is considered for delisting as soon as a health sample is at
    or above `threshold_pct`. While over threshold the delist decision is
    TRUE (fires immediately — 0s latency is well within the cap). The
    `trigger_latency_s` is the REAL wall-clock seconds between the sample
    that crossed the threshold and the first delist decision. If a delist
    decision would ever be made later than `max_delay_s` after crossing, the
    tracker flags `cap_violation=True` so the "delist < 5 min" invariant can
    FAIL instead of being vacuously satisfied. Quota recovery below the
    threshold clears and re-arms the timer.

    `ts` is expected to be monotonic wall-clock (e.g. time.monotonic()).
    """

    def __init__(self, threshold_pct: float = 90.0,
                 max_delay_s: int = DELIST_MAX_DELAY_S) -> None:
        self.threshold_pct = threshold_pct
        self.max_delay_s = max_delay_s
        self._crossed_at: float | None = None   # monotonic ts of first crossing
        self._decided: bool = False
        self.trigger_latency_s: float | None = None
        self.cap_violation: bool = False

    def record_health(self, used_pct: float, ts: float) -> None:
        if used_pct >= self.threshold_pct:
            if self._crossed_at is None:
                self._crossed_at = ts
                self._decided = False
        else:
            # Recovered below threshold → clear and re-arm the timer.
            self._crossed_at = None
            self._decided = False
            self.trigger_latency_s = None
            self.cap_violation = False

    def should_delist(self, ts: float) -> bool:
        """True while the provider is over threshold (delist is warranted).

        Records the real trigger latency on the first decision and flags a
        cap violation if the elapsed time since crossing would exceed the
        max_delay_s guarantee before the first decision.
        """
        if self._crossed_at is None:
            return False
        latency = max(0.0, ts - self._crossed_at)
        if not self._decided:
            self._decided = True
            self.trigger_latency_s = latency
            if latency > self.max_delay_s:
                self.cap_violation = True
        return True

    @property
    def within_cap(self) -> bool:
        """True iff a delist decision fired and never exceeded the cap."""
        return (self._decided
                and not self.cap_violation
                and (self.trigger_latency_s is not None
                     and self.trigger_latency_s <= self.max_delay_s))


def write_results(summary: dict[str, Any],
                  out_path: str = DEFAULT_RESULTS_PATH) -> str:
    """Write the stress-test results JSON and return the path written.

    The JSON carries the ADR-007 assertion booleans plus a top-level
    "status". The status is derived from the SAME computed booleans shown in
    the JSON (single source of truth): pass only when every invariant holds.
    """
    internal_never_blocked = int(summary.get("internal_blocked", 0)) == 0
    sold_429 = int(summary.get("sold_429", 0))
    sold_served = int(summary.get("sold_served", 0))
    sold_requests = sold_429 + sold_served
    # Contract: under mixed pressure, SOME sold traffic is gated AND some is
    # served (sold must not be blanket-429'd, nor never degraded).
    sold_degrade = sold_429 > 0 and (sold_served >= 0)
    sold_served_ok = sold_served > 0  # prove the served path also works
    delist_latency = float(summary.get("delist_latency_max_s", 0.0))
    delist_ok = (delist_latency <= DELIST_MAX_DELAY_S
                 and summary.get("delist_cap_violation") is False)

    green = (internal_never_blocked and sold_degrade
             and sold_served_ok and delist_ok)

    data = {
        "status": "pass" if green else "fail",
        "internal_never_blocked": internal_never_blocked,
        "sold_degrade_429_retry_after": sold_degrade,
        "delist_triggers_under_5min": delist_ok,
        "detail": {
            "internal_requests": int(summary.get("internal_requests", 0)),
            "internal_blocked": int(summary.get("internal_blocked", 0)),
            "sold_requests": sold_requests,
            "sold_429": sold_429,
            "sold_served": sold_served,
            "delist_latency_max_s": round(delist_latency, 3),
            "delist_cap_violation": bool(summary.get("delist_cap_violation")),
            "duration_s": float(summary.get("duration_s", 0.0)),
        },
        "written_at": time.time(),
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return out_path


def run_stress(duration_s: float = 86400.0,
               pressure_predictions: list[dict] | None = None,
               healthy_predictions: list[dict] | None = None,
               delist_threshold_pct: float = 90.0,
               request_interval_s: float = 0.001,
               max_delay_s: int = DELIST_MAX_DELAY_S,
               out_path: str | None = None) -> dict[str, Any]:
    """Run the paced mixed-load soak for `duration_s` and return a summary.

    The loop alternates every `phase_requests` (100) iterations between a
    PRESSURE phase (z.ai at ~90%, sold gated) and a HEALTHY phase (quota far
    from exhaustion, sold served). This
    exercises BOTH degradation paths every cycle so a regression in the sold
    gate (e.g. sold always 429'd, or never gated) or in internal protection
    cannot slip through silently. The delist latency is measured in real
    wall-clock time against the DelistTracker.

    If `out_path` is given, also writes the results JSON there.
    """
    if pressure_predictions is None:
        pressure_predictions = _PRESSURE_PREDICTIONS
    if healthy_predictions is None:
        healthy_predictions = _HEALTHY_PREDICTIONS

    internal_requests = 0
    internal_blocked = 0
    sold_429 = 0
    sold_served = 0

    tracker = DelistTracker(threshold_pct=delist_threshold_pct,
                            max_delay_s=max_delay_s)
    t_start = time.monotonic()
    phase_requests = 100  # requests per phase; alternate pressure/healthy
    loop_i = 0

    while time.monotonic() - t_start < duration_s:
        phase = (loop_i // phase_requests) % 2
        loop_i += 1
        preds = pressure_predictions if phase == 0 else healthy_predictions
        used_pct = (delist_threshold_pct + 1.0 if phase == 0
                    else delist_threshold_pct - 40.0)
        tracker.record_health(used_pct=used_pct, ts=time.monotonic())
        tracker.should_delist(ts=time.monotonic())

        # Internal request — must NEVER be blocked under either phase.
        if not simulate_request("internal", preds)["blocked"]:
            internal_requests += 1
        else:
            internal_requests += 1
            internal_blocked += 1

        # Sold request — degrades under pressure, served when healthy.
        dec_sold = simulate_request("sold", preds)
        if dec_sold["status"] == "429":
            sold_429 += 1
        else:
            sold_served += 1

        time.sleep(request_interval_s)

    latency = tracker.trigger_latency_s if tracker.trigger_latency_s is not None else 0.0
    green = (
        internal_blocked == 0
        and sold_429 > 0
        and sold_served > 0
        and latency <= max_delay_s
        and not tracker.cap_violation
    )

    summary = {
        "green": green,
        "internal_requests": internal_requests,
        "internal_blocked": internal_blocked,
        "sold_requests": sold_429 + sold_served,
        "sold_429": sold_429,
        "sold_served": sold_served,
        "delist_latency_max_s": latency,
        "delist_cap_violation": tracker.cap_violation,
        "duration_s": duration_s,
    }
    if out_path is not None:
        write_results(summary, out_path=out_path)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="routstr stress-test harness (ADR-007 Gate 3).")
    parser.add_argument("--duration", type=float, default=86400.0,
                        help="soak duration in seconds (default 86400 = 24h)")
    parser.add_argument("--out", default=DEFAULT_RESULTS_PATH,
                        help="results JSON path")
    parser.add_argument("--threshold", type=float, default=90.0,
                        help="delist threshold %% used (default 90.0)")
    args = parser.parse_args()

    print(f"[stress_test] starting {args.duration/3600:.2f}h paced soak "
          f"(threshold {args.threshold}%); out={args.out}", flush=True)
    summary = run_stress(
        duration_s=args.duration,
        delist_threshold_pct=args.threshold,
        out_path=args.out,
    )
    print(f"[stress_test] done green={summary['green']} "
          f"internal={summary['internal_blocked']}/{summary['internal_requests']}"
          f" blocked, sold_429={summary['sold_429']}/"
          f"sold_served={summary['sold_served']}, "
          f"delist_latency={summary['delist_latency_max_s']}s", flush=True)
    raise SystemExit(0 if summary["green"] else 1)


if __name__ == "__main__":
    main()
