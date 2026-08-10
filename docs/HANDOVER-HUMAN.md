# Sovereign Engineering — Human Handover

Welcome. Here's what we built and how to use it.

## What This Is

An autonomous AI engineering system where Hermes (an AI agent) manages
worker AI agents that write, test, review, and ship code — all gated
by quality rules that prevent broken work from reaching production.

You talk to Hermes via Signal or Matrix. Hermes delegates to workers.
Workers push code. You review.

## The 30-Second Tour

1. You tell Hermes what to build (Signal message)
2. Hermes creates kanban tasks with quality gates baked in
3. Workers pick up tasks, write code, run tests
4. A different AI family reviews the code (cross-family cold review)
5. Worker commits + pushes to a feature branch
6. Hermes reviews, merges to main, pushes

## How to Give Instructions

Just talk normally in Signal. Examples:
- "Fix the burn rate calculation in the dashboard"
- "Add a new provider to the pricing engine"
- "Deploy the dashboard to a fresh nsite"

Hermes will figure out the tasks, assign workers, and report back.

## Key URLs

- Dashboard: check the latest nsite URL (changes on redeploy)
- Code: https://github.com/felixfelix-bot/merchant-routing-engine
- System overview: docs/SYSTEM-OVERVIEW.md in the repo
- ADRs: docs/adr/ in the repo

## Key Commands (if you want to drive directly)

```bash
# Check proxy health
curl -s http://localhost:9099/v1/models | python3 -m json.tool | head

# Check quota
curl -s http://localhost:9099/quota | python3 -m json.tool

# See kanban board
hermes kanban --board merchant-routing ls

# Dispatch ready tasks
hermes kanban --board merchant-routing dispatch

# Disable a z.ai key (stop retry waste)
touch ~/.hermes/bot/.key_disabled_ours

# Re-enable
rm ~/.hermes/bot/.key_disabled_ours

# Deploy dashboard to nsite (fresh key = no cache)
nak key generate  # get new nsec
cd ~/merchant-routing-engine/demo/display-deploy
nsyte deploy . --sec <nsec> --use-fallbacks
```

## What's Live Right Now

- Proxy: localhost:9099, rotating z.ai keys + Ollama fallback
- Both z.ai keys: exhausted (429). Ollama Cloud is the active path
- Our z.ai key: manually disabled (flag file exists)
- Dashboard: deployed to nsite (URL changes per deploy)
- CVM server: localhost:3000, publishing to Nostr
- 85+ cron jobs running (monitors, auto-healers, watchers)

## What to Watch Out For

- **Quota**: z.ai resets every 5 hours. When it resets, workers auto-resume
- **Models**: kimi-k2.7-code for code/spatial, glm-5.2 for reasoning
- **Don't flash boards without the mutex lock** (balloon project)
- **Never put worktrees in /tmp** — it's RAM, cleared on reboot
- **Push = done. Unpushed = lost.**

## Where to Read More

- `docs/SYSTEM-OVERVIEW.md` — the full architecture
- `docs/adr/` — 8 ADRs covering key design decisions
- `docs/SYSTEM-OVERVIEW-LLM-HANDOVER.md` — the detailed LLM version