# Sovereign Engineering — System Overview

**Purpose:** Explain how the entire Hermes-based autonomous AI engineering
system interlocks — from quality gates at the top, through kanban dispatch,
down to the LLM proxy that makes real-time routing decisions.

**Audience:** New team members, interested friends, anyone who wants to
understand how Felix's Hermes setup works end-to-end.

---

## 1. The Big Picture

```
┌─────────────────────────────────────────────────────────────────┐
│                    FELIX (Operator)                              │
│         Signal / Matrix / CLI — gives directions                  │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│              HERMES MANAGER PROFILE                               │
│  (Orchestrator — makes decisions, delegates work)                │
│                                                                  │
│  SOUL.md — unbreakable principles (push, delegate, quota-gate)  │
│  MEMORY — durable facts across sessions                          │
│  SKILLS — procedural knowledge (quality-gates, dispatch, etc.)  │
│  CRON — 85+ scheduled jobs (monitors, watchers, auto-healers)   │
└──────┬───────────────┬───────────────┬──────────────────────────┘
       │               │               │
       ▼               ▼               ▼
┌──────────┐  ┌──────────────┐  ┌──────────────────┐
│ KANBAN   │  │ DELEGATE     │  │ CRON JOBS        │
│ BOARDS   │  │ TASK         │  │ (script + LLM)   │
│ (SQLite) │  │ (subagents)  │  │                  │
└────┬─────┘  └──────┬───────┘  └────────┬─────────┘
     │               │                   │
     ▼               ▼                   ▼
┌─────────────────────────────────────────────────────────────────┐
│              WORKER PROFILES (12 profiles)                       │
│  worker-balloon, worker-pcb, worker-fips, worker-tollgate,      │
│  worker-plebeian, worker-admin, worker-inspector, etc.           │
│                                                                  │
│  Each has: config.yaml (model, timeout), SOUL.md, skills         │
│  Quality Gates force-loaded into every worker                     │
│  Worktree isolation — each worker gets its own git worktree      │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│              ZAI PROXY (localhost:9099)                          │
│  Local reverse proxy for all LLM API calls                       │
│                                                                  │
│  Key rotation (ours + friend z.ai keys)                          │
│  Ollama Cloud fallback (kimi-k2.7-code, kimi-k3:cloud)          │
│  Shadow mode price-first optimizer (read-only alongside live)    │
│  Kalman filter quota prediction (will_exhaust)                  │
│  Per-model pricing (measured, amortized, estimated rates)        │
│  Quota pressure (exponential cost multiplier as quota fills)     │
│  Routing decision logging (SQLite — zai_usage.db)                │
│  Dispatch gate endpoint (/v1/dispatch_gate)                     │
│  Manual key disable (.key_disabled_ours flag file)               │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│              EXTERNAL API PROVIDERS                              │
│  z.ai (ours) — glm-5.2, glm-4.5-flash, glm-4.5-air              │
│  z.ai (friend) — same models, separate quota                    │
│  Ollama Cloud — kimi-k2.7-code, kimi-k3:cloud (free tier)       │
│  OpenRouter — $5 remaining (pay-per-use, last resort)           │
│  PPQ — dead (key invalid)                                        │
│  DeepInfra — dead (401 unauthorized)                             │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. Quality Gates — 7 Mandatory Gates

Every worker MUST pass all 7 gates before a task is considered done.
The `quality-gates` skill is force-loaded into every worker profile.

| Gate | Name | What It Checks |
|------|------|----------------|
| 1 | TEST-FIRST (TDD) | Failing test written BEFORE implementation |
| 2 | ALL TESTS PASS | Full test suite run, zero failures, ≥80% coverage |
| 2.5 | COLD REVIEW | Cross-family LLM reviews diff with zero context (GLM→Kimi, Kimi→GLM) |
| 3 | DOCS UPDATED | Documentation in same commit as code changes |
| 4 | ATOMIC COMMITS | One concern per commit, conventional messages |
| 5 | PUSH TO REMOTE | `git push` succeeded, working tree clean |
| 5.5 | CI PASSES | For CI/CD changes — all GitHub Actions workflows green |
| 6 | MANAGER VALIDATION | Worker sets status to `review`, NOT `done` — manager approves |
| 7 | DEEP ADVERSARIAL | Opt-in only — adversarial prompting via script |

**Cross-family review (Gate 2.5):** A GLM worker gets reviewed by a Kimi
model, and vice versa. Different LLM architectures have different blind
spots. Same-family review is an echo chamber — banned.

**Work that isn't pushed isn't done.** This is the unbreakable law.
A kanban task is not complete until `git status` shows clean AND
`git push` succeeded.

---

## 3. Kanban System — Task Lifecycle

### Architecture

- **SQLite-backed** — durable, shared across profiles
- **Board-scoped** — each project has its own board (merchant-routing,
  tollgate, microfips, balloon, plebeian, etc.)
- **Gateway-integrated** — dispatch runs inside the Hermes gateway process
  (not a standalone daemon). Polls every 60 seconds.

### Task States

```
ready → running → review (blocked) → done (manager approves)
                ↘ blocked (circuit breaker tripped)
                ↘ blocked (dependency not met)
```

### Task Metadata

Each kanban task carries:
- `title` — what to do
- `body` — full task spec including quality gate requirements
- `assignee` — which worker profile handles it
- `priority` — 0 (critical) to 3 (low)
- `status` — ready, running, blocked, review, done
- `model_tier` — heavy (glm-5.2), mid (glm-4.5), air, flash
- `urgency` — urgent, normal, low
- Dependencies — parent/child links (parent must complete first)

### What Goes In a Task Body

Quality gate requirements go IN THE BODY at creation time, never as
follow-up comments (workers complete before comments arrive). A well-formed
task body includes:

1. **Task description** — what to build/fix
2. **Quality gate checklist** — which gates apply, which are exempt
3. **Circuit breaker instructions** — "if same test fails 3×, stop and BLOCK"
4. **Git instructions** — exact commit/push commands
5. **Verification requirements** — specific files that must exist
6. **Model routing** — which model to use if not the profile default

### Dispatch Gating (3-Layer Defense)

Before any worker spawns:

1. **Resource Gate** (HARDEST BLOCK) — RAM < 2500MB? Swap > 8GB? CPU > 3.0?
   Active workers ≥ 2? → BLOCK
2. **Quota Gate** — Both z.ai keys exhausted? → BLOCK
3. **Freeze Marker** — `~/.hermes/bot/.dispatch_frozen` exists? → BLOCK

### Quota-Aware Model Tiering

Target distribution: 10% heavy (glm-5.2), 80% mid (glm-4.5), 10% economy
(flash/air). Dynamic thresholds auto-adjust weekly from Kalman data.

| Urgency | Behavior |
|---------|----------|
| urgent | Always dispatched, peak cap removed |
| normal | Standard rules, blocked in CRITICAL quota |
| low | Only dispatched in PLENTYFUL state, queued otherwise |

### Circuit Breaker

After 3 consecutive failures with the same error signature:
- Worker ABORTS
- Writes structured failure summary
- Returns BLOCKED status
- Manager re-evaluates (new approach, different model, or split task)

---

## 4. Worker Profiles (12 profiles, 4 tiers)

| Tier | Model | Workers | Use Case |
|------|-------|---------|----------|
| Spatial | kimi-k2.7-code | worker-layout | PCB, KiCad, visual/spatial |
| Reasoning | glm-5.2 | manager, worker-fips, worker-tollgate, worker-continuum | Architecture, complex integration |
| Code | kimi-k2.7-code | worker-balloon, worker-plebeian, worker-admin, worker-inspector | Firmware, CLI tools, refactoring |
| Fast | glm-4.5-flash | worker-dq05, worker-base | File lookups, formatting, simple tasks |

**Key lesson:** Implementation tasks (JNI, firmware, protocol work) MUST use
kimi-k2.7-code or glm-5.2 — NEVER auto-downgrade to flash/air. Flash models
produce plausible-looking broken code.

**Worker isolation:** Each worker gets its own git worktree (never in /tmp —
that's tmpfs, cleared on reboot). Workers push to feature branches, never
touch main directly. Manager handles merge/rebase choreography.

---

## 5. ZAI Proxy — The Routing Brain

### What It Does

The proxy sits between Hermes and all external LLM APIs. Every LLM call
goes through it. It makes real-time routing decisions based on:

1. **Key health** — which z.ai key has quota remaining
2. **Quota pressure** — exponential cost multiplier as quota fills
3. **Per-model pricing** — measured rates from real billing data
4. **Kalman filter prediction** — will quota last through this task?
5. **Shadow mode** — price-first optimizer runs read-only alongside live routing

### Key Rotation

Two z.ai API keys ("ours" and "friend"), each with separate quota windows
(5-hour, weekly, monthly). The proxy:
1. Tries the healthiest key first
2. On 429 (quota exhausted), retries the other key
3. If both exhausted, falls through to Ollama Cloud (free tier)
4. If Ollama also fails, falls to OpenRouter (paid, last resort)

Manual key disable: `touch ~/.hermes/bot/.key_disabled_ours`
Re-enable: `rm ~/.hermes/bot/.key_disabled_ours`

### Per-Model Pricing

Three rate sources, in order of accuracy:

| Source | How Calculated | Accuracy |
|--------|----------------|----------|
| MEASURED | Real billing API data — actual $/tokens from provider | Best |
| AMORTIZED | Total spend / total tokens over a window | Good |
| ESTIMATED | Cost basis × usage ratio (for providers without billing API) | Approximate |

### Quota Pressure (Exponential)

As quota fills, the effective price increases exponentially:

```
effective_price = base_price × (1 + K × t / (1 - t))
```

Where `t` = quota used % and `K` = pressure constant. This makes the
optimizer prefer cheaper providers as quota fills, naturally throttling
spend. The asymptote at t→1 means price goes to infinity at 100% quota.

### Shadow Mode

The price-first optimizer runs in "shadow mode" — it computes what it
WOULD route to, but doesn't actually change the live routing. Its decisions
are logged alongside the real routing decisions for comparison and
validation before going live.

### Dispatch Gate Endpoint

`GET /v1/dispatch_gate?estimated_tokens=200000&task_type=coding`

Returns whether a task can be dispatched based on:
- Remaining quota headroom (2× safety margin)
- Kalman prediction of hours until exhaustion
- Recommended model (with downgrade if needed)
- Predicted cost

### Usage Logging

SQLite database at `~/.hermes/bot/zai_usage.db` (WAL mode):
- `api_calls` — one row per request (tokens, model, key, status, duration)
- `key_decisions` — one row per key-selection decision
- `routing_decisions` — recent routing decisions for dashboard display

---

## 6. CVM Server — ContextVM Dashboard

A TypeScript server (`demo/cvm-server/src/cvm-server.ts`) that:
- Publishes snapshots to Nostr via kind 25910 events (NIP-59 gift wrap)
- Serves HTTP on localhost:3000
- Exposes 5 tools: get_snapshot, send_prompt, register_participant,
  get_price_history, get_ledger
- Dashboard fetches from both Nostr (decentralized) and HTTP (local fallback)

The dashboard (`demo/display-deploy/index.html`) shows:
- Provider status (z.ai, Ollama, OpenRouter, PPQ, DeepInfra)
- Per-model pricing charts (log-scale Y, per-model not per-provider)
- Quota bars (5h, weekly, monthly windows)
- Request flow (last 20 routing decisions, filtered to real requests only)
- Cost meter (margin %, $/hour burn rate, requests today)
- SATs/USD toggle (BTC price = $100,000)
- Dispatch gate status

---

## 7. Cron Jobs (85+ scheduled tasks)

### Categories

| Category | Count | Examples |
|----------|-------|---------|
| System monitors | 15 | disk-watch, zai-watch, proxy-watchdog, session-health |
| Kanban/dispatch | 8 | auto-assigner, stale-resetter, worker-audit, dispatch-health |
| Kalman | 4 | data-collect, retune, push-dq05, dashboard-refresh |
| FIPS/mesh | 6 | auto-heal, health-check, interop-test, serial-logger |
| VPS/infra | 5 | health-check, daily-maintenance, watchdog, disk-cleanup |
| Balloon | 3 | board-access-monitor, discovery-sync, sub-manager-pulse |
| Nostr | 4 | kanban-sync, kanban-inbound, blossom-wot-cleanup, wiki-health |
| Pricing | 1 | pricing-health-monitor (every 10m) |

### Quota Gate (5-Layer Model)

All LLM-driven crons start with:
```
QUOTA GATE: Run 'python3 ~/.hermes/profiles/manager/scripts/zai-quota-gate.py'
— if exit non-zero, skip and exit silently.
```

| Layer | What It Catches | Status |
|-------|----------------|--------|
| 1. Cron prompt gates | All scheduled LLM work | ✅ 27/27 covered |
| 2. SOUL.md principle | Future sessions, new crons | ✅ Active |
| 3. Skill documentation | Agents loading this skill | ✅ Active |
| 4. Dispatch daemon gate | All worker spawns | ✅ Implemented |
| 4.5. delegate_task gate | Subagent dispatch | ⚠️ Manual check needed |
| 4.6. Ollama model check | kimi-* model availability | ⚠️ Manual check needed |
| 4.7. Kalman dispatch gate | Proxy endpoint | 📋 Approved, not implemented |
| 5. Proxy 503 | Everything downstream | ✅ Active |
| 6. Cron plugin | All crons automatically | 📋 Proposed |

---

## 8. What's Implemented vs Planned

### ✅ IMPLEMENTED (Live in Production)

- ZAI proxy with key rotation (ours + friend)
- Ollama Cloud fallback (kimi-k2.7-code, kimi-k3:cloud)
- Shadow mode price-first optimizer (read-only)
- Per-model pricing (measured, amortized, estimated)
- Quota pressure (exponential cost multiplier)
- Kalman filter quota prediction
- Usage logging (SQLite, WAL mode)
- Manual key disable/enable
- CVM server (Nostr + HTTP)
- Dashboard with real-time updates
- SATs/USD toggle
- Per-model consumer chart (ADR-001 locked in)
- Request flow (filtered to real requests only)
- Quota bars (5h, weekly, monthly)
- 7 quality gates (including cross-family cold review)
- 12 worker profiles (4 tiers)
- Kanban dispatch (gateway-integrated, 60s poll)
- 3-layer dispatch gating (resource + quota + freeze)
- Circuit breaker (3-strike rule)
- 85+ cron jobs (27/27 LLM crons quota-gated)
- Adaptive model tiering (10/80/10 target)
- Weekly threshold auto-tuning
- Board access mutex (hard device lock for shared hardware)
- Git dual-push (GitHub + ngit/nostr)

### 📋 PLANNED (Approved, Not Yet Implemented)

- Kalman dispatch gate endpoint (`/v1/dispatch_gate`) — proxy endpoint
  that uses live Kalman state to predict task feasibility
- Cron scheduler plugin — Hermes plugin that auto-gates ALL crons
- delegate_task quota gate — automatic pre-dispatch check for subagents
- Live routing (currently shadow mode only) — kill switch file
  `.enable_live_routing` must exist for optimizer to control routing
- Balance-delta per-call cost extraction for PPQ and OpenRouter
  (currently cost_usd=0 for these providers — API doesn't return
  per-call cost in response body)
- Parallel burn script (currently sequential — one model at a time)

### 📋 DOCUMENTED BUT NOT BUILT

- TollGate marketplace integration (Cashu payments for API access)
- Merchant module complete master plan (see docs/merchant-module-*.md)
- Routster marketplace intelligence (ADR-007)
- Demand Kalman + margin layer (Phase 5 — on github/master branch)

---

## 9. Repositories and Branches

### merchant-routing-engine (PRIMARY)

The main repo. Contains the proxy code, pricing engine, dashboard, CVM
server, and all ADRs.

**Remotes:**
- GitHub: https://github.com/felixfelix-bot/merchant-routing-engine
- ngit: nostr://npub1xtzgnzzu88yfv9es3evykl3ympjz0gc3umy2e6rs3jazruhjyevqe63edh/relay.ngit.dev/merchant-routing-engine

**Branches:**

| Branch | Tip | Description |
|--------|-----|-------------|
| `converged-rate-replay` | `95cd141` | **ACTIVE** — dashboard fixes, SATs, per-model charts, request flow filter |
| `master` | `b5a876c` | Stable — pace_windows integration test fixes |
| `fix/live-router-none-on-failover` | `f882812` | Fix: prevent (None,None) return on failover |
| `range-tests` | `b772a77` | Phase 2 execution schedule (advisor mode) |
| `review/phase2-findings` | `648d75a` | Phase 2 code review findings |

**GitHub links:**
- Active branch: https://github.com/felixfelix-bot/merchant-routing-engine/tree/converged-rate-replay
- Master: https://github.com/felixfelix-bot/merchant-routing-engine/tree/master

**ADRs (Architecture Decision Records):**

| ADR | Title | Status |
|-----|-------|--------|
| ADR-001 | Price-first routing | Accepted |
| ADR-002 | Multi-Kalman separation | Accepted |
| ADR-003 | Deterministic peak multiplier | Accepted |
| ADR-004 | Effective price positivity | Accepted |
| ADR-005 | Three-layer actor separation | Accepted |
| ADR-006 | Shadow mode validation | Accepted |
| ADR-007 | Routster marketplace intelligence | Proposed |
| ADR-008 | Deterministic multipliers outside Kalman | Accepted |
| ADR-001 (consumer) | Consumer chart shows price per model | Accepted |

### balloon-fresh (HARDWARE)

ESP32 + RP2040 balloon tracking firmware, PCB design, mesh networking.

**Remote:** ngit (nostr-based git)
**Active branch:** `autonomous/mesh-baseline`
**Worktree:** `~/repos/balloon-fresh`

### tollgate-infrastructure-kit (INFRA)

VPS deployment, Ansible playbooks, Cashu mint, Blossom server, Nostr relays.

**Remote:** ngit
**Active branch:** `main`
**Path:** `~/tollgate-infrastructure-kit`

---

## 10. Key Files

| File | Purpose |
|------|---------|
| `~/.hermes/bot/zai_proxy.py` | Production proxy (source of truth) |
| `~/.hermes/bot/zai_usage.db` | Usage logging database |
| `~/.hermes/bot/zai_state.json` | Quota state cache |
| `~/.hermes/bot/.key_disabled_ours` | Manual key disable flag |
| `~/.hermes/bot/.enable_live_routing` | Live routing kill switch |
| `~/.hermes/bot/model_tier_thresholds.json` | Dynamic tier thresholds |
| `~/.hermes/bot/kalman_price_state.json` | Kalman filter state |
| `~/.hermes/bot/real_rates_export.json` | Measured rates export |
| `~/merchant-routing-engine/demo/display-deploy/index.html` | Dashboard (deployed) |
| `~/merchant-routing-engine/demo/cvm-server/src/cvm-server.ts` | CVM server |
| `~/merchant-routing-engine/docs/` | All ADRs, plans, reports |
| `~/.hermes/profiles/manager/scripts/` | Cron scripts (85+) |
| `~/.hermes/profiles/manager/skills/` | Skill definitions |
| `~/.hermes/profiles/manager/SOUL.md` | Unbreakable principles |

---

## 11. How It All Interlocks

```
Operator says "build X"
        │
        ▼
Manager creates kanban tasks (with quality gates in body)
        │
        ▼
Dispatch daemon checks: resource gate → quota gate → freeze marker
        │
        ▼
Worker profile spawned (model selected by tier + urgency)
        │
        ▼
Worker makes LLM API calls → through zai_proxy
        │
        ▼
Proxy routes to best available key/provider
(based on quota, price, Kalman prediction)
        │
        ▼
Worker writes code, runs tests (Gate 1-2)
        │
        ▼
Cross-family cold review (Gate 2.5 — different LLM family)
        │
        ▼
Worker commits + pushes (Gate 4-5)
        │
        ▼
Worker sets status to "review" (Gate 6)
        │
        ▼
Manager reviews, approves → task "done"
        │
        ▼
Manager merges feature branch → main → pushes
```

Every step is gated. Every decision is logged. Every artifact is pushed.
That's sovereign engineering.