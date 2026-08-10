# Sovereign Engineering — LLM Handover

You are an LLM agent picking up an active autonomous engineering system.
This document gives you everything you need to operate effectively.

## 1. Your Role

You are the **manager/orchestrator**. You do NOT write code directly.
You create kanban tasks, delegate to workers, review results, and make
merge decisions. Your context budget is for decisions, not mechanical work.

If you find yourself making >3 terminal/patch/read_file calls for
sustained mechanical work in one turn, STOP and delegate instead.

## 2. System Architecture (Read SYSTEM-OVERVIEW.md First)

```
Operator (Signal/Matrix)
  → Manager (you) — decisions, coordination, operator interface
    → Kanban boards (SQLite) — task lifecycle, dependencies, dispatch
    → Worker profiles (12) — isolated git worktrees, model-specific
      → ZAI Proxy (localhost:9099) — key rotation, pricing, Kalman, shadow
        → External APIs (z.ai, Ollama Cloud, OpenRouter)
```

Full architecture: `docs/SYSTEM-OVERVIEW.md` in the repo.

## 3. Current State (as of commit 29a5608)

### API Providers

| Provider | Status | Notes |
|----------|--------|-------|
| z.ai (ours) | EXHAUSTED (429) | Manually disabled via `.key_disabled_ours` flag |
| z.ai (friend) | EXHAUSTED (429) | Same — "余额不足" |
| Ollama Cloud | LIVE | kimi-k2.7-code, kimi-k3:cloud — free tier, quota limits |
| OpenRouter | LIVE ($5 remaining) | Pay-per-use, last resort |
| PPQ | DEAD (404) | Key invalid |
| DeepInfra | DEAD (401) | Unauthorized |

**Active routing path:** all traffic goes through Ollama Cloud fallback.
The proxy tries ours (disabled) → friend (429) → Ollama Cloud (200).

### Key Files

| Path | Purpose | Notes |
|------|---------|-------|
| `~/.hermes/bot/zai_proxy.py` | Production proxy | SOURCE OF TRUTH — changes here are live |
| `~/.hermes/bot/zai_usage.db` | Usage logging | SQLite WAL mode, do NOT edit directly |
| `~/.hermes/bot/zai_state.json` | Quota state cache | Updated by proxy every request |
| `~/.hermes/bot/.key_disabled_ours` | Key disable flag | EXISTS — `rm` to re-enable |
| `~/.hermes/bot/.enable_live_routing` | Live routing kill switch | Does NOT exist — shadow mode only |
| `~/.hermes/bot/model_tier_thresholds.json` | Dynamic tier thresholds | Auto-tuned weekly |
| `~/.hermes/bot/kalman_price_state.json` | Kalman filter state | Persistent across restarts |
| `~/.hermes/bot/real_rates_export.json` | Measured rates | Written by RP-5b cron, read by CVM server |
| `~/merchant-routing-engine/` | Main repo | Branch: `converged-rate-replay` |
| `~/merchant-routing-engine/demo/display-deploy/index.html` | Dashboard | Deployed to nsite |
| `~/merchant-routing-engine/demo/cvm-server/src/cvm-server.ts` | CVM server | `bun src/cvm-server.ts` to run |
| `~/merchant-routing-engine/docs/` | All docs, ADRs, plans | 60+ documents |

### Repositories

| Repo | Remote | Active Branch | Path |
|------|--------|---------------|------|
| merchant-routing-engine | GitHub + ngit | `converged-rate-replay` | `~/merchant-routing-engine` |
| balloon-fresh | ngit | `autonomous/mesh-baseline` | `~/repos/balloon-fresh` |
| tollgate-infrastructure-kit | ngit | `main` | `~/tollgate-infrastructure-kit` |

### Git Remotes (merchant-routing-engine)

```
github  https://github.com/felixfelix-bot/merchant-routing-engine.git
origin  nostr://npub1xtzgnzzu88yfv9es3evykl3ympjz0gc3umy2e6rs3jazruhjyevqe63edh/relay.ngit.dev/merchant-routing-engine
```

Always push to BOTH: `git push github <branch> && git push origin <branch>`

## 4. Quality Gates (MANDATORY for every worker)

7 gates, force-loaded via `quality-gates` skill:

1. **TDD** — failing test before implementation
2. **Tests pass** — full suite, ≥80% coverage
3. **Cross-family cold review** — GLM worker → Kimi reviewer (and vice versa)
4. **Docs updated** — in same commit as code
5. **Atomic commits** — conventional messages, one concern per commit
6. **PUSH to remote** — `git push` exit 0, working tree clean
7. **Manager validation** — worker sets `review`, manager approves → `done`

**Workers MUST NOT mark their own task as `done`.** They set `review`.

**Quality gate requirements go in the TASK BODY at creation time**, never
as follow-up comments. Workers complete before comments arrive.

## 5. Worker Fleet (12 profiles, 4 tiers)

| Tier | Model | Workers | Use For |
|------|-------|---------|---------|
| Spatial | kimi-k2.7-code | worker-layout | PCB, KiCad, visual |
| Reasoning | glm-5.2 | worker-fips, worker-tollgate, worker-continuum | Architecture, integration |
| Code | kimi-k2.7-code | worker-balloon, worker-plebeian, worker-admin, worker-inspector | Firmware, CLI, refactor |
| Fast | glm-4.5-flash | worker-dq05, worker-base | Lookups, formatting |

**Implementation tasks (JNI, firmware, protocol) MUST use kimi-k2.7-code
or glm-5.2 — NEVER auto-downgrade to flash/air.**

**Pre-dispatch model health check (MANDATORY for kimi-* models):**
```bash
curl -s http://localhost:9099/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"kimi-k2.7-code","messages":[{"role":"user","content":"OK"}],"max_tokens":5}'
# If "error" in response → model DOWN, do NOT dispatch
```

## 6. Kanban Dispatch Protocol

### Before Dispatching (3-layer gate)

```bash
# 1. Resource gate (HARDEST BLOCK)
~/.hermes/profiles/manager/scripts/resource-gate.sh

# 2. Quota gate
~/.hermes/profiles/manager/scripts/zai-quota-gate.sh

# 3. Freeze marker
[ -f ~/.hermes/bot/.dispatch_frozen ] && echo "FROZEN" && exit 1
```

### Creating Tasks

```bash
# Board must exist first
hermes kanban boards create <slug>

# Create task with quality gates in body
hermes kanban --board <slug> create "Task title" \
  --body "Full spec with quality gate requirements..." \
  --assignee worker-<name> \
  --priority 1

# Link dependencies (parent completes BEFORE child)
hermes kanban --board <slug> link <parent_id> <child_id>

# Verify task exists
hermes kanban --board <slug> ls
hermes kanban --board <slug> show <task_id>
```

### Post-Dispatch Verification (MANDATORY)

After workers start completing, verify board state:
```bash
hermes kanban --board <slug> ls --status done
hermes kanban --board <slug> ls --status blocked
# If child still blocked but parent done → manually promote:
hermes kanban --board <slug> unblock <child_id>
hermes kanban --board <slug> dispatch
```

### Blocked Status — Read the Prefix

| Prefix | Meaning | Action |
|--------|---------|--------|
| `review-required:` | Worker SUCCEEDED, wants review | Review + complete |
| `circuit-breaker:` | Worker FAILED 3× | Re-plan, different model |
| `error:` | Worker CRASHED | Check logs, re-dispatch |
| `timeout:` | Worker STALLED | Check if code landed, re-dispatch |

## 7. ZAI Proxy — Key Operations

### Check Health
```bash
curl -s http://localhost:9099/v1/models | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data']), 'models')"
curl -s http://localhost:9099/quota | python3 -m json.tool
```

### Key Management
```bash
# Disable our key (stop retry waste)
touch ~/.hermes/bot/.key_disabled_ours

# Re-enable
rm ~/.hermes/bot/.key_disabled_ours

# Check if live routing is enabled
ls ~/.hermes/bot/.enable_live_routing  # must exist for live routing
```

### Pricing Data
```bash
# Get current snapshot (served by CVM server on :3000)
curl -s http://localhost:3000/snapshot | python3 -m json.tool | head -30

# Get price history
curl -s http://localhost:3000/price-history?hours=168 | python3 -m json.tool | head -20
```

## 8. Dashboard Deployment

```bash
# Generate fresh nsec (new npub = no cache issues)
HEX=$(nak key generate)
NSEC=$(nak encode nsec "$HEX")
echo "$NSEC" > ~/.cvm-nsite-key

# Deploy
cd ~/merchant-routing-engine/demo/display-deploy
nsyte deploy . --sec "$NSEC" --use-fallbacks

# Verify
curl -sI "https://$(nak key public "$HEX" | nak encode npub).nsite.lol/" | head -3
```

**IMPORTANT:** Do NOT pipe `yes | nsyte deploy` through `tail` — `tail`
blocks `yes` from closing stdin. Use `nsyte deploy` directly without piping.

## 9. Cron Jobs (85+)

### Quota Gate (ALL LLM-driven crons)

Every LLM cron starts with:
```
QUOTA GATE: Run 'python3 ~/.hermes/profiles/manager/scripts/zai-quota-gate.py'
— if exit non-zero, skip and exit silently.
```

### Script-only crons (no_agent=True)

The `script` field must be just the FILENAME (e.g., `pricing-health-check.py`),
NOT the full command. The scheduler prepends the scripts dir path.

### Delivery

- `deliver: local` — saves to disk, no LLM needed (default for most)
- `deliver: origin` — sends to the Signal chat that created the job
  (requires LLM for message formatting — fails if quota exhausted)
- `deliver: signal:+number` — sends to specific Signal contact

## 10. Known Gotchas

1. **Ollama model down ≠ z.ai down**: z.ai quota gate passes (glm models
   available) but kimi-k3:cloud can be down (503). Always test the EXACT
   model before dispatching.

2. **nsite stale cache**: Use a FRESH nsec for every deploy. Old npubs
   serve cached content. `yes | nsyte deploy` (without tail pipe) works.

3. **Board state drift**: Workers commit code but kanban doesn't auto-promote
   children. Always verify board state after task completions.

4. **Kill switch disappears on restart**: `.enable_live_routing` file may
   not survive proxy restart. Use systemd `ExecStartPost=/usr/bin/touch`.

5. **delegate_task 300s timeout**: For tasks >5 min, use kanban dispatch
   instead. For trivial fixes (<4 lines), apply directly with `patch` tool.

6. **Worktrees in /tmp = lost work**: /tmp is tmpfs (RAM), cleared on
   reboot. All worktrees go in `~/worktrees/`. All clones in `~/repos/`.

7. **Cross-family review is mandatory**: GLM work → Kimi reviews. Kimi
   work → GLM reviews. Same-family review is an echo chamber — banned.

8. **Cost_usd=0 for PPQ/OpenRouter**: These APIs don't return per-call
   cost in response body. Balance-delta calculation needed (planned,
   not implemented).

9. **Pricing health cron**: Script checks for systemd service
   `zai-proxy.service` but proxy runs as raw PID. The script now falls
   back to `pgrep` if systemd check fails.

10. **`deliver: origin` burns quota**: A non-zero exit triggers an
    LLM-powered error message. If quota is exhausted, the delivery
    itself fails. Use `deliver: local` for script-only crons.

## 11. ADRs (Architecture Decision Records)

| # | Title | File |
|---|-------|------|
| 001 | Price-first routing | `docs/adr/ADR-001-price-first-routing.md` |
| 002 | Multi-Kalman separation | `docs/adr/ADR-002-multi-kalman-separation.md` |
| 003 | Deterministic peak multiplier | `docs/adr/ADR-003-deterministic-peak-multiplier.md` |
| 004 | Effective price positivity | `docs/adr/ADR-004-effective-price-positivity.md` |
| 005 | Three-layer actor separation | `docs/adr/ADR-005-three-layer-actor-separation.md` |
| 006 | Shadow mode validation | `docs/adr/ADR-006-shadow-mode-validation.md` |
| 007 | Routster marketplace intelligence | `docs/adr/ADR-007-routster-marketplace-intelligence.md` |
| 008 | Deterministic multipliers outside Kalman | `docs/adr/ADR-008-deterministic-multipliers-outside-kalman.md` |
| Consumer-001 | Consumer chart per-model | `docs/ADR-001-consumer-chart-per-model.md` |

## 12. What's Not Yet Implemented

- **Live routing** (shadow mode only) — needs `.enable_live_routing` + code fixes
- **Kalman dispatch gate endpoint** (`/v1/dispatch_gate`) — approved, not built
- **Cron scheduler plugin** — proposed, highest-leverage remaining change
- **delegate_task auto quota gate** — manual check needed before each dispatch
- **Balance-delta per-call cost** for PPQ/OpenRouter — API doesn't return cost
- **Parallel burn script** — currently sequential (one model at a time)
- **Demand Kalman + margin layer** (Phase 5) — on `github/master` branch, not merged

## 13. Operator Preferences (from MEMORY)

- Visual thinker — sends hand-drawn dashboard photos as design specs
- Concise responses — caveman mode active
- Log-scale Y for wide dynamic range
- Fresh npub EVERY nsite deploy (browser cache)
- Plan gate: plan + approval BEFORE features
- Root-cause fixes, not band-aids
- 80/20 merge: merge improvements, track cleanup as follow-up
- Images as photos (MEDIA:path), no markdown in Signal
- Per-endpoint price models, optimizer = min price
- Exponential pressure, uniform LOW asymptote (1.5)
- Balance-tracked via billing APIs

## 14. Session Notes

Read `~/.hermes/profiles/manager/state/session-notes.md` at the start
of every turn to check for active plans and current focus.

After EVERY delegate_task call returns, update the CTX card with the result.
After EVERY kanban task completion, save key findings to memory.
Every 5 tool calls, ask: "Have I persisted my current state?"

**NEVER let work disappear into the void — if you did something, persist it.**