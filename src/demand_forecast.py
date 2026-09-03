#!/usr/bin/env python3
"""
demand_forecast_shadow.py — Workstream C Phase 0: two-component demand forecaster.

Shadow logger + walk-forward backtest. Nothing reads its output yet; it only
writes to the `demand_forecast_shadow` table in zai_usage.db.

Model (per handover §3.2):
    forecast_tokens(t, horizon) = Σ cron jobs firing in window   [deterministic, jobs.json]
                                + seasonal_profile[hour_of_week] [median of interactive-only history]

Key invariants (handover §3.6):
  * jobs.json schedules are +05:30 LOCAL; api_calls.ts is UTC epoch seconds.
    Everything is normalized to UTC epoch at parse time.  TZ_OFFSET = +05:30.
  * Seasonal profile is computed from `session_id IS NOT NULL` rows ONLY, so
    gate-deferred cron shifts can never pollute it (anti-self-fulfilling).
  * hour-of-week is bucketed in +05:30 LOCAL time so the seasonal profile and
    the cron component are additive in the same wall-clock frame.

Self-contained: stdlib + sqlite3 only.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TZ_OFFSET = 5 * 3600 + 30 * 60          # +05:30 in seconds
DB_PATH = os.path.expanduser("~/.hermes/bot/zai_usage.db")
JOBS_PATH = os.path.expanduser("~/.hermes/profiles/manager/cron/jobs.json")
SEED_TOKENS = 50000                     # per-job magnitude seed when no history
EWMA_ALPHA = 0.3                         # rolling EWMA smoothing for per-job magnitude
WINDOW_SEC = 600                         # ±10 min attribution window around a fire time
SEASONAL_WEEKS = 8                       # trailing weeks for seasonal median
MAGNITUDE_WEEKS = 8                      # trailing weeks for per-job magnitude EWMA
SHADOW_TABLE = "demand_forecast_shadow"


# ---------------------------------------------------------------------------
# Timezone helpers (unit-tested)
# ---------------------------------------------------------------------------
def parse_local_ts(s):
    """Parse a +05:30 local ISO-8601 string -> UTC epoch seconds.

    Handles both offset-bearing strings ("2026-09-04T02:00:00+05:30") and
    naive strings (assumed +05:30).  This is the #1 bug-risk path; see
    `_run_unit_tests`.
    """
    if s is None:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(seconds=TZ_OFFSET)))
    return dt.timestamp()


def hour_of_week(ts):
    """Hour-of-week index (0..167) in +05:30 LOCAL time for a UTC epoch ts."""
    return int((ts + TZ_OFFSET) // 3600) % 168


# ---------------------------------------------------------------------------
# Cron expression expansion (5-field, stdlib only)
# ---------------------------------------------------------------------------
def _expand_field(field, lo, hi):
    vals = set()
    for part in field.split(","):
        if part == "*":
            vals.update(range(lo, hi + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            vals.update(range(lo, hi + 1, step))
        elif "-" in part:
            a, b = part.split("-")
            vals.update(range(int(a), int(b) + 1))
        else:
            vals.add(int(part))
    return vals


def cron_fire_times(expr, start_ts, end_ts):
    """Yield UTC epoch fire times for a 5-field cron expr within [start_ts, end_ts].

    The expr is interpreted in +05:30 LOCAL time (matching jobs.json).
    """
    minute_f, hour_f, dom_f, month_f, dow_f = expr.split()
    minutes = _expand_field(minute_f, 0, 59)
    hours = _expand_field(hour_f, 0, 23)
    doms = _expand_field(dom_f, 1, 31)
    months = _expand_field(month_f, 1, 12)
    dows = _expand_field(dow_f, 0, 6)  # 0=Sunday .. 6=Saturday

    tz = timezone(timedelta(seconds=TZ_OFFSET))
    d = datetime.fromtimestamp(start_ts, tz=tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(end_ts, tz=tz)

    dom_restricted = dom_f != "*"
    dow_restricted = dow_f != "*"

    out = []
    while d <= end:
        py_dow = (d.weekday() + 1) % 7  # python Mon=0 -> cron Sun=0
        dom_ok = d.day in doms
        dow_ok = py_dow in dows
        if dom_restricted and dow_restricted:
            day_ok = dom_ok or dow_ok          # Vixie cron: OR when both restricted
        else:
            day_ok = dom_ok and dow_ok
        if d.month in months and day_ok:
            for h in hours:
                for m in minutes:
                    fire = d.replace(hour=h, minute=m)
                    ts = fire.timestamp()
                    if start_ts <= ts <= end_ts:
                        out.append(ts)
        d += timedelta(days=1)
    return sorted(out)


# ---------------------------------------------------------------------------
# jobs.json parsing
# ---------------------------------------------------------------------------
def load_jobs(path=JOBS_PATH):
    with open(path) as f:
        data = json.load(f)
    return data.get("jobs", []), os.path.getmtime(path)


def enabled_llm_jobs(jobs):
    """Enabled jobs that actually consume LLM tokens (no_agent scripts are excluded)."""
    return [j for j in jobs if j.get("enabled") and not j.get("no_agent")]


def job_fire_times(job, start_ts, end_ts):
    """Enumerate a job's fire times (UTC epoch) within [start_ts, end_ts]."""
    sched = job.get("schedule", {})
    kind = sched.get("kind")
    if kind == "cron":
        return cron_fire_times(sched["expr"], start_ts, end_ts)
    if kind == "interval":
        minutes = sched.get("minutes", 0)
        if not minutes:
            return []
        anchor = parse_local_ts(job.get("next_run_at"))
        if anchor is None:
            return []
        step = minutes * 60
        # walk backward from the next fire to cover the past, then forward for the future
        out = []
        t = anchor
        while t >= start_ts:
            if t <= end_ts:
                out.append(t)
            t -= step
        t = anchor + step
        while t <= end_ts:
            out.append(t)
            t += step
        return sorted(out)
    if kind == "once":
        ts = parse_local_ts(sched.get("run_at") or job.get("next_run_at"))
        if ts is not None and start_ts <= ts <= end_ts:
            return [ts]
        return []
    return []


# ---------------------------------------------------------------------------
# Per-job token magnitude (rolling EWMA from history)
# ---------------------------------------------------------------------------
def _median(vals):
    vals = sorted(vals)
    n = len(vals)
    if n == 0:
        return 0.0
    if n % 2:
        return float(vals[n // 2])
    return (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def compute_job_magnitudes(conn, jobs, now_ts):
    """Per-job token magnitude via rolling EWMA over past fire-time windows.

    Attribution is "nearest job": each 5-min bucket of null-session tokens is
    attributed to the single nearest fire time (across all jobs) within ±10 min.
    This avoids double-counting when many jobs fire simultaneously (e.g. the
    02:00 local burst).  Seed 50K tokens/job when no history exists.
    """
    start = now_ts - MAGNITUDE_WEEKS * 7 * 86400
    # Load null-session tokens into 5-min buckets once (efficient).
    rows = conn.execute(
        "SELECT CAST(ts/300 AS INTEGER) b, SUM(total_tokens) "
        "FROM api_calls WHERE session_id IS NULL AND ts >= ? GROUP BY b",
        (start,),
    ).fetchall()
    bucket_tokens = {b: tot for b, tot in rows}

    # All fire times over the window, per job.
    job_fires = {}
    for job in jobs:
        job_fires[job["id"]] = job_fire_times(job, start, now_ts)

    # Flatten to a sorted list of (fire_ts, job_id) for nearest-neighbor lookup.
    all_fires = []
    for jid, fires in job_fires.items():
        for ft in fires:
            all_fires.append((ft, jid))
    all_fires.sort()
    fire_ts_list = [f[0] for f in all_fires]

    attributed = defaultdict(list)
    for b, tot in bucket_tokens.items():
        bucket_center = b * 300 + 150
        idx = bisect_left(fire_ts_list, bucket_center)
        best_jid = None
        best_d = WINDOW_SEC + 1
        for i in (idx - 1, idx):
            if 0 <= i < len(all_fires):
                ft, jid = all_fires[i]
                d = abs(ft - bucket_center)
                if d < best_d:
                    best_d = d
                    best_jid = jid
        if best_jid is not None and best_d <= WINDOW_SEC:
            attributed[best_jid].append(tot)

    magnitudes = {}
    for job in jobs:
        samples = attributed.get(job["id"], [])
        if samples:
            # rolling EWMA over chronological samples
            mag = SEED_TOKENS
            for s in samples:
                mag = EWMA_ALPHA * s + (1 - EWMA_ALPHA) * mag
            magnitudes[job["id"]] = mag
        else:
            magnitudes[job["id"]] = SEED_TOKENS
    return magnitudes


# ---------------------------------------------------------------------------
# Seasonal component (median tokens per hour-of-week, interactive only)
# ---------------------------------------------------------------------------
def compute_seasonal(conn, now_ts):
    """Median tokens per hour-of-week (168 buckets) over trailing 8 weeks,
    from session_id IS NOT NULL rows ONLY."""
    start = now_ts - SEASONAL_WEEKS * 7 * 86400
    rows = conn.execute(
        "SELECT CAST(ts/3600 AS INTEGER) h, SUM(total_tokens) "
        "FROM api_calls WHERE session_id IS NOT NULL AND ts >= ? GROUP BY h",
        (start,),
    ).fetchall()
    buckets = defaultdict(list)
    for h, tot in rows:
        buckets[hour_of_week(h * 3600)].append(tot)
    seasonal = {}
    for hw in range(168):
        seasonal[hw] = _median(buckets.get(hw, []))
    return seasonal


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------
def forecast_tokens(now_ts, horizon_hours, granularity_min, jobs, magnitudes, seasonal):
    """Return buckets [(bucket_ts, cron_tokens, seasonal_tokens, total)]."""
    horizon_sec = horizon_hours * 3600
    gran_sec = granularity_min * 60
    end_ts = now_ts + horizon_sec
    start_ts = int(now_ts // gran_sec) * gran_sec  # bucket-aligned start

    # Precompute all fire times in the horizon, bucketed.
    fire_buckets = defaultdict(float)
    for job in jobs:
        mag = magnitudes.get(job["id"], SEED_TOKENS)
        for ft in job_fire_times(job, start_ts, end_ts):
            b = int(ft // gran_sec) * gran_sec
            fire_buckets[b] += mag

    buckets = []
    t = start_ts
    while t < end_ts:
        cron_tokens = fire_buckets.get(t, 0.0)
        seasonal_tokens = seasonal.get(hour_of_week(t), 0.0) * (granularity_min / 60.0)
        total = cron_tokens + seasonal_tokens
        buckets.append((t, cron_tokens, seasonal_tokens, total))
        t += gran_sec
    return buckets


# ---------------------------------------------------------------------------
# Shadow log
# ---------------------------------------------------------------------------
def ensure_shadow_table(conn):
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SHADOW_TABLE} (
            ts REAL NOT NULL,
            bucket_ts REAL NOT NULL,
            forecast_tokens REAL,
            cron_component REAL,
            seasonal_component REAL,
            actual_tokens REAL
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{SHADOW_TABLE}_bucket ON {SHADOW_TABLE}(bucket_ts)"
    )


def backfill_actuals(conn, now_ts, granularity_min):
    """Fill actual_tokens for forecast buckets older than 1h."""
    gran_sec = granularity_min * 60
    cutoff = now_ts - 3600
    rows = conn.execute(
        f"SELECT bucket_ts FROM {SHADOW_TABLE} "
        "WHERE actual_tokens IS NULL AND bucket_ts < ?",
        (cutoff,),
    ).fetchall()
    for (bt,) in rows:
        actual = conn.execute(
            "SELECT COALESCE(SUM(total_tokens),0) FROM api_calls "
            "WHERE ts >= ? AND ts < ?",
            (bt, bt + gran_sec),
        ).fetchone()[0]
        conn.execute(
            f"UPDATE {SHADOW_TABLE} SET actual_tokens=? WHERE bucket_ts=? AND actual_tokens IS NULL",
            (actual, bt),
        )


def run_once(now_ts=None):
    now_ts = now_ts if now_ts is not None else time.time()
    jobs, mtime = load_jobs()
    llm_jobs = enabled_llm_jobs(jobs)

    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_shadow_table(conn)
        magnitudes = compute_job_magnitudes(conn, llm_jobs, now_ts)
        seasonal = compute_seasonal(conn, now_ts)

        # 5-min / 0-6h forecast (gate + failover horizon)
        buckets = forecast_tokens(now_ts, 6, 5, llm_jobs, magnitudes, seasonal)
        inserted = 0
        for (bt, cron_t, seas_t, total) in buckets:
            conn.execute(
                f"INSERT INTO {SHADOW_TABLE} "
                "(ts, bucket_ts, forecast_tokens, cron_component, seasonal_component, actual_tokens) "
                "VALUES (?,?,?,?,?,NULL)",
                (now_ts, bt, total, cron_t, seas_t),
            )
            inserted += 1

        # backfill actuals for buckets older than 1h (5-min granularity)
        backfill_actuals(conn, now_ts, 5)

        conn.commit()
    finally:
        conn.close()

    return {
        "now_ts": now_ts,
        "jobs_mtime": mtime,
        "n_llm_jobs": len(llm_jobs),
        "inserted": inserted,
        "sample": buckets[0] if buckets else None,
        "n_seasonal_nonzero": sum(1 for v in seasonal.values() if v > 0),
    }


# ---------------------------------------------------------------------------
# Backtest (walk-forward)
# ---------------------------------------------------------------------------
def _hourly_actuals(conn, start_ts, end_ts):
    rows = conn.execute(
        "SELECT CAST(ts/3600 AS INTEGER) h, SUM(total_tokens) "
        "FROM api_calls WHERE ts >= ? AND ts < ? GROUP BY h",
        (start_ts, end_ts),
    ).fetchall()
    return {h * 3600: tot for h, tot in rows}


def _trailing_7d_median(full_actuals, hour_ts):
    """Median of the same hour-of-week over the trailing 7 days (naive baseline).

    `full_actuals` is the complete hourly token map (all history), so the
    trailing window is always populated.
    """
    hw = hour_of_week(hour_ts)
    vals = []
    for d in range(1, 8):
        t = hour_ts - d * 86400
        if hour_of_week(t) == hw and t in full_actuals:
            vals.append(full_actuals[t])
    return _median(vals)


def run_backtest():
    conn = sqlite3.connect(DB_PATH)
    try:
        min_ts, max_ts = conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM api_calls"
        ).fetchone()
        if min_ts is None:
            print("No api_calls data.")
            return

        # Full-history hourly actuals (for trailing-7d baseline + spike detection).
        full_actuals = _hourly_actuals(conn, min_ts, max_ts)

        # Walk-forward: train N weeks -> predict next week, slide 1 week.
        train_weeks = 4
        week = 7 * 86400
        # If data is too short for a 4-week train, shrink to fit (as far as data allows).
        span = max_ts - min_ts
        while train_weeks > 1 and (train_weeks + 1) * week > span:
            train_weeks -= 1

        folds = []
        train_start = min_ts
        while train_start + (train_weeks + 1) * week <= max_ts:
            train_end = train_start + train_weeks * week
            pred_start = train_end
            pred_end = pred_start + week
            folds.append((train_start, train_end, pred_start, pred_end))
            train_start += week

        if not folds:
            print("Not enough data for walk-forward backtest "
                  f"(span={span/86400:.1f}d, train_weeks={train_weeks}).")
            return

        # Aggregate metrics across folds.
        mae = defaultdict(float)
        n_hours = 0
        spike_actual = 0
        spike_hit = defaultdict(int)   # model -> true positives
        spike_pred = defaultdict(int)  # model -> predicted spikes
        models = ["full", "cron", "seasonal", "naive"]

        for (tr_s, tr_e, pr_s, pr_e) in folds:
            jobs, _ = load_jobs()
            llm_jobs = enabled_llm_jobs(jobs)
            magnitudes = compute_job_magnitudes(conn, llm_jobs, tr_e)
            seasonal = compute_seasonal(conn, tr_e)

            actuals = _hourly_actuals(conn, pr_s, pr_e)
            # hourly forecast over the prediction week
            buckets = forecast_tokens(pr_s, (pr_e - pr_s) / 3600.0, 60,
                                      llm_jobs, magnitudes, seasonal)
            pred = {bt: (cron_t, seas_t, total) for (bt, cron_t, seas_t, total) in buckets}

            for bt in sorted(actuals):
                actual = actuals[bt]
                baseline = _trailing_7d_median(full_actuals, bt)
                cron_t, seas_t, total = pred.get(bt, (0.0, 0.0, 0.0))
                naive = baseline
                is_spike = baseline > 0 and actual > 2 * baseline

                mae["full"] += abs(total - actual)
                mae["cron"] += abs(cron_t - actual)
                mae["seasonal"] += abs(seas_t - actual)
                mae["naive"] += abs(naive - actual)
                n_hours += 1

                if is_spike:
                    spike_actual += 1
                for name, pv in (("full", total), ("cron", cron_t),
                                 ("seasonal", seas_t), ("naive", naive)):
                    if baseline > 0 and pv > 2 * baseline:
                        spike_pred[name] += 1
                        if is_spike:
                            spike_hit[name] += 1

        # Report
        print(f"Backtest: {len(folds)} fold(s), train={train_weeks}w, predict=1w, "
              f"data span={span/86400:.1f}d, {n_hours} hourly buckets")
        print()
        hdr = f"{'model':<10} {'MAE':>12} {'spike_recall':>13} {'spike_prec':>12}"
        print(hdr)
        print("-" * len(hdr))
        for name in models:
            m = mae[name] / n_hours if n_hours else 0.0
            rec = spike_hit[name] / spike_actual if spike_actual else 0.0
            prec = spike_hit[name] / spike_pred[name] if spike_pred[name] else 0.0
            print(f"{name:<10} {m:>12,.0f} {rec:>13.3f} {prec:>12.3f}")
        print()
        print(f"actual spikes: {spike_actual}")
        print("MAE relative to naive baseline:")
        base = mae["naive"] / n_hours if n_hours else 1.0
        for name in ["full", "cron", "seasonal"]:
            m = mae[name] / n_hours if n_hours else 0.0
            print(f"  {name:<10} {m/base*100:>6.1f}% of naive MAE")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unit tests (inline)
# ---------------------------------------------------------------------------
def _run_unit_tests():
    failures = []

    def check(name, cond, detail=""):
        if cond:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name}  {detail}")
            failures.append(name)

    print("Unit tests:")
    # 1. +05:30 -> UTC epoch conversion
    ts = parse_local_ts("2026-09-04T02:00:00+05:30")
    # 02:00 +05:30 == 20:30 UTC on 2026-09-03
    expected = datetime(2026, 9, 3, 20, 30, 0, tzinfo=timezone.utc).timestamp()
    check("tz: 02:00+05:30 -> 20:30 UTC prev day", abs(ts - expected) < 1e-6,
          f"got {ts} expected {expected}")

    # 2. naive string assumed +05:30
    ts2 = parse_local_ts("2026-09-04T02:00:00")
    check("tz: naive string assumed +05:30", abs(ts2 - expected) < 1e-6,
          f"got {ts2} expected {expected}")

    # 3. hour_of_week is week-indexed (0..167), epoch-aligned to Thursday 00:00 local.
    #    Key property: same local wall-clock hour across weeks -> same bucket.
    hw = hour_of_week(ts)
    hw_next_week = hour_of_week(ts + 7 * 86400)
    check("tz: hour_of_week periodic (168h)", hw == hw_next_week,
          f"got {hw} vs {hw_next_week}")

    # 4. consecutive hours increment by 1 (mod 168)
    hw_plus1 = hour_of_week(ts + 3600)
    check("tz: hour_of_week increments by 1", (hw + 1) % 168 == hw_plus1,
          f"got {hw} -> {hw_plus1}")

    # 5. cron expansion: daily 02:00 local fires once per day
    fires = cron_fire_times("0 2 * * *", ts - 3 * 86400, ts + 3 * 86400)
    check("cron: daily 02:00 -> 7 fires in 7 days", len(fires) == 7, f"got {len(fires)}")

    # 6. cron expansion: every 30 min (inclusive window -> 3 fires at 02:00/02:30/03:00)
    fires = cron_fire_times("*/30 * * * *", ts, ts + 3600)
    check("cron: */30 -> 3 fires in inclusive 1h", len(fires) == 3, f"got {len(fires)}")

    # 7. cron dow: Monday only (0 9 * * 1)
    fires = cron_fire_times("0 9 * * 1", ts, ts + 7 * 86400)
    check("cron: Monday 09:00 -> 1 fire in 7 days", len(fires) == 1, f"got {len(fires)}")

    print(f"  {len(failures)} failure(s)")
    return len(failures) == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Demand forecast shadow logger + backtest")
    ap.add_argument("--once", action="store_true", help="single run (cron wrapper)")
    ap.add_argument("--backtest", action="store_true", help="walk-forward backtest")
    ap.add_argument("--test", action="store_true", help="run inline unit tests")
    args = ap.parse_args()

    if args.test:
        ok = _run_unit_tests()
        sys.exit(0 if ok else 1)

    if args.backtest:
        run_backtest()
        return

    if args.once:
        res = run_once()
        print(f"once run @ {datetime.fromtimestamp(res['now_ts'], tz=timezone.utc).isoformat()}")
        print(f"  jobs.json mtime: {datetime.fromtimestamp(res['jobs_mtime']).isoformat()}")
        print(f"  enabled LLM jobs: {res['n_llm_jobs']}")
        print(f"  seasonal buckets nonzero: {res['n_seasonal_nonzero']}/168")
        print(f"  inserted forecast rows: {res['inserted']}")
        if res["sample"]:
            bt, cron_t, seas_t, total = res["sample"]
            print(f"  sample bucket: bucket_ts={bt} "
                  f"({datetime.fromtimestamp(bt, tz=timezone.utc).isoformat()}) "
                  f"cron={cron_t:,.0f} seasonal={seas_t:,.0f} total={total:,.0f}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
