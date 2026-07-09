"""backoff.py — Binary exponential backoff for rate limit handling.

Between key switches: short jittered delay (1-2s) prevents hammering.
Full cycle (all keys tried): exponential backoff with Kalman override.

Extracted from ~/.hermes/bot/zai_proxy.py (Phase 1 — standalone copy).
"""
from __future__ import annotations
import random
import time


def key_switch_delay() -> float:
    """Brief delay between key switches (1-2s jitter)."""
    delay = 1.0 + random.random()
    time.sleep(delay)
    return delay


def full_cycle_backoff(retry_num: int, kalman_wait: float | None = None) -> float:
    """Binary exponential backoff after all keys exhausted.

    Args:
        retry_num: Which retry cycle (0=first, 1=second, ...)
        kalman_wait: If Kalman predictor is available, use its prediction instead.

    Returns:
        Actual wait time in seconds.
    """
    if kalman_wait is not None:
        wait = kalman_wait
    else:
        # Binary exponential: 2s, 4s, 8s, 16s, 32s, 60s cap
        wait = min(2 ** (retry_num + 1), 60)

    # Add 25-75% jitter to prevent thundering herd
    wait *= (0.75 + random.random() * 0.5)
    time.sleep(wait)
    return wait


def attempt_retry(error, attempt: int, name: str, t0: float, key_order: list,
                  rate_limit_predictor=None) -> bool:
    """Decide whether to retry and apply appropriate backoff.

    Returns True if the caller should continue (try next key or retry),
    False if the safety cap is exhausted.

    Args:
        error: The exception/error that triggered the retry
        attempt: Current attempt index in the key_order
        name: Name of the key that was tried
        t0: Start time of the request (for logging)
        key_order: List of key names in order of preference
        rate_limit_predictor: Optional Kalman-based predictor for smart backoff

    Returns:
        True = retry (try next key or same key after backoff)
        False = safety cap exhausted, give up
    """
    if attempt >= len(key_order) - 1:
        # All keys exhausted — full backoff cycle
        retry_num = attempt - len(key_order) + 1
        if retry_num >= 50:
            return False  # Safety cap

        kalman_wait = None
        if rate_limit_predictor is not None:
            rate_limit_predictor.record_429()
            kalman_wait = rate_limit_predictor.predict_retry_at()

        full_cycle_backoff(retry_num, kalman_wait)
        return True
    else:
        # Between key switches
        key_switch_delay()
        return True
