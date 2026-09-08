# Gap Analysis + Hardening Design — Garbage Detection & Market Price-Bump Routing

**Consultant:** routing-architecture review, 2026-09-08. **Design-only — no production files modified.**
**Scope:** the LIVE mechanism in `~/.hermes/bot/garbage_detector.py` + `flat_router.py` (§3c) + `zai_proxy.py` (4 call sites). Every claim is grounded in the actual files read this session.

---

## 0. What is actually live (grounded)

- **The only live backoff is the soft price multiplier.** The 24h "hard demotion" circuit breaker `t_b7725426` is **NOT deployed**: `PLAN-garbage-output-detection.md` (2026-09-07) §"Supersedes" removes it, and a grep of live `zai_proxy.py` for `_check_garbage_cb` / `_garbage_cb` / `GARBAGE_CB_TTL` returns **zero** matches. The flat-router-architecture SKILL.md's "Garbage Circuit-Breaker — LIVE" section is **stale** and must not be trusted for current behavior.
- **Detectors** (`garbage_detector.py` `looks_like_garbage`, L263): `empty` (L269), `mojibake`/`control_chars` (`_nonascii_reason` L214), `repetition_compress`/`repetition_lines` (`_repetition_reason` L170), `bad_json` (L202, only when request demanded JSON), `gibberish_english` (`_gibberish_reason` L228, ≥120 words + common-word hitrate), plus `oversized` (>32k completion tokens) folded in at `inspect_response` L389.
- **Price** (`garbage_price_mult` L410): `BASE^n` (3.0ⁿ), `inf` at `PRICE_MAX_STRIKES` (4), decays to 1.0 once strikes age past `PRICE_WINDOW` (3600s). Fail-open ×1.0 on error/kill-switch. Per-`(provider, model_lower)` in-memory `_STRIKES`.
- **Wiring:** `flat_router._garbage_mult_or_one` (L897) → applied in `select_provider()` step 3c (L1088-1090, reason `garbage×N` L1100). **Pass-through by design** — the first garbage response is still delivered; strikes only steer *future* routing (docstring L476-477).
- **Call sites:** `_garbage_check` fires from `_try_zai_key` L5313, `_try_external_single` L5787, `_try_external_failover` L6005, `_proxy` catch-all L6842. **Not** from `_try_ollama_cloud` (returns True L5197 with no check), `_try_telnyx`, or `_try_opencode_go`.

---

## 1. Gap-analysis table (user ask → status → evidence)

| # | User ask | Status | Code evidence |
|---|----------|--------|---------------|
| (a) | Detect garbled LLM output | **EXISTS** | 7 detectors in `garbage_detector.py`, Pure/no-IO, run on JSON **and** SSE (`inspect_response` L329). Ledger + `MODEL_GARBAGE` alert-once. |
| (b) | Bump the price for "some time" | **PARTIAL — gap** | `BASE^n`, `inf` at 4 strikes, decay out of 3600s window. "Some time" is fixed 1h with **no escalation**; persists only in-memory (lost on restart, PLAN L78 "not in this pass"). |
| (c) | Route around via market | **EXISTS** | `select_provider()` L1088 multiplies effective cost by `_gmult`; cheaper healthy lanes win. Soft, never removes a lane (only-lane → still served). |
| (d) | DECAY / "for some time" semantics | **GAP — oscillation** | Strikes age out of the 1h window (L420-421, L489-491) regardless of magnitude. **Live:** `neuralwatt/deepseek-v4-flash` hit **101 strikes / ∞ mult** then reset to 1.0 ~20 min later → price ∞ → 1h → 1.0 → garbles → ∞. No "recovery/re-probe before trusting again" state exists. |
| (e) | Cover ALL endpoints | **GAP** | Garbage on `ollama_cloud` / `telnyx` / `opencode_go` lanes is **never detected** → never strikes → market never routes around a garbling flat-rate lane (and flat-rate lanes are often cheapest, so this is the *most* likely to be wrongly trusted). |

---

## 2. The real gaps

**G1 — Strike-decay oscillation (the core defect).** `PRICE_WINDOW` is a flat 1h roll. 101 strikes in one window vs 1 strike produce the *same* decay: back to 1.0 after an hour. A *chronically* broken pair is repeatedly re-admitted, garbles, re-penalized — the exact pendulum the operator observed. There is no longer-lived "warm"/cooldown state and no requirement of a successful (non-garbage, HTTP 200) response before re-trusting.

**G2 — Coverage hole on 3 lanes.** `_try_ollama_cloud` / `_try_telnyx` / `_try_opencode_go` success paths return without ever calling `_garbage_check`. `ollama_cloud` is the flat-rate/included provider that wins on price during z.ai peak (docstring L5076-78) — exactly when hidden garbage would silently degrade agent work without moving the market.

**G3 — Keying consistency unverified.** The price lookup uses flat_router's `_resolve_model_for_provider(name, model_id)` (L1088) while `_try_external_single` records strikes under the raw `model` arg (`_garbage_check(provider_name, model, ...)` L5787), and dispatch translates separately via `_PROVIDER_MODEL_NAMES` (L5736). If those strings differ in form (canonical/slashed vs provider-native/bare), the strike key and the price-lookup key **mismatch** → the bump is computed for a key no strike exists on → silent no-op. Needs a normalization-invariant consistency test.

**G4 — First garbage is still delivered (pass-through).** Deliberate (operator choice, L7-8 / L476-477), but means a user/agent *does* receive garbage before the market learns. For telltale classes (`empty`, `bad_json`) this is a wasted call + wasted prompt tokens (the neuralwatt case burned a ~57k-token prompt on an empty return).

**G5 — No persistence / no long-term quality signal.** Strikes are in-memory only (reset on restart, PLAN L78 deferred). A fabric that reboots clean every hour re-learns the same breakage from zero.

**G6 — Reasoning-model false-positive risk (assessed below).**

---

## 3. Hardened recovery state-machine design (fixes G1 + G5)

Add to `garbage_detector.py` a per-`(provider, model)` **recovery state machine** layered *on top of* the existing strike window — **price-multiplier only, never a hard cap**, preserving the NO-CAPS/flat-market principle.

**Escalation curve — BINARY EXPONENTIAL (operator decision, 2026-09-08):** the operator chose binary-exponential price bumps, mirroring the router's existing binary-exponential backoff convention for HTTP failures (30s→60s→120s→…→1h cap). So both components escalate by ~doubling:
- **Price:** `BASE = 2.0` → bump = `2^n` per strike in window (2×, 4×, 8×…), `inf` at `PRICE_MAX_STRIKES` (4). (Was `3^n`; operator prefers base-2 to match backoff.)
- **Hold interval (Escalated):** the recovery cooldown itself escalates binarily too — `ESCALATE_TTL = 15m → 30m → 60m → 2h → 4h → …` doubling on successive re-escalations, cap at **24h** (`GARBAGE_ESCALATE_TTL_CAP = 86400`), so a *repeatedly* re-broken pair gets held out progressively longer — the direct fix for the neuralwatt 101-strike pendulum. A chronically-garbling endpoint (strikes≥4 recurring each day for days) is held out for up to a full day before it gets another chance to re-prove itself.

**Three-tier multiplier** (all via `garbage_price_mult`, fail-open 1.0):

| State | Trigger | Multiplier | Exit |
|-------|---------|-----------|------|
| **Clean** | no strikes in `COOL_WINDOW` | 1.0 | strikes → Warning |
| **Warning** | `1 ≤ strikes < MAX` | `2^n` (existing pattern, base-2) | decay out of window → Clean; hits MAX → Escalated |
| **Escalated** | `strikes ≥ MAX` (4) in `ESCALATE_TRIGGER` | `max(inf, 2^n)` held for `ESCALATE_TTL` (default **15 min**, doubles on re-escalation up to cap) **or** until a successful re-probe | after TTL, require a **successful in-flight re-probe** (live 200 + non-garbage) before dropping to Clean; missing re-probe → stay Escalated (never a hard block) |

**Decay/re-probe/rescue semantics:**
- **Re-probe:** in `Escalated`, route a *cheap* probe completion to the pair (a real caller request already being routed there is sufficient — no synthetic traffic). On a clean 200, record a "recovery" and move to Clean after one clean response (confidence-1). On garbage, refresh the Escalated TTL. This is strictly **strike-count + freshness** driven — never a hard "block", so escalant still flows if it is the only lane (graceful degradation, matches L893).
- **Config knobs (new):** `GARBAGE_ESCALATE_TTL` (900s), `GARBAGE_REPROBE_WINDOW` (300s), `GARBAGE_RECOVERY_CLEAN_HITS` (1). Existing `PRICE_WINDOW` (3600s) stays as the Warning→Clean decay; Escalation adds a *second, longer memory*.
- **Fail-open:** every new code path wraps in try/except → returns current multiplier; any parse/state error returns the last-known value, never blocks routing, never raises (mirrors L405-429).
- **Persistence (G5):** optional append of `{ts, provider, model, state, strikes}` to the *existing* JSONL ledger (or a small sqlite table) so a restart can seed Escalated state from the last `ESCALATE_TTL` of ledger rows. Default off; env `GARBAGE_PERSIST_STATE=0`.

**Why no hard block:** the flat market needs the lane available as price-eligible; a hard demotion is precisely what `t_b7725426` was and what the operator rejected (PLAN L5). Escalation caps at `∞` effective-cost (sort to end) but never removes eligibility.

---

## 4. SSE / reasoning-model false-positive assessment (G6)

`inspect_response` L357-358 and `_extract_sse` L325 substitute `reasoning`/`reasoning_content` into `content` when `content` is empty — so **a CoT-heavy model (kimi/claude/deepseek-reasoner) that spends its budget in reasoning then returns a short *real* answer is judged on that answer, not the reasoning → low false-positive risk for `gibberish`/`repetition`/`empty`.** `gibberish` also self-skips structured/code/symbol-heavy and <120-word text (L236-256).

The one genuine risk vector: **reasoning-only, empty-answer responses** are *not* flagged empty (reasoning is substituted in), so an endpoint that returns only CoT with no actionable answer passes detection unflagged (miss, not false-positive). And a model that emits *degenerate looping reasoning* would be caught by `repetition` on the substituted text — arguably correct (that's garbage). Recommendation: keep substitution; add `empty`-or-`reasoning_only` as a soft class only when the *request* demanded an answer (e.g. non-`stream` plain chat) and `content==""` with `finish_reason=="length"` — but gate behind the deployed schedule, tuned from ledger, so we never penalize legit reasoning. **Bottom line: current detectors are conservative and false-positive-light; do not loosen thresholds before 24-48h of ledger data (PLAN L77).**

---

## 5. Pass-through vs optional in-flight retry (recommendation, operator-gated)

**Default stays pass-through** (operator chose it; changing delivery semantics is an operator call, not mine).
**Add an opt-in in-flight retry**, gated so it fails open and never degrades latency:
- Env `GARBAGE_INFLIGHT_RETRY=0` (default off). When on, in `_garbage_check`, IF (a) the request is **retryable** (non-`stream`, or stream with buffer not yet flushed for the *first* chunk), (b) the detected class is definitive (`empty`, `bad_json`, `mojibake` — high-confidence, not `gibberish`), AND (c) `select_provider` has a **cheaper/healthy alternate lane** for the model → retry once on the alternate lane before delivering; keep the original response as fallback and deliver it if the retry fails. Never retry `oversized` (legit long output). On any uncertainty or single-lane → deliver original (pass-through preserved).
- This gives the operator a knob to eliminate G4's wasted prompts on *definitive* failure without risking double-delivery: the end-200 original is always retained as the safe fallback.

---

## 6. Phased implementation plan (precedence order)

**Phase A — Cover the 3 gaps in detection (G2, G3).** *Owner: worker-routing (code) + worker-admin (config).*
1. Add `_garbage_check` to the success path of `_try_ollama_cloud` (just before `return True` L5197), `_try_telnyx`, and `_try_opencode_go` (pass `bytes(response_buffer)`).
2. Add a **keying-consistency test**: assert `_resolve_model_for_provider(name, m)` == the model string recorded by `_garbage_check` for every `(provider, model)` in `PROVIDER_MODELS` (normalize by lower()+canonicalize); if they diverge, unify the key via one helper.
- **Gate (TDD-first):** failing-first unit tests for both; `test_flat_router.py` + `test_garbage_pricing.py` green; live kill-switch untouched; atomic commit + dual-push hermes-bot + repo mirror; no secrets.

**Phase B — Recovery state machine (G1, G5).** *Owner: worker-routing (code) + worker-admin (wiring/config), cross-family cold review.*
3. Implement escalated tier + re-probe + optional persistence in `garbage_detector.py`; add env knobs (`GARBAGE_ESCALATE_TTL`, `GARBAGE_REPROBE_WINDOW`, `GARBAGE_RECOVERY_CLEAN_HITS`, `GARBAGE_PERSIST_STATE`).
- **Gate (TDD-first):** red→green tests: escalation at n≥4, re-probe clears on clean 200, refresh on garbage, fail-open, never-blocks on single lane, decay into long memory; docs-update-same-commit (PLAN-garbage-output-detection.md + flat-router SKILL.md stale-CB correction); atomic commit + pushes; no secret composition.

**Phase C — Optional in-flight retry (G4).** *Owner: worker-routing + operator sign-off.*
4. Ship default-off `GARBAGE_INFLIGHT_RETRY=0` with the conservative-class + alternate-lane + retained-fallback logic above.
- **Gate:** target-fault tests (each of the 4 call sites) with `_response_started` guard checked (no double status-line, per flat-router pitfall); happy-path E2E unchanged; operator flips knob in config only, not code.

**Quality gates every phase:** TDD failing-first; full `test_garbage_pricing.py` (41) + `test_flat_router.py` (109) + `test_garbage_circuit_breaker.py` green; docs in the same commit; atomic commit + dual-push (hermes-bot + merchant-routing-engine/${hermes-orchestration}); never compose secrets; cross-family cold review before merge (verify served model, not just routing decision).

---

## 7. Honest bottom line

**Already works well:** detection breadth ((a)), market price-bump wiring ((c)), fail-open everywhere. **Real gaps to fix:** the 1h flat-window oscillation for chronically-broken pairs ((d)), zero coverage on the 3 flat-rate lanes ((e)), keying-consistency unverified, and no operator-optional in-flight safety net. The hardening keeps everything market-based (price multiplier, never a hard cap), fails open, and adds an escalation memory so a 101-strike endpoint stops being re-admitted every hour.
