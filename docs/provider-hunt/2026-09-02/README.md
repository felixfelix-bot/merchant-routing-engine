# Provider-Hunt Evidence Archive

Raw scouting + consultant-vetting reports backing the [endpoint backlog](../endpoint-backlog.md).
Published so others can benefit from the findings: prices, ToS traps, and verdicts are
all live-verified with the exact evidence behind each call.

**Method (reusable):** scout (daily cron, no signup/no spend) → two-consultant vetting
(cost economist + ops architect) → manager spot-verifies load-bearing claims live →
verdict lands in the backlog with revisit triggers. Everything probed from outside:
pricing page + `/v1/models` + ToS/terms pages.

## 2026-09-02 — first hunt

| File | What |
|---|---|
| `scout-report.md` | Daily hunt output: Standard Compute / Morph / Featherless + pricing probes + red flags |
| `consultant-a-cost-featherless.md` | Cost economist: live price re-verify, routing-win analysis, empirical demand check (token percentiles, dead telnyx lane), credit-pool tiering. VERDICT: PARK |
| `consultant-b-ops-featherless.md` | Ops architect: legitimacy triage (real: Recursal.AI/RWKV founders), ToS/automation risk, serving reality, integration sketch (~10 worker-hours). VERDICT: CONDITIONAL |
| `plugsky-canary.md` | Plugsky reality-check canary: NVIDIA-NIM resell, fabricated-stats kit, PARK. Root lesson: AI transcripts fabricate providers — always verify live |

**Headline findings:**
- Featherless: excellent catalog, real company — but ToS bans automation on Developer
  tier ("individual plans are for interactive use... terminated and no refund") and its
  prices lose every routing decision 9–90×. PARK.
- Dead-lane lesson: telnyx kimi-k3 had 490M lifetime tokens, ~0 since Aug 25 —
  "cheaper than telnyx" is worthless if there's no live demand to displace.
- Context-window lesson: p99 request is 181K tokens; only 0.08% of requests exceed 256K —
  long-context premiums are speculative at our traffic shape.
- ToS-over-marketing rule: marketing copy ("designed for agent fleets") never beats the
  contract. The ToS is the contract.