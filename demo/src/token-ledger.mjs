// token-ledger.mjs — Token economy + npub access control for the Sovereign
// Engineering demo (PLAN-sovereign-demo.md, Task A3).
//
// WHAT THIS IS
//   The core demo mechanic: npub-gated participants each get a token budget;
//   every prompt deducts `est_tokens × price_per_token × scarcity_factor`,
//   where the scarcity factor ramps (1.0 → 2.0) as the aggregate demo budget
//   is consumed. Rate-limited to 1 prompt / 5s / npub.
//
// TWO WAYS TO USE IT
//   1. As a LIBRARY (Task A1 dashboard-server.mjs imports it):
//        import { createTokenLedger } from './src/token-ledger.mjs';
//        const ledger = await createTokenLedger({ dbPath, whitelistPath, ... });
//        await ledger.register(npub);
//        const r = await ledger.charge({ npub, est_tokens, price_per_token });
//   2. As a STANDALONE HTTP server (for testing / cold review):
//        node demo/src/token-ledger.mjs            # listens on :3002
//        PORT=3003 node demo/src/token-ledger.mjs
//
// UNIT CONTRACT for `price_per_token`
//   price_per_token is in DEMO-tokens per estimated-token (dimensionless-ish),
//   NOT dollars. Default 1.0 ⇒ a 1000-token prompt costs 1000 demo tokens at
//   scarcity 1.0. Task A1 converts the live Kalman $/M rate into this unit
//   before calling charge(); see README-token-ledger.md.
//
// STATUS CODES
//   200 ok · 400 bad request · 401 bad admin password · 403 not whitelisted
//   404 not registered · 402 insufficient tokens · 409 already registered
//   429 rate limited · 500 internal error
//
// No external dependencies — Node 22 built-ins only (node:sqlite, node:http).

import { DatabaseSync } from 'node:sqlite';
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const DEMO_ROOT = resolve(__dirname, '..');

// ── defaults ────────────────────────────────────────────────────────────────

const DEFAULTS = {
  dbPath: join(DEMO_ROOT, 'demo-ledger.db'),
  whitelistPath: join(DEMO_ROOT, 'demo-whitelist.json'),
  adminPassword: process.env.DEMO_ADMIN_PASSWORD || 'sovereign-demo',
  startingBalance: 50_000,        // tokens granted on registration
  rateLimitMs: 5_000,             // 1 prompt / 5s / npub
  basePricePerToken: 1.0,         // demo-tokens per estimated-token (standalone)
  // Fixed aggregate demo budget. null ⇒ derive from sum of granted balances
  // (auto-scales with registrations). Set a number for a fixed pool.
  totalDemoBudget: null,
  // When true, POST /reset requires the admin password (safety default).
  requireResetPassword: false,
};

// Scarcity bands keyed off % of total demo budget consumed (task A3 spec).
//   <20%  → 1.0x    20–40% → 1.2x    40–60% → 1.5x
//   60–80% → 1.8x   ≥80%   → 2.0x
// Boundaries are inclusive on the lower edge (20% ⇒ 1.2x band).
const SCARCITY_BANDS = [
  { ceiling: 20, factor: 1.0 },
  { ceiling: 40, factor: 1.2 },
  { ceiling: 60, factor: 1.5 },
  { ceiling: 80, factor: 1.8 },
  { ceiling: Infinity, factor: 2.0 },
];

export function scarcityFactorForPct(pctUsed) {
  for (const band of SCARCITY_BANDS) {
    if (pctUsed < band.ceiling) return band.factor;
  }
  return 2.0;
}

function scarcityBandLabel(pctUsed) {
  for (const band of SCARCITY_BANDS) {
    if (pctUsed < band.ceiling) {
      return band.factor === 2.0
        ? '≥80%'
        : `<${band.ceiling}%`;
    }
  }
  return '≥80%';
}

// ── factory ─────────────────────────────────────────────────────────────────

/**
 * Build a TokenLedger. Opens (creating if needed) the SQLite DB and loads the
 * whitelist file. Returns an object with state methods + `handler` (HTTP) +
 * `listen`.
 *
 * @param {object} [opts] — overrides on top of DEFAULTS.
 */
export async function createTokenLedger(opts = {}) {
  // Drop undefined/null overrides so explicit-undefined (e.g. an unset env
  // var passed as `dbPath: process.env.DB_PATH`) doesn't clobber DEFAULTS.
  const clean = {};
  for (const [k, v] of Object.entries(opts)) {
    if (v !== undefined && v !== null) clean[k] = v;
  }
  const cfg = { ...DEFAULTS, ...clean };
  // Normalise absolute paths.
  cfg.dbPath = resolve(cfg.dbPath);
  cfg.whitelistPath = resolve(cfg.whitelistPath);

  const dbDir = dirname(cfg.dbPath);
  if (!existsSync(dbDir)) mkdirSync(dbDir, { recursive: true });

  const db = new DatabaseSync(cfg.dbPath);
  db.exec(`
    CREATE TABLE IF NOT EXISTS demo_participants (
      npub            TEXT    PRIMARY KEY,
      balance         INTEGER NOT NULL DEFAULT 0,
      granted         INTEGER NOT NULL DEFAULT 0,
      total_spent     INTEGER NOT NULL DEFAULT 0,
      prompt_count    INTEGER NOT NULL DEFAULT 0,
      created_at      TEXT    NOT NULL,
      last_prompt_at  INTEGER
    );
    CREATE TABLE IF NOT EXISTS demo_ledger (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      npub            TEXT    NOT NULL,
      delta           INTEGER NOT NULL,   -- +grant / -charge
      est_tokens      INTEGER,
      price_per_token REAL,
      scarcity_factor REAL,
      reason          TEXT    NOT NULL,   -- 'grant' | 'charge' | 'reset'
      balance_after   INTEGER NOT NULL,
      ts              INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_ledger_npub ON demo_ledger(npub);
    CREATE INDEX IF NOT EXISTS idx_ledger_ts   ON demo_ledger(ts);
  `);

  // Prepared statements (reuse across requests).
  const stmts = {
    getParticipant: db.prepare('SELECT * FROM demo_participants WHERE npub = ?'),
    insertParticipant: db.prepare(`
      INSERT INTO demo_participants
        (npub, balance, granted, total_spent, prompt_count, created_at, last_prompt_at)
      VALUES (?, ?, ?, 0, 0, ?, NULL)`),
    sumGranted: db.prepare('SELECT COALESCE(SUM(granted), 0) AS v FROM demo_participants'),
    sumSpent: db.prepare('SELECT COALESCE(SUM(total_spent), 0) AS v FROM demo_participants'),
    countParticipants: db.prepare('SELECT COUNT(*) AS v FROM demo_participants'),
    allParticipants: db.prepare(`
      SELECT * FROM demo_participants
      ORDER BY total_spent DESC, prompt_count DESC, created_at ASC`),
    applyCharge: db.prepare(`
      UPDATE demo_participants
         SET balance = ?, total_spent = ?, prompt_count = ?, last_prompt_at = ?
       WHERE npub = ?`),
    touchLastPrompt: db.prepare(`
      UPDATE demo_participants SET last_prompt_at = ? WHERE npub = ?`),
    insertLedger: db.prepare(`
      INSERT INTO demo_ledger
        (npub, delta, est_tokens, price_per_token, scarcity_factor, reason, balance_after, ts)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)`),
    clearParticipants: db.prepare('DELETE FROM demo_participants'),
    clearLedger: db.prepare('DELETE FROM demo_ledger'),
    recentLedger: db.prepare(`
      SELECT * FROM demo_ledger ORDER BY ts DESC, id DESC LIMIT ?`),
  };

  // Rate-limit state is in-memory (fast, per-process). Reset on /reset.
  const lastPromptAt = new Map(); // npub → epoch ms

  // ── whitelist ────────────────────────────────────────────────────────────
  function loadWhitelistFile() {
    if (!existsSync(cfg.whitelistPath)) return [];
    try {
      const raw = JSON.parse(readFileSync(cfg.whitelistPath, 'utf8'));
      if (!Array.isArray(raw)) return [];
      return raw
        .map((e) => (typeof e === 'string' ? e : e?.npub))
        .filter((s) => typeof s === 'string' && s.length > 0);
    } catch {
      return [];
    }
  }
  let whitelistSet = new Set(loadWhitelistFile());

  function persistWhitelist() {
    const arr = [...whitelistSet];
    mkdirSync(dirname(cfg.whitelistPath), { recursive: true });
    writeFileSync(cfg.whitelistPath, JSON.stringify(arr, null, 2) + '\n', 'utf8');
  }

  function isWhitelisted(npub) {
    return whitelistSet.has(npub);
  }

  function addWhitelist(npub) {
    if (!npub || typeof npub !== 'string') {
      throw Object.assign(new Error('npub required'), { code: 'BAD_REQUEST' });
    }
    const added = !whitelistSet.has(npub);
    whitelistSet.add(npub);
    if (added) persistWhitelist();
    return { npub, added, whitelist: [...whitelistSet] };
  }

  // ── budget / scarcity ────────────────────────────────────────────────────
  function totalGranted() {
    return Number(stmts.sumGranted.get().v) || 0;
  }
  function totalSpent() {
    return Number(stmts.sumSpent.get().v) || 0;
  }
  function totalBudget() {
    if (typeof cfg.totalDemoBudget === 'number' && cfg.totalDemoBudget > 0) {
      return cfg.totalDemoBudget;
    }
    return totalGranted();
  }
  function budgetUsedPct() {
    const budget = totalBudget();
    if (budget <= 0) return 0;
    return (totalSpent() / budget) * 100;
  }
  function currentScarcity() {
    return scarcityFactorForPct(budgetUsedPct());
  }

  function snapshot() {
    const budget = totalBudget();
    const spent = totalSpent();
    const granted = totalGranted();
    const pct = budget > 0 ? (spent / budget) * 100 : 0;
    return {
      participants: Number(stmts.countParticipants.get().v) || 0,
      total_granted: granted,
      total_spent: spent,
      total_budget: budget,
      budget_used_pct: Math.round(pct * 100) / 100,
      scarcity_factor: currentScarcity(),
      scarcity_band: scarcityBandLabel(pct),
      rate_limit_ms: cfg.rateLimitMs,
      starting_balance: cfg.startingBalance,
      generated_at: new Date().toISOString(),
    };
  }

  // ── core operations ──────────────────────────────────────────────────────

  /**
   * Register an npub. Whitelisted ⇒ grant starting balance. Idempotency:
   * a second registration for the same npub returns 409 (one balance per npub,
   * balance is never double-granted).
   * @returns {object} participant row
   * @throws {Error} with .code and .httpStatus on failure.
   */
  function register(npub) {
    if (!npub || typeof npub !== 'string') {
      throw httpError(400, 'bad_request', 'npub required');
    }
    if (!isWhitelisted(npub)) {
      throw httpError(403, 'not_authorized', 'not authorized for this demo');
    }
    const existing = stmts.getParticipant.get(npub);
    if (existing) {
      throw httpError(
        409,
        'already_registered',
        'npub already registered',
        { participant: row(existing) },
      );
    }
    const now = new Date().toISOString();
    const ts = Date.now();
    db.exec('BEGIN');
    try {
      stmts.insertParticipant.run(npub, cfg.startingBalance, cfg.startingBalance, now);
      stmts.insertLedger.run(npub, cfg.startingBalance, null, null, null, 'grant', cfg.startingBalance, ts);
      db.exec('COMMIT');
    } catch (e) {
      db.exec('ROLLBACK');
      throw e;
    }
    return row(stmts.getParticipant.get(npub));
  }

  /**
   * Charge a participant for one prompt. Applies rate limit + scarcity, then
   * deducts `est_tokens × price_per_token × scarcity_factor`.
   *
   * @param {object} args
   * @param {string} args.npub
   * @param {number} args.est_tokens       — estimated real tokens for the prompt
   * @param {number} [args.price_per_token]— demo-tokens/est-token (default cfg.basePricePerToken)
   * @param {number} [args.now]            — injectable clock (testing)
   * @param {boolean} [args.topUp=false]   — adjustment to an in-flight prompt
   *   (e.g. real tokens exceeded the pre-flight estimate). NOT a new prompt:
   *   bypasses the rate limit and does NOT increment prompt_count. Lets Task
   *   A1's /prompt top up a charge after routing without tripping the window.
   * @returns {object} charge result with scarcity + balances
   * @throws {Error} with .code/.httpStatus on failure.
   */
  function charge({ npub, est_tokens, price_per_token, now, topUp = false }) {
    if (!npub || typeof npub !== 'string') {
      throw httpError(400, 'bad_request', 'npub required');
    }
    const est = Number(est_tokens);
    const ppt = Number(price_per_token ?? cfg.basePricePerToken);
    if (!Number.isFinite(est) || est < 0) {
      throw httpError(400, 'bad_request', 'est_tokens must be a non-negative number');
    }
    if (!Number.isFinite(ppt) || ppt < 0) {
      throw httpError(400, 'bad_request', 'price_per_token must be a non-negative number');
    }
    const ts = now ?? Date.now();

    const participant = stmts.getParticipant.get(npub);
    if (!participant) {
      throw httpError(404, 'not_registered', 'npub is not registered');
    }

    // Rate limit: 1 prompt / rateLimitMs / npub. A participant's FIRST prompt
    // is never rate-limited (no prior entry ⇒ no window to be inside). A topUp
    // is part of an in-flight prompt, so it never consumes a rate-limit slot.
    // Checked & marked BEFORE the balance check so a flood still throttles.
    if (!topUp) {
      if (lastPromptAt.has(npub)) {
        const last = lastPromptAt.get(npub);
        if (ts - last < cfg.rateLimitMs) {
          const retryIn = Math.ceil((cfg.rateLimitMs - (ts - last)) / 1000);
          throw httpError(429, 'rate_limited', `rate limited; retry in ${retryIn}s`, {
            retry_after_s: retryIn,
          });
        }
      }
      lastPromptAt.set(npub, ts);
      stmts.touchLastPrompt.run(ts, npub);
    }

    const newPromptCount = participant.prompt_count + (topUp ? 0 : 1);
    const scarcity = currentScarcity();
    const deduction = Math.round(est * ppt * scarcity);
    if (deduction <= 0) {
      // Nothing to charge — still counts as a prompt for rate-limit/counter
      // (a zero-cost topUp changes nothing).
      stmts.applyCharge.run(participant.balance, participant.total_spent, newPromptCount, ts, npub);
      return {
        ok: true,
        npub,
        est_tokens: est,
        price_per_token: ppt,
        scarcity_factor: scarcity,
        deduction: 0,
        balance_after: participant.balance,
        total_spent: participant.total_spent,
        prompt_count: newPromptCount,
        topUp,
        note: topUp ? 'zero-cost top-up' : 'zero-cost prompt (deduction rounded to 0)',
      };
    }
    if (participant.balance < deduction) {
      throw httpError(402, 'insufficient_tokens', 'insufficient tokens', {
        balance: participant.balance,
        required: deduction,
        deficit: deduction - participant.balance,
        scarcity_factor: scarcity,
        topUp,
      });
    }

    const newBalance = participant.balance - deduction;
    const newSpent = participant.total_spent + deduction;
    db.exec('BEGIN');
    try {
      stmts.applyCharge.run(newBalance, newSpent, newPromptCount, ts, npub);
      stmts.insertLedger.run(npub, -deduction, est, ppt, scarcity, topUp ? 'topup' : 'charge', newBalance, ts);
      db.exec('COMMIT');
    } catch (e) {
      db.exec('ROLLBACK');
      throw e;
    }
    return {
      ok: true,
      npub,
      est_tokens: est,
      price_per_token: ppt,
      scarcity_factor: scarcity,
      deduction,
      balance_after: newBalance,
      total_spent: newSpent,
      prompt_count: newPromptCount,
      topUp,
    };
  }

  function getLedger() {
    return stmts.allParticipants.all().map(row);
  }

  function recentTransactions(limit = 50) {
    return stmts.recentLedger.all(limit).map((r) => ({
      ...r,
      delta: Number(r.delta),
      balance_after: Number(r.balance_after),
      est_tokens: r.est_tokens == null ? null : Number(r.est_tokens),
      price_per_token: r.price_per_token == null ? null : Number(r.price_per_token),
      scarcity_factor: r.scarcity_factor == null ? null : Number(r.scarcity_factor),
    }));
  }

  function reset() {
    db.exec('BEGIN');
    try {
      stmts.clearLedger.run();
      stmts.clearParticipants.run();
      db.exec('COMMIT');
    } catch (e) {
      db.exec('ROLLBACK');
      throw e;
    }
    lastPromptAt.clear();
    return { ok: true, cleared: true };
  }

  // ── HTTP layer ───────────────────────────────────────────────────────────
  /**
   * Node http handler. Routes by method + path, parses JSON bodies, calls the
   * ledger methods, returns JSON. Task A1 may mount these same routes on its
   * own server instead by importing the methods directly.
   */
  async function handler(req, res) {
    const url = new URL(req.url, 'http://localhost');
    const path = url.pathname.replace(/\/+$/, '') || '/';
    const method = req.method;

    // CORS + JSON helpers for a local demo.
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET,POST,OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    if (method === 'OPTIONS') return send(res, 204);

    let body = {};
    if (method === 'POST' || method === 'PUT') {
      body = await readJson(req).catch(() => null);
      if (body === null) return send(res, 400, { error: 'bad_request', message: 'invalid JSON body' });
    }

    try {
      // ── GET routes ─────────────────────────────────────────────────────
      if (method === 'GET' && path === '/health') {
        return send(res, 200, { ok: true, service: 'token-ledger', ts: Date.now() });
      }
      if (method === 'GET' && path === '/snapshot') {
        return send(res, 200, snapshot());
      }
      if (method === 'GET' && path === '/ledger') {
        return send(res, 200, { participants: getLedger(), stats: snapshot() });
      }
      if (method === 'GET' && path === '/ledger/recent') {
        const limit = clampInt(url.searchParams.get('limit'), 1, 500, 50);
        return send(res, 200, { transactions: recentTransactions(limit) });
      }
      if (method === 'GET' && path === '/whitelist') {
        return send(res, 200, { whitelist: [...whitelistSet] });
      }

      // ── POST routes ────────────────────────────────────────────────────
      if (method === 'POST' && path === '/register') {
        const p = register(body.npub);
        return send(res, 200, { ok: true, participant: p });
      }

      if (method === 'POST' && path === '/admin/whitelist') {
        if (body.password !== cfg.adminPassword) {
          throw httpError(401, 'unauthorized', 'invalid admin password');
        }
        const r = addWhitelist(body.npub);
        return send(res, 200, { ok: true, ...r });
      }

      if (method === 'POST' && path === '/prompt') {
        // Standalone-friendly: estimate est_tokens from prompt text if omitted.
        const est_tokens = body.est_tokens != null
          ? body.est_tokens
          : estimateTokens(body.prompt);
        const ppt = body.price_per_token ?? cfg.basePricePerToken;
        const r = charge({ npub: body.npub, est_tokens, price_per_token: ppt });
        return send(res, 200, r);
      }

      if (method === 'POST' && path === '/charge') {
        // Explicit library-style charge (no token estimation).
        const r = charge({
          npub: body.npub,
          est_tokens: body.est_tokens,
          price_per_token: body.price_per_token,
        });
        return send(res, 200, r);
      }

      if (method === 'POST' && path === '/reset') {
        if (cfg.requireResetPassword && body.password !== cfg.adminPassword) {
          throw httpError(401, 'unauthorized', 'invalid admin password');
        }
        return send(res, 200, reset());
      }

      return send(res, 404, { error: 'not_found', message: `no route for ${method} ${path}` });
    } catch (e) {
      const status = e.httpStatus || 500;
      const payload = { error: e.code || 'internal_error', message: e.message };
      if (e.detail) payload.detail = e.detail;
      if (status >= 500) console.error('[token-ledger] internal error:', e);
      return send(res, status, payload);
    }
  }

  function listen({ port = Number(process.env.PORT) || 3002, host = '0.0.0.0' } = {}) {
    const server = createServer((req, res) => {
      handler(req, res).catch((e) => {
        console.error('[token-ledger] handler crash:', e);
        try { send(res, 500, { error: 'internal_error', message: String(e) }); } catch {}
      });
    });
    server.listen(port, host, () => {
      const snap = snapshot();
      console.log(`[token-ledger] listening on http://${host}:${port}`);
      console.log(`[token-ledger] db=${cfg.dbPath}`);
      console.log(`[token-ledger] whitelist=${cfg.whitelistPath} (${whitelistSet.size} npubs)`);
      console.log(`[token-ledger] starting_balance=${cfg.startingBalance} rate_limit=${cfg.rateLimitMs}ms scarcity=${snap.scarcity_factor}x`);
    });
    return server;
  }

  function close() {
    try { db.close(); } catch {}
  }

  return {
    cfg,
    // state queries
    isWhitelisted,
    snapshot,
    getLedger,
    recentTransactions,
    // mutations
    register,
    charge,
    addWhitelist,
    reset,
    // http
    handler,
    listen,
    close,
  };
}

// ── helpers ─────────────────────────────────────────────────────────────────

function httpError(status, code, message, detail) {
  const e = new Error(message);
  e.httpStatus = status;
  e.code = code;
  if (detail) e.detail = detail;
  return e;
}

// Normalise a sqlite row (BIGINT-ish → JS number) into plain JSON.
function row(r) {
  if (!r) return null;
  return {
    npub: r.npub,
    balance: Number(r.balance),
    granted: Number(r.granted),
    total_spent: Number(r.total_spent),
    prompt_count: Number(r.prompt_count),
    created_at: r.created_at,
    last_prompt_at: r.last_prompt_at == null ? null : Number(r.last_prompt_at),
  };
}

function clampInt(v, min, max, fallback) {
  const n = Math.floor(Number(v));
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}

// Rough token estimate from text (~4 chars/token). Standalone fallback only —
// Task A1 passes real est_tokens derived from the routed prompt.
function estimateTokens(text) {
  if (!text || typeof text !== 'string') return 1;
  return Math.max(1, Math.ceil(text.length / 4));
}

async function readJson(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const text = Buffer.concat(chunks).toString('utf8').trim();
  if (!text) return {};
  return JSON.parse(text);
}

function send(res, status, body) {
  const payload = body === undefined || status === 204 ? '' : JSON.stringify(body);
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json');
  res.end(payload);
}

// ── standalone entry ────────────────────────────────────────────────────────

if (import.meta.url === `file://${process.argv[1]}`) {
  const ledger = await createTokenLedger({
    dbPath: process.env.DB_PATH,
    whitelistPath: process.env.WHITELIST_PATH,
    adminPassword: process.env.DEMO_ADMIN_PASSWORD,
    startingBalance: intEnv('STARTING_BALANCE'),
    rateLimitMs: intEnv('RATE_LIMIT_MS'),
    basePricePerToken: numEnv('BASE_PRICE_PER_TOKEN'),
    totalDemoBudget: intEnv('TOTAL_DEMO_BUDGET') ?? null,
    requireResetPassword: process.env.REQUIRE_RESET_PASSWORD === '1',
  });
  ledger.listen();
}

function intEnv(name) {
  const v = process.env[name];
  if (v === undefined || v === '') return undefined;
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
}
function numEnv(name) {
  return intEnv(name);
}
