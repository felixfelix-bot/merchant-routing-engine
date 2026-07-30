// dashboard-server.mjs — Sovereign Engineering Demo Dashboard server (Task A1).
//
// Node.js 22 + node:sqlite HTTP + WebSocket server on localhost:3001.
// Serves the read-only dashboard data by reading the live z.ai usage DB
// directly (fast, <50ms), the Kalman price-state JSON, /proc, and the running
// z.ai proxy (:9099) for live quota + dispatch-gate signals. Demo prompts are
// routed through the proxy and token-charged via the shared token-ledger
// (Task A3, ./token-ledger.mjs).
//
// Spec: docs/PLAN-sovereign-demo.md §A1. API shapes: demo/API-CONTRACT.md.
//
// No external dependencies — Node 22 built-ins only (node:sqlite, node:http,
// node:crypto, node:fs). WebSocket is implemented by hand (RFC 6455 subset) so
// the demo runs fully offline with zero npm install.
//
//   node demo/src/dashboard-server.mjs                 # :3001
//   PORT=3002 node demo/src/dashboard-server.mjs        # alternate port
//
// Run the gate suite:
//   node demo/tests/verify-a1-gates.mjs

import { createServer } from 'node:http';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync, existsSync, statSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createTokenLedger, scarcityFactorForPct } from './token-ledger.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const DEMO_ROOT = resolve(__dirname, '..');

// ── configuration (env-overridable) ────────────────────────────────────────
const CFG = {
  port: intEnv('PORT', 3001),
  host: process.env.HOST || '0.0.0.0',
  zaiDb: process.env.ZAI_USAGE_DB || '/home/c03rad0r/.hermes/bot/zai_usage.db',
  burnDb: process.env.BURN_DB || '/home/c03rad0r/.hermes/bot/api_burn.db',
  kalmanState: process.env.KALMAN_STATE || '/home/c03rad0r/.hermes/bot/kalman_price_state.json',
  proxy: (process.env.PROXY_URL || 'http://localhost:9099').replace(/\/$/, ''),
  publicDir: resolve(DEMO_ROOT, 'public'),
  // Model used for demo prompts (cheap, deterministic for the live demo).
  demoModel: process.env.DEMO_MODEL || 'glm-4.5-flash',
  // Pricing knobs. cost_basis for the flat-rate z.ai keys is an amortisation
  // decision (true marginal cost ≈ 0), so it is configurable; metered keys
  // (ppq, ollama) derive their cost_basis from real spend data.
  flatKeyCostPerM: numEnv('FLAT_KEY_COST_PER_M', 0.02), // $/M for ours+friend
  ollamaMonthlyUsd: numEnv('OLLAMA_MONTHLY_USD', 100.0),
  margin: numEnv('MARGIN', 0.30), // fractional markup → your_price
  btcPriceUsd: numEnv('BTC_PRICE_USD', 100000), // only for sat→$ display of Kalman rate
  // Snapshot proxy-cache freshness (ms). Serving from cache keeps /api/snapshot
  // comfortably under the 50ms gate even when the proxy is slow.
  proxyCacheMs: intEnv('PROXY_CACHE_MS', 2000),
  wsPollMs: intEnv('WS_POLL_MS', 2000),
};

const round = (x, p = 4) => {
  const f = Math.pow(10, p);
  return Math.round((x + Number.EPSILON) * f) / f;
};

// ── DB connections (read-only on the live DBs; never write the proxy's DB) ──
let zaiDb = null, burnDb = null;
function openZai() {
  if (zaiDb) return zaiDb;
  zaiDb = new DatabaseSync(CFG.zaiDb, { readOnly: true });
  zaiDb.exec('PRAGMA query_only = ON');
  return zaiDb;
}
function openBurn() {
  if (burnDb) return burnDb;
  try { burnDb = new DatabaseSync(CFG.burnDb, { readOnly: true }); } catch { burnDb = null; }
  return burnDb;
}

function qall(db, sql, params = []) {
  try { return db.prepare(sql).all(...params); } catch { return []; }
}
function qone(db, sql, params = []) {
  try { return db.prepare(sql).get(...params); } catch { return null; }
}

// ── token-ledger integration (Task A3) ─────────────────────────────────────
const ledger = await createTokenLedger({
  dbPath: process.env.LEDGER_DB || join(DEMO_ROOT, 'demo-ledger.db'),
  whitelistPath: join(DEMO_ROOT, 'demo-whitelist.json'),
  adminPassword: process.env.DEMO_ADMIN_PASSWORD || 'sovereign-demo',
  startingBalance: intEnv('STARTING_BALANCE', 50_000),
  rateLimitMs: intEnv('RATE_LIMIT_MS', 5_000),
  basePricePerToken: 1.0,
});

// ── proxy fetchers (cached + refreshed in the background) ──────────────────
const proxyCache = { quota: null, gate: null, kalman: null, at: 0 };
function readKalmanFile() {
  try { return JSON.parse(readFileSync(CFG.kalmanState, 'utf8')); } catch { return null; }
}
async function fetchJson(url, timeoutMs) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, { signal: ctrl.signal });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; } finally { clearTimeout(t); }
}
async function refreshProxyCache() {
  const [quota, gate] = await Promise.all([
    fetchJson(`${CFG.proxy}/quota`, 2000),
    fetchJson(`${CFG.proxy}/v1/dispatch_gate`, 2000),
  ]);
  if (quota) proxyCache.quota = quota;
  if (gate) proxyCache.gate = gate;
  const kalman = readKalmanFile();
  if (kalman) proxyCache.kalman = kalman;
  proxyCache.at = Date.now();
}
await refreshProxyCache();               // warm before serving
setInterval(refreshProxyCache, CFG.proxyCacheMs);

// ── aggregate cache — expensive scans (1h/7d/30d, /proc, proxy) run in the
//    background so the snapshot request path stays well under the 50ms gate.
const agg = { pricing: null, distribution: null, costToday: 0, burnRate: 0, quota: {}, system: null, at: 0 };
function refreshAgg() {
  try {
    agg.pricing = computePricing();
    agg.distribution = computeDistribution();
    agg.costToday = computeCostToday();
    agg.burnRate = computeBurnRate();
    agg.quota = computeQuota();
    agg.system = computeSystem();
    agg.at = Date.now();
  } catch (e) { console.error('[agg] refresh error:', e.message); }
}
refreshAgg();
setInterval(refreshAgg, 8000);

// ── pricing (per API-CONTRACT.md `pricing`) ────────────────────────────────
function satToUsdPerM(priceSats) {
  // sats/token → $/M tokens via configurable BTC price (display only).
  return (priceSats || 0) * (CFG.btcPriceUsd / 1e8) * 1e6;
}

function computePricing() {
  const db = openZai();
  const now = Math.floor(Date.now() / 1000);
  const monthAgo = now - 30 * 86400;
  const weekAgo = now - 7 * 86400;

  // ollama_cloud: amortise the monthly flat fee over 30d of tokens.
  const ollamaTokens = qone(db,
    `SELECT COALESCE(SUM(total_tokens),0) v FROM api_calls
     WHERE key_name='ollama_cloud' AND ts > ?`, [monthAgo])?.v || 0;
  const ollamaCostPerM = ollamaTokens > 0
    ? CFG.ollamaMonthlyUsd / (ollamaTokens / 1e6)
    : CFG.ollamaMonthlyUsd / 1; // no traffic yet → cost/M undefined; floor

  // ppq: real $/M from the ppq_queries spend ledger (api_burn.db).
  let ppqCostPerM = CFG.flatKeyCostPerM * 1.5; // sensible default
  const bdb = openBurn();
  if (bdb) {
    const ppq = qone(bdb,
      `SELECT COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(total_tokens),0) tok
       FROM ppq_queries WHERE ts > ?`, [weekAgo]);
    if (ppq && ppq.tok > 0) ppqCostPerM = ppq.cost / (ppq.tok / 1e6);
  }

  // ours / friend: flat-rate keys. cost_basis is the configured amortised rate;
  // we also surface the Kalman converged sat-rate (converted) as kalman_rate.
  const kalman = proxyCache.kalman || {};
  const make = (costBasis) => {
    const yourPrice = costBasis * (1 + CFG.margin);
    const marginPct = yourPrice > 0 ? (yourPrice - costBasis) / yourPrice * 100 : 0;
    return {
      cost_basis: round(costBasis, 4),
      your_price: round(yourPrice, 4),
      margin_pct: round(marginPct, 2),
      effective_rate: round(yourPrice, 4),
    };
  };
  return {
    ours: make(CFG.flatKeyCostPerM),
    friend: make(CFG.flatKeyCostPerM),
    ollama: make(Math.max(0.001, ollamaCostPerM)),
    ppq: make(Math.max(0.001, ppqCostPerM)),
    _meta: {
      ollama_tokens_30d: ollamaTokens,
      ollama_monthly_usd: CFG.ollamaMonthlyUsd,
      ppq_cost_per_m_real: round(ppqCostPerM, 4),
      flat_key_cost_per_m: CFG.flatKeyCostPerM,
      margin: CFG.margin,
      kalman_rate_per_m: {
        ours: round(satToUsdPerM(kalman.ours?.price_sats), 5),
        friend: round(satToUsdPerM(kalman.friend?.price_sats), 5),
        ppq: round(satToUsdPerM(kalman.ppq?.price_sats), 5),
      },
    },
  };
}

// ── quota (Panel 3) — from proxy /quota, normalised to the contract shape ──
function computeQuota() {
  const q = proxyCache.quota;
  const out = {};
  if (!q) return out;
  for (const key of ['ours', 'friend']) {
    const k = q[key];
    if (!k) continue;
    const maxPct = k.max_pct ?? 0;
    // Prefer the 5-hour / token window for the live bar.
    const win = (k.windows || []).find(w => /hour|token/i.test(w.name || '') || w.type === 'TOKENS_LIMIT')
      || (k.windows || [])[0];
    const usedPct = win?.used_pct ?? maxPct ?? 0;
    const remaining = k.predictions?.[0]?.estimated_capacity_tokens ?? null;
    const resetsIn = win?.resets_at ? Math.max(0, Math.round((win.resets_at - Date.now() / 1000) / 60)) : null;
    out[key] = {
      used_pct: round(usedPct, 1),
      remaining,
      total: remaining != null ? Math.round(remaining / (1 - usedPct / 100)) : null,
      healthy: !k.locked,
      locked: !!k.locked,
      locked_window: k.locked_window || null,
      resets_in_min: resetsIn,
    };
  }
  return out;
}

// ── routing decisions (Panel 4) — last N from key_decisions + api_calls ────
// NOTE: a SQL correlated subquery joining key_decisions↔api_calls errors under
// node:sqlite ("no such column") and is slow; we fetch both recent sets by PK
// (indexed, fast) and join by nearest ts in JS instead.
function nearestCall(calls, ts) {
  let best = null, bd = Infinity;
  for (const c of calls) {
    const d = Math.abs(c.ts - ts);
    if (d < bd) { bd = d; best = c; }
  }
  return best;
}
function computeRequests(limit = 20) {
  const db = openZai();
  const decisions = qall(db,
    'SELECT id, ts, chosen_key, reason FROM key_decisions ORDER BY id DESC LIMIT ?', [limit]);
  if (!decisions.length) return [];
  const minTs = Math.min(...decisions.map(d => d.ts)) - 2;
  const calls = qall(db,
    'SELECT ts, key_name, model, total_tokens, duration_ms, status_code FROM api_calls WHERE ts > ? ORDER BY id DESC LIMIT 160',
    [minTs]);
  return decisions.map(d => {
    const c = nearestCall(calls, d.ts);
    return {
      ts: round(d.ts, 3),
      requester: requesterForTs(d.ts),
      provider: d.chosen_key || c?.key_name || null,
      model: c?.model || null,
      tokens: c?.total_tokens ?? 0,
      cost: c ? costForCall(c) : 0,
      reason: d.reason,
      status: c?.status_code ?? null,
      duration_ms: c?.duration_ms ?? null,
    };
  });
}

// Attach a demo requester npub when a token-ledger charge landed near this ts.
function requesterForTs(ts) {
  try {
    const txs = ledger.recentTransactions(40) || [];
    const hit = txs.find(t => t.reason === 'charge' && Math.abs((t.ts / 1000) - ts) < 4);
    return hit?.npub || null;
  } catch { return null; }
}

function costForCall(r) {
  const tokens = r.total_tokens ?? 0;
  if (tokens <= 0) return 0;
  const perM = r.key_name === 'ppq' ? (proxyCache.kalman ? 0 : 0)
    : r.key_name === 'ollama_cloud' ? CFG.ollamaMonthlyUsd / Math.max(1, 1)
    : CFG.flatKeyCostPerM;
  return round(tokens / 1e6 * (perM || CFG.flatKeyCostPerM), 6);
}

// ── provider distribution (Panel 5) — share of requests, last 1h ───────────
function computeDistribution() {
  const db = openZai();
  const since = Math.floor(Date.now() / 1000) - 3600;
  const rows = qall(db,
    `SELECT key_name, COUNT(*) c FROM api_calls
     WHERE ts > ? AND key_name IS NOT NULL GROUP BY key_name`, [since]);
  const total = rows.reduce((s, r) => s + r.c, 0) || 1;
  const dist = {};
  for (const r of rows) {
    const key = r.key_name === 'ollama_cloud' ? 'ollama' : r.key_name;
    dist[key] = round(r.c / total, 4);
  }
  return dist;
}

// ── cost today (Panel 6) — real spend from ppq + modelled flat-key cost ─────
function computeCostToday() {
  const db = openZai();
  const startOfDay = startOfTodayUnix();
  const byKey = qall(db,
    `SELECT key_name, COALESCE(SUM(total_tokens),0) tok
     FROM api_calls WHERE ts > ? AND key_name IS NOT NULL GROUP BY key_name`,
    [startOfDay]);
  let total = 0;
  for (const r of byKey) {
    const perM = r.key_name === 'ppq' ? ppqRealPerM()
      : r.key_name === 'ollama_cloud' ? ollamaAmortizedPerM()
      : CFG.flatKeyCostPerM;
    total += (r.tok / 1e6) * perM;
  }
  // add real ppq $ spend today from the burn ledger
  const bdb = openBurn();
  if (bdb) {
    const ppq = qone(bdb,
      `SELECT COALESCE(SUM(cost_usd),0) v FROM ppq_queries WHERE ts > ?`, [startOfDay]);
    if (ppq?.v) total += ppq.v;
  }
  return round(total, 4);
}
function computeBurnRate() {
  const db = openZai();
  const since = Math.floor(Date.now() / 1000) - 3600;
  const row = qone(db,
    `SELECT COALESCE(SUM(total_tokens),0) tok FROM api_calls WHERE ts > ? AND key_name IS NOT NULL`,
    [since]);
  const perM = CFG.flatKeyCostPerM; // blended approx
  return round((row?.tok || 0) / 1e6 * perM, 4);
}
function ppqRealPerM() {
  const bdb = openBurn();
  if (!bdb) return CFG.flatKeyCostPerM * 1.5;
  const r = qone(bdb,
    `SELECT COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(total_tokens),0) tok
     FROM ppq_queries WHERE ts > ?`, [Math.floor(Date.now() / 1000) - 7 * 86400]);
  return r && r.tok > 0 ? r.cost / (r.tok / 1e6) : CFG.flatKeyCostPerM * 1.5;
}
function ollamaAmortizedPerM() {
  const db = openZai();
  const since = Math.floor(Date.now() / 1000) - 30 * 86400;
  const tok = qone(db,
    `SELECT COALESCE(SUM(total_tokens),0) v FROM api_calls WHERE key_name='ollama_cloud' AND ts > ?`,
    [since])?.v || 0;
  return tok > 0 ? CFG.ollamaMonthlyUsd / (tok / 1e6) : CFG.ollamaMonthlyUsd;
}

// ── dispatch gate (Panel 7) — passthrough from proxy ───────────────────────
function computeGate() {
  const g = proxyCache.gate;
  if (!g) return { available: false };
  return {
    available: true,
    can_dispatch: g.can_dispatch,
    reason: g.reason,
    recommended_model: g.recommended_model,
    recommended_provider: g.recommended_provider,
    effective_price_per_m: round(g.effective_price_per_m ?? 0, 5),
    scarcity_factor: round(g.scarcity_factor ?? 1, 3),
    safety_margin: g.safety_margin,
    hours_until_exhaustion: g.hours_until_exhaust || g.hours_until_exhaustion,
    downgraded: !!g.downgraded,
    quota_state: g.quota_state,
    downgrade_chain: g.downgrade_chain,
  };
}

// ── system stats — latest system_readings row, fallback to /proc ───────────
function computeSystem() {
  const db = openZai();
  const r = qone(db, `SELECT * FROM system_readings ORDER BY id DESC LIMIT 1`);
  if (r) {
    return {
      cpu_pct: round(r.cpu_smoothed ?? r.load_per_core * 100 ?? 0, 1),
      mem_pct: round(r.mem_pct ?? 0, 1),
      load_per_core: round(r.load_per_core ?? 0, 2),
      swap_kb: r.swap_kb ?? 0,
      running_workers: r.running_workers ?? 0,
      source: 'system_readings',
      ts: r.ts,
    };
  }
  return readProcFallback();
}
function readProcFallback() {
  try {
    const loadavg = readFileSync('/proc/loadavg', 'utf8').split(' ');
    const meminfo = readFileSync('/proc/meminfo', 'utf8');
    const memTotal = parseInt((meminfo.match(/MemTotal:\s+(\d+)/) || [])[1] || '0', 10);
    const memAvail = parseInt((meminfo.match(/MemAvailable:\s+(\d+)/) || [])[1] || '0', 10);
    const memPct = memTotal > 0 ? (1 - memAvail / memTotal) * 100 : 0;
    return {
      cpu_pct: round(parseFloat(loadavg[0] || '0') * 100, 1),
      mem_pct: round(memPct, 1),
      load_per_core: round(parseFloat(loadavg[0] || '0'), 2),
      source: '/proc',
    };
  } catch { return { cpu_pct: 0, mem_pct: 0, source: 'none' }; }
}

// ── the single snapshot blob (GET /api/snapshot, <50ms from cache+local DB) ─
function buildSnapshot() {
  const gate = computeGate();
  const pricing = agg.pricing || computePricing();
  return {
    ts: Math.floor(Date.now() / 1000),
    pricing: pricing ? {
      ours: pricing.ours, friend: pricing.friend,
      ollama: pricing.ollama, ppq: pricing.ppq,
    } : {},
    pricing_meta: pricing?._meta || null,
    quota: agg.quota || computeQuota(),
    requests: computeRequests(20),
    provider_distribution: agg.distribution || computeDistribution(),
    dispatch_gate: gate,
    cost_today: agg.costToday ?? computeCostToday(),
    burn_rate_per_hour: agg.burnRate ?? computeBurnRate(),
    system: agg.system || computeSystem(),
    agg_age_ms: agg.at ? Date.now() - agg.at : null,
    proxy: { active: proxyCache.quota?.active || null, cached_at: proxyCache.at },
  };
}

// ── helpers ────────────────────────────────────────────────────────────────
function startOfTodayUnix() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return Math.floor(d.getTime() / 1000);
}
function intEnv(name, fallback) {
  const v = process.env[name];
  if (v === undefined || v === '') return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? (Number.isInteger(n) ? n : Math.floor(n)) : fallback;
}
function numEnv(name, fallback) {
  const v = process.env[name];
  if (v === undefined || v === '') return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}
function estimateTokens(text) {
  if (!text || typeof text !== 'string') return 1;
  return Math.max(1, Math.ceil(text.length / 4));
}

// ── prompt routing via the proxy ───────────────────────────────────────────
async function routeViaProxy(prompt) {
  const db = openZai();
  const preId = qone(db, 'SELECT COALESCE(MAX(id),0) v FROM api_calls')?.v || 0;
  const startTs = Date.now() / 1000;
  const body = {
    model: CFG.demoModel,
    messages: [{ role: 'user', content: prompt }],
    stream: false,
    max_tokens: 512,
  };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 60_000);
  let resp;
  try {
    resp = await fetch(`${CFG.proxy}/v1/chat/completions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
  } finally {
    clearTimeout(timer);
  }
  if (!resp.ok) {
    const txt = await resp.text().catch(() => '');
    const err = new Error(`proxy ${resp.status}`);
    err.httpStatus = 502;
    err.detail = txt.slice(0, 300);
    throw err;
  }
  // The proxy truncates the logged response body, so recover provider/model/
  // tokens from the row it writes to api_calls (robust under concurrency).
  const call = await findRoutedCall(preId, startTs, CFG.demoModel, 2000);
  const content = await resp.text().catch(() => '');
  let model = CFG.demoModel;
  try { model = JSON.parse(content).model || model; } catch {}
  // The proxy occasionally logs 0 tokens for a call; fall back through the
  // component counts, then a text estimate, so we always report real usage.
  const dbTotal = call?.total_tokens || 0;
  const dbParts = (call?.prompt_tokens || 0) + (call?.completion_tokens || 0);
  const est = estimateTokens(prompt);
  const tokens = dbTotal || dbParts || est;
  return {
    ok: true,
    provider: call?.key_name || null,
    model: call?.model || model,
    prompt_tokens: call?.prompt_tokens ?? null,
    completion_tokens: call?.completion_tokens ?? null,
    tokens,
    token_source: dbTotal ? 'db_total' : dbParts ? 'db_parts' : 'estimate',
    reason: call?.reason || null,
    duration_ms: call?.duration_ms ?? null,
  };
}
async function findRoutedCall(preId, startTs, model, maxWaitMs = 3000) {
  const db = openZai();
  const deadline = Date.now() + maxWaitMs;
  const since = startTs - 30; // generous clock-skew window
  const cols = 'id, ts, key_name, model, prompt_tokens, completion_tokens, total_tokens, duration_ms, status_code';
  while (Date.now() < deadline) {
    // Candidates: rows for our model arriving after preId in the skew window.
    // (Concurrent traffic can add same-model rows, so we pick by nearest ts.)
    const cands = qall(db,
      `SELECT ${cols} FROM api_calls WHERE id > ? AND ts >= ? AND model = ? ORDER BY id DESC LIMIT 12`,
      [preId, since, model]);
    let row = null;
    if (cands.length) {
      // prefer a candidate with non-zero tokens, nearest to our request time
      const scored = cands
        .map(c => ({ c, dist: Math.abs(c.ts - startTs), hasTok: (c.total_tokens || (c.prompt_tokens + c.completion_tokens)) > 0 }))
        .sort((a, b) => (b.hasTok - a.hasTok) || (a.dist - b.dist));
      row = scored[0].c;
    }
    if (!row) {
      // fallback: any new row after preId in the window
      row = qone(db,
        `SELECT ${cols} FROM api_calls WHERE id > ? AND ts >= ? ORDER BY id ASC LIMIT 1`,
        [preId, since]);
    }
    if (row) {
      const reason = qone(db,
        'SELECT reason FROM key_decisions WHERE ts BETWEEN ? AND ? ORDER BY id DESC LIMIT 1',
        [row.ts - 2, row.ts + 2]);
      row.reason = reason?.reason || null;
      return row;
    }
    await new Promise((r) => setTimeout(r, 120));
  }
  return null;
}

// ── minimal WebSocket (RFC 6455 subset, server→client text push only) ──────
const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const wsClients = new Set();
function wsAccept(key) {
  return createHash('sha1').update(key + WS_GUID).digest('base64');
}
function handleUpgrade(req, socket) {
  const key = req.headers['sec-websocket-key'];
  if (!key) { socket.destroy(); return; }
  socket.write(
    'HTTP/1.1 101 Switching Protocols\r\n' +
    'Upgrade: websocket\r\n' +
    'Connection: Upgrade\r\n' +
    `Sec-WebSocket-Accept: ${wsAccept(key)}\r\n\r\n`
  );
  const client = { socket, alive: true };
  wsClients.add(client);
  socket.on('data', (buf) => handleWsFrame(client, buf));
  socket.on('close', () => wsClients.delete(client));
  socket.on('error', () => wsClients.delete(client));
  socket.setTimeout(0);
}
function handleWsFrame(client, buf) {
  if (buf.length < 2) return;
  const opcode = buf[0] & 0x0f;
  if (opcode === 0x8) { try { client.socket.end(); } catch {} wsClients.delete(client); return; }
  if (opcode === 0x9) { // ping → pong (echo payload, unmasked)
    const payload = unmaskFrame(buf);
    sendWsFrame(client.socket, 0xa, payload);
    return;
  }
  // ignore client text/binary
}
function unmaskFrame(buf) {
  if (buf.length < 6) return Buffer.alloc(0);
  const maskLen = 4;
  let payloadLen = buf[1] & 0x7f;
  let off = 2;
  if (payloadLen === 126) { payloadLen = buf.readUInt16BE(2); off = 4; }
  else if (payloadLen === 127) { payloadLen = Number(buf.readBigUInt64BE(2)); off = 8; }
  const mask = buf.slice(off, off + maskLen);
  const data = buf.slice(off + maskLen, off + maskLen + payloadLen);
  for (let i = 0; i < data.length; i++) data[i] ^= mask[i % 4];
  return data;
}
function sendWsFrame(socket, opcode, payload) {
  payload = Buffer.isBuffer(payload) ? payload : Buffer.from(payload);
  let header;
  if (payload.length < 126) {
    header = Buffer.alloc(2); header[1] = payload.length;
  } else if (payload.length < 65536) {
    header = Buffer.alloc(4); header[1] = 126; header.writeUInt16BE(payload.length, 2);
  } else {
    header = Buffer.alloc(10); header[1] = 127; header.writeBigUInt64BE(BigInt(payload.length), 2);
  }
  header[0] = 0x80 | opcode;
  try { socket.write(Buffer.concat([header, payload])); } catch {}
}
function broadcastWs(obj) {
  if (wsClients.size === 0) return;
  const msg = JSON.stringify(obj);
  for (const c of wsClients) {
    try { sendWsFrame(c.socket, 0x1, msg); }
    catch { wsClients.delete(c); }
  }
}

// Background poll: push new routing events within ~wsPollMs of DB insert.
let lastSeenDecisionId = null;
function startStreamPoller() {
  const db = openZai();
  lastSeenDecisionId = qone(db, 'SELECT COALESCE(MAX(id),0) v FROM key_decisions')?.v || 0;
  setInterval(() => {
    try {
      const rows = qall(db,
        'SELECT id, ts, chosen_key, reason FROM key_decisions WHERE id > ? ORDER BY id ASC',
        [lastSeenDecisionId]);
      if (!rows.length) return;
      lastSeenDecisionId = rows[rows.length - 1].id;
      const minTs = Math.min(...rows.map(r => r.ts)) - 2;
      const calls = qall(db,
        'SELECT ts, key_name, model, total_tokens FROM api_calls WHERE ts > ? ORDER BY id DESC LIMIT 200',
        [minTs]);
      for (const r of rows) {
        const c = nearestCall(calls, r.ts);
        broadcastWs({
          type: 'request',
          data: {
            ts: round(r.ts, 3),
            requester: requesterForTs(r.ts),
            provider: r.chosen_key || c?.key_name || null,
            model: c?.model || null,
            tokens: c?.total_tokens ?? 0,
            cost: c ? costForCall(c) : 0,
            reason: r.reason,
          },
        });
      }
    } catch (e) {
      console.error('[stream] poll error:', e.message);
    }
  }, CFG.wsPollMs);
}

// ── HTTP routing ───────────────────────────────────────────────────────────
async function readJson(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const text = Buffer.concat(chunks).toString('utf8').trim();
  if (!text) return {};
  return JSON.parse(text);
}
function send(res, status, body, headers = {}) {
  const payload = body === undefined || status === 204 ? '' : JSON.stringify(body);
  res.writeHead(status, { 'Content-Type': 'application/json', ...headers });
  res.end(payload);
}

async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') return send(res, 204);

  const url = new URL(req.url, `http://${CFG.host}:${CFG.port}`);
  const path = url.pathname.replace(/\/+$/, '') || '/';
  const method = req.method;

  try {
    // ── health ──
    if (method === 'GET' && path === '/health') {
      return send(res, 200, {
        ok: true, service: 'dashboard-server', ts: Date.now(),
        uptime_s: Math.round(process.uptime()),
        proxy_active: proxyCache.quota?.active || null,
        ws_clients: wsClients.size,
      });
    }

    // ── snapshot (<50ms) ──
    if (method === 'GET' && path === '/api/snapshot') {
      const t0 = process.hrtime.bigint();
      const snap = buildSnapshot();
      const ms = Number(process.hrtime.bigint() - t0) / 1e6;
      snap._ms = round(ms, 2);
      return send(res, 200, snap);
    }

    // ── HTML dashboard (serves A2's public/index.html, inline fallback) ──
    if (method === 'GET' && (path === '/' || path === '/index.html')) {
      const file = join(CFG.publicDir, 'index.html');
      if (existsSync(file)) {
        const html = readFileSync(file, 'utf8');
        return send(res, 200, html, { 'Content-Type': 'text/html; charset=utf-8' });
      }
      return send(res, 200, fallbackHTML(), { 'Content-Type': 'text/html; charset=utf-8' });
    }

    // ── token economy: /ledger (Panel 2) ──
    if (method === 'GET' && path === '/ledger') {
      const snap = ledger.snapshot();
      const gate = computeGate();
      const effPerM = (gate.effective_price_per_m || 0) * (gate.scarcity_factor || 1);
      const participants = ledger.getLedger().map(p => ({
        npub: p.npub,
        balance: p.balance,
        spent: p.total_spent,
        prompts: p.prompt_count,
      }));
      return send(res, 200, {
        participants,
        current_price_per_token: round(effPerM / 1e6, 8),
        scarcity_factor: snap.scarcity_factor,
        scarcity_band: snap.scarcity_band,
        starting_balance: snap.starting_balance,
        total_budget: snap.total_budget,
        total_consumed: snap.total_spent,
      });
    }

    if (method === 'GET' && path === '/ledger/recent') {
      const limit = Math.min(500, Math.max(1, parseInt(url.searchParams.get('limit') || '50', 10)));
      return send(res, 200, { transactions: ledger.recentTransactions(limit) });
    }

    // ── register (Panel 2) ──
    if (method === 'POST' && path === '/register') {
      const body = await readJson(req);
      try {
        const p = ledger.register(body.npub);
        return send(res, 200, { ok: true, npub: p.npub, balance: p.balance });
      } catch (e) {
        return send(res, e.httpStatus || 500, {
          ok: false,
          error: e.message,
          ...(e.httpStatus === 409 ? { balance: e.detail?.participant?.balance } : {}),
        });
      }
    }

    // ── admin whitelist ──
    if (method === 'POST' && path === '/admin/whitelist') {
      const body = await readJson(req);
      if (body.password !== ledger.cfg.adminPassword) {
        return send(res, 401, { ok: false, error: 'invalid admin password' });
      }
      const r = ledger.addWhitelist(body.npub);
      return send(res, 200, { ok: true, ...r });
    }

    // ── prompt (Panel 2): route via proxy + charge tokens ──
    if (method === 'POST' && path === '/prompt') {
      const body = await readJson(req);
      if (!body.prompt || typeof body.prompt !== 'string') {
        return send(res, 400, { ok: false, error: 'prompt required' });
      }
      const npub = body.requester_npub || body.npub || null;
      const est = estimateTokens(body.prompt);

      // Charge FIRST (checks rate limit + balance) so we never spend quota on a
      // prompt that can't be paid for. Unregistered/anonymous → route only.
      let chargeResult = null;
      if (npub) {
        try {
          chargeResult = ledger.charge({ npub, est_tokens: est, price_per_token: 1.0 });
        } catch (e) {
          if (e.httpStatus === 404) {
            // not registered — allow anonymous routing, no balance change
          } else {
            return send(res, e.httpStatus || 500, { ok: false, error: e.message, ...(e.detail || {}) });
          }
        }
      }

      // Route through the proxy.
      let routed;
      try {
        routed = await routeViaProxy(body.prompt);
      } catch (e) {
        return send(res, e.httpStatus || 502, { ok: false, error: 'proxy error', detail: e.message });
      }

      const gate = computeGate();
      const pricePerM = round((gate.effective_price_per_m || 0) * (gate.scarcity_factor || 1), 5);
      const cost = round((routed.tokens || 0) / 1e6 * pricePerM, 6);

      // If real tokens materially exceeded the estimate, top up the charge so
      // the ledger reflects actual usage (best-effort, never blocks the reply).
      if (chargeResult && routed.tokens > est * 1.5) {
        try {
          const extra = Math.max(0, routed.tokens - est);
          chargeResult = ledger.charge({ npub, est_tokens: extra, price_per_token: 1.0 });
        } catch { /* rate-limit on top-up: ignore */ }
      }

      return send(res, 200, {
        ok: true,
        provider: routed.provider,
        model: routed.model,
        tokens: routed.tokens,
        prompt_tokens: routed.prompt_tokens,
        completion_tokens: routed.completion_tokens,
        cost,
        price_per_m: pricePerM,
        balance_after: chargeResult?.balance_after ?? null,
        scarcity_factor: chargeResult?.scarcity_factor ?? gate.scarcity_factor ?? null,
        reason: routed.reason,
        duration_ms: routed.duration_ms,
      });
    }

    // ── reset (demo restart) ──
    if (method === 'POST' && path === '/reset') {
      const body = await readJson(req).catch(() => ({}));
      if (ledger.cfg.requireResetPassword && body.password !== ledger.cfg.adminPassword) {
        return send(res, 401, { ok: false, error: 'invalid admin password' });
      }
      return send(res, 200, ledger.reset());
    }

    if (method === 'GET' && path === '/snapshot-econ') {
      return send(res, 200, ledger.snapshot());
    }

    return send(res, 404, { error: 'not_found', path });
  } catch (e) {
    console.error('[dashboard] handler error:', e);
    return send(res, 500, { error: 'internal_error', message: e.message });
  }
}

function fallbackHTML() {
  return `<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sovereign Demo Dashboard</title>
<style>
body{background:#0d1117;color:#c9d1d9;font-family:system-ui,sans-serif;margin:0;padding:2rem}
h1{font-size:1.4rem}pre{background:#161b22;padding:1rem;border-radius:8px;overflow:auto}
.badge{display:inline-block;background:#1f6feb;color:#fff;padding:2px 8px;border-radius:12px;font-size:.8rem}
</style></head><body>
<h1>Sovereign Engineering Demo <span class="badge">A1 server up</span></h1>
<p>Dashboard HTML (<code>public/index.html</code>) not found — serving the live
<code>/api/snapshot</code> below. Drop A2's <code>index.html</code> into
<code>demo/public/</code> for the full 7-panel UI.</p>
<pre id="snap">loading…</pre>
<script>
async function tick(){try{const r=await fetch('/api/snapshot');const j=await r.json();
document.getElementById('snap').textContent=JSON.stringify(j,null,2);}catch(e){
document.getElementById('snap').textContent='(fetch failed) '+e;}}
tick();setInterval(tick,5000);
</script></body></html>`;
}

// ── boot ───────────────────────────────────────────────────────────────────
const server = createServer((req, res) => {
  handler(req, res).catch((e) => {
    console.error('[dashboard] crash:', e);
    try { send(res, 500, { error: 'internal_error' }); } catch {}
  });
});
server.on('upgrade', (req, socket) => {
  const url = new URL(req.url, `http://${CFG.host}:${CFG.port}`);
  if (url.pathname === '/stream') return handleUpgrade(req, socket);
  socket.destroy();
});

server.listen(CFG.port, CFG.host, () => {
  console.log(`[dashboard] http://${CFG.host}:${CFG.port}  (model=${CFG.demoModel})`);
  console.log(`[dashboard] snapshot <50ms · WS /stream polls every ${CFG.wsPollMs}ms`);
  console.log(`[dashboard] zai_db=${CFG.zaiDb}`);
  console.log(`[dashboard] proxy=${CFG.proxy}  active=${proxyCache.quota?.active || '?'}`);
  startStreamPoller();
});

// graceful shutdown — close DB handles
for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    console.log(`[dashboard] ${sig} — shutting down`);
    try { ledger.close(); } catch {}
    try { zaiDb?.close(); } catch {}
    try { burnDb?.close(); } catch {}
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 1000).unref();
  });
}

export { buildSnapshot, computePricing, routeViaProxy, CFG };
