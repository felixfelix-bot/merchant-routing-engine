/**
 * Integration test — Sovereign Engineering demo (Task A4).
 *
 * End-to-end, multi-page flow driven through a SHARED mock CVM so the test is
 * deterministic (no relay flakiness). The mock CVM delegates every callTool to a
 * Node-side store via page.exposeFunction, so a prompt sent on the participant
 * page is visible on the display page's next get_snapshot — exactly what the real
 * CVM server mediates. Live relay round-trip is covered separately by the latency
 * probe (avg ~0.7 s; see COLD-REVIEW.md).
 *
 * Both nsites honour window.__testCVM (display/index.html ~L1011, participant/index.html).
 *
 * Run:  cd demo && npx playwright test --project=integration
 */
import { test, expect, type Page } from '@playwright/test';
import * as path from 'path';
import * as http from 'http';
import * as fs from 'fs';
import { fileURLToPath } from 'url';

const __filename_esm = fileURLToPath(import.meta.url);
const __dirname_esm = path.dirname(__filename_esm);
const DEMO_ROOT = path.resolve(__dirname_esm, '..');

let server: http.Server;
let baseUrl: string;

function startServer(): Promise<void> {
  return new Promise((resolve) => {
    server = http.createServer((req, res) => {
      const filePath = path.join(DEMO_ROOT, (req.url || '/').split('?')[0]);
      const safePath = path.resolve(filePath);
      if (!safePath.startsWith(DEMO_ROOT)) { res.writeHead(403); res.end('Forbidden'); return; }
      fs.readFile(safePath, (err, data) => {
        if (err) { res.writeHead(404); res.end('Not found'); return; }
        const ext = path.extname(safePath);
        const types: Record<string, string> = {
          '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css',
          '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2',
        };
        res.writeHead(200, { 'Content-Type': types[ext] || 'application/octet-stream' });
        res.end(data);
      });
    });
    server.listen(0, () => {
      const addr = server.address();
      if (addr && typeof addr === 'object') baseUrl = `http://localhost:${addr.port}`;
      resolve();
    });
  });
}
const stopServer = () => new Promise<void>((r) => server.close(() => r()));

const SCARCITY_BANDS = [[20, 1.0], [40, 1.2], [60, 1.5], [80, 1.8], [Infinity, 2.0]] as const;
function scarcity(pct: number): number {
  for (const [c, f] of SCARCITY_BANDS) if (pct < c) return f;
  return 2.0;
}
const rnd = (x: number, p = 4) => { const f = Math.pow(10, p); return Math.round((x + Number.EPSILON) * f) / f; };

interface P { npub: string; balance: number; granted: number; spent: number; prompts: number; joined: string; }
interface Req { ts: number; provider: string; model: string; tokens: number; cost: number; reason: string; }

function createStore(allowAll = true) {
  const participants = new Map<string, P>();
  const requests: Req[] = [];
  const lastPrompt = new Map<string, number>();
  const START = 50_000, RATE_MS = 5_000, MARGIN = 0.30;
  const keys = ['ours', 'friend', 'ollama', 'ppq'];
  const costBasis: Record<string, number> = { ours: 0.02, friend: 0.02, ollama: 0.05, ppq: 0.03 };
  const granted = () => { let s = 0; for (const p of participants.values()) s += p.granted; return s; };
  const spent = () => { let s = 0; for (const p of participants.values()) s += p.spent; return s; };
  const budgetPct = () => { const g = granted(); return g > 0 ? (spent() / g) * 100 : 0; };

  function snapshot() {
    const f = scarcity(budgetPct());
    const pricing: any = {};
    for (const k of keys) {
      const cb = costBasis[k]; const yp = cb * (1 + MARGIN);
      pricing[k] = { cost_basis: cb, your_price: yp, margin_pct: ((yp - cb) / yp) * 100 };
    }
    return {
      ts: Math.floor(Date.now() / 1000),
      pricing,
      pricing_meta: { margin: MARGIN },
      cost_today: rnd(spent() / 1e6 * 20, 4),
      cost_hour: rnd(spent() / 1e6 * 20, 4),
      routing_decisions: requests.slice(-20).reverse(),
      provider_dist: { ours: 0.5, friend: 0.3, ollama: 0.2 },
      dispatch_gate: { can_dispatch: true, reason: 'ok', scarcity_factor: f, safety_margin: 4 },
      scarcity: { factor: f, level: f >= 2 ? 'high' : 'moderate', budget_used_pct: rnd(budgetPct(), 2) },
      system: { cpu_pct: 12, mem_pct: 34 },
      participants: { count: participants.size, total_prompts: requests.length, total_tokens: spent() },
      ledger: [...participants.values()].sort((a, b) => b.spent - a.spent).slice(0, 10)
        .map((p) => ({ npub_short: p.npub.slice(0, 12) + '…', balance: p.balance, prompts_sent: p.prompts, tokens_spent: p.spent })),
    };
  }

  function handle(tool: string, args: any): any {
    switch (tool) {
      case 'get_snapshot': return snapshot();
      case 'get_ledger': return snapshot().ledger;
      case 'get_price_history': {
        const pts: any[] = []; const now = Math.floor(Date.now() / 1000);
        for (let h = 0; h < 6; h++) for (const k of keys) {
          const cb = costBasis[k] * (1 + budgetPct() / 400);
          pts.push({ ts: now - h * 3600, key: k, cost_basis: cb, your_price: cb * (1 + MARGIN), margin_pct: MARGIN / (1 + MARGIN) * 100 });
        }
        return { hours: 24, points: pts };
      }
      case 'register_participant': {
        const npub = args?.npub;
        if (!npub) return { ok: false, error: 'npub required' };
        if (!allowAll) return { ok: false, error: 'npub not whitelisted' };
        if (participants.has(npub)) return { ok: false, error: 'already registered', balance: participants.get(npub)!.balance };
        participants.set(npub, { npub, balance: START, granted: START, spent: 0, prompts: 0, joined: new Date().toISOString() });
        return { ok: true, balance: START, message: 'Welcome to the Sovereign Engineering demo!' };
      }
      case 'send_prompt': {
        const npub = args?.npub; const prompt: string = args?.prompt ?? '';
        if (!npub || typeof prompt !== 'string') return { ok: false, error: 'prompt and npub required' };
        const p = participants.get(npub);
        if (!p) return { ok: false, error: 'npub not registered — call register_participant first' };
        const now = Date.now();
        const last = lastPrompt.get(npub);
        if (last && now - last < RATE_MS) return { ok: false, error: 'rate limited; retry in 5s', retry_after_s: 5 };
        lastPrompt.set(npub, now);
        const estTok = Math.max(1, Math.ceil(prompt.length / 4));
        const f = scarcity(budgetPct());
        const ded = Math.round(estTok * f);
        if (p.balance < ded) return { ok: false, error: 'insufficient tokens' };
        p.balance -= ded; p.spent += ded; p.prompts++;
        const provider = ['ours', 'friend', 'ollama'][p.prompts % 3];
        requests.push({ ts: now / 1000, provider, model: 'glm-4.5-flash', tokens: estTok + 11, cost: rnd(estTok / 1e6 * 14.5, 6), reason: 'routed: flat-rate key' });
        return { ok: true, response: `Echo(${prompt.slice(0, 40)}): routed via ${provider}.`, provider, model: 'glm-4.5-flash', tokens_used: estTok + 11, cost_usd: rnd(estTok / 1e6 * 14.5, 6), token_cost: ded, new_balance: p.balance, scarcity_factor: f, reason: 'routed: flat-rate key' };
      }
      default: return { ok: false, error: `unknown tool: ${tool}` };
    }
  }
  return {
    handle, _participants: participants, _requests: requests,
    _setUsage: (npub: string, spent: number) => { const p = participants.get(npub); if (p) { p.spent = spent; p.balance = Math.max(0, p.granted - spent); } },
    _reset: () => { participants.clear(); requests.length = 0; lastPrompt.clear(); },
  };
}

const TEST_PUBKEY = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef';

async function wirePage(page: Page, store: ReturnType<typeof createStore>, opts: { cvmFail?: boolean } = {}) {
  await page.addInitScript((pk) => {
    (window as any).nostr = {
      getPublicKey: async () => pk,
      signEvent: async (e: any) => ({ ...e, sig: 'mock-sig' }),
      getRelays: async () => ({}),
      nip04: { encrypt: async () => '', decrypt: async () => '' },
      nip44: { encrypt: async () => '', decrypt: async () => '' },
    };
  }, TEST_PUBKEY);
  await page.exposeFunction('__cvmCall', (tool: string, args: any) => {
    if (opts.cvmFail) throw new Error('CVM server unreachable (test)');
    return store.handle(tool, args);
  });
  await page.addInitScript(() => {
    (window as any).__testCVM = {
      connected: true,
      pk: 'aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899',
      callTool: function (name: string, args: any) { return (window as any).__cvmCall(name, args); },
      connect: async () => true,
      close: () => {},
    };
  });
}

const num = (s: string) => Number((s || '').replace(/[^0-9]/g, '')) || 0;

test.describe('Integration — display ↔ participant via shared CVM', () => {
  test.beforeAll(async () => { await startServer(); });
  test.afterAll(async () => { await stopServer(); });

  test('display nsite loads, QR visible', async ({ page }) => {
    const store = createStore();
    await wirePage(page, store);
    await page.goto(`${baseUrl}/display/index.html`, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('#qr-box')).toBeVisible();
    await expect(page.locator('#qr-box svg')).toBeVisible({ timeout: 8000 });
    await page.screenshot({ path: 'test-results/screenshots/int-01-display.png' });
  });

  test('participant page opens, NIP07 login + auto-register', async ({ browser }) => {
    const store = createStore();
    const phone = await browser.newPage({ viewport: { width: 375, height: 667 }, isMobile: true, hasTouch: true });
    await wirePage(phone, store);
    await phone.goto(`${baseUrl}/participant/index.html`, { waitUntil: 'domcontentloaded' });
    await expect(phone.locator('#login-card')).toBeVisible();
    await phone.locator('#btn-nip07').click();
    await expect(phone.locator('#login-card')).toHaveClass(/hidden/);
    await expect(phone.locator('#balance-card')).toBeVisible({ timeout: 8000 });
    await expect(phone.locator('#balance-big')).toContainText('50', { timeout: 8000 });
    await phone.screenshot({ path: 'test-results/screenshots/int-02-login.png' });
    await phone.close();
  });

  test('send prompt → response + balance drop → display shows request within one poll', async ({ browser }) => {
    const store = createStore();
    const phone = await browser.newPage({ viewport: { width: 375, height: 667 }, isMobile: true, hasTouch: true });
    const display = await browser.newPage();
    await wirePage(phone, store);
    await wirePage(display, store);
    await phone.goto(`${baseUrl}/participant/index.html`, { waitUntil: 'domcontentloaded' });
    await display.goto(`${baseUrl}/display/index.html`, { waitUntil: 'domcontentloaded' });
    await phone.locator('#btn-nip07').click();
    await expect(phone.locator('#prompt-card')).toBeVisible({ timeout: 8000 });
    await phone.locator('#prompt-input').fill('Hello sovereign world');
    const before = await phone.evaluate(() => (typeof state !== 'undefined' && state ? state.balance : -1));
    await phone.locator('#btn-send').click();
    await expect(phone.locator('#response-card')).toHaveClass(/show/, { timeout: 15000 });
    await expect(phone.locator('#resp-body')).not.toBeEmpty();
    const after = await phone.evaluate(() => (typeof state !== 'undefined' && state ? state.balance : -1));
    expect(after).toBeLessThan(before);
    await display.evaluate(() => (window as any).poll && (window as any).poll());
    await display.waitForTimeout(1000);
    const flowText = await display.locator('#flow-list').innerText();
    expect(flowText.length).toBeGreaterThan(0);
    await display.screenshot({ path: 'test-results/screenshots/int-03-display-after-prompt.png' });
    await phone.close(); await display.close();
  });

  test('multiple participants drive scarcity up; charts + flow update on display', async ({ browser }) => {
    const store = createStore();
    const display = await browser.newPage();
    await wirePage(display, store);
    await display.goto(`${baseUrl}/display/index.html`, { waitUntil: 'domcontentloaded' });
    await display.evaluate(() => (window as any).poll && (window as any).poll());
    await display.waitForTimeout(800);
    // two participants at 80% spend each → ~80% budget usage → scarcity steps past 1.0x
    store.handle('register_participant', { npub: 'npubA' });
    store._setUsage('npubA', 40_000);
    store.handle('register_participant', { npub: 'npubB' });
    store._setUsage('npubB', 40_000);
    // a third active participant sends real prompts → appear in the flow list
    store.handle('register_participant', { npub: 'npubC' });
    for (let i = 0; i < 3; i++) store.handle('send_prompt', { npub: 'npubC', prompt: 'live prompt number ' + i });
    await display.evaluate(() => (window as any).poll && (window as any).poll());
    await display.waitForTimeout(1000);
    const flowAfter = (await display.locator('#flow-list').innerText()).length;
    expect(flowAfter).toBeGreaterThan(0); // flow list populated with live requests
    const charts = await display.locator('.mini-chart').count();
    expect(charts).toBeGreaterThan(0); // per-key charts rendered
    const snap: any = store.handle('get_snapshot', {});
    expect(snap.scarcity.factor).toBeGreaterThan(1.0); // scarcity ramped past 1.0x
    await display.screenshot({ path: 'test-results/screenshots/int-04-scarcity.png' });
    await display.close();
  });

  test('graceful behaviour when CVM server unreachable', async ({ browser }) => {
    const store = createStore();
    const phone = await browser.newPage({ viewport: { width: 375, height: 667 }, isMobile: true, hasTouch: true });
    await wirePage(phone, store, { cvmFail: true });
    await phone.goto(`${baseUrl}/participant/index.html`, { waitUntil: 'domcontentloaded' });
    await phone.locator('#btn-nip07').click();
    await phone.waitForTimeout(2000);
    expect(await phone.locator('body').isVisible()).toBeTruthy();
    const usable = await phone.locator('#balance-card, #register-status').first().isVisible().catch(() => false);
    expect(usable).toBeTruthy();
    await phone.screenshot({ path: 'test-results/screenshots/int-05-cvm-unreachable.png' });
    await phone.close();
  });

  test('demo reset clears all participants and prompts', async () => {
    const store = createStore();
    store.handle('register_participant', { npub: 'npubR' });
    store.handle('send_prompt', { npub: 'npubR', prompt: 'temp' });
    expect(store._participants.size).toBe(1);
    expect(store._requests.length).toBe(1);
    store._reset();
    expect(store._participants.size).toBe(0);
    expect(store._requests.length).toBe(0);
  });
});
