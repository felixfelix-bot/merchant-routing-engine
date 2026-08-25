# ADR-011: Config-Driven Amortized Seed Pricing in real_price_tracker

- **Status:** Accepted (2026-08-25)
- **Context:** `real_price_tracker.py` seeded z.ai subscriptions at a hard `$0.001/M` floor. CG-3 introduced amortized annual-budget seeds (`ours: 0.03` @ $960/yr, `friend: 0.015` @ $300/yr) with `fee=0` as a cold-start observation (not a floor), reading fees from `config/providers.yaml` / env (`ZAI_ANNUAL_BUDGET`, `ZAI_OURS_BUDGET`, `ZAI_FRIEND_BUDGET`). During consolidation the live bot and engine worktree already agreed on the amortized seeds; `HEAD`'s `$0.001` was stale.
- **Decision:** Adopt the worktree's amortized, config-driven seed table as canonical. These seeds are **Kalman initialization priors** (overwritten by measured samples via `real_price_tracker`), NOT a routing floor. The `$0.001/M` live routing floor is a separate concept owned by `flat_router.MIN_EFFECTIVE_PRICE` and is intentionally untouched. Do not resurrect locally-learned/drifted values (e.g. `neuralwatt: 0.21`) into `_SEED_COSTS`; learned values belong in `real_price_tracker` state, not seed constants.
- **Consequences:**
  - (+) Seeds reflect real subscription economics; Kalman converges from a realistic prior.
  - (+) Repo matches the running production proxy (removes HEAD<->prod drift).
  - Scope guard: no change to `flat_router.MIN_EFFECTIVE_PRICE` or the shadow/live split.