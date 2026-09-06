# OpenInference onboarding email — draft for Felix to send (2026-09-06)

**To:** markian@openinference.ai
**Subject:** Org key request — automated internal API usage (DeepSeek-V4-Flash)

Hi Markian,

I'd like to start using Open Inference for automated, internal-only
workloads (DeepSeek-V4-Flash via api.openinference.ai/v1).

Two quick confirmations before I create an account and deposit:

1. We plan to use a single org API key for automated internal
   tooling (no resale, no multi-tenant serving — internal use only).
   Please confirm that fits your ToS.
2. We'd keep the balance small (~$20–50) and top up as needed —
   happy to hear if you recommend a specific tier for that usage
   pattern.

If it's easier, a signup link or the right onboarding path would be
appreciated — we didn't find a self-serve account page on
openinference.ai.

Thanks,
Felix

---

## Fleet context (not part of the email)

- Cheapest measured PAYG lane: $0.03/M in, $0.075/M out, $0.007/M
  cache-read → eff ~$0.0093/M at h=0.9 (`cam_probes` + hunt verdict).
- API live + catalog open: api.openinference.ai/v1 (2 models,
  DeepSeek-V4-Flash + 0731, 1M ctx).
- Signup is contact-gated: the site is a JS shell (only /blog /contact
  /privacy /terms routes exist — no /signup; a Google search conflates
  it with Arize's "OpenInference" telemetry SDK).
- After the key arrives: store as OPENINFERENCE_API_KEY (600, no echo),
  run the <$5 probe suite from the hunt verdict doc (uptime, latency,
  concurrency, cache-billing honesty, authenticity via logprobs), then
  the ADR-014 canary, then wire as transient lane. Nothing routes before.
