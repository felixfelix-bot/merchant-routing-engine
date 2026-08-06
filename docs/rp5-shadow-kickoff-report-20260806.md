# RP-5 Shadow-Mode Kickoff Report — 2026-08-06

**Task:** t_bf86f0e4 — RP-5: cron collector + commit + push + 48h shadow validation
**Kickoff (UTC):** 2026-08-06 15:12
**48h window ends (UTC):** ~2026-08-08 15:12
**Commit:** 8291ecd (converged-rate-replay)
**Cron job:** `7ea621e653f4` — RealtimePricing refresh every 5 min (no_agent, watchdog)

## 1. What shipped (steps 1–4)

| Step | Status | Detail |
|------|--------|--------|
| 1. Cron collector | ✅ DONE | `scripts/realtime_pricing_cron.sh` + `~/.hermes/scripts/` delegate; every-5-min `RealtimePricing.get_instance().refresh()`; watchdog (silent on success, alert on NaN/stale/exception). |
| 2. Commit RP-1..RP-4 | ✅ DONE (concurrent) | `92899f9 feat(RP-4)` committed the 6 previously-dirty files (a parallel worker in this shared workspace). |
| 3. Push to converged-rate-replay | ✅ DONE | `8291ecd` pushed to `github/converged-rate-replay`; local 0/0 vs remote. |
| 4. Enable shadow mode | ✅ DONE | `REALTIME_PRICING_ENABLED=true` (default; cron exports it explicitly). **Shadow-only is structural**: `RealtimePricing` has **zero call sites** in the routing hot path (`live_router`/`pricing_engine`/`shadow_hook`) — verified by grep. Enabling it only populates the measured-rate snapshot; routing is untouched. |

## 2. CRITICAL FIX — ollama collector parser bug (found during validation)

`_measure_ollama_billing` was **silently discarding 100% of the real billing data**.

**Root cause:** the collector iterated `activity.items()`, but the real Ollama `/api/usage` payload is:

```json
{"activity": {"cost": "60.00", "period": {...},
              "models": [{"name": "glm-5.2", "request_count": 954, "cost": "32.25"}, ...]}}
```

The per-model data lives in `activity["models"]` (a **list**). The `isinstance(entry, dict)` guard skipped every top-level key (`cost`=string, `period`=dict-with-no-cost, `models`=list), so `total_tokens` stayed 0 and every refresh fell to the `$100/mo` amortization fallback → **$0.223/M** (wrong by 14×).

**This slipped past RP-4** (the Gate-2.5 cold review): `t_5eacec6c` was archived with **zero runs** — the review never executed.

**Fix (8291ecd):** read `activity["models"]`; estimate per-model tokens proportionally (`request_count × avg_tokens_per_call` from the trailing 4-week `api_calls` volume — the method in `extra-usage-real-data-analysis.md`); provider-level blended rate = `activity.cost / 4-week-token-volume`. The fix is **shadow-only** (no routing impact). 4 stale tests updated to the real API shape; **all 1768 tests pass**.

## 3. Immediate validation — 5 of 6 criteria PASS (`scripts/rp5_validate.py`)

```
RP-5 shadow validation @ 2026-08-06T15:12:31Z
  refresh_count=1  snapshot_age=0.0 min   REALTIME_PRICING_ENABLED=True

  (a) ✅ PASS  6/6 providers present; 2 measured (zai + ollama); ppq cold-start
  (b) ❌ FAIL  ollama_cloud=$0.02456/M  target=$0.0155 [±10%]  src=ollama_billing_api
  (c) ✅ PASS  per-model extra obs present: glm-5.2 $0.4632, kimi-k3 $0.5042,
              kimi-k2.7-code $0.2619, deepseek-v4-flash $0.0220 (all measured)
  (d) ✅ PASS  glm-5.2 extra=$0.4632/M  target=$0.46 [±30%]
  (e) ✅ PASS  no NaN/inf; snapshot fresh (0.0 min)
  (f) ✅ PASS  routing_wired=False; kill-switch no-ops correctly
  price_observations rows persisted: 54
```

## 4. Criterion (b) — target is STALE (decision needed)

`$0.0155/M` came from the analysis doc as `activity.cost / total_tokens` **at doc-writing time**, when extra-usage spend was **$38.52**. Extra spend has since grown to **$60.00** while 4-week token volume is flat (~2.44 B), so the same formula now yields **$0.0246/M** — outside the ±10% band. The rate is **correct per the documented methodology**; the **target value is stale**.

This is **not a code defect** — it is a gate-target calibration question:

- **Option A:** recalibrate criterion (b) to the current measured baseline (e.g. `$0.025/M ±20%`, wider band to absorb extra-spend fluctuation) and certify.
- **Option B:** treat the doubling ($0.0155→$0.0246) as a signal that extra-usage spend is growing faster than volume — investigate before accepting.

Either way, the literal gate "$0.0155 ±10%" cannot be certified against current data. **This is the one decision blocking gate certification.**

## 5. Notes for the 48h window

- **(c)** is *conditional* on `limits.session.usage ≥ 1.0`. It is currently **0.185** (extra-usage not active). Detection is verified working (per-model obs produced whenever extra models appear in `activity.models`); whether a session crosses 1.0 during the window depends on live traffic.
- **(a)** `ppq` shows cold-start because `ppq_queries` has no recent rows in `api_burn.db` — a data/traffic matter, not a code defect; it will measure once PPQ traffic resumes.
- **kimi-k3** measures **$0.50/M** (proportional-token estimate) vs the doc's **$7.53/M**. The doc's $7.53 used a much smaller token basis (123 K) than the current 4-week volume justifies (~44 M proportional); this is a data-window/model-name reconciliation item, **not a gate criterion** (the gate's extra-rate check is glm-5.2-specific).

## 6. What runs next

- The every-5-min cron (`7ea621e653f4`) continues collecting → `price_observations` accumulates convergence data.
- A one-shot **48h review job** (`rp5-48h-review`) fires ~2026-08-08 15:30 UTC: re-runs `rp5_validate.py`, inspects `price_observations` convergence, and reports the gate status.

**GATE status today: 5/6 PASS; (b) pending recalibration decision; 48h convergence pending wall-clock.**
