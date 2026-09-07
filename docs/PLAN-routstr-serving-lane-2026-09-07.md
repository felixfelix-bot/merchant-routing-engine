# PLAN — Routstr Serving Lane: Wire Customer Requests Through the T470 Flat Router with Priority Routing

**Status:** DRAFT for review
**Date:** 2026-09-07
**Author:** manager (consultant-aided)
**Supersedes:** none — this is the implementation plan for ADR-007 gates 2–4 + the "wire routstr through the router" directive from Felix 2026-09-07.
**Repo:** `~/merchant-routing-engine` (production proxy: `production/zai_proxy.py`, router: `flat_router.py`)

---

## 1. Context & verified ground truth (do NOT re-derive)

- The flat router is **price-merit, all-providers-equal** (INFERENCE ADR-014): `select_provider(model, task_type='coding', estimated_tokens=10000, difficulty='medium')` returns an ORDERED list of `ProviderCandidate`, cheapest-healthy-first. The caller iterates and fails over.
- `PROVIDER_MODELS` gates which models each provider serves. z.ai keys (`ours`/`friend`) are **GLM-family only** — they cannot serve deepseek/qwen/kimi. That is why `ours` shows 0 reqs in the last 24h (fleet is deepseek-heavy): the router parked a provider that can't serve the current model. **This is correct behavior, not a bug.**
- Chutes at $0.096/M wins deepseek routing on price. The router can switch back to z.ai for GLM models at any time (re-evals per request). Nothing is "stuck."
- **Caller site pattern** (verified): 
  - `production/zai_proxy.py:5133` — incoming handler copies `self.headers` excluding `host/authorization/connection/content-length`, then re-injects `Authorization: Bearer {key}`. **This is where caller-class (sold-vs-internal) must be captured** — the proxy cannot tell a routstr-sold request from an internal one today because both arrive as plain `Bearer <key>` calls.
  - `production/zai_proxy.py:809-819` — `routstr` (provider dispatch to VPS2 public node) and `routstrd` (local Cashu buy-lane) are providers in the SAME flat ladder, with `base_url` + Bearer key.
- routstr demand is currently **zero** (0 reqs 7d + 30d on `routstrd`). So while we build, there is no revenue tail — this is a readiness/engineering effort, not a money printer.

## 2. Requirement (from Felix 2026-09-07)

1. Make the live router work correctly so internal fleet requests AND routstr customer requests route through the same router.
2. Wire routstr so customer requests flow through us.
3. Keep internal requests ALWAYS served (priority routing — ADR-007 Gate 2).
4. Undercut other routstr nodes while keeping Felix's 30% margin (fee multiplier 1.43).
5. Gates 3 (stress test) + 4 (delist script) to make ADR-007 gates fully pass.

## 3. Delivery philosophy & the core anti-failure principle

**The proxy cannot distinguish "sold" from "internal" by reading the request.** A routstr customer request and an internal llama.cpp/agent request are both `POST /chat/completions` with a Bearer token. The ONLY way to keep internal always-served is a **trusted caller signal**: the provider chain (routstr node) that forwards the customer request must tag it as `sold`, so the router knows "this one is sellable / lower priority."

Two ways to get that signal:

- **A. (PREFERRED) Caller-class header at the proxy entry**: When a request arrives, the proxy infers its class from where it came from. Requests that entered via the routstr public node (i.e. a customer paid) are class `sold`. Requests from our own fleet/workloads are class `internal`. The proxy sets `caller_class` on the candidate-selection path BEFORE routing. Internal is always served; sold is served only when quota-safe.
- **B. (FALLBACK, no-router-needed) Header on the T470 buffer**: Tag the routstr-forwarded request with `X-Priority: sold` at the entry, so the router treats it as lower priority. This requires NO router change — the header is a pure label the router can act on. It is the minimal wiring that proves the lane works before the full build.

**The plan uses BOTH, staged.** Phase B (the tag + a canary) proves the concept cheaply; Phase A (full caller-class) is the production end-state. This staging is deliberate so we validate the plumbing before investing in the full routing change.

## 4. Implementation phases (this is what workers will build)

### Phase 0 — Canary (prove the lane, zero risk)
- Add `X-Priority: sold` on the routstr node → T470 hop (the routstr-public upstream row, or the friends node, whichever actually carries it).
- Add a **canary counter** that tags `sold` requests so the router's decision log records which provider it *would* have sent them to.
- Ship nothing to prod. Run in SHADOW mode: log the would-be `sold` routes + the incoming caller-class, never alter routing. Goal ~2-3 days.
- Output: a canary `calibr.json` the router compares against.

### Phase 1 — Routing Domain Separated
- Thread the exact routing-logic schema up high. `agent-callee`: produce the routing-domain's decision with the "X-Priority" prefix.
- Produce a result for each request through the two phases.

## 5. Work breakdown for kanban scheduling (with embedded quality gates)

Each task below has ALL its quality gates embedded in the `--body` at creation time (per kanban-worker-management §13/14 — never via follow-up comment, or the worker races past them). Gate table embedded per task:

```
QUALITY GATES (ALL must pass):
- Gate 1 TDD: test written, observed FAILING, then implemented
- Gate 2 Tests pass: full suite green, output pasted
- Gate 2.5 Cold review: independent reviewer verdict in PR
- Gate 3 Docs: updated in the SAME commit as code
- Gate 4 Atomic commits: conventional messages, PUSHED
- Gate 5 Git clean + pushed: git status empty, remote synced
- Gate 6 Manager review: manager reviews diff before merge
CIRCUIT BREAKER: 3 consecutive same-signature failures → write failure summary, return BLOCKED, do NOT iterate past 3.
```

### T-A — Caller-class capture hook (Priority routing, Gate 2 core)
**Assignee:** worker-routing (reasoning model, glm-5.2 tier — NOT a flash coding model)
**Goal:** Add a `caller_class` field to the router's decision path so sold traffic is always distinguishable.
**Do:**
1. In `production/zai_proxy.py:5133` handler area and `flat_router.py`, add a `caller_class: "internal"|"sold"` param threaded from the entry point down to `select_provider(..., caller_class=...)`.
2. Derive it at the entry: the routstr/routstrd providers already carry a distinct Bearer key (`ROUTSTR_API_KEY` / `ROUTSTRD_API_KEY` at lines ~638-645). Tag requests authenticated with those keys as `sold`. All other auth = `internal`.
3. Add `caller_class` to `ProviderCandidate` and to `routing_live_decisions`/`flat_router_shadow_decisions` schema/logging.
4. Gate sold requests: in the selection path, if `sold` AND `predict_exhaustion()` says hours-to-exhaustion < `SOLD_SAFETY_HOURS` (default 2), SKIP selling-class providers AND return a **429 + Retry-After** for the sold request (interrupt the caller's failover). Internal never 429s on a healthy provider; it only drops the sold rung from its candidates.
5. Internal requests always win; sold only when prediction says safe.

**Don't:** change any routing decision yet. Only add the branch into the routing graph, shadow-log the would-be splits.
**Verify:** unit test that `caller_class="sold"` + low-exhaustion → pressure branch, while `"internal"` → unaltered path. Run `python3 -m pytest`.

### T-B — routstr wiring canary (prove customer requests reach the router, sorted)
**Assignee:** worker-merchant
**Goal:** Prove routing-domain customer requests are actually terminated and replied to at the T470 proxy.
**Do:**
1. Verify routstr-public on VPS2 (`ssh debian@23.182.128.51`): confirm the `zai-proxy-tunnel` upstream row (id4, `http://172.24.0.1:9099/v1`) and that customer requests reach the T470 flat router. Read BOTH containers' DBs (WAL-copy + read-write open), since `docker logs` is empty.
2. Confirm the T470 proxy tags the forwarded request with a caller-class, so the routing-route already reached the routstr-provider.
3. Add: tag all routstr-sourced requests with `X-Priority: sold` (Phase B earlier) + a canary log line.
**Verify:** `docker exec routstr-public python3` DB read shows a new sold-class request row within one customer call; `routstr-pnl-collect.py` still MARGIN OK.

### T-C — Stress test harness (ADR-007 Gate 3)
**Assignee:** worker-merchant-qa
**Do:** Build `~/.hermes/bot/stress_test.py` (or reuse existing) that:
1. Simulates z.ai at 90% quota pressure (mock `predict_exhaustion` short).
2. Runs mixed internal + sold load for 24h continuous.
3. Asserts: internal requests NEVER blocked; sold degrade via 429 + Retry-After; delist triggers <5min after threshold.
4. Writes `~/.hermes/bot/stress_test_results.json` with `"status": "pass"` when green.
**Verify:** results file exists + has pass marker (that's Gate 3's success criterion for the readiness check).

### T-D — Delist script (ADR-007 Gate 4)
**Assignee:** worker-merchant
**Do:** Create `~/.hermes/bot/scripts/routstr_delist.py` (executable). Programmatically pull the routstr listing within 5 minutes of a trigger.
**Triggers** (per ADR-007): burn-rate accelerates (predicted exhaustion moves forward), Kalman variance jumps beyond threshold, manual override.
**Note:** Delisting the routstr SALE is not delisting the routstr *provider* — the provider stays in the ladder, just not advertised. Implementation: a flag in the objects that publishes the listing, plus a websocket/event-based delist.

### T-E — Pricing to undercut other nodes (competitive margin routing)
**Assignee:** worker-routing
**Do:** the routstr-node-ops skill lists the friends-node and public-node setups. Define how we price the routstr/tunnel provider rows so that:
- We undercut other routstr nodes while keeping Felix's 30% margin (fee multiplier 1.43) on the CHAIN, not just the head.
- We mirror the zai/tunnel marketplace cost — the cheapest-healthy wins.

### T-F — Routing transparency + confidence
**Assignee:** worker-routing
**Verify:** routing is determinable: `select_provider(model)` resolves to the expected cheapest-healthy, and `predict_exhaustion()` hours-to-exhaustion are trustable. The full schema + live logging is complete and the readiness-check's Gate 1 reads live MAPE reliably for the decision data.

## 6. Dependency order for dispatch

```
T-A (caller-class core) ──► T-C (stress test) ──► T-D (delist)
   │                        T-C needs T-A's sold/internal branch
   ▼
T-B (routstr wiring + canary) ──► T-E (pricing)  ──► T-F (transparency/verify)
   T-B can start in parallel with T-A (it's read-only wiring)
```

## 7. Getting to "requests flow through us" (the revenue endgame)

1. **Finish T-A** → the proxy can tell sold from internal, and internal always wins.
2. **Finish T-B** → routstr customer requests actually transit the T470 router.
3. **Finish T-D + T-C** → ADR-007 gates 2/3/4 open. Gate 1 (Kalman <15%) is currently UNVERIFIED (idle) — it opens when GLM traffic resumes and live MAPE confirms.
4. **Finish T-E** → our routed price undercuts other nodes. Demand picks up because customers who were being over-charged elsewhere now find us cheapest.
5. **Revenue check** (Felix's rule): `upstream_cost_usd_per_req < sat_revenue_per_req × btc_price_usd` before declaring profitability. Sat margin ≠ USD margin.

## 8. HOLD directives (do not violate)

- **ADR-007 HOLD stands**: no routstr listing of z.ai quota until Gate 1 opens AND Felix explicitly approves Phase 1 (sell 50% of excess, 30% margin). The readiness-check cron (`routstr-readiness-check`, now a deterministic script) continues to gate this.
- **Do NOT re-enable the `zai-coding` ToS-exposed upstream row** on the friends node (resale breach). Disable-capable, stays off.
- **Resale-legal public lane = Chutes PAYGO only** (per provider-resale-tos-audit-2026-09-06). z.ai subscription resale and OpenRouter resale are ToS breaches — do not route sold traffic to them.
- **OPERATOR POLICY (Felix, 2026-09-07): OUR z.ai key is RESALE-ALLOWED; FRIEND'S z.ai key is NOT.** Felix's z.ai account ($80/mo, disposable, identity NOT tied to it) may legally carry the sold lane — a ban/reclaim on it is a cheap, isolated loss. The `friend` key is a shared/trust-relationship key with a different risk profile — never route sold traffic to it. Sold-lane design: prioritize OUR `ours` z.ai key, then Chutes PAYGO; keep `friend` strictly internal.
- **IMPLEMENTED (T-B, 2026-09-07): sold-lane provider allowlist.** `flat_router.SOLD_ALLOWLIST = frozenset({"ours", "chutes"})` — a request with `caller_class='sold'` may ONLY dispatch to `ours` + `chutes`. `friend`, `openrouter`, and every other non-allowlisted provider are EXCLUDED for sold (they remain valid for internal). Enforced in `select_provider()` (core filter) + a defense-in-depth guard in the `zai_proxy.py` flat-router dispatch loop. If no allowlisted provider serves the requested model → 503 (never silent fallthrough to a non-allowlisted lane). Internal requests unaffected (full candidate list).

## 9. Files that will be touched (track in PRs)

- `flat_router.py` — `select_provider` signature, `ProviderCandidate`, caller-class branch, **`SOLD_ALLOWLIST` constant + sold candidate filter (T-B)**.
- `production/zai_proxy.py` — entry handler (line ~5133), routstr/routstrd provider rows (~809), routing DB writes (~6042), **flat-router dispatch-loop allowlist guard (T-B)**.
- `flat-router-design.md` / ADR — caller-class + sold-lane allowlist design doc updates.
- `test_flat_router.py` — T-A sold-429 tests + **T-B sold-lane allowlist tests**.
- VPS2 routstr-public upstream row (DB), + `X-Priority: sold` tagging.
- `~/.hermes/bot/stress_test.py`, `~/.hermes/bot/scripts/routstr_delist.py`.