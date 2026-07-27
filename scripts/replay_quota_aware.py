"""replay_quota_aware.py — Full replay with real quota state + scarcity + pace.

Replays 520K key_decisions through the RoutingOptimizer with:
- CONVERGED Kalman base rates (proven in converged-rate replay)
- REAL quota state from key_decisions (ours_pct, friend_pct)
- scarcity_factor: ramps price as quota fills
- pace_factor: ramps price if burning faster than budget
- peak_multiplier: 3x during 6-10 UTC

Compares: live routing vs optimizer routing.
Answers: does scarcity pricing smooth the friend-fallback cliff?

Usage:
    python3 scripts/replay_quota_aware.py
    python3 scripts/replay_quota_aware.py --sample-every 10
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from src.price_kalman import PriceKalman, scarcity_factor, peak_multiplier, health_pricing_factor
from src.routing_optimizer import RoutingOptimizer
from scripts.feed_historical_costs import load_historical_rates

# Converged rates (from feed_historical_costs.py)
CONVERGED_COSTS = {
    "ours":          0.001,    # clamped from -0.000968
    "friend":        0.028983,
    "ollama_cloud":  0.023952,
    "ppq":           0.14,
    "openrouter":    0.135,
    "deepinfra":     1.30,
}

# Approximate quota totals per 5h window
QUOTA_TOTALS = {
    "ours": 2_000_000,
    "friend": 2_000_000,
}


class _StubCK:
    def will_exhaust(self, remaining, horizon):
        return (False, 9999.0)


def build_optimizer_with_quota(
    rates: dict[str, float],
    ours_pct: float,
    friend_pct: float,
    ours_exhausted: bool,
    friend_exhausted: bool,
    hour: int,
) -> tuple[RoutingOptimizer, dict]:
    """Build optimizer with real quota state for scarcity computation."""
    opt = RoutingOptimizer(peak_hours_utc=(6, 10), peak_mult=3.0, exhaustion_horizon=1)

    provider_state = {}
    for name, rate in rates.items():
        pk = PriceKalman(initial_rate=max(rate, 0.001))

        if name in ("ours", "friend"):
            tier, model = "high", "glm-5.2"
            prov_peak, prov_peak_mult = (6, 10), 3.0
            # Real quota state
            pct = ours_pct if name == "ours" else friend_pct
            total = QUOTA_TOTALS.get(name, 2e6)
            remaining = max(0, total * (1 - pct / 100.0))
            exhausted = ours_exhausted if name == "ours" else friend_exhausted
            scar_pct = pct
        elif name == "ollama_cloud":
            tier, model = "high", "glm-5.2"
            prov_peak, prov_peak_mult = None, 1.0
            remaining, total, exhausted = 1e12, 1e12, False
            scar_pct = 0.0
        else:
            tier, model = "low", "deepseek/deepseek-v4-flash"
            prov_peak, prov_peak_mult = None, 1.0
            remaining, total, exhausted = 1e12, 1e12, False
            scar_pct = 0.0

        opt.add_provider(
            name=name,
            price_kalman=pk,
            consumption_kalman=_StubCK(),
            quota_remaining=remaining,
            breaker_tripped=exhausted,  # treat exhausted as tripped breaker
            model_tier=tier,
            model=model,
            quota_total=total,
            peak_hours_utc=prov_peak,
            peak_mult=prov_peak_mult,
            failure_count=999 if exhausted else 0,
        )

        # Compute scarcity for reporting
        if name in ("ours", "friend"):
            scar = scarcity_factor(scar_pct)
            peak = peak_multiplier(hour=hour, peak_hours_utc=(6, 10), peak_mult=3.0)
            eff_price = max(rate * peak * scar * (10.0 if exhausted else 1.0), 0.001)
            provider_state[name] = {
                "pct": pct,
                "scarcity": scar,
                "peak_mult": peak,
                "exhausted": exhausted,
                "effective_price": eff_price,
            }

    return opt, provider_state


def replay(db_path: str, rates: dict[str, float], sample_every: int = 1) -> dict:
    """Replay key_decisions with full pricing system."""
    conn = sqlite3.connect(db_path)
    
    # Load key_decisions (sampled)
    rows = conn.execute(
        f"SELECT ts, chosen_key, reason, ours_pct, friend_pct, "
        f"ours_available, friend_available "
        f"FROM key_decisions "
        f"WHERE id % {sample_every} = 0 "
        f"ORDER BY ts"
    ).fetchall()
    conn.close()

    total = len(rows)
    print(f"  Loaded {total:,} sampled decisions (every {sample_every}th)")

    # Stats
    live_picks = Counter()
    opt_picks = Counter()
    
    # Per-quota-bracket tracking
    brackets = defaultdict(lambda: {
        "live_ours": 0, "live_friend": 0, "live_other": 0,
        "opt_ours": 0, "opt_friend": 0, "opt_ollama": 0, "opt_other": 0,
        "total": 0,
        "ours_eff_price_sum": 0.0,
        "friend_eff_price_sum": 0.0,
    })

    # Time-series of when optimizer starts shifting (cliff smoothing)
    shift_points = []  # (ours_pct, opt_pick)

    # Agreement
    agree = 0

    for i, (ts, chosen, reason, ours_pct, friend_pct, ours_avail, friend_avail) in enumerate(rows):
        ours_exhausted = (ours_avail == 0)
        friend_exhausted = (friend_avail == 0)
        hour = int(datetime.fromtimestamp(ts, tz=timezone.utc).hour)

        opt, state = build_optimizer_with_quota(
            rates, ours_pct or 0, friend_pct or 0,
            ours_exhausted, friend_exhausted, hour,
        )

        # Determine difficulty from reason (most are high/medium for manager)
        difficulty = "high"  # conservative default

        result = opt.route(difficulty=difficulty, estimated_tokens=10000, hour=hour)
        opt_pick = result["chosen_provider"]

        # Normalize live choice
        if chosen and "ours" in chosen.lower():
            live_norm = "ours"
        elif chosen and "friend" in chosen.lower():
            live_norm = "friend"
        elif chosen in ("ollama_cloud",):
            live_norm = "ollama_cloud"
        elif chosen:
            live_norm = chosen
        else:
            live_norm = "none"

        live_picks[live_norm] += 1
        opt_picks[opt_pick] += 1

        if live_norm == opt_pick:
            agree += 1

        # Bracket tracking
        def bracket(pct):
            if pct < 10: return "0-9%"
            if pct < 25: return "10-24%"
            if pct < 50: return "25-49%"
            if pct < 75: return "50-74%"
            if pct < 90: return "75-89%"
            if pct < 100: return "90-99%"
            return "100%+"

        b = bracket(ours_pct or 0)
        bdata = brackets[b]
        bdata["total"] += 1
        
        if live_norm == "ours": bdata["live_ours"] += 1
        elif live_norm == "friend": bdata["live_friend"] += 1
        else: bdata["live_other"] += 1
        
        if opt_pick == "ours": bdata["opt_ours"] += 1
        elif opt_pick == "friend": bdata["opt_friend"] += 1
        elif opt_pick == "ollama_cloud": bdata["opt_ollama"] += 1
        else: bdata["opt_other"] += 1
        
        if "ours" in state:
            bdata["ours_eff_price_sum"] += state["ours"]["effective_price"]
        if "friend" in state:
            bdata["friend_eff_price_sum"] += state["friend"]["effective_price"]

        # Track when optimizer starts shifting away from ours
        if opt_pick != "ours" and (ours_pct or 0) < 100:
            shift_points.append((ours_pct or 0, opt_pick, friend_pct or 0))

        if (i + 1) % 50000 == 0:
            print(f"    {i+1:,}/{total:,} processed...")

    return {
        "total": total,
        "live_picks": dict(live_picks),
        "opt_picks": dict(opt_picks),
        "agree": agree,
        "brackets": {k: v for k, v in sorted(brackets.items(), key=lambda x: x[0])},
        "shift_points": shift_points[:200],  # sample for reporting
        "shift_count": len(shift_points),
    }


def generate_report(stats: dict, rates: dict[str, float]) -> str:
    total = stats["total"]
    lines = []
    lines.append("# Quota-Aware Routing Replay — Full Pricing System Test")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"**Decisions replayed:** {total:,} (sampled from 520K key_decisions)")
    lines.append(f"**Rates:** CONVERGED Kalman (ours=${rates['ours']:.4f}, friend=${rates['friend']:.4f})")
    lines.append(f"**Pricing:** base × peak(3x 6-10UTC) × scarcity(1.0→2.0 ramp) × health(breaker)")
    lines.append("")

    # Provider distribution
    lines.append("## Provider Distribution")
    lines.append("")
    lines.append("| Provider | Live (actual) | Optimizer (converged+scarcity) |")
    lines.append("|----------|---------------|--------------------------------|")
    all_provs = sorted(set(list(stats["live_picks"].keys()) + list(stats["opt_picks"].keys())))
    for p in all_provs:
        live = stats["live_picks"].get(p, 0)
        opt = stats["opt_picks"].get(p, 0)
        lines.append(f"| {p} | {live:,} ({live/total*100:.1f}%) | {opt:,} ({opt/total*100:.1f}%) |")
    lines.append("")

    agree_pct = stats["agree"] / total * 100
    lines.append(f"**Agreement:** {stats['agree']:,}/{total:,} ({agree_pct:.1f}%)")
    lines.append("")

    # Quota bracket analysis — THE KEY TABLE
    lines.append("## Quota Bracket Analysis — Does Scarcity Smooth the Cliff?")
    lines.append("")
    lines.append("Shows routing behavior at different ours_pct levels.")
    lines.append("Live = production proxy (binary: available/exhausted).")
    lines.append("Optimizer = price-based with scarcity ramp.")
    lines.append("")
    lines.append("| ours_pct | Total | Live→ours | Live→friend | Opt→ours | Opt→friend | Opt→ollama | Ours eff $/M | Friend eff $/M |")
    lines.append("|----------|-------|-----------|-------------|----------|------------|------------|-------------|---------------|")
    for bracket, d in stats["brackets"].items():
        t = d["total"]
        if t == 0:
            continue
        lo = d["live_ours"]/t*100
        lf = d["live_friend"]/t*100
        oo = d["opt_ours"]/t*100
        of = d["opt_friend"]/t*100
        ol = d["opt_ollama"]/t*100
        oe = d["ours_eff_price_sum"]/t if t else 0
        fe = d["friend_eff_price_sum"]/t if t else 0
        lines.append(
            f"| {bracket:>7s} | {t:,} | {lo:.0f}% | {lf:.0f}% | "
            f"{oo:.0f}% | {of:.0f}% | {ol:.0f}% | ${oe:.4f} | ${fe:.4f} |"
        )
    lines.append("")

    # Cliff smoothing analysis
    lines.append("## Cliff Smoothing Analysis")
    lines.append("")
    sp = stats["shift_points"]
    if sp:
        # Find the lowest ours_pct where optimizer started shifting
        sp_sorted = sorted(sp, key=lambda x: x[0])
        earliest = sp_sorted[0]
        lines.append(f"- Optimizer first shifted away from 'ours' at **{earliest[0]:.0f}% quota** → picked {earliest[1]}")
        lines.append(f"  (friend was at {earliest[2]:.0f}% at that point)")
        
        # Distribution of shift points
        shift_brackets = Counter()
        for pct, pick, _ in sp:
            if pct < 50: shift_brackets["<50%"] += 1
            elif pct < 75: shift_brackets["50-74%"] += 1
            elif pct < 90: shift_brackets["75-89%"] += 1
            elif pct < 100: shift_brackets["90-99%"] += 1
            else: shift_brackets["100%+"] += 1
        
        lines.append(f"- Optimizer shifted away from ours **{stats['shift_count']:,} times** total")
        lines.append("- Shift distribution by quota bracket:")
        for b in ["<50%", "50-74%", "75-89%", "90-99%", "100%+"]:
            lines.append(f"  - {b}: {shift_brackets.get(b, 0):,}")
    else:
        lines.append("- Optimizer NEVER shifted away from 'ours' when quota < 100%")
        lines.append("- Only shifted when ours was fully exhausted (breaker tripped)")
    lines.append("")

    # Findings
    lines.append("## Findings")
    lines.append("")
    lines.append("### 1. Scarcity ramp effect")
    lines.append("")
    ours_rate = rates.get("ours", 0.001)
    friend_rate = rates.get("friend", 0.029)
    # At what scarcity does ours+peak exceed friend?
    # ours * peak * scarcity > friend → scarcity > friend / (ours * peak)
    # Peak = 3.0 during 6-10 UTC, 1.0 otherwise
    for peak_label, peak_mult in [("non-peak (1x)", 1.0), ("peak (3x)", 3.0)]:
        threshold = friend_rate / (ours_rate * peak_mult) if ours_rate * peak_mult > 0 else 999
        # scarcity_factor(pct) = 1 + max(0, (pct-50)/50)
        # threshold = 1 + (pct-50)/50 → pct = 50 + (threshold-1)*50
        if threshold > 1.0:
            crossover_pct = 50 + (threshold - 1) * 50
            lines.append(
                f"- During {peak_label}: ours crosses friend price at scarcity={threshold:.1f}x "
                f"(quota {crossover_pct:.0f}% used)"
            )
        else:
            lines.append(f"- During {peak_label}: ours is ALWAYS cheaper than friend (even at full scarcity)")
    lines.append("")

    lines.append("### 2. Production vs optimizer cliff comparison")
    lines.append("")
    lines.append(
        "Production proxy uses a BINARY cliff: ours_available=1 → route to ours, "
        "ours_available=0 → route to friend. This causes sudden traffic spikes on friend.\n\n"
        "The optimizer with scarcity ramps ours's price gradually as quota fills. "
        "At converged rates, ours starts at $0.001/M — even at 2x scarcity + 3x peak = "
        f"${ours_rate * 6:.4f}/M, still cheaper than friend's ${friend_rate:.4f}/M. "
        "So scarcity alone does NOT shift traffic at these rates.\n\n"
        "The only mechanism that shifts traffic is the HARD exhaustion gate "
        "(breaker_tripped when ours_available=0). Scarcity pricing smooths the "
        "transition but doesn't cause it — the rates are too far apart."
    )
    lines.append("")

    lines.append("### 3. What WOULD cause earlier shifting?")
    lines.append("")
    lines.append(
        "With converged rates, ours is ~30x cheaper than friend. Scarcity (2x) + "
        "peak (3x) = 6x — not enough. To make the optimizer shift traffic to friend "
        "BEFORE exhaustion, we'd need either:\n\n"
        "1. **Higher scarcity ceiling** (e.g., 10x at 90% instead of 2x at 100%)\n"
        "2. **Quota reservation** — reserve last 20% for high-priority only\n"
        "3. **Pace-based shifting** — if burn rate predicts exhaustion within X hours, "
        "gradually raise price to start pre-shifting traffic\n"
        "4. **Accept the binary cliff** — ours is so cheap that maxing it out before "
        "falling back is actually optimal. The cliff IS the right behavior when rates "
        "are this far apart."
    )
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay with full pricing system")
    parser.add_argument("--db", default=os.path.expanduser("~/.hermes/bot/zai_usage.db"))
    parser.add_argument("--sample-every", type=int, default=5, help="Sample every Nth decision")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: DB not found: {args.db}")
        return 1

    print(f"Replaying key_decisions with full pricing system...")
    print(f"  Rates: CONVERGED")
    print(f"  Sampling: every {args.sample_every}th decision")
    print()

    t0 = time.time()
    stats = replay(args.db, CONVERGED_COSTS, args.sample_every)
    elapsed = time.time() - t0
    print(f"  Replay took {elapsed:.1f}s")

    report = generate_report(stats, CONVERGED_COSTS)
    print()
    print(report)

    output_path = args.output or os.path.join(_PARENT, "docs", "quota-aware-replay-report.md")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(report)
    print(f"\nReport written to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
