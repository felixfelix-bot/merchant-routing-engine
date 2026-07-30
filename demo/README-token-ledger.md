# token-ledger — Token Economy + npub Access Control (Task A3)

The core demo mechanic for the Sovereign Engineering dashboard
(`docs/PLAN-sovereign-demo.md`, Task A3): npub-gated participants each get a
token budget; every prompt deducts `est_tokens × price_per_token × scarcity_factor`,
where scarcity ramps 1.0 → 2.0 as the aggregate demo budget is consumed.
Rate-limited to **1 prompt / 5s / npub**.

Zero external dependencies — Node 22 built-ins only (`node:sqlite`, `node:http`).

---

## Files

| path | purpose |
|------|---------|
| `src/token-ledger.mjs` | the module: factory + HTTP handlers + standalone server |
| `demo-whitelist.json`  | array of npubs allowed to register (seeded with 3 demo npubs) |
| `demo-ledger.db`       | SQLite DB (created on first run; gitignore-friendly, in demo root) |
| `tests/verify-gates.mjs` | gate verification (all A3 quality gates) |

---

## Quick start (standalone server)

```bash
cd demo
node src/token-ledger.mjs                     # listens on :3002, uses demo-whitelist.json
# or with options:
DB_PATH=/tmp/x.db PORT=3199 DEMO_ADMIN_PASSWORD=secret node src/token-ledger.mjs
```

Smoke test (all gates):

```bash
node tests/verify-gates.mjs                   # prints 53 PASS / 0 FAIL on success
```

---

## Two ways to use it

**1. Library (how Task A1's `dashboard-server.mjs` uses it):**

```js
import { createTokenLedger, scarcityFactorForPct } from './src/token-ledger.mjs';

const ledger = await createTokenLedger({
  dbPath: 'demo-ledger.db',
  whitelistPath: 'demo-whitelist.json',
  adminPassword: process.env.DEMO_ADMIN_PASSWORD || 'sovereign-demo',
  startingBalance: 50_000,
  rateLimitMs: 5_000,
  basePricePerToken: 1.0,
  totalDemoBudget: null,        // null ⇒ sum of granted balances (auto-scales)
  requireResetPassword: false,
});

const p = ledger.register('npub1…');                              // throws 403/409
const r = ledger.charge({ npub, est_tokens, price_per_token });   // throws 402/404/429
ledger.addWhitelist('npub1…');
ledger.getLedger();        // participants sorted by spend desc
ledger.snapshot();         // { scarcity_factor, scarcity_band, budget_used_pct, ... }
ledger.recentTransactions(50);
ledger.reset();            // clears participants + ledger + rate-limit window
ledger.close();
```

**2. Standalone HTTP server** (for testing / cold review). Run the file directly;
it serves the routes below on a real socket.

---

## Library methods (the A1 integration contract)

Every error thrown by `register`/`charge` carries `.httpStatus`, `.code`,
`.message`, and (when useful) `.detail`. A1's `dashboard-server.mjs` already
catches these and maps them to its own HTTP responses.

| method | returns | throws |
|--------|---------|--------|
| `register(npub)` | participant row `{npub,balance,granted,total_spent,prompt_count,created_at,last_prompt_at}` | 403 `not_authorized`, 409 `already_registered` (`detail.participant`) |
| `charge({npub, est_tokens, price_per_token?, now?, topUp?})` | `{ok, deduction, scarcity_factor, balance_after, total_spent, prompt_count, topUp}` | 404 `not_registered`, 429 `rate_limited` (`detail.retry_after_s`), 402 `insufficient_tokens` (`detail.{balance,required,deficit}`) |
| `addWhitelist(npub)` | `{npub, added, whitelist[]}` | — |
| `getLedger()` | `participant[]` sorted by `total_spent` desc | — |
| `snapshot()` | `{participants, total_granted, total_spent, total_budget, budget_used_pct, scarcity_factor, scarcity_band, …}` | — |
| `recentTransactions(limit=50)` | `txn[]` newest-first | — |
| `reset()` | `{ok, cleared:true}` | — |

### `topUp` — important for A1's `/prompt`

A top-up is an adjustment to an **in-flight prompt** (e.g. the real token count
exceeded the pre-flight estimate after routing). It is NOT a new prompt, so it
**bypasses the rate limit and does not increment `prompt_count`** — but it still
deducts tokens and logs a `'topup'` ledger row. `dashboard-server.mjs` should
pass `topUp: true` on its post-routing top-up charge so long completions are
fully accounted without tripping the 1-prompt/5s window. (Default `topUp=false`
keeps full rate limiting — all standalone gates still pass.)

---

## HTTP routes (standalone surface)

| method | path | notes |
|--------|------|-------|
| GET  | `/health` | liveness |
| GET  | `/snapshot` | economy state (scarcity, budget, counts) |
| GET  | `/ledger` | `{ participants[], stats }` |
| GET  | `/ledger/recent?limit=50` | recent transactions |
| GET  | `/whitelist` | current whitelist |
| POST | `/register` | body `{npub}` → 200 / 403 / 409 |
| POST | `/admin/whitelist` | body `{password, npub}` → 200 / 401 |
| POST | `/prompt` | body `{npub, prompt, est_tokens?, price_per_token?}`; estimates tokens from text if omitted |
| POST | `/charge` | body `{npub, est_tokens, price_per_token, topUp?}` (explicit, no estimation) |
| POST | `/reset` | clears all participants + ledger (password only if `requireResetPassword`) |

Status codes: 200 · 400 · 401 · 402 · 403 · 404 · 409 · 429 · 500.

> Note: `dashboard-server.mjs` (A1) serves its OWN `/register`, `/prompt`,
> `/ledger`, `/admin/whitelist`, `/reset` that conform to `API-CONTRACT.md`,
> built on top of these library methods. The routes above are the module's own
> standalone surface for verification/cold-review.

---

## Token deduction & scarcity

```
deduction = round(est_tokens × price_per_token × scarcity_factor)
```

`scarcity_factor` ramps with **% of total demo budget consumed** (distinct from
the proxy's quota-based scarcity). Bands are inclusive on the lower edge:

| budget consumed | scarcity |
|-----------------|----------|
| < 20%           | 1.0×     |
| 20 – 40%        | 1.2×     |
| 40 – 60%        | 1.5×     |
| 60 – 80%        | 1.8×     |
| ≥ 80%           | 2.0×     |

`total_demo_budget` defaults to the **sum of granted starting balances**
(auto-scales with registrations); set `totalDemoBudget` to a fixed number for a
fixed pool. See `snapshot().budget_used_pct` for the live value.

### Unit contract for `price_per_token`

`price_per_token` is in **demo-tokens per estimated-token**, not dollars.
Default `1.0` ⇒ a 1000-token prompt costs 1000 demo tokens at scarcity 1.0, so a
50,000-token budget ≈ 50 prompts at base (fewer as scarcity ramps). A1 converts
the live Kalman $/M rate into this unit before calling `charge()`.

---

## Configuration

All optional; every override is `undefined`-safe (won't clobber defaults).

| opt / env | default | meaning |
|-----------|---------|---------|
| `dbPath` / `DB_PATH` | `demo/demo-ledger.db` | SQLite file |
| `whitelistPath` / `WHITELIST_PATH` | `demo/demo-whitelist.json` | whitelist array |
| `adminPassword` / `DEMO_ADMIN_PASSWORD` | `sovereign-demo` | protects `/admin/whitelist` |
| `startingBalance` / `STARTING_BALANCE` | `50000` | tokens granted on register |
| `rateLimitMs` / `RATE_LIMIT_MS` | `5000` | 1 prompt / N ms / npub |
| `basePricePerToken` / `BASE_PRICE_PER_TOKEN` | `1.0` | standalone default price |
| `totalDemoBudget` / `TOTAL_DEMO_BUDGET` | `null` (sum of grants) | aggregate pool for scarcity |
| `requireResetPassword` / `REQUIRE_RESET_PASSWORD` | `false` | gate `/reset` behind admin pw |
| `PORT` | `3002` | standalone listen port |

---

## Quality gates (all verified by `tests/verify-gates.mjs` → 53 PASS)

- **G0** scarcity band boundaries (0→1.0, 20→1.2, 40→1.5, 60→1.8, 80→2.0)
- **G1** whitelist: whitelisted registers; non-whitelisted → 403; duplicate → 409; admin-added registers
- **G2** balance: deduction = est×ppt×scarcity; balance/spent track; over-budget → 402; unregistered → 404; ledger sorted
- **G3** scarcity visibly ramps 1.0→1.2→1.5→1.8→2.0 and is non-decreasing
- **G4** rate limit: 2nd within 5s → 429; per-npub; resets after window & after `/reset`; `topUp` bypasses
- **G5** HTTP surface over a real socket: health/snapshot/register/admin/prompt/ledger/reset

---

## Design notes

- **Whitelist** is a JSON file, reloaded on construction and re-persisted on
  `addWhitelist`. `register` is idempotent by rejection: a second register for
  the same npub returns 409 with the existing balance (balance never double-granted).
- **Rate limit** is in-memory per process (fast). `/reset` clears it.
- **SQLite** (`node:sqlite`, Node 22) matches A1's shared-DB design; emits a
  harmless `ExperimentalWarning` on stderr.
- **Atomic writes**: every balance change + ledger insert is wrapped in a
  `BEGIN/COMMIT` transaction.
