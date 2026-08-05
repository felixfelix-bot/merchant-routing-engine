/**
 * Sovereign Engineering Demo — CVM Server
 *
 * Direct nostr-tools implementation of the ContextVM wire protocol.
 * Exposes 5 tools (get_snapshot, send_prompt, register_participant,
 * get_price_history, get_ledger) over Nostr relays via kind 25910
 * messages wrapped in NIP-59 gift wrap (kind 1059).
 *
 * NO @contextvm/sdk — its transport (NostrServerTransport / ApplesauceRelayPool)
 * silently hangs. This direct implementation is simpler, debuggable, and works.
 *
 * Run:  bun src/cvm-server.ts     (NOT 'bun run' — it swallows console output)
 */

import { Database } from "bun:sqlite";
import {
  finalizeEvent,
  generateSecretKey,
  getPublicKey,
  nip44,
} from "nostr-tools";
import { Relay } from "nostr-tools/relay";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// ═══════════════════════════════════════════════════════════════════════════
// PATHS & CONFIG
// ═══════════════════════════════════════════════════════════════════════════

const __dirname = dirname(fileURLToPath(import.meta.url));
const DEMO_ROOT = resolve(__dirname, "../.."); // merchant-routing-engine/demo

const CFG = {
  relays: [
    "wss://nostr.mom",
    "wss://relay.primal.net",
    "wss://nos.lol",
  ],
  // Live data sources
  zaiDbPath: "/home/c03rad0r/.hermes/bot/zai_usage.db",
  burnDbPath: "/home/c03rad0r/.hermes/bot/api_burn.db",
  kalmanStatePath: "/home/c03rad0r/.hermes/bot/kalman_price_state.json",
  proxyUrl: (process.env.PROXY_URL || "http://localhost:9099").replace(/\/$/, ""),
  // Demo economy
  ledgerDbPath: process.env.LEDGER_DB || join(DEMO_ROOT, "demo_ledger.db"),
  whitelistPath: join(DEMO_ROOT, "demo-whitelist.json"),
  keyFilePath: join(DEMO_ROOT, "cvm-server-key.json"),
  startingBalance: 50_000,
  rateLimitMs: 5_000,       // 1 prompt / 5s / npub
  basePricePerToken: 1.0,   // demo-tokens per estimated-token
  // Pricing model
  flatKeyCostPerM: 0.02,    // $/M for ours+friend (flat-rate keys)
  ollamaMonthlyUsd: 100.0,
  margin: 0.266,            // 21% displayed margin (0.266 markup = 21% of sell price)
  btcPriceUsd: 100_000,
  demoModel: process.env.DEMO_MODEL || "glm-4.5-flash",
  proxyCacheMs: 2_000,
};

// Scarcity bands: <20%=1.0x, 20-40%=1.2x, 40-60%=1.5x, 60-80%=1.8x, >80%=2.0x
const SCARCITY_BANDS = [
  { ceiling: 20, factor: 1.0 },
  { ceiling: 40, factor: 1.2 },
  { ceiling: 60, factor: 1.5 },
  { ceiling: 80, factor: 1.8 },
  { ceiling: Infinity, factor: 2.0 },
];

function scarcityFactorForPct(pct: number): number {
  for (const band of SCARCITY_BANDS) {
    if (pct < band.ceiling) return band.factor;
  }
  return 2.0;
}

function scarcityBandLabel(pct: number): string {
  for (const band of SCARCITY_BANDS) {
    if (pct < band.ceiling) {
      return band.factor === 2.0 ? "≥80%" : `<${band.ceiling}%`;
    }
  }
  return "≥80%";
}

// ═══════════════════════════════════════════════════════════════════════════
// NOSTR KEY MANAGEMENT
// ═══════════════════════════════════════════════════════════════════════════

function loadOrCreateServerKey(): { hex: string; sk: Uint8Array; pk: string } {
  let keyData: { hex?: string; npub?: string } = {};

  // Try to load existing key
  if (existsSync(CFG.keyFilePath)) {
    try {
      keyData = JSON.parse(readFileSync(CFG.keyFilePath, "utf8"));
    } catch { /* corrupt file, regenerate */ }
  }

  // Validate existing hex (must be exactly 64 hex chars)
  if (keyData.hex && /^[0-9a-f]{64}$/i.test(keyData.hex.trim())) {
    const hex = keyData.hex.trim();
    const sk = new Uint8Array(hex.match(/.{2}/g)!.map((b) => parseInt(b, 16)));
    const pk = getPublicKey(sk);
    console.log(`[key] Loaded persistent server key from ${CFG.keyFilePath}`);
    return { hex, sk, pk };
  }

  // Generate new key
  const sk = generateSecretKey();
  const hex = Array.from(sk).map((b) => b.toString(16).padStart(2, "0")).join("");
  const pk = getPublicKey(sk);

  // Persist — verify 64 hex chars + newline (truncation guard)
  const keyFile = {
    hex,
    npub: pk,
    created_at: new Date().toISOString(),
    note: "CVM server key for Sovereign Engineering demo. Do NOT share the hex.",
  };
  const content = JSON.stringify(keyFile, null, 2) + "\n";
  writeFileSync(CFG.keyFilePath, content, { mode: 0o600 });

  // Verify write (truncation check)
  const readBack = JSON.parse(readFileSync(CFG.keyFilePath, "utf8"));
  if (!readBack.hex || readBack.hex.length !== 64) {
    throw new Error(`Key file truncation detected! Expected 64 hex chars, got ${readBack.hex?.length}. Aborting.`);
  }

  console.log(`[key] Generated new server key, saved to ${CFG.keyFilePath}`);
  return { hex, sk, pk };
}

const { sk: serverSk, pk: serverPk } = loadOrCreateServerKey();
console.log(`[key] Server pubkey: ${serverPk}`);

// ═══════════════════════════════════════════════════════════════════════════
// DATABASE CONNECTIONS
// ═══════════════════════════════════════════════════════════════════════════

const zaiDb = new Database(CFG.zaiDbPath, { readonly: true });
let burnDb: Database | null = null;
try {
  burnDb = new Database(CFG.burnDbPath, { readonly: true });
} catch {
  console.log("[db] api_burn.db not found — ppq pricing will use fallback");
}

// ── Token Ledger (demo_ledger.db) ──────────────────────────────────────────

const ledgerDb = new Database(CFG.ledgerDbPath);
ledgerDb.exec(`
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
    delta           INTEGER NOT NULL,
    est_tokens      INTEGER,
    price_per_token REAL,
    scarcity_factor REAL,
    reason          TEXT    NOT NULL,
    balance_after   INTEGER NOT NULL,
    ts              INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_ledger_npub ON demo_ledger(npub);
  CREATE INDEX IF NOT EXISTS idx_ledger_ts   ON demo_ledger(ts);
`);

// ── Whitelist ──────────────────────────────────────────────────────────────

function loadWhitelist(): Set<string> {
  if (!existsSync(CFG.whitelistPath)) return new Set();
  try {
    const raw = JSON.parse(readFileSync(CFG.whitelistPath, "utf8"));
    if (!Array.isArray(raw)) return new Set();
    return new Set(raw.map((e: any) => (typeof e === "string" ? e : e?.npub)).filter((s: any) => typeof s === "string" && s.length > 0));
  } catch {
    return new Set();
  }
}

const whitelist = loadWhitelist();

// ── Rate limiting (in-memory) ──────────────────────────────────────────────

const lastPromptAt = new Map<string, number>(); // npub → epoch ms

// ═══════════════════════════════════════════════════════════════════════════
// HELPER FUNCTIONS
// ═══════════════════════════════════════════════════════════════════════════

const round = (x: number, p = 4): number => {
  const f = Math.pow(10, p);
  return Math.round((x + Number.EPSILON) * f) / f;
};

function qall(db: Database, sql: string, params: any[] = []): any[] {
  try { return db.prepare(sql).all(...params); } catch { return []; }
}

function qone(db: Database, sql: string, params: any[] = []): any {
  try { return db.prepare(sql).get(...params); } catch { return null; }
}

function estimateTokens(text: string): number {
  if (!text || typeof text !== "string") return 1;
  return Math.max(1, Math.ceil(text.length / 4));
}

function startOfTodayUnix(): number {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return Math.floor(d.getTime() / 1000);
}

function shortNpub(npub: string): string {
  if (!npub || npub.length < 16) return npub || "";
  return npub.slice(0, 12) + "…" + npub.slice(-4);
}

function satToUsdPerM(priceSats: number): number {
  return (priceSats || 0) * (CFG.btcPriceUsd / 1e8) * 1e6;
}

// ═══════════════════════════════════════════════════════════════════════════
// PROXY CACHE (refreshed in background)
// ═══════════════════════════════════════════════════════════════════════════

const proxyCache: { quota: any; gate: any; kalman: any; at: number } = {
  quota: null, gate: null, kalman: null, at: 0,
};

function readKalmanFile(): any {
  try { return JSON.parse(readFileSync(CFG.kalmanStatePath, "utf8")); } catch { return null; }
}

async function fetchJson(url: string, timeoutMs: number): Promise<any> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, { signal: ctrl.signal });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; } finally { clearTimeout(t); }
}

async function refreshProxyCache(): Promise<void> {
  const [quota, gate] = await Promise.all([
    fetchJson(`${CFG.proxyUrl}/quota`, 2000),
    fetchJson(`${CFG.proxyUrl}/v1/dispatch_gate`, 2000),
  ]);
  if (quota) proxyCache.quota = quota;
  if (gate) proxyCache.gate = gate;
  const kalman = readKalmanFile();
  if (kalman) proxyCache.kalman = kalman;
  proxyCache.at = Date.now();
}

// Warm cache on startup
await refreshProxyCache();
setInterval(() => refreshProxyCache().catch(() => {}), CFG.proxyCacheMs);

// ═══════════════════════════════════════════════════════════════════════════
// PRICING COMPUTATION
// ═══════════════════════════════════════════════════════════════════════════

function computePricing(): any {
  const now = Math.floor(Date.now() / 1000);
  const monthAgo = now - 30 * 86400;
  const weekAgo = now - 7 * 86400;

  // Ollama: amortise monthly flat fee over 30d tokens
  const ollamaTokens = qone(zaiDb,
    `SELECT COALESCE(SUM(total_tokens),0) v FROM api_calls WHERE key_name='ollama_cloud' AND ts > ?`, [monthAgo])?.v || 0;
  const ollamaCostPerM = ollamaTokens > 0
    ? CFG.ollamaMonthlyUsd / (ollamaTokens / 1e6)
    : CFG.ollamaMonthlyUsd;

  // ppq: real $/M from burn ledger
  let ppqCostPerM = CFG.flatKeyCostPerM * 1.5;
  if (burnDb) {
    const ppq = qone(burnDb,
      `SELECT COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(total_tokens),0) tok FROM ppq_queries WHERE ts > ?`, [weekAgo]);
    if (ppq && ppq.tok > 0) ppqCostPerM = ppq.cost / (ppq.tok / 1e6);
  }

  const kalman = proxyCache.kalman || {};
  const make = (costBasis: number) => {
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

// ═══════════════════════════════════════════════════════════════════════════
// QUOTA, REQUESTS, SYSTEM, COST COMPUTATIONS
// ═══════════════════════════════════════════════════════════════════════════

// Ollama API cache — avoid hitting ollama.com on every snapshot
let ollamaApiCache: { at: number; session: number; weekly: number } = { at: 0, session: 0, weekly: 0 };
const OLLAMA_CACHE_TTL = 30_000; // 30s
// Thundering-herd guard: prevents concurrent computeQuota() calls from both
// firing Ollama API fetches at once.
let ollamaFetching = false;

async function computeQuota(): Promise<any> {
  const q = proxyCache.quota;
  const out: any = {};
  // z.ai flat-rate keys — real quota windows from proxy
  if (q) {
    for (const key of ["ours", "friend"]) {
      const k = q[key];
      if (!k) continue;
      const win = (k.windows || []).find((w: any) => /hour|token/i.test(w.name || "") || w.type === "TOKENS_LIMIT")
        || (k.windows || [])[0];
      const usedPct = win?.used_pct ?? 0;
      out[key] = {
        used_pct: round(usedPct, 1),
        remaining: k.predictions?.[0]?.estimated_capacity_tokens ?? null,
        healthy: !k.locked,
        locked: !!k.locked,
        resets_in_min: win?.resets_at ? Math.max(0, Math.round((win.resets_at - Date.now() / 1000) / 60)) : null,
      };
    }
  }
  // Ollama Cloud — real quota from ollama.com/api/usage (cached 30s)
  let ollamaSessionPct = ollamaApiCache.session;
  let ollamaWeeklyPct = ollamaApiCache.weekly;
  if (Date.now() - ollamaApiCache.at > OLLAMA_CACHE_TTL && !ollamaFetching) {
    const ollamaKey = process.env.OLLAMA_CLOUD_API_KEY || "";
    if (ollamaKey) {
      ollamaFetching = true;
      try {
        const resp = await fetch("https://ollama.com/api/usage", {
          headers: { Authorization: `Bearer ${ollamaKey}` },
          signal: AbortSignal.timeout(2000),
        });
        if (resp.ok) {
          const data = await resp.json() as any;
          ollamaSessionPct = round((data?.limits?.session?.usage || 0) * 100, 1);
          ollamaWeeklyPct = round((data?.limits?.weekly?.usage || 0) * 100, 1);
          ollamaApiCache = { at: Date.now(), session: ollamaSessionPct, weekly: ollamaWeeklyPct };
        } else {
          // Non-OK response — still update cache timestamp so TTL backoff
          // applies. Otherwise the next 5s tick would re-fetch immediately,
          // causing ~720 req/hour against ollama.com when the API is down.
          ollamaApiCache.at = Date.now();
        }
      } catch (e: any) {
        console.warn(`[cvm] ollama quota fetch failed: ${e.message}`);
        // Update cache timestamp on failure too — this is the MEDIUM cache
        // stampede fix. A failed fetch should still back off for OLLAMA_CACHE_TTL,
        // otherwise setInterval ticks every 5s would each fire a new request
        // while the API is down.
        ollamaApiCache.at = Date.now();
      } finally {
        ollamaFetching = false;
      }
    }
  }
  out.ollama = {
    used_pct: ollamaSessionPct,
    weekly_pct: ollamaWeeklyPct,
    remaining: null,
    healthy: true,
    locked: false,
    resets_in_min: 300,
    note: `session ${ollamaSessionPct}% / weekly ${ollamaWeeklyPct}%`,
  };
  // PPQ — pay-per-use, no quota cap
  out.ppq = {
    used_pct: 0,
    remaining: null,
    healthy: true,
    locked: false,
    resets_in_min: null,
    note: "pay-per-use — no quota cap",
  };
  return out;
}

function computeRequests(limit = 20): any[] {
  return qall(zaiDb,
    `SELECT d.ts, d.chosen_key as provider, d.reason,
            a.model, a.total_tokens as tokens, a.key_name,
            a.prompt_tokens, a.completion_tokens, a.duration_ms, a.status_code
     FROM key_decisions d
     LEFT JOIN api_calls a
       ON a.id = (SELECT id FROM api_calls WHERE ts BETWEEN d.ts-1.5 AND d.ts+1.5
                  ORDER BY ABS(ts-d.ts) ASC LIMIT 1)
     ORDER BY d.id DESC LIMIT ?`, [limit]).map((r: any) => ({
    ts: round(r.ts, 3),
    provider: r.provider || r.key_name,
    model: r.model,
    tokens: r.tokens ?? 0,
    cost: costForCall(r),
    reason: r.reason,
    status: r.status_code,
    duration_ms: r.duration_ms,
  }));
}

function costForCall(r: any): number {
  const tokens = r.tokens ?? 0;
  if (tokens <= 0) return 0;
  const perM = r.key_name === "ppq" ? 0 : r.key_name === "ollama_cloud" ? CFG.ollamaMonthlyUsd : CFG.flatKeyCostPerM;
  return round(tokens / 1e6 * (perM || CFG.flatKeyCostPerM), 6);
}

function computeDistribution(): any {
  const since = Math.floor(Date.now() / 1000) - 3600;
  const rows = qall(zaiDb,
    `SELECT key_name, COUNT(*) c FROM api_calls WHERE ts > ? AND key_name IS NOT NULL GROUP BY key_name`, [since]);
  const total = rows.reduce((s, r) => s + r.c, 0) || 1;
  const dist: any = {};
  for (const r of rows) {
    const key = r.key_name === "ollama_cloud" ? "ollama" : r.key_name;
    dist[key] = round(r.c / total, 4);
  }
  return dist;
}

function computeCostToday(): number {
  const startOfDay = startOfTodayUnix();
  const byKey = qall(zaiDb,
    `SELECT key_name, COALESCE(SUM(total_tokens),0) tok FROM api_calls WHERE ts > ? AND key_name IS NOT NULL GROUP BY key_name`,
    [startOfDay]);
  let total = 0;
  for (const r of byKey) {
    const perM = r.key_name === "ppq" ? ppqRealPerM()
      : r.key_name === "ollama_cloud" ? ollamaAmortizedPerM()
      : CFG.flatKeyCostPerM;
    total += (r.tok / 1e6) * perM;
  }
  if (burnDb) {
    const ppq = qone(burnDb, `SELECT COALESCE(SUM(cost_usd),0) v FROM ppq_queries WHERE ts > ?`, [startOfDay]);
    if (ppq?.v) total += ppq.v;
  }
  return round(total, 4);
}

function ppqRealPerM(): number {
  if (!burnDb) return CFG.flatKeyCostPerM * 1.5;
  const r = qone(burnDb,
    `SELECT COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(total_tokens),0) tok FROM ppq_queries WHERE ts > ?`,
    [Math.floor(Date.now() / 1000) - 7 * 86400]);
  return r && r.tok > 0 ? r.cost / (r.tok / 1e6) : CFG.flatKeyCostPerM * 1.5;
}

function ollamaAmortizedPerM(): number {
  const since = Math.floor(Date.now() / 1000) - 30 * 86400;
  const tok = qone(zaiDb,
    `SELECT COALESCE(SUM(total_tokens),0) v FROM api_calls WHERE key_name='ollama_cloud' AND ts > ?`, [since])?.v || 0;
  return tok > 0 ? CFG.ollamaMonthlyUsd / (tok / 1e6) : CFG.ollamaMonthlyUsd;
}

function computeSystem(): any {
  const r = qone(zaiDb, `SELECT * FROM system_readings ORDER BY id DESC LIMIT 1`);
  if (r) {
    return {
      cpu_pct: round(r.cpu_smoothed ?? r.load_per_core * 100 ?? 0, 1),
      mem_pct: round(r.mem_pct ?? 0, 1),
      load_per_core: round(r.load_per_core ?? 0, 2),
      running_workers: r.running_workers ?? 0,
      source: "system_readings",
      ts: r.ts,
    };
  }
  return readProcFallback();
}

function readProcFallback(): any {
  try {
    const loadavg = readFileSync("/proc/loadavg", "utf8").split(" ");
    const meminfo = readFileSync("/proc/meminfo", "utf8");
    const memTotal = parseInt((meminfo.match(/MemTotal:\s+(\d+)/) || [])[1] || "0", 10);
    const memAvail = parseInt((meminfo.match(/MemAvailable:\s+(\d+)/) || [])[1] || "0", 10);
    const memPct = memTotal > 0 ? (1 - memAvail / memTotal) * 100 : 0;
    return {
      cpu_pct: round(parseFloat(loadavg[0] || "0") * 100, 1),
      mem_pct: round(memPct, 1),
      load_per_core: round(parseFloat(loadavg[0] || "0"), 2),
      source: "/proc",
    };
  } catch {
    return { cpu_pct: 0, mem_pct: 0, source: "none" };
  }
}

function computeGate(): any {
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
    downgraded: !!g.downgraded,
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// TOKEN LEDGER OPERATIONS
// ═══════════════════════════════════════════════════════════════════════════

function ledgerTotalGranted(): number {
  return qone(ledgerDb, "SELECT COALESCE(SUM(granted),0) v FROM demo_participants")?.v || 0;
}
function ledgerTotalSpent(): number {
  return qone(ledgerDb, "SELECT COALESCE(SUM(total_spent),0) v FROM demo_participants")?.v || 0;
}
function ledgerCount(): number {
  return qone(ledgerDb, "SELECT COUNT(*) v FROM demo_participants")?.v || 0;
}

function ledgerBudgetUsedPct(): number {
  const budget = ledgerTotalGranted();
  if (budget <= 0) return 0;
  return (ledgerTotalSpent() / budget) * 100;
}

function ledgerSnapshot(): any {
  const granted = ledgerTotalGranted();
  const spent = ledgerTotalSpent();
  const pct = granted > 0 ? (spent / granted) * 100 : 0;
  return {
    participants: ledgerCount(),
    total_granted: granted,
    total_spent: spent,
    total_budget: granted,
    budget_used_pct: round(pct, 2),
    scarcity_factor: scarcityFactorForPct(pct),
    scarcity_band: scarcityBandLabel(pct),
    rate_limit_ms: CFG.rateLimitMs,
    starting_balance: CFG.startingBalance,
  };
}

function ledgerGetParticipant(npub: string): any {
  return qone(ledgerDb, "SELECT * FROM demo_participants WHERE npub = ?", [npub]);
}

function ledgerRegister(npub: string): { ok: boolean; balance?: number; message?: string; error?: string } {
  if (!npub) return { ok: false, error: "npub required" };
  if (!whitelist.has(npub)) return { ok: false, error: "npub not whitelisted" };
  const existing = ledgerGetParticipant(npub);
  if (existing) {
    return { ok: false, error: "already registered", balance: existing.balance };
  }
  const now = new Date().toISOString();
  const ts = Date.now();
  ledgerDb.exec("BEGIN");
  try {
    ledgerDb.prepare(
      "INSERT INTO demo_participants (npub, balance, granted, total_spent, prompt_count, created_at, last_prompt_at) VALUES (?, ?, ?, 0, 0, ?, NULL)"
    ).run(npub, CFG.startingBalance, CFG.startingBalance, now);
    ledgerDb.prepare(
      "INSERT INTO demo_ledger (npub, delta, est_tokens, price_per_token, scarcity_factor, reason, balance_after, ts) VALUES (?, ?, NULL, NULL, NULL, 'grant', ?, ?)"
    ).run(npub, CFG.startingBalance, CFG.startingBalance, ts);
    ledgerDb.exec("COMMIT");
  } catch (e) {
    ledgerDb.exec("ROLLBACK");
    throw e;
  }
  return { ok: true, balance: CFG.startingBalance, message: "Welcome to the Sovereign Engineering demo!" };
}

interface ChargeResult {
  ok: boolean;
  error?: string;
  deduction?: number;
  balance_after?: number;
  scarcity_factor?: number;
  retry_after_s?: number;
}

function ledgerCharge(npub: string, estTokens: number): ChargeResult {
  const participant = ledgerGetParticipant(npub);
  if (!participant) return { ok: false, error: "not registered" };

  const ts = Date.now();
  // Rate limit: 1 prompt / 5s / npub
  if (lastPromptAt.has(npub)) {
    const last = lastPromptAt.get(npub)!;
    if (ts - last < CFG.rateLimitMs) {
      const retryIn = Math.ceil((CFG.rateLimitMs - (ts - last)) / 1000);
      return { ok: false, error: `rate limited; retry in ${retryIn}s`, retry_after_s: retryIn };
    }
  }
  lastPromptAt.set(npub, ts);

  const scarcity = scarcityFactorForPct(ledgerBudgetUsedPct());
  const deduction = Math.round(estTokens * CFG.basePricePerToken * scarcity);
  if (deduction <= 0) {
    ledgerDb.prepare("UPDATE demo_participants SET last_prompt_at = ? WHERE npub = ?").run(ts, npub);
    return { ok: true, deduction: 0, balance_after: participant.balance, scarcity_factor: scarcity };
  }
  if (participant.balance < deduction) {
    return { ok: false, error: "insufficient tokens" };
  }

  const newBalance = participant.balance - deduction;
  const newSpent = participant.total_spent + deduction;
  ledgerDb.exec("BEGIN");
  try {
    ledgerDb.prepare("UPDATE demo_participants SET balance = ?, total_spent = ?, prompt_count = prompt_count + 1, last_prompt_at = ? WHERE npub = ?")
      .run(newBalance, newSpent, ts, npub);
    ledgerDb.prepare(
      "INSERT INTO demo_ledger (npub, delta, est_tokens, price_per_token, scarcity_factor, reason, balance_after, ts) VALUES (?, ?, ?, ?, ?, 'charge', ?, ?)"
    ).run(npub, -deduction, estTokens, CFG.basePricePerToken, scarcity, newBalance, ts);
    ledgerDb.exec("COMMIT");
  } catch (e) {
    ledgerDb.exec("ROLLBACK");
    throw e;
  }
  return { ok: true, deduction, balance_after: newBalance, scarcity_factor: scarcity };
}

function ledgerGetLedger(): any[] {
  return qall(ledgerDb,
    `SELECT npub, balance, total_spent, prompt_count, created_at FROM demo_participants ORDER BY total_spent DESC, prompt_count DESC`)
    .map((r: any) => ({
      npub_short: shortNpub(r.npub),
      balance: r.balance,
      prompts_sent: r.prompt_count,
      tokens_spent: r.total_spent,
      joined_at: r.created_at,
    }));
}

// ═══════════════════════════════════════════════════════════════════════════
// PROMPT ROUTING VIA PROXY
// ═══════════════════════════════════════════════════════════════════════════

async function routeViaProxy(prompt: string): Promise<any> {
  const preId = qone(zaiDb, "SELECT COALESCE(MAX(id),0) v FROM api_calls")?.v || 0;
  const startTs = Date.now() / 1000;
  const body = {
    model: CFG.demoModel,
    messages: [{ role: "user", content: prompt }],
    stream: false,
    max_tokens: 512,
  };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 60_000);
  let resp: Response;
  try {
    resp = await fetch(`${CFG.proxyUrl}/v1/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
  } finally {
    clearTimeout(timer);
  }
  if (!resp.ok) {
    const txt = await resp.text().catch(() => "");
    throw new Error(`proxy ${resp.status}: ${txt.slice(0, 200)}`);
  }
  const call = await findRoutedCall(preId, startTs, CFG.demoModel, 3000);
  const content = await resp.text().catch(() => "");
  let model = CFG.demoModel;
  let responseText = "";
  try {
    const parsed = JSON.parse(content);
    model = parsed.model || model;
    responseText = parsed.choices?.[0]?.message?.content || "";
  } catch {}
  return {
    response: responseText,
    provider: call?.key_name || null,
    model: call?.model || model,
    prompt_tokens: call?.prompt_tokens ?? null,
    completion_tokens: call?.completion_tokens ?? null,
    tokens_used: call?.total_tokens ?? estimateTokens(prompt),
    reason: call?.reason || null,
    duration_ms: call?.duration_ms ?? null,
  };
}

async function findRoutedCall(preId: number, startTs: number, model: string, maxWaitMs: number): Promise<any> {
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    let row = qone(zaiDb,
      `SELECT a.id, a.ts, a.key_name, a.model, a.prompt_tokens, a.completion_tokens,
              a.total_tokens, a.duration_ms, a.status_code,
              (SELECT reason FROM key_decisions WHERE ts BETWEEN a.ts-1.5 AND a.ts+1.5
               ORDER BY ABS(ts-a.ts) ASC LIMIT 1) reason
       FROM api_calls a
       WHERE a.id > ? AND a.ts >= ? AND a.model = ?
       ORDER BY a.id ASC LIMIT 1`, [preId, startTs - 1, model]);
    if (!row) {
      row = qone(zaiDb,
        `SELECT a.id, a.ts, a.key_name, a.model, a.prompt_tokens, a.completion_tokens,
                a.total_tokens, a.duration_ms, a.status_code,
                (SELECT reason FROM key_decisions WHERE ts BETWEEN a.ts-1.5 AND a.ts+1.5
                 ORDER BY ABS(ts-a.ts) ASC LIMIT 1) reason
         FROM api_calls a WHERE a.id > ? AND a.ts >= ? ORDER BY a.id ASC LIMIT 1`,
        [preId, startTs - 1]);
    }
    if (row) return row;
    await new Promise((r) => setTimeout(r, 120));
  }
  return null;
}

// ═══════════════════════════════════════════════════════════════════════════
// THE 5 CVM TOOLS
// ═══════════════════════════════════════════════════════════════════════════

type ToolHandler = (args: any) => any | Promise<any>;

// ── Snapshot builder ───────────────────────────────────────────────────────
const TOOLS: Record<string, ToolHandler> = {
  // ── Tool 1: get_snapshot ──────────────────────────────────────────────────
  // Returns everything the display dashboard needs in one call.
  get_snapshot: async (_args: any) => {
    const gate = computeGate();
    const pricing = computePricing();
    const econ = ledgerSnapshot();
    // Task 13: include 24h price history in the snapshot so the display's
    // Panel 3 charts can bootstrap immediately in Nostr mode (which skips the
    // separate /price-history HTTP fetch). Trimmed to the same 24h/1h-bucket
    // shape the dedicated tool returns.
    let priceHistory = (TOOLS.get_price_history({ hours: 24 })?.points) || [];
    return {
      ts: Math.floor(Date.now() / 1000),
      quota: await computeQuota(),
      pricing: {
        ours: pricing.ours,
        friend: pricing.friend,
        ollama: pricing.ollama,
        ppq: pricing.ppq,
      },
      pricing_meta: pricing._meta,
      cost_today: computeCostToday(),
      cost_hour: round(computeCostToday() / Math.max(1, new Date().getHours() + new Date().getMinutes() / 60), 4),
      routing_decisions: computeRequests(20),
      provider_dist: computeDistribution(),
      dispatch_gate: gate,
      scarcity: {
        factor: econ.scarcity_factor,
        level: econ.scarcity_band,
        budget_used_pct: econ.budget_used_pct,
      },
      system: computeSystem(),
      participants: {
        count: econ.participants,
        total_prompts: qone(ledgerDb, "SELECT COALESCE(SUM(prompt_count),0) v FROM demo_participants")?.v || 0,
        total_tokens: econ.total_spent,
      },
      ledger: ledgerGetLedger().slice(0, 10),
      price_history: priceHistory,
    };
  },

  // ── Tool 2: send_prompt ───────────────────────────────────────────────────
  // Routes a prompt via the proxy, deducts tokens at scarcity-adjusted price.
  send_prompt: async (args: any) => {
    const prompt = args?.prompt;
    const npub = args?.npub;
    if (!prompt || typeof prompt !== "string") {
      return { ok: false, error: "prompt required" };
    }
    if (!npub || typeof npub !== "string") {
      return { ok: false, error: "npub required" };
    }

    // Must be registered
    const participant = ledgerGetParticipant(npub);
    if (!participant) {
      return { ok: false, error: "npub not registered — call register_participant first" };
    }

    // Charge FIRST (rate limit + balance check)
    const estTokens = estimateTokens(prompt);
    const charge = ledgerCharge(npub, estTokens);
    if (!charge.ok) {
      return { ok: false, error: charge.error, ...(charge.retry_after_s ? { retry_after_s: charge.retry_after_s } : {}) };
    }

    // Route through proxy
    let routed;
    try {
      routed = await routeViaProxy(prompt);
    } catch (e: any) {
      // Refund the charge if routing fails
      ledgerDb.exec("BEGIN");
      try {
        ledgerDb.prepare("UPDATE demo_participants SET balance = balance + ?, prompt_count = prompt_count - 1 WHERE npub = ?")
          .run(charge.deduction || 0, npub);
        ledgerDb.prepare("INSERT INTO demo_ledger (npub, delta, est_tokens, price_per_token, scarcity_factor, reason, balance_after, ts) VALUES (?, ?, NULL, NULL, NULL, 'refund', ?, ?)")
          .run(npub, charge.deduction || 0, participant.balance, Date.now());
        ledgerDb.exec("COMMIT");
      } catch {
        ledgerDb.exec("ROLLBACK");
      }
      return { ok: false, error: "proxy error", detail: e.message };
    }

    // If real tokens materially exceeded estimate, top up the charge
    if (routed.tokens_used > estTokens * 1.5) {
      const extra = Math.max(0, routed.tokens_used - estTokens);
      // Extra charge doesn't re-check rate limit (already past it)
      const scarcity = scarcityFactorForPct(ledgerBudgetUsedPct());
      const extraDeduction = Math.round(extra * CFG.basePricePerToken * scarcity);
      const current = ledgerGetParticipant(npub);
      if (current && current.balance >= extraDeduction && extraDeduction > 0) {
        const newBal = current.balance - extraDeduction;
        const newSpent = current.total_spent + extraDeduction;
        ledgerDb.exec("BEGIN");
        try {
          ledgerDb.prepare("UPDATE demo_participants SET balance = ?, total_spent = ? WHERE npub = ?").run(newBal, newSpent, npub);
          ledgerDb.prepare("INSERT INTO demo_ledger (npub, delta, est_tokens, price_per_token, scarcity_factor, reason, balance_after, ts) VALUES (?, ?, ?, ?, ?, 'charge', ?, ?)")
            .run(npub, -extraDeduction, extra, CFG.basePricePerToken, scarcity, newBal, Date.now());
          ledgerDb.exec("COMMIT");
          charge.balance_after = newBal;
          charge.deduction = (charge.deduction || 0) + extraDeduction;
        } catch {
          ledgerDb.exec("ROLLBACK");
        }
      }
    }

    const gate = computeGate();
    const pricePerM = round((gate.effective_price_per_m || 0) * (charge.scarcity_factor || 1), 5);
    const costUsd = round((routed.tokens_used || 0) / 1e6 * pricePerM, 6);

    return {
      ok: true,
      response: routed.response,
      provider: routed.provider,
      model: routed.model,
      tokens_used: routed.tokens_used,
      cost_usd: costUsd,
      price_per_m: pricePerM,
      token_cost: charge.deduction || 0,
      new_balance: charge.balance_after ?? null,
      scarcity_factor: charge.scarcity_factor ?? null,
      reason: routed.reason,
      duration_ms: routed.duration_ms,
    };
  },

  // ── Tool 3: register_participant ──────────────────────────────────────────
  // Whitelist check, creates participant with 50K tokens.
  register_participant: (args: any) => {
    const npub = args?.npub;
    if (!npub) return { ok: false, error: "npub required" };
    return ledgerRegister(npub);
  },

  // ── Tool 4: get_price_history ─────────────────────────────────────────────
  // Returns pricing history for charts, bucketed by hour.
  // Capped at 200 data points to stay well under relay size limits (~50KB).
  get_price_history: (args: any) => {
    const hours = Math.min(168, Math.max(1, args?.hours || 24));
    const now = Math.floor(Date.now() / 1000);
    const since = now - hours * 3600;
    // Adaptive bucket size: aim for ~hours/4 data points per key (4 keys → ~hours points total)
    // For 24h → 1h buckets (24 points/key), for 168h → 4h buckets (42 points/key)
    const bucketSize = hours <= 24 ? 3600 : 3600 * 4;

    // Aggregate api_calls per key per bucket — limit to keep response small
    const maxBuckets = 50;
    const buckets = qall(zaiDb,
      `SELECT (ts / ?) * ? as bucket_ts, key_name,
              COALESCE(SUM(total_tokens),0) as tokens,
              COUNT(*) as calls
       FROM api_calls
       WHERE ts > ? AND key_name IS NOT NULL
       GROUP BY bucket_ts, key_name
       ORDER BY bucket_ts ASC
       LIMIT ?`, [bucketSize, bucketSize, since, maxBuckets * 4]);

    // Build pricing history — one entry per key per bucket
    const history: any[] = [];
    const seenBuckets = new Set<number>();
    const ollamaMonthlyTok = qone(zaiDb,
      `SELECT COALESCE(SUM(total_tokens),0) v FROM api_calls WHERE key_name='ollama_cloud' AND ts > ?`,
      [now - 30 * 86400])?.v || 0;
    const ppqCost = ppqRealPerM();

    for (const b of buckets) {
      if (seenBuckets.has(b.bucket_ts)) continue;
      seenBuckets.add(b.bucket_ts);
      if (seenBuckets.size > maxBuckets) break;

      for (const key of ["ours", "friend", "ollama", "ppq"]) {
        const row = buckets.find((r) => {
          const rKey = r.key_name === "ollama_cloud" ? "ollama" : r.key_name;
          return rKey === key && r.bucket_ts === b.bucket_ts;
        });

        let costBasis: number;
        if (key === "ours" || key === "friend") {
          costBasis = CFG.flatKeyCostPerM;
        } else if (key === "ollama") {
          costBasis = ollamaMonthlyTok > 0 ? CFG.ollamaMonthlyUsd / (ollamaMonthlyTok / 1e6) : CFG.ollamaMonthlyUsd;
        } else {
          costBasis = ppqCost;
        }

        const yourPrice = costBasis * (1 + CFG.margin);
        const marginPct = yourPrice > 0 ? (yourPrice - costBasis) / yourPrice * 100 : 0;
        history.push({
          ts: b.bucket_ts,
          key,
          cost_basis: round(costBasis, 4),
          your_price: round(yourPrice, 4),
          margin_pct: round(marginPct, 2),
          calls: row?.calls || 0,
          tokens: row?.tokens || 0,
        });
      }
    }

    return { hours, bucket_seconds: bucketSize, points: history };
  },

  // ── Tool 5: get_ledger ────────────────────────────────────────────────────
  // Returns all participants with their balances and stats.
  get_ledger: (_args: any) => {
    return ledgerGetLedger();
  },
};

// ═══════════════════════════════════════════════════════════════════════════
// CVM PROTOCOL — JSON-RPC HANDLER
// ═══════════════════════════════════════════════════════════════════════════

const TOOL_DEFS = Object.keys(TOOLS).map((name) => ({
  name,
  description: {
    get_snapshot: "Returns all dashboard data: quota, pricing, routing decisions, dispatch gate, scarcity, system stats, participants, and ledger in one call.",
    send_prompt: "Route a prompt through the proxy and deduct tokens. Input: { prompt, npub }. Returns response with provider, model, tokens, cost, and new balance.",
    register_participant: "Register a whitelisted npub with 50K starting tokens. Input: { npub }. Returns { success, balance, message }.",
    get_price_history: "Get pricing history per key, bucketed by hour. Input: { hours }. Returns array of { ts, key, cost_basis, your_price, margin_pct }.",
    get_ledger: "Get all participants with balances and stats. Returns array of { npub_short, balance, prompts_sent, tokens_spent, joined_at }.",
  }[name] || `Tool: ${name}`,
  inputSchema: { type: "object", properties: {} },
}));

async function handleMcpMessage(message: any, clientPubkey: string, relays: Relay[]): Promise<void> {
  console.log(`[cvm] ${message.method} (id=${message.id}) from ${clientPubkey.slice(0, 16)}…`);

  let responseContent: any = null;

  if (message.method === "initialize") {
    responseContent = {
      jsonrpc: "2.0", id: message.id,
      result: {
        protocolVersion: "2024-11-05",
        capabilities: { tools: {} },
        serverInfo: { name: "sovereign-demo-cvm", version: "1.0.0" },
      },
    };
  } else if (message.method === "notifications/initialized") {
    return; // no response for notifications
  } else if (message.method === "tools/list") {
    responseContent = {
      jsonrpc: "2.0", id: message.id,
      result: { tools: TOOL_DEFS },
    };
  } else if (message.method === "tools/call") {
    const toolName = message.params?.name;
    const args = message.params?.arguments || {};
    if (!TOOLS[toolName]) {
      responseContent = {
        jsonrpc: "2.0", id: message.id,
        error: { code: -32601, message: `Unknown tool: ${toolName}` },
      };
    } else {
      try {
        const result = await TOOLS[toolName](args);
        responseContent = {
          jsonrpc: "2.0", id: message.id,
          result: { content: [{ type: "text", text: JSON.stringify(result) }] },
        };
        console.log(`[cvm] ${toolName} → ${JSON.stringify(result).length} bytes`);
      } catch (e: any) {
        responseContent = {
          jsonrpc: "2.0", id: message.id,
          error: { code: -32603, message: e.message },
        };
        console.error(`[cvm] ${toolName} error:`, e.message);
      }
    }
  } else {
    responseContent = {
      jsonrpc: "2.0", id: message.id,
      error: { code: -32601, message: `Unknown method: ${message.method}` },
    };
  }

  if (responseContent) {
    await sendResponse(responseContent, clientPubkey, relays);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// GIFT WRAP — SEND RESPONSE TO CLIENT
// ═══════════════════════════════════════════════════════════════════════════

async function sendResponse(mcpMessage: any, clientPubkey: string, relays: Relay[]): Promise<void> {
  // Inner event (kind 25910) — signed by server
  const innerEvent = {
    pubkey: serverPk,
    kind: 25910,
    tags: [["p", clientPubkey]],
    content: JSON.stringify(mcpMessage),
    created_at: Math.floor(Date.now() / 1000),
  };
  const signedEvent = finalizeEvent(innerEvent, serverSk);

  // Gift wrap to client (kind 1059) — random one-time key
  const wrapSk = generateSecretKey();
  const wrapPk = getPublicKey(wrapSk);
  const convKey = nip44.v2.utils.getConversationKey(wrapSk, clientPubkey);
  const encrypted = nip44.v2.encrypt(JSON.stringify(signedEvent), convKey);

  const giftWrap = finalizeEvent({
    kind: 1059,
    content: encrypted,
    tags: [["p", clientPubkey]],
    created_at: Math.floor(Date.now() / 1000),
    pubkey: wrapPk,
  }, wrapSk);

  for (const relay of relays) {
    try {
      await relay.publish(giftWrap);
    } catch (e: any) {
      console.warn(`[cvm] publish failed on relay: ${e.message}`);
    }
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// MAIN — connect to relays, subscribe, handle requests
// ═══════════════════════════════════════════════════════════════════════════

async function main(): Promise<void> {
  const connectedRelays: Relay[] = [];

  // Event deduplication — the same gift-wrap event arrives from multiple relays.
  // Shared set across ALL relays to prevent processing the same event twice.
  const seenEventIds = new Set<string>();
  const MAX_SEEN = 5000; // prevent unbounded growth
  let seenCursor = 0;

  for (const url of CFG.relays) {
    try {
      console.log(`[cvm] Connecting to ${url}…`);
      const relay = await Promise.race([
        Relay.connect(url),
        new Promise<never>((_, reject) =>
          setTimeout(() => reject(new Error("Connection timeout (10s)")), 10_000),
        ),
      ]);
      console.log(`[cvm] Connected to ${url}`);

      // Broad filter + client-side p-tag check (NIP-12 gap: #p filter unreliable for kind 1059)
      relay.subscribe(
        [{ kinds: [1059, 21059], limit: 0 }],
        {
          onevent: async (event) => {
            try {
              // Dedup: skip if we already processed this event
              if (seenEventIds.has(event.id)) return;
              seenEventIds.add(event.id);
              seenCursor++;
              if (seenCursor > MAX_SEEN) {
                // Clear old entries (simple strategy: clear half)
                const toKeep = [...seenEventIds].slice(MAX_SEEN / 2);
                seenEventIds.clear();
                for (const id of toKeep) seenEventIds.add(id);
                seenCursor = seenEventIds.size;
              }

              const pTag = event.tags?.find((t) => t[0] === "p");
              if (!pTag || pTag[1] !== serverPk) return;

              // Decrypt gift wrap
              const convKey = nip44.v2.utils.getConversationKey(serverSk, event.pubkey);
              const decrypted = nip44.v2.decrypt(event.content, convKey);
              const innerEvent = JSON.parse(decrypted);
              const mcpMessage = JSON.parse(innerEvent.content);
              const clientPubkey = innerEvent.pubkey || event.pubkey;

              await handleMcpMessage(mcpMessage, clientPubkey, connectedRelays);
            } catch (e: any) {
              console.error(`[cvm] event handler error: ${e.message}`);
            }
          },
          oneose: () => console.log(`[cvm] EOSE from ${url}`),
        },
      );
      connectedRelays.push(relay);
    } catch (e: any) {
      console.error(`[cvm] Failed to connect ${url}: ${e.message}`);
    }
  }

  console.log(`[cvm] ────────────────────────────────────────────────`);
  console.log(`[cvm] Server live! ${connectedRelays.length}/${CFG.relays.length} relays connected`);
  console.log(`[cvm] Pubkey: ${serverPk}`);
  console.log(`[cvm] npub:   ${process.env.npub || "(see cvm-server-key.json)"}`);
  console.log(`[cvm] Tools:  ${Object.keys(TOOLS).join(", ")}`);
  console.log(`[cvm] Whitelist: ${whitelist.size} npubs`);
  console.log(`[cvm] Waiting for requests…`);
  console.log(`[cvm] Test:  cvmi call ${serverPk} tool:get_snapshot`);

  // ═══════════════════════════════════════════════════════════════════════════
  // HTTP ENDPOINT — serves real snapshot data for the display dashboard
  // ═══════════════════════════════════════════════════════════════════════════

  const httpPort = parseInt(process.env.CVM_HTTP_PORT || "3000", 10);
  const corsHeaders = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };

  Bun.serve({
    port: httpPort,
    async fetch(req) {
      const url = new URL(req.url);
      if (req.method === "OPTIONS") {
        return new Response(null, { status: 204, headers: corsHeaders });
      }
      if (url.pathname === "/snapshot") {
        const data = await TOOLS.get_snapshot({});
        return Response.json(data, { headers: corsHeaders });
      }
      if (url.pathname === "/price-history") {
        const hours = parseInt(url.searchParams.get("hours") || "24", 10);
        const data = TOOLS.get_price_history({ hours });
        return Response.json(data, { headers: corsHeaders });
      }
      if (url.pathname === "/health") {
        return Response.json({ ok: true, ts: Date.now(), participants: ledgerCount() }, { headers: corsHeaders });
      }
      return new Response("Not found\n\nEndpoints: /snapshot, /price-history, /health\n", {
        status: 404, headers: { ...corsHeaders, "Content-Type": "text/plain" },
      });
    },
  });
  console.log(`[cvm] HTTP server on http://localhost:${httpPort} (endpoints: /snapshot, /price-history, /health)`);

  // ═══════════════════════════════════════════════════════════════════════════
  // PUBLIC SNAPSHOT PUBLISHER — kind 30315 (replaceable parameterized)
  // Broadcasts the current snapshot as plain JSON every 5 seconds so the
  // display dashboard (and anyone else) can read it without a private key.
  // ═══════════════════════════════════════════════════════════════════════════
  setInterval(async () => {
    try {
      const snap = await TOOLS.get_snapshot({});
      const content = JSON.stringify(snap);
      const signedEvent = finalizeEvent({
        kind: 30315,
        pubkey: serverPk,
        content,
        tags: [["d", "cvm-snapshot"]],
        created_at: Math.floor(Date.now() / 1000),
      }, serverSk);

      for (const relay of connectedRelays) {
        try { await relay.publish(signedEvent); } catch (e: any) {
          // non-fatal — relay may be temporarily down
        }
      }
      console.log(`[cvm] published public snapshot: ${content.length} bytes`);
    } catch (e: any) {
      console.warn(`[cvm] public publish failed: ${e.message}`);
    }
  }, 5_000);

  // Heartbeat every 60s
  setInterval(() => {
    const mem = process.memoryUsage();
    console.log(`[cvm] heartbeat — RSS: ${Math.round(mem.rss / 1e6)}MB, relays: ${connectedRelays.length}, participants: ${ledgerCount()}`);
  }, 60_000);
}

// Graceful shutdown
for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    console.log(`[cvm] ${sig} — shutting down`);
    try { zaiDb.close(); } catch {}
    try { burnDb?.close(); } catch {}
    try { ledgerDb.close(); } catch {}
    process.exit(0);
  });
}

main().catch((err) => {
  console.error("[cvm] FATAL:", err);
  process.exit(1);
});
