# Jev (TypeSafe AI, "System One") — fit verdict for the Hermes / routing stack

Date: 2026-09-24 · Status: verified, decision pending (operator)
Sources: live fetches only. Raw artifacts: `~/jev_probe/` (consultant pass) + the
URLs quoted inline. Everything below was re-checked by the manager on 2026-09-24.

## Why this exists

An AI-mode conversation (DE) proposed using Jev to upgrade our Kalman LLM routing:
Jev as a semantic feature-extraction layer in front of the router, "<80 ms",
"virtually free", feeding complexity scores into the Kalman Q/R matrices and into
per-category (intent) filters, with Nouls (calibrated probabilities) as the
measurement weight. That is a proposal, not a fact — this file is the vetted base.

## 1. Verified facts (REAL)

| Fact | Verbatim / evidence | Source |
|---|---|---|
| Model | `jev-1.13.0`; aliases `jev-latest`, `jev-preview`. **No `jev-1`** | docs.typesafe.ai/models.md |
| Price | "Price (per Btok / per Mtok) \| $42 / $0.042"; "Charged per input token. Output tokens are free." | docs.typesafe.ai/models.md |
| Cross-check price | `typesafe-ai/jev` → `pricing.input = 0.000000042`, `pricing.output = 0` | ai-gateway.vercel.sh/v1/models |
| Rate limits | "250,000 tokens per second / 1,200 requests per minute" (documented as dynamically adjusting) | docs.typesafe.ai/models.md |
| Context | 64k per request; **32k for `state` + the longest question** | docs.typesafe.ai/models.md |
| API shape | `POST https://api.typesafe.ai/v1/systemone`, `Authorization: Bearer <key>`; without a key → `403 {"error_type":"authentication_error"}` | live probe |
| Question primitives | `choice` (label + per-option probability), `score` (rubric level + probability per level), `noul` (0–1 for a yes/no statement); several questions per request, evaluated independently | docs.typesafe.ai/primitives.md |
| Why probabilities are trustworthy | RLCD — "reinforcement learning for calibrated decisions … trains TypeSafe to return decisions and calibrated probabilities instead of generated text" | docs.typesafe.ai/concepts/system-one.md |
| Measured latency + cost | Batched: "one call, all 13 \| 1 \| $0.000497 \| **0.27s**" vs "13 calls, one each \| 13 \| $0.006090 \| 2.71s" → "12.2x cheaper, 10.0x faster" | docs.typesafe.ai/cookbooks/parallel_questions.md |
| ToS | §2.1(b)/§2.2 permit embedding the API in Customer Applications; §2.3(a) no standalone resale, §2.3(b) no distillation | typesafe.ai/legal/mca (updated 2026-09-23) |
| SDKs (official) | `@typesafe-ai/sdk` 0.6.0 (JS, repo 232★), `typesafe-sdk` 0.7.1 (Py, repo 219★) | npm / pypi |
| Agent surfaces | official agent skill `typesafe-ai/skills` (2041★, updated 2026-09-12); MCP: `jev-mcp` inside the unofficial `jev-cli` (pypi 0.6.3, 2026-09-23) + `jevcore-mcp` | pypi / npm / github |
| API-shaped shim | `typesafe-ai/system-one-adapter-python` (286★) — "drop-in replacement for `typesafe_sdk`'s `system_one` evaluation API, backed by LLM APIs instead of TypeSafe" | github |

## 2. FABRICATED / overstated (from the paste and from the ecosystem)

- **`jev-1`** as the model name — the docs list only `jev-1.13.0` / aliases.
- **"<80 ms", "sub-100 ms", "practically nothing"** — no such claim exists on any
  TypeSafe page. Measured: **0.27 s** for a 13-question batch (docs). Unofficial
  jevai.org says "~120ms end to end"; jevctl README says "a few hundred
  milliseconds". Treat the latency as a documented ~0.3 s one-way call.
- **OpenRouter lane** (`typesafe/jev-1.13`, asserted in the unofficial jev-cli
  README): live `404`; `?q=jev` → `{"data":[],"total_count":0}`. FABRICATED.
- **Open weights / local inference of Jev**: FABRICATED. `TypeSafeAI` on HF hosts
  only `Step-5-Preview-BF16` / `-GGUF` (StepFun Step-5 MoE). Third-party
  "Jev-style" decision models exist (`Open-Jev-2B/9B`, `Jev-Style-Qwen3.5-2B-Decision-GGUF`,
  `mini-Jev`, …) — those are community clones, not Jev.
- **"Free playground"**: real copy, gated — "Uses the website's server key. Up to
  8 questions and 32 KiB per run"; live probe `POST /api/playground` → `401 Sign in
  required`. The countdown is static markup (`04d 14:19:11` on two fetches) and
  inconsistent with "through Sept 25". Site self-declares "not the official
  product site".
- Ecosystem star-inflation signal: `tamaratran/fast-jev-compaction` 6630★ on a repo
  created 2026-09-17; `vinnylarouge/jevlike` 1274★ created 2026-09-16. Tutorials and
  "how Jev upgrades your X" posts are a hype cluster, not evidence.

## 3. Economics against our own numbers (decides everything)

Our live usage (`~/.hermes/bot/zai_usage.db`, 7d): deepseek lane 63,453 calls /
7,552 Mtok / $260.78; `ours` 16,480 calls / 663 Mtok / $0.00; ollama_cloud 13,902 /
964 Mtok / $82.41; total 6.3k–18.3k calls/day, **average prompt ≈ 106k–135k tokens**.

Cost is **state size × $0.042/Mtok** (output free):

| Jev state per call | $ / call | @ 18k calls/day |
|---|---|---|
| 2k tokens (turn-level label) | $0.00008 | **$1.5 / day** |
| 32k tokens (max state) | $0.00134 | $24 / day |
| 110k (our average prompt — also **over Jev's 64k/32k limit**) | not possible | — |

So "virtually free" is true **only for small states**. Feeding a request into a
router-side Jev call at our prompt sizes is either impossible (context) or a new
$20–80/day line — i.e. it would compete with the entire inference bill it is
supposed to optimise. Design rule: **small state, batched questions, off the hot
path.**

Latency on the hot path: one Jev call ≈ **+0.27 s** to every routed request. Our
per-call wall clock is dominated by 100k-token prompts (seconds), so at turn level
it is noise; **inside the proxy per inference call it is a per-iteration tax** on
every tool loop. Async/shadow use avoids it entirely.

## 4. Where it actually fits (ranked; cold read of the live stack)

1. **Skill routing / skill suggestion — best evidence, and it is literally our
   harness.** TypeSafe's own cookbook measured this on Hermes: roster of 182 skills,
   Hermes' 60-char truncated index, 488 requests vs `claude-haiku-4-5`:
   wrong-skill loads **16.8% → 7.3%**, spurious loads **9.8% → 4.0%** (floor when
   handed the answer: 2.5% / 1.2%). Two calls per turn: rank-all → re-read top 3
   (free to reject all). We have 361 SKILL.md files, a 57-char index, an existing
   embedding router (`~/.hermes/profiles/manager/plugins/search/skill_router.py` →
   `route_skills`, Ollama nomic-embed + LanceDB), and both hooks needed to wire it:
   `pre_llm_call` (agent/turn_context.py:1159 — injects into the user message, so the
   system prompt stays byte-stable = cache-safe) and `on_skill_lifecycle`
   (tools/skill_usage.py:826 — load telemetry, so we can measure wrong-load rate
   ourselves). Embeddings answer "what is similar"; they do not answer "should any
   skill load, or is the top-3 good enough" — that is the decision Jev is trained for.
2. **Garbage / refusal / truncation labelling of lane failures — evidence of a real
   wrong label.** `garbage_detector.py:324-348` / `:485-504`, consumed at
   `zai_proxy.py:1611-1630,6003,6527`: 16,113 ledger events / 14d, of which
   **15,066 got `price_mult=inf`** (lane priced out of routing), top offenders
   deepseek-flash 7,070, neuralwatt/kimi-k3 2,607, ours/glm-5.2 963. The detector is
   regex + common-word hit-rate + `json.loads`, and `:96 bad_json` already fired on a
   **legitimate refusal**. A typed label (`{garbage, refusal, truncated-reasoning,
   tool-call, ok}`) over the same text is exactly a System One question.
3. **Blocked-card disposition — currently a prose LLM answering a label question.**
   `task-lifecycle-governor.py:136-151` classifies crash-loop/dependency/needs-input/
   sticky, then a consultant LLM is dispatched to "challenge the auto-verdicts where
   they look wrong": deepseek-v4-pro **2,335 calls / 275.7 Mtok / 176.7 s average**
   in 14d. Same question, typed answer, ~0.3 s, cents.
4. **Advisory triage for gate prose** (kanban `classify_tier` default `code`
   over-gating — balloon/t_1359baab needed 6 re-dispatches 2026-09-18;
   `review_artifact_present` accepts a 120-byte stub as a hard gate). Jev must stay
   **advisory/pre-filter here** — a calibrated 0.9 is not a substitute for §4
   cross-family review, and gate enforcement stays deterministic.

Non-fits (checked, do not re-open): `flat_router.select_provider` lane ranking is a
numeric cost sort (`flat_router.py:1443-1560`, cheapest-first `:1560`, hysteresis
`:1566-1574`) — replacing it with a classifier is a downgrade; our measured
`flat_router_shadow_decisions` = 242,524 rows vs `routing_live_decisions` = 1,587
(LiveRouter is failover-only, exit switch `zai_proxy.py:5636-5663`); alert matchers
are deterministic and their one real false positive was fixed with a regression test;
`X-Task-Type` / `api_calls.task_type` is NULL on 217,276/217,276 rows — measure
whether any caller sets it before designing anything around it; and Jev cannot
replace any text-generation lane (docs: "Jev is not a chat or code-completion LLM",
"not a drop-in replacement for the LLM behind Claude Code, Cursor, opencode…").

## 5. What the pasted proposal gets wrong about our Kalman

- **"Expected token length" is not a Jev question for us — we already know it
  exactly.** Input tokens are countable locally, for free, before routing. A
  calibrated guess at a quantity we measure exactly is dominated.
- **Q/R scaling needs an observable covariate with measured predictive power.**
  Nothing here demonstrates that a semantic complexity score predicts *our* lane
  latency/cost better than features we already log (exact tokens, cache hit, model
  id, lane, retry count, garbage label). Test: regress observed latency/residuals on
  those features; only if residuals remain structured is a semantic covariate worth a
  probe.
- **Our Kalman tracks price/quota burn, not per-request latency.** The paste's
  architecture (per-intent sub-filters, reliability vectors per prompt class) is a
  different design than the one we run. Port first, don't assume.
- The paste is otherwise a fair summary of Jev's *mechanics* (System One, Nouls,
  RLCD, $0.042/Mtok) — it is wrong mainly on model id, latency, "free", and the
  OpenRouter lane.

## 6. Bounded next steps (each ≤ one experiment, no refactors)

- **E1 (recommended first, offline, $0):** replay 200 rows of
  `garbage_ledger.jsonl` through a typed label set and count rows where the current
  regex delists but truth is refusal/truncation/tool-call. Needs a key only for the
  Jev arm; the baseline arm is free.
- **E2 (highest ceiling):** skill-suggestion A/B on stored sessions — take 100 past
  user turns, compare route_skills top-3 vs a Jev suggestion vs the skill actually
  loaded (telemetry), and only then wire `pre_llm_call` + `on_skill_lifecycle`
  measurement into a canary profile (never the manager profile first).
- **E3 (cost win):** replace the blocked-card prose consultant with a typed
  disposition call + a short deterministic summary; compare decisions on one weekly
  digest (2,335 calls/14d at 176.7 s is the current bill).
- **Gating before any probe:** a key requires an account; **no plan pricing is
  published** (`/pricing`, `/docs`, `pricing-plans` → 404; only token pricing is
  documented) and the playground is sign-in-gated. Treat vendor signup as a bounded
  spend (ADR-014: cheapest-HQ, internal-only, ≤$50 deposit, canary + garbage circuit
  breaker, no provider commitment).
- **Do not:** put Jev on the proxy's hot path, feed it 100k-token prompts, use it for
  gate enforcement or review verdicts, or buy ecosystem "Jev-style" tutorials as
  evidence.

## 7. Doc corrections made in the same commit

- `AGENTS.md` in this repo still states "LiveRouter (Kalman-based) runs as the
  primary" — stale; the primary selector is `flat_router.select_provider`
  (242,524 shadow decisions vs 1,587 live decisions in `zai_usage.db`).
