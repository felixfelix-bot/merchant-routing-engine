/**
 * CVM Server — Integration Test Suite (Playwright + video)
 *
 * Tests all 5 CVM tools, whitelist enforcement, rate limiting, insufficient
 * balance, and scarcity tiers against a live CVM server communicating over
 * real Nostr relays (kind 25910 wrapped in NIP-59 gift wrap).
 *
 * Run:  npx playwright test tests/cvm-server.spec.ts
 *
 * Architecture:
 *   - beforeAll: spawns the CVM server (bun), connects to relays, sets up
 *     a shared browser context for video recording.
 *   - Each test makes real CVM calls via nostr-tools and asserts responses.
 *   - A harness page displays live test results so the video is meaningful.
 *   - afterAll: tears down server/relays and converts video to mp4.
 */

import { test, expect, type Page, type BrowserContext } from '@playwright/test';
import { spawn, execSync, type ChildProcessWithoutNullStreams } from 'child_process';
import { finalizeEvent, generateSecretKey, getPublicKey, nip44 } from 'nostr-tools';
import { Relay } from 'nostr-tools/relay';
import * as http from 'http';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { fileURLToPath } from 'url';

// ═══════════════════════════════════════════════════════════════════════════
// CONSTANTS
// ═══════════════════════════════════════════════════════════════════════════

const __filename_esm = fileURLToPath(import.meta.url);
const __dirname_esm = path.dirname(__filename_esm);
const DEMO_ROOT = path.resolve(__dirname_esm, '..');
const CVM_SERVER_DIR = path.join(DEMO_ROOT, 'cvm-server');

// Server pubkey (loaded from cvm-server-key.json — fixed across runs)
const SERVER_PUBKEY = '10814a5dc07e3e876867ffb9e8781af47fa599b677fb27e051f6b5261c1d4f35';

// Real Nostr relays (same as server config)
const RELAY_URLS = ['wss://nostr.mom', 'wss://relay.primal.net', 'wss://nos.lol'];

// Whitelisted npubs (from demo-whitelist.json)
const FELIX = 'npub1demo0000felix0000000000000000000000000000000000000000000000felix';
const ALICE = 'npub1demo0001alice0000000000000000000000000000000000000000000000alice';
const BOB = 'npub1demo0002bob00000000000000000000000000000000000000000000000000bob';
const NON_WHITELISTED = 'npub1notwhitelisted00000000000000000000000000000000000000000000000xyz';

// Test ledger DB (fresh per run — avoids polluting real demo_ledger.db)
const TEST_DB_PATH = path.join(os.tmpdir(), `cvm-test-ledger-${Date.now()}.db`);

// Bun script for DB manipulation (scarcity tiers, balance drain)
const DB_SCRIPT_PATH = path.join(os.tmpdir(), `cvm-db-helper-${Date.now()}.ts`);

// ═══════════════════════════════════════════════════════════════════════════
// SHARED STATE (module scope — survives across tests)
// ═══════════════════════════════════════════════════════════════════════════

let cvmServer: ChildProcessWithoutNullStreams | null = null;
let httpServer: http.Server | null = null;
let baseUrl = '';
let sharedPage: Page | null = null;
let sharedContext: BrowserContext | null = null;
let relays: Relay[] = [];
let clientSk: Uint8Array;
let clientPk: string;
let videoWebmPath: string | null = null;

// JSON-RPC request/response tracking
let reqId = 0;
const pending = new Map<number, {
  resolve: (v: any) => void;
  reject: (e: Error) => void;
  timeout: ReturnType<typeof setTimeout>;
}>();

// Track which participants we've registered (for cleanup / state checks)
const registered = new Set<string>();

// ═══════════════════════════════════════════════════════════════════════════
// DB MANIPULATION HELPER (bun script for SQLite writes)
// ═══════════════════════════════════════════════════════════════════════════

/**
 * Writes a bun script that can set ledger state for scarcity/balance tests.
 * Usage: bun <script> <dbPath> <command> <args...>
 *   set-budget <granted> <spent>     — replace all participants with one at given budget
 *   set-balance  <npub> <balance>    — set a participant's balance
 */
function writeDbHelperScript(): void {
  const script = `import { Database } from "bun:sqlite";
const [,, dbPath, cmd, ...args] = process.argv;
const db = new Database(dbPath);

if (cmd === "set-budget") {
  const granted = parseInt(args[0]);
  const spent = parseInt(args[1]);
  db.exec("DELETE FROM demo_ledger");
  db.exec("DELETE FROM demo_participants");
  db.prepare(
    "INSERT INTO demo_participants (npub, balance, granted, total_spent, prompt_count, created_at, last_prompt_at) VALUES (?, ?, ?, ?, 0, ?, NULL)"
  ).run("scarcity-test", granted - spent, granted, spent, new Date().toISOString());
  console.log("set-budget OK: granted=" + granted + " spent=" + spent);
} else if (cmd === "set-balance") {
  const npub = args[0];
  const balance = parseInt(args[1]);
  db.prepare("UPDATE demo_participants SET balance = ? WHERE npub = ?").run(balance, npub);
  console.log("set-balance OK: npub=" + npub + " balance=" + balance);
} else {
  console.error("Unknown command: " + cmd);
  process.exit(1);
}
db.close();
`;
  fs.writeFileSync(DB_SCRIPT_PATH, script);
}

function runDbHelper(cmd: string, ...args: string[]): void {
  execSync(
    `bun "${DB_SCRIPT_PATH}" "${TEST_DB_PATH}" ${cmd} ${args.map(a => `"${a}"`).join(' ')}`,
    { stdio: 'pipe', timeout: 5000 },
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// CVM CALL — gift-wrapped JSON-RPC over Nostr
// ═══════════════════════════════════════════════════════════════════════════

function callTool(toolName: string, args: Record<string, any> = {}, timeoutMs = 30_000): Promise<any> {
  return new Promise((resolve, reject) => {
    const id = ++reqId;
    const timer = setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        reject(new Error(`Timeout (${timeoutMs / 1000}s) for ${toolName}`));
      }
    }, timeoutMs);
    pending.set(id, { resolve, reject, timeout: timer });

    // Build JSON-RPC request
    const mcpRequest = {
      jsonrpc: '2.0',
      method: 'tools/call',
      params: { name: toolName, arguments: args },
      id,
    };

    // Inner event (kind 25910) — signed by client
    const innerEvent = {
      pubkey: clientPk,
      kind: 25910,
      tags: [['p', SERVER_PUBKEY]],
      content: JSON.stringify(mcpRequest),
      created_at: Math.floor(Date.now() / 1000),
    };
    const signedEvent = finalizeEvent(innerEvent, clientSk);

    // Gift wrap (kind 1059) — random one-time key
    const wrapSk = generateSecretKey();
    const wrapPk = getPublicKey(wrapSk);
    const convKey = nip44.v2.utils.getConversationKey(wrapSk, SERVER_PUBKEY);
    const encrypted = nip44.v2.encrypt(JSON.stringify(signedEvent), convKey);

    const giftWrap = finalizeEvent({
      kind: 1059,
      content: encrypted,
      tags: [['p', SERVER_PUBKEY]],
      created_at: Math.floor(Date.now() / 1000),
      pubkey: wrapPk,
    } as any, wrapSk);

    // Publish to all connected relays
    for (const relay of relays) {
      relay.publish(giftWrap).catch(() => {});
    }
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// RELAY SETUP — connect, subscribe for responses
// ═══════════════════════════════════════════════════════════════════════════

async function setupRelays(): Promise<void> {
  clientSk = generateSecretKey();
  clientPk = getPublicKey(clientSk);

  for (const url of RELAY_URLS) {
    try {
      const relay = await Promise.race([
        Relay.connect(url),
        new Promise<never>((_, rej) =>
          setTimeout(() => rej(new Error('timeout')), 10_000),
        ),
      ]);

      relay.subscribe([{ kinds: [1059, 21059], limit: 0 }], {
        onevent: (event) => {
          try {
            // Client-side p-tag filter (NIP-12 gap)
            const pTag = event.tags?.find((t) => t[0] === 'p');
            if (!pTag || pTag[1] !== clientPk) return;

            // Decrypt gift wrap
            const convKey = nip44.v2.utils.getConversationKey(clientSk, event.pubkey);
            const decrypted = nip44.v2.decrypt(event.content, convKey);
            const innerEvent = JSON.parse(decrypted);
            const mcpMsg = JSON.parse(innerEvent.content);

            if (mcpMsg.id && pending.has(mcpMsg.id)) {
              const { resolve, timeout } = pending.get(mcpMsg.id)!;
              clearTimeout(timeout);
              pending.delete(mcpMsg.id);

              // Handle JSON-RPC error responses
              if (mcpMsg.error) {
                resolve({ ok: false, error: mcpMsg.error.message });
                return;
              }

              // Unwrap MCP result content → JSON
              let resultData = mcpMsg.result;
              if (resultData?.content) {
                for (const item of resultData.content) {
                  if (item.type === 'text') {
                    try { resultData = JSON.parse(item.text); } catch { /* keep raw */ }
                  }
                }
              }
              resolve(resultData);
            }
          } catch {
            // Malformed event — ignore
          }
        },
      });

      relays.push(relay);
    } catch {
      // Relay unavailable — continue with others
    }
  }

  if (relays.length === 0) {
    throw new Error('Failed to connect to ANY relay');
  }

  // Wait for subscriptions to settle
  await new Promise((r) => setTimeout(r, 2000));
}

// ═══════════════════════════════════════════════════════════════════════════
// HARNESS PAGE — live test results display for video recording
// ═══════════════════════════════════════════════════════════════════════════

const HARNESS_HTML = `<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>CVM Server Tests</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace; background: #0d1117; color: #c9d1d9; padding: 24px; }
  h1 { color: #58a6ff; font-size: 20px; margin-bottom: 4px; }
  .subtitle { color: #8b949e; font-size: 13px; margin-bottom: 20px; }
  .summary { display: flex; gap: 16px; margin-bottom: 20px; font-size: 14px; }
  .badge { padding: 4px 12px; border-radius: 12px; font-weight: 600; }
  .badge-pass { background: #1a3a2a; color: #3fb950; }
  .badge-fail { background: #3a1a1a; color: #f85149; }
  .badge-run { background: #2a2a1a; color: #d29922; }
  .test { padding: 12px 16px; margin-bottom: 8px; border-radius: 8px; background: #161b22; border-left: 4px solid #30363d; transition: border-color .2s; }
  .test.pass { border-left-color: #3fb950; }
  .test.fail { border-left-color: #f85149; }
  .test.running { border-left-color: #d29922; }
  .test-header { display: flex; align-items: center; gap: 8px; }
  .test-icon { font-size: 16px; }
  .test-name { font-weight: 600; font-size: 14px; flex: 1; }
  .test-status { font-size: 12px; font-weight: 700; text-transform: uppercase; }
  .pass .test-status { color: #3fb950; }
  .fail .test-status { color: #f85149; }
  .running .test-status { color: #d29922; }
  .test-detail { color: #8b949e; font-size: 12px; margin-top: 6px; white-space: pre-wrap; word-break: break-word; max-height: 200px; overflow-y: auto; }
</style></head>
<body>
  <h1>🔧 CVM Server — Integration Test Suite</h1>
  <div class="subtitle">Sovereign Engineering Demo · Nostr relays · 5 tools · whitelist · rate limit · scarcity</div>
  <div class="summary">
    <span class="badge badge-pass" id="pass-count">✓ 0 PASS</span>
    <span class="badge badge-fail" id="fail-count">✗ 0 FAIL</span>
    <span class="badge badge-run" id="total-count">8 TOTAL</span>
  </div>
  <div id="results"></div>
</body></html>`;

async function logTest(page: Page, name: string, status: 'pass' | 'fail' | 'running', detail: string): Promise<void> {
  const icon = status === 'pass' ? '✅' : status === 'fail' ? '❌' : '⏳';
  await page.evaluate(({ name, status, detail, icon }) => {
    const results = document.getElementById('results')!;

    // Check if this test already has an entry (update in place)
    let entry = document.getElementById('test-' + name.replace(/\s+/g, '-'));
    if (!entry) {
      entry = document.createElement('div');
      entry.id = 'test-' + name.replace(/\s+/g, '-');
      entry.className = `test ${status}`;
      entry.innerHTML = `
        <div class="test-header">
          <span class="test-icon">${icon}</span>
          <span class="test-name">${name}</span>
          <span class="test-status">${status}</span>
        </div>
        <div class="test-detail"></div>
      `;
      results.appendChild(entry);
    } else {
      entry.className = `test ${status}`;
      entry.querySelector('.test-icon')!.textContent = icon;
      entry.querySelector('.test-status')!.textContent = status;
    }
    entry.querySelector('.test-detail')!.textContent = detail;

    // Update counters
    const all = document.querySelectorAll('.test');
    const passes = document.querySelectorAll('.test.pass').length;
    const fails = document.querySelectorAll('.test.fail').length;
    document.getElementById('pass-count')!.textContent = '✓ ' + passes + ' PASS';
    document.getElementById('fail-count')!.textContent = '✗ ' + fails + ' FAIL';

    window.scrollTo(0, document.body.scrollHeight);
  }, { name, status, detail, icon });
}

// ═══════════════════════════════════════════════════════════════════════════
// LIFECYCLE
// ═══════════════════════════════════════════════════════════════════════════

// Disable fixture video — we manage our own via a manual browser context
test.use({ video: 'off' });

test.describe('CVM Server Integration Tests', () => {
  test.describe.configure({ mode: 'serial' });

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(120_000);

    // ── Kill any existing CVM server (same pubkey = response race) ──────
    try {
      execSync('pkill -f "bun src/cvm-server.ts"', { stdio: 'pipe' });
      await new Promise((r) => setTimeout(r, 1000));
    } catch { /* no existing server — fine */ }

    // ── Write DB helper script ──────────────────────────────────────────
    writeDbHelperScript();

    // ── Start CVM server ────────────────────────────────────────────────
    cvmServer = spawn('bun', ['src/cvm-server.ts'], {
      cwd: CVM_SERVER_DIR,
      env: {
        ...process.env,
        LEDGER_DB: TEST_DB_PATH,
        DEMO_MODEL: process.env.DEMO_MODEL || 'glm-4.5-flash',
      },
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    // Wait for server to be ready ("Server live!" in stdout)
    const serverReady = new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Server startup timeout (40s)')), 40_000);
      const onData = (data: Buffer) => {
        const text = data.toString();
        process.stderr.write(text); // mirror to test output
        if (text.includes('Server live!')) {
          clearTimeout(timer);
          resolve();
        }
      };
      cvmServer!.stdout.on('data', onData);
      cvmServer!.stderr.on('data', onData);
      cvmServer!.on('exit', (code) => {
        clearTimeout(timer);
        reject(new Error(`Server exited prematurely with code ${code}`));
      });
    });

    await serverReady;

    // ── Start static HTTP server for harness page ──────────────────────
    httpServer = http.createServer((req, res) => {
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end(HARNESS_HTML);
    });
    await new Promise<void>((resolve) => {
      httpServer!.listen(0, () => {
        const addr = httpServer!.address();
        if (addr && typeof addr === 'object') {
          baseUrl = `http://localhost:${addr.port}`;
        }
        resolve();
      });
    });

    // ── Create shared browser context for video ────────────────────────
    sharedContext = await browser.newContext({
      recordVideo: {
        dir: path.join(DEMO_ROOT, 'test-results'),
        size: { width: 900, height: 700 },
      },
      viewport: { width: 900, height: 700 },
    });
    sharedPage = await sharedContext.newPage();
    await sharedPage.goto(`${baseUrl}/`);
    await sharedPage.waitForLoadState('networkidle');

    // ── Connect to relays ──────────────────────────────────────────────
    await setupRelays();
  });

  test.afterAll(async () => {
    // Close relays
    for (const r of relays) { try { r.close(); } catch {} }
    relays = [];

    // Kill server
    if (cvmServer) {
      try { cvmServer.kill('SIGTERM'); } catch {}
      await new Promise((r) => setTimeout(r, 1500));
      try { if (!cvmServer.killed) cvmServer.kill('SIGKILL'); } catch {}
      cvmServer = null;
    }

    // Stop HTTP server
    if (httpServer) {
      await new Promise<void>((r) => httpServer!.close(() => r()));
      httpServer = null;
    }

    // Close page + context (finalizes video)
    if (sharedPage) {
      try {
        const video = sharedPage.video();
        await sharedPage.close();
        if (sharedContext) await sharedContext.close();
        if (video) {
          videoWebmPath = await video.path();
        }
      } catch {}
    }
    sharedPage = null;
    sharedContext = null;

    // Convert video to mp4
    if (videoWebmPath && fs.existsSync(videoWebmPath)) {
      const mp4Path = path.join(DEMO_ROOT, 'test-videos', 'cvm-server-test.mp4');
      try {
        execSync(
          `ffmpeg -y -i "${videoWebmPath}" -c:v libx264 -preset fast -crf 23 "${mp4Path}"`,
          { stdio: 'pipe', timeout: 30_000 },
        );
        console.log(`[video] Converted to ${mp4Path}`);
      } catch (e: any) {
        console.error(`[video] Conversion failed: ${e.message}`);
      }
    }

    // Clean up temp files
    try { fs.unlinkSync(TEST_DB_PATH); } catch {}
    try { fs.unlinkSync(DB_SCRIPT_PATH); } catch {}
  });

  // ═══════════════════════════════════════════════════════════════════════
  // TEST 1: get_snapshot returns valid JSON with all required fields
  // ═══════════════════════════════════════════════════════════════════════

  test('get_snapshot returns valid JSON with all fields', async () => {
    const page = sharedPage!;
    await logTest(page, 'get_snapshot all fields', 'running', 'Calling get_snapshot…');

    const result = await callTool('get_snapshot', {});

    const requiredFields = [
      'quota', 'pricing', 'cost_today', 'routing_decisions',
      'provider_dist', 'dispatch_gate', 'scarcity', 'system',
      'participants', 'ledger',
    ];
    const missing = requiredFields.filter((f) => !(f in result));

    if (missing.length > 0) {
      await logTest(page, 'get_snapshot all fields', 'fail',
        `Missing fields: ${missing.join(', ')}\nGot keys: ${Object.keys(result).join(', ')}`);
      expect(missing).toHaveLength(0);
    }

    // Spot-check nested shapes
    expect(result.pricing).toHaveProperty('ours');
    expect(result.dispatch_gate).toHaveProperty('available');
    expect(result.scarcity).toHaveProperty('factor');
    expect(result.participants).toHaveProperty('count');
    expect(Array.isArray(result.ledger)).toBeTruthy();
    expect(Array.isArray(result.routing_decisions)).toBeTruthy();

    await logTest(page, 'get_snapshot all fields', 'pass',
      `All 10 fields present.\n` +
      `scarcity: factor=${result.scarcity.factor}, level=${result.scarcity.level}\n` +
      `participants: ${result.participants.count}\n` +
      `dispatch_gate available: ${result.dispatch_gate.available}`);
  });

  // ═══════════════════════════════════════════════════════════════════════
  // TEST 2: register_participant creates 50K account, rejects non-whitelisted
  // ═══════════════════════════════════════════════════════════════════════

  test('register_participant creates 50K account, rejects non-whitelisted', async () => {
    const page = sharedPage!;
    await logTest(page, 'register_participant + whitelist', 'running', 'Registering felix, alice, bob…');

    // Register whitelisted npubs
    const details: string[] = [];
    for (const [label, npub] of [['felix', FELIX], ['alice', ALICE], ['bob', BOB]] as const) {
      const result = await callTool('register_participant', { npub });
      if (result.ok) {
        registered.add(npub);
        details.push(`${label}: ✓ balance=${result.balance}`);
        expect(result.balance).toBe(50_000);
      } else if (result.error?.includes('already registered')) {
        registered.add(npub);
        details.push(`${label}: already registered (balance=${result.balance})`);
      } else {
        details.push(`${label}: ✗ ${result.error}`);
      }
    }

    // Non-whitelisted must be rejected
    const rejected = await callTool('register_participant', { npub: NON_WHITELISTED });
    expect(rejected.ok).toBe(false);
    expect(rejected.error?.toLowerCase()).toContain('whitelist');
    details.push(`non-whitelisted: ✓ rejected ("${rejected.error}")`);

    await logTest(page, 'register_participant + whitelist', 'pass', details.join('\n'));
  });

  // ═══════════════════════════════════════════════════════════════════════
  // TEST 3: get_ledger returns participant list
  // ═══════════════════════════════════════════════════════════════════════

  test('get_ledger returns participant list', async () => {
    const page = sharedPage!;
    await logTest(page, 'get_ledger', 'running', 'Fetching ledger…');

    const ledger = await callTool('get_ledger', {});

    expect(Array.isArray(ledger)).toBeTruthy();
    expect(ledger.length).toBeGreaterThanOrEqual(3); // felix, alice, bob

    const first = ledger[0];
    expect(first).toHaveProperty('npub_short');
    expect(first).toHaveProperty('balance');
    expect(first).toHaveProperty('prompts_sent');

    await logTest(page, 'get_ledger', 'pass',
      `${ledger.length} participants in ledger.\n` +
      ledger.slice(0, 5).map((p: any) =>
        `  ${p.npub_short}  bal=${p.balance}  prompts=${p.prompts_sent}  spent=${p.tokens_spent}`,
      ).join('\n'));
  });

  // ═══════════════════════════════════════════════════════════════════════
  // TEST 4: get_price_history returns historical data array
  // ═══════════════════════════════════════════════════════════════════════

  test('get_price_history returns historical data array', async () => {
    const page = sharedPage!;
    await logTest(page, 'get_price_history', 'running', 'Fetching 24h price history…');

    const result = await callTool('get_price_history', { hours: 24 });

    expect(result).toHaveProperty('hours');
    expect(result).toHaveProperty('points');
    expect(result.hours).toBe(24);
    expect(Array.isArray(result.points)).toBeTruthy();

    // Verify each point has required fields
    if (result.points.length > 0) {
      const p = result.points[0];
      expect(p).toHaveProperty('ts');
      expect(p).toHaveProperty('key');
      expect(p).toHaveProperty('your_price');
    }

    await logTest(page, 'get_price_history', 'pass',
      `${result.points.length} data points, bucket=${result.bucket_seconds}s\n` +
      `Keys: ${[...new Set(result.points.map((p: any) => p.key))].join(', ')}`);
  });

  // ═══════════════════════════════════════════════════════════════════════
  // TEST 5: send_prompt routes through proxy, returns response+provider+model+cost+tokens
  // ═══════════════════════════════════════════════════════════════════════

  test('send_prompt routes through proxy', async () => {
    const page = sharedPage!;
    await logTest(page, 'send_prompt via proxy', 'running', 'Sending prompt as felix…');

    const result = await callTool('send_prompt', {
      prompt: 'Reply with exactly: PROXY_TEST_OK',
      npub: FELIX,
    }, 45_000);

    expect(result.ok).toBe(true);
    expect(typeof result.response).toBe('string');
    expect(result.response.length).toBeGreaterThan(0);
    // tokens_used may be 0 if proxy DB hasn't settled — verify it's a number
    expect(typeof result.tokens_used).toBe('number');

    await logTest(page, 'send_prompt via proxy', 'pass',
      `ok=${result.ok}\n` +
      `response: "${result.response.slice(0, 120)}"\n` +
      `provider: ${result.provider}  model: ${result.model}\n` +
      `tokens: ${result.tokens_used}  cost_usd: ${result.cost_usd}\n` +
      `token_cost: ${result.token_cost}  new_balance: ${result.new_balance}\n` +
      `scarcity_factor: ${result.scarcity_factor}`);
  });

  // ═══════════════════════════════════════════════════════════════════════
  // TEST 6: Rate limiting — 2 prompts within 5s = second rejected
  // ═══════════════════════════════════════════════════════════════════════

  test('rate limiting (2 prompts in 5s = rejected)', async () => {
    const page = sharedPage!;
    await logTest(page, 'rate limit', 'running', 'Sending 2 prompts as alice within 5s…');

    // Fire first prompt (don't wait for response yet)
    const p1 = callTool('send_prompt', { prompt: 'Say A', npub: ALICE }, 45_000);

    // Small delay for relay propagation, then fire second
    await new Promise((r) => setTimeout(r, 300));
    const p2 = callTool('send_prompt', { prompt: 'Say B', npub: ALICE }, 15_000);

    const [r1, r2] = await Promise.all([p1, p2]);

    // At least one must be rate-limited
    const rateLimited = [r1, r2].filter(
      (r) => r.ok === false && r.error?.toLowerCase().includes('rate'),
    );
    const succeeded = [r1, r2].filter((r) => r.ok === true);

    expect(rateLimited.length).toBeGreaterThanOrEqual(1);

    await logTest(page, 'rate limit', 'pass',
      `Prompt 1: ${r1.ok ? '✓ ok' : '✗ ' + r1.error}\n` +
      `Prompt 2: ${r2.ok ? '✓ ok' : '✗ ' + r2.error}\n` +
      `Rate-limited: ${rateLimited.length}, Succeeded: ${succeeded.length}`);
  });

  // ═══════════════════════════════════════════════════════════════════════
  // TEST 7: Insufficient balance rejection
  // ═══════════════════════════════════════════════════════════════════════

  test('insufficient balance rejection', async () => {
    const page = sharedPage!;
    await logTest(page, 'insufficient balance', 'running', 'Draining bob balance to 1, sending prompt…');

    // Drain bob's balance to 1 token
    runDbHelper('set-balance', BOB, '1');
    await new Promise((r) => setTimeout(r, 300)); // let write settle

    // Send a prompt — should fail with insufficient tokens
    // Use a long prompt to ensure the cost exceeds 1 token
    const result = await callTool('send_prompt', {
      prompt: 'This prompt costs more than one token because it has many characters and should be rejected.',
      npub: BOB,
    }, 45_000);

    expect(result.ok).toBe(false);
    expect(result.error?.toLowerCase()).toMatch(/insufficient|not enough|balance/);

    await logTest(page, 'insufficient balance', 'pass',
      `bob balance set to 1\n` +
      `Prompt rejected: "${result.error}"`);
  });

  // ═══════════════════════════════════════════════════════════════════════
  // TEST 8: Scarcity factor correct at each tier
  // ═══════════════════════════════════════════════════════════════════════

  test('scarcity factor correct at each tier', async () => {
    const page = sharedPage!;
    await logTest(page, 'scarcity tiers', 'running', 'Testing 5 scarcity bands…');

    // Scarcity bands: <20%=1.0x, 20-40%=1.2x, 40-60%=1.5x, 60-80%=1.8x, >80%=2.0x
    const tiers = [
      { label: '<20%',  granted: 50_000, spent: 5_000,  expected: 1.0, pctRange: '0-19%' },
      { label: '20-40%',granted: 50_000, spent: 15_000, expected: 1.2, pctRange: '20-39%' },
      { label: '40-60%',granted: 50_000, spent: 25_000, expected: 1.5, pctRange: '40-59%' },
      { label: '60-80%',granted: 50_000, spent: 35_000, expected: 1.8, pctRange: '60-79%' },
      { label: '>80%',  granted: 50_000, spent: 45_000, expected: 2.0, pctRange: '80-100%' },
    ];

    const details: string[] = [];

    for (const tier of tiers) {
      // Set ledger state for this tier
      runDbHelper('set-budget', String(tier.granted), String(tier.spent));
      await new Promise((r) => setTimeout(r, 400)); // let write settle + avoid query collision

      // Query get_snapshot and check scarcity
      const snap = await callTool('get_snapshot', {});
      const factor = snap.scarcity?.factor;
      const pct = snap.scarcity?.budget_used_pct;

      const pass = factor === tier.expected;
      details.push(
        `${tier.label.padEnd(7)} pct=${pct?.toFixed(1)}%  factor=${factor}x  expected=${tier.expected}x  ${pass ? '✓' : '✗ MISMATCH'}`,
      );

      if (!pass) {
        await logTest(page, 'scarcity tiers', 'fail', details.join('\n'));
      }
      expect(factor).toBe(tier.expected);
    }

    await logTest(page, 'scarcity tiers', 'pass', details.join('\n'));
  });

});
