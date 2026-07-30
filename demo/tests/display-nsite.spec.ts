import { test, expect, type Page } from '@playwright/test';
import * as path from 'path';
import * as http from 'http';
import * as fs from 'fs';
import { fileURLToPath } from 'url';

// ═══════════════════════════════════════════════════════════════════════════
// STATIC FILE SERVER — serves display/index.html (+ vendored libs) for tests
// ═══════════════════════════════════════════════════════════════════════════

const __filename_esm = fileURLToPath(import.meta.url);
const __dirname_esm = path.dirname(__filename_esm);
const DEMO_ROOT = path.resolve(__dirname_esm, '..');
let server: http.Server;
let baseUrl: string;

function startServer(): Promise<void> {
  return new Promise((resolve) => {
    server = http.createServer((req: http.IncomingMessage, res: http.ServerResponse) => {
      const urlPath = req.url || '/';
      const filePath = path.join(DEMO_ROOT, urlPath.split('?')[0]);
      const safePath = path.resolve(filePath);
      if (!safePath.startsWith(DEMO_ROOT)) { res.writeHead(403); res.end('Forbidden'); return; }
      fs.readFile(safePath, (err: NodeJS.ErrnoException | null, data: Buffer) => {
        if (err) { res.writeHead(404); res.end('Not found'); return; }
        const ext = path.extname(safePath);
        const types: Record<string, string> = {
          '.html': 'text/html', '.js': 'application/javascript', '.mjs': 'application/javascript',
          '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml',
          '.woff': 'font/woff', '.woff2': 'font/woff2', '.png': 'image/png',
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

function stopServer(): Promise<void> {
  return new Promise((resolve) => server.close(() => resolve()));
}

// ═══════════════════════════════════════════════════════════════════════════
// MOCK CVM CLIENT  (exercises the LIVE get_snapshot path deterministically)
// Mirrors the participant nsite's window.__testCVM hook. `snapshots` is a
// queue: each get_snapshot() call returns the next, then repeats the last —
// so stateful tests can simulate a new routing decision arriving on the next
// 5s poll by forcing poll() after the first render.
// ═══════════════════════════════════════════════════════════════════════════

function injectCVM(page: Page, snapshots: any[]) {
  return page.addInitScript((snaps: any[]) => {
    let idx = 0;
    (window as any).__testCVM = {
      connected: true,
      pk: '00'.repeat(32),
      callTool: async (name: string) => {
        if (name !== 'get_snapshot') return { ok: false, error: 'no mock for ' + name };
        const s = snaps[Math.min(idx, snaps.length - 1)];
        idx++;
        return s;
      },
      connect: async () => true,
      close: () => {},
    };
  }, snapshots);
}

// Build a realistic get_snapshot payload. Override any field per-test.
function snap(overrides: Record<string, any> = {}): any {
  const now = Date.now() / 1000;
  return {
    ts: now,
    pricing: {
      ours:   { cost_basis: 0.020, your_price: 0.030, margin_pct: 33, effective_rate: 0.030 },
      friend: { cost_basis: 0.024, your_price: 0.034, margin_pct: 29, effective_rate: 0.034 },
      ollama: { cost_basis: 0.008, your_price: 0.014, margin_pct: 43, effective_rate: 0.014 },
      ppq:    { cost_basis: 0.014, your_price: 0.021, margin_pct: 33, effective_rate: 0.021 },
    },
    quota: {
      ours:   { used_pct: 38, remaining: 1240000, total: 2000000, healthy: true,  resets_in_min: 180 },
      friend: { used_pct: 22, remaining: 1560000, total: 2000000, healthy: true,  resets_in_min: 160 },
    },
    requests: [
      { ts: now - 5,  requester: 'npub1felix2026', provider: 'ours',   model: 'glm-5.2', tokens: 1200, cost: 0.0036, reason: 'sufficient headroom' },
      { ts: now - 25, requester: 'npub1satoshi9k', provider: 'friend', model: 'glm-4.5', tokens: 800,  cost: 0.0027, reason: 'failover from ours' },
    ],
    provider_distribution: { ours: 0.60, friend: 0.25, ollama: 0.10, ppq: 0.05 },
    dispatch_gate: {
      can_dispatch: true, reason: 'sufficient headroom (ours key) with 2× margin',
      recommended_model: 'glm-5.2', effective_price_per_m: 0.030,
      scarcity_factor: 1.0, safety_margin: 2.0, downgraded: false,
    },
    cost_today: 2.50,
    burn_rate_per_hour: 0.12,
    system: { cpu_pct: 9, mem_pct: 45 },
    scarcity: { factor: 1.0, level: 'low', budget_used_pct: 30 },
    participants: { count: 6, total_prompts: 14, total_tokens: 42000 },
    ledger: [
      { npub_short: 'npub1felix…2026', balance: 45000, prompts_sent: 4, tokens_spent: 5000 },
      { npub_short: 'npub1satoshi…9k', balance: 38000, prompts_sent: 3, tokens_spent: 12000 },
    ],
    ...overrides,
  };
}

const DISPLAY_URL = () => `${baseUrl}/display/index.html`;

// Wait for init() to finish: connection badge flips to DEMO/LIVE, QR renders,
// and the 4 per-key chart wrappers exist.
async function waitForReady(page: Page, live = false) {
  await page.waitForFunction((liv: boolean) => {
    const t = (document.getElementById('conn-text') || {}).textContent || '';
    return liv ? /LIVE/.test(t) : /DEMO MODE/.test(t);
  }, live, { timeout: 15000 });
  await expect(page.locator('#qr-box svg')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('#charts-grid .mini-chart-wrap')).toHaveCount(4, { timeout: 15000 });
}

// Force an immediate poll() (rather than waiting the 5s interval) so stateful
// mock tests can advance to the next queued snapshot.
const forcePoll = (page: Page) => page.evaluate(() => (window as any).poll());

// ═══════════════════════════════════════════════════════════════════════════
// TESTS
// ═══════════════════════════════════════════════════════════════════════════

test.describe('Display Nsite — 8-Panel Dashboard', () => {

  test.beforeAll(async () => { await startServer(); });
  test.afterAll(async () => { await stopServer(); });

  // ── Test 1: Page loads, all 8 panels visible ─────────────────────────────
  test('1. page loads, all 8 panels visible', async ({ page }) => {
    await page.goto(DISPLAY_URL());
    await waitForReady(page);

    await expect(page).toHaveTitle(/Sovereign Routing Engine/);
    // 1 QR · 2 system diagram · 3 per-key charts · 4 quota bars ·
    // 5 cost meter · 6 request flow · 7 dispatch gate · 8 token economy
    await expect(page.locator('#qr-box')).toBeVisible();
    await expect(page.locator('#diagram')).toBeVisible();
    await expect(page.locator('#charts-grid')).toBeVisible();
    await expect(page.locator('#quota-wrap')).toBeVisible();
    await expect(page.locator('#cost-big')).toBeVisible();
    await expect(page.locator('#flow-list')).toBeVisible();
    await expect(page.locator('#gate-light')).toBeVisible();
    await expect(page.locator('#econ-participants')).toBeVisible();

    await page.screenshot({ path: 'test-results/screenshots/d-01-panels.png', fullPage: true });
  });

  // ── Test 2: CVM get_snapshot call succeeds, data populates panels ────────
  test('2. CVM get_snapshot call succeeds, data populates panels', async ({ page }) => {
    // distinctive mock values (14 prompts / 42000 tokens) — differ from demo
    await injectCVM(page, [snap()]);
    await page.goto(DISPLAY_URL());
    await waitForReady(page, true);

    // live path was taken (not demo fallback)
    await expect(page.locator('#conn-text')).toContainText(/LIVE/);
    // mock data reached the DOM
    await expect(page.locator('#econ-prompts')).toHaveText('14');          // total_prompts
    await expect(page.locator('#econ-tokens')).toContainText(/42/);        // 42000 -> 42.0K
    await expect(page.locator('#gate-model')).toContainText('glm-5.2');
    await expect(page.locator('#flow-list .flow-card')).toHaveCount(2);

    await page.screenshot({ path: 'test-results/screenshots/d-02-cvm-live.png', fullPage: true });
  });

  // ── Test 3: Per-key price charts render 4 charts × 3 lines ───────────────
  test('3. per-key price charts render 4 charts with 3 lines each', async ({ page }) => {
    await page.goto(DISPLAY_URL());
    await waitForReady(page);

    await expect(page.locator('#charts-grid .mini-chart-wrap')).toHaveCount(4);

    const info = await page.evaluate(() => {
      const ids = ['mc-ours', 'mc-friend', 'mc-ollama', 'mc-ppq'];
      return ids.map((id) => {
        const el = document.getElementById(id) as any;
        if (!el || !el.data) return { traces: 0, nonEmpty: false, hasMargin: false };
        return {
          traces: el.data.length,
          nonEmpty: el.data.every((t: any) => Array.isArray(t.y) && t.y.length > 0),
          hasMargin: el.data.some((t: any) => t.fill === 'tozeroy'), // margin% area fill
        };
      });
    });

    for (const c of info) {
      expect(c.traces).toBe(3);            // cost basis · your price · margin%
      expect(c.nonEmpty).toBeTruthy();     // each line has data points
    }
    expect(info.filter((c) => c.hasMargin).length).toBe(4);

    await page.screenshot({ path: 'test-results/screenshots/d-03-charts.png' });
  });

  // ── Test 4: QR code generates and encodes participant nsite URL ──────────
  test('4. QR code generates and encodes participant nsite URL', async ({ page }) => {
    await page.goto(DISPLAY_URL());
    await waitForReady(page);

    const url = 'https://participant.nsite.lol';
    await expect(page.locator('#qr-url')).toContainText(url);

    // a real QR renders as an SVG with hundreds of module rects
    const rectCount = await page.locator('#qr-box svg rect').count();
    expect(rectCount).toBeGreaterThan(50);

    // qrcode lib loaded locally (offline bundle)
    const libs = await page.evaluate(() => ({
      qrcode: typeof (window as any).qrcode,
      plotly: typeof (window as any).Plotly,
      nostr: !!(window as any).NostrTools,
    }));
    expect(libs.qrcode).toBe('function');
    expect(libs.plotly).toBe('object');
    expect(libs.nostr).toBe(true);

    await page.screenshot({ path: 'test-results/screenshots/d-04-qr.png' });
  });

  // ── Test 5: Quota bars show correct colors ───────────────────────────────
  test('5. quota bars show correct colors (green<50, yellow 50-80, red>80)', async ({ page }) => {
    await injectCVM(page, [snap({
      quota: {
        lowkey:  { used_pct: 30, remaining: 1400000, total: 2000000, healthy: true,  resets_in_min: 180 },
        midkey:  { used_pct: 65, remaining:  700000, total: 2000000, healthy: true,  resets_in_min: 160 },
        highkey: { used_pct: 92, remaining:  160000, total: 2000000, healthy: false, resets_in_min: 120 },
      },
    })]);
    await page.goto(DISPLAY_URL());
    await waitForReady(page, true);

    await expect(page.locator('#quota-wrap .qb')).toHaveCount(3);

    const cls = async (name: string) => {
      const el = page.locator('#quota-wrap .qb', { hasText: name }).first();
      return (await el.locator('.qb-fill').getAttribute('class')) || '';
    };
    expect(await cls('LOWKEY')).toContain('green');   // 30%
    expect(await cls('MIDKEY')).toContain('yellow');  // 65%
    expect(await cls('HIGHKEY')).toContain('red');    // 92%

    await page.screenshot({ path: 'test-results/screenshots/d-05-quota.png' });
  });

  // ── Test 6: Request flow shows new card on new routing decision ──────────
  test('6. request flow shows new card when new routing decision arrives', async ({ page }) => {
    const now = Date.now() / 1000;
    const req2 = [
      { ts: now - 5, requester: 'npub1aaa', provider: 'ours',   model: 'glm-5.2',      tokens: 100, cost: 0.0010, reason: 'headroom' },
      { ts: now - 9, requester: 'npub1bbb', provider: 'friend', model: 'glm-4.5',      tokens: 200, cost: 0.0020, reason: 'failover' },
    ];
    const req5 = [
      ...req2,
      { ts: now - 2, requester: 'npub1ccc', provider: 'ours',   model: 'glm-5.2',      tokens: 150, cost: 0.0015, reason: 'headroom' },
      { ts: now - 3, requester: 'npub1ddd', provider: 'ollama', model: 'glm-4.5-air',  tokens:  90, cost: 0.0009, reason: 'cheap path' },
      { ts: now - 4, requester: 'npub1eee', provider: 'ppq',    model: 'glm-4.5-flash',tokens:  60, cost: 0.0006, reason: 'flat-rate' },
    ];
    await injectCVM(page, [snap({ requests: req2 }), snap({ requests: req5 })]);
    await page.goto(DISPLAY_URL());
    await waitForReady(page, true);

    await expect(page.locator('#flow-list .flow-card')).toHaveCount(2);

    // a new routing decision arrives on the next poll → flow grows to 5
    await forcePoll(page);
    await expect(page.locator('#flow-list .flow-card')).toHaveCount(5);

    await page.screenshot({ path: 'test-results/screenshots/d-06-flow.png' });
  });

  // ── Test 7: Cost meter increments on new request ─────────────────────────
  test('7. cost meter increments on new request', async ({ page }) => {
    const now = Date.now() / 1000;
    const r1 = [{ ts: now - 5, requester: 'npub1x', provider: 'ours', model: 'glm-5.2', tokens: 100, cost: 0.001, reason: 'h' }];
    const r2 = [r1[0], { ts: now - 2, requester: 'npub1y', provider: 'ours', model: 'glm-5.2', tokens: 120, cost: 0.0012, reason: 'h' }];
    await injectCVM(page, [
      snap({ cost_today: 2.10, requests: r1 }),
      snap({ cost_today: 3.45, requests: r2 }),
    ]);
    await page.goto(DISPLAY_URL());
    await waitForReady(page, true);

    await expect(page.locator('#cost-big')).toContainText('$2');
    await expect(page.locator('#cost-ticks')).toContainText('1 request');

    // a new request lands → cost meter ticks up
    await forcePoll(page);
    await expect(page.locator('#cost-big')).toContainText('$3');
    await expect(page.locator('#cost-ticks')).toContainText('2 request');

    await page.screenshot({ path: 'test-results/screenshots/d-07-cost.png' });
  });

  // ── Test 8: Dispatch gate shows green/red based on can_dispatch ──────────
  test('8. dispatch gate shows green/red based on can_dispatch', async ({ page }) => {
    await injectCVM(page, [
      snap({ dispatch_gate: { can_dispatch: true,  reason: 'sufficient headroom',            recommended_model: 'glm-5.2', effective_price_per_m: 0.030, scarcity_factor: 1.0, safety_margin: 2.0, downgraded: false } }),
      snap({ dispatch_gate: { can_dispatch: false, reason: 'quota pressure — holding',       recommended_model: null,      effective_price_per_m: 0.060, scarcity_factor: 1.8, safety_margin: 2.0, downgraded: true  } }),
    ]);
    await page.goto(DISPLAY_URL());
    await waitForReady(page, true);

    // CLEAR (green)
    await expect(page.locator('#gate-light')).toHaveClass(/\bgo\b/);
    await expect(page.locator('#gate-status')).toContainText('CLEAR');

    // next decision → HOLD (red)
    await forcePoll(page);
    await expect(page.locator('#gate-light')).toHaveClass(/\bhold\b/);
    await expect(page.locator('#gate-status')).toContainText('HOLD');
    await expect(page.locator('#gate-reason')).toContainText(/holding|pressure/i);

    await page.screenshot({ path: 'test-results/screenshots/d-08-gate.png' });
  });

  // ── Test 9: Data refreshes every 5s without page reload ──────────────────
  test('9. data refreshes every 5s without page reload', async ({ page }) => {
    await page.goto(DISPLAY_URL());
    await waitForReady(page);

    const before = await page.evaluate(() => {
      let histLen = -1;
      try { histLen = (window as any).state.priceHist.ours.length; } catch (e) {}
      return { histLen, price: ((document.getElementById('mc-price-ours') || {}) as any).textContent || '' };
    });

    // sentinel that only survives if the page does NOT reload
    await page.evaluate(() => { (window as any).__noReload = 987654; });

    // one POLL_MS interval (5000ms)
    await page.waitForTimeout(5500);

    const after = await page.evaluate(() => {
      let histLen = -1;
      try { histLen = (window as any).state.priceHist.ours.length; } catch (e) {}
      return { histLen, price: ((document.getElementById('mc-price-ours') || {}) as any).textContent || '', marker: (window as any).__noReload };
    });

    expect(after.marker).toBe(987654);                  // no reload happened
    const grew = after.histLen > before.histLen;        // demo appends a price point per poll
    const priceChanged = after.price !== before.price;  // price drifts each poll
    expect(grew || priceChanged).toBeTruthy();          // the 5s interval fired + refreshed data

    await page.screenshot({ path: 'test-results/screenshots/d-09-refresh.png' });
  });

  // ── Test 10: Offline mode (Plotly + nostr-tools bundled, no CDN) ─────────
  test('10. offline mode works (Plotly + nostr-tools bundled, no external CDN)', async ({ page }) => {
    const external: string[] = [];
    page.on('request', (req) => {
      const u = req.url();
      if ((/^https?:\/\//.test(u) || /^wss?:\/\//.test(u)) && !/localhost|127\.0\.0\.1/.test(u)) {
        external.push(u);
      }
    });

    await page.goto(DISPLAY_URL());
    await waitForReady(page);
    await page.waitForTimeout(500); // let any deferred requests settle

    // zero requests left the machine
    expect(external).toEqual([]);

    // vendored libs loaded from local vendor/, available on window
    const libs = await page.evaluate(() => ({
      plotly: typeof (window as any).Plotly,
      nostr: !!(window as any).NostrTools,
      qrcode: typeof (window as any).qrcode,
    }));
    expect(libs.plotly).toBe('object');
    expect(libs.nostr).toBe(true);
    expect(libs.qrcode).toBe('function');

    // every <script src> is a local vendor path — no CDN
    const srcs = await page.evaluate(() =>
      Array.from(document.querySelectorAll('script[src]')).map((s) => (s as HTMLScriptElement).src));
    expect(srcs.length).toBe(3); // plotly · nostr-tools · qrcode
    for (const s of srcs) {
      expect(s).toContain('vendor/');
      expect(s).not.toMatch(/cdn|unpkg|jsdelivr|cloudflare|googleapis|cdnjs/i);
    }

    await page.screenshot({ path: 'test-results/screenshots/d-10-offline.png', fullPage: true });
  });

});
