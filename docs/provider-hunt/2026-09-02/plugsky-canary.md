# Plugsky Trial Canary — Verdict: PARK (no signup)

Date: 2026-09-02
Method: live probes, no account created, $0 spent.
Scope requested: signup + shadow probe + catalog check (recon-first staging).
Result: catalog check alone settled go/no-go → signup cancelled.

## Verified live facts

1. API live, OpenAI-compatible: https://api.plugsky.com/v1/models returns 200
   UNAUTHENTICATED with full model metadata (upstream, fallback chains,
   benchmarks). api.plugsky.com → 200.
2. Catalog = NVIDIA resell, nothing else. Every entry's `upstream` field is
   nvidia/*:
   - plugsky-micro  → nvidia/nvidia-nemotron-nano-9b-v2 (free tier)
   - plugsky-lite   → nvidia/nemotron-mini-4b-instruct (free tier; noted
     "mini-4b degraded on NVIDIA 2026-08-18"), backup stepfun-ai/step-3.7-flash
   - plugsky-plus   → nvidia/nemotron-3-ultra-550b-a55b (MoE 550B, ~55B act)
   - fallback chains point only at other plugsky-* aliases of the same stack
   ZERO overlap with our routing mix (glm / kimi / deepseek / qwen families).
3. Pricing (live page, 2026-09-02): Free $0, Hobby $5.60/mo ($4 yearly),
   Starter $14, Builder $42, Scale $84, Enterprise $15–25K/yr. Launch
   discount "30% off monthly, 50% off yearly" (limited time). NOT the
   "$20–120 unlimited" the AI transcript claimed.
4. Trust red flags:
   - Homepage stats ("35M+ API calls/day", "2M+ agents", "50,000+ Developers
     Addicted") carry footnote: "Figures sourced from the Plugsky data room
     and pending independent validation."
   - Logo wall claims backing by NVIDIA, Microsoft, OpenAI, AWS, Google,
     PayPal, Meta, Shopify simultaneously.
   - docs.plugsky.com → connection failure (000). Dead.
   - First-party products are rebranded forks: CLI = opencode fork (MIT),
     Desktop = Jan fork (Apache-2.0).
   - Status page self-hosted, "no incidents in last 90 days" (self-reported,
     no third-party uptime verification, no subscriber metrics exposed).
   - "Free, unlimited usage" headline contradicted by fair-use/bounded-queue
     terms deeper in the site.
5. Status page live (200) at status.plugsky.com — single-page JS app.

## Why parked

- Free tier = NVIDIA NIM free GPU credits with a middleman. NVIDIA NIM is
  directly accessible (build.nvidia.com) — same upstream, no added latency,
  no reseller ToS risk.
- No catalog overlap: our workers are pinned to glm/kimi/deepseek families;
  nothing here would ever win a routing decision.
- Vendor trust insufficient for even T7-experimental production dispatch
  (unvalidated stats, dead docs, fork-only products).
- "Unlimited" flat pricing carries fair-use asterisks — the flat-rate
  overflow-upstream thesis dies at our volume (2.95B tokens/30d).

## Only revisit trigger

Free-tier signup as a $0 gateway to eval Nemotron 3 Ultra 550B (legitimately
interesting big MoE) — IF we ever want that model family evaluated. That is a
model-eval question, not a router-integration question. Say the word →
signup + shadow probe of plugsky-plus.

## Provenance

- Catalog probe: curl https://api.plugsky.com/v1/models (no auth), 2026-09-02
- Pricing: https://plugsky.com/pricing (browser, 2026-09-02)
- Homepage + status page: live fetch, 2026-09-02
- Prior consultant reality-check transcript (fabrications list) on file.