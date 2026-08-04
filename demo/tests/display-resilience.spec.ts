import { test, expect } from '@playwright/test';
import * as path from 'path';
import * as http from 'http';
import * as fs from 'fs';
import { fileURLToPath } from 'url';

/*
 * Display resilience + Nostr-native transport tests (DR1, Tasks 1-4).
 * Served from a static file server over http://localhost:<port>.
 * A mock CVM HTTP API backs the ?api= local-testing path.
 */

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
      if (!safePath.startsWith(DEMO_ROOT)) { res.writeHead(403); res.end(); return; }
      fs.readFile(safePath, (err, data) => {
        if (err) { res.writeHead(404); res.end(); return; }
        const ext = path.extname(safePath);
        const types: Record<string, string> = {
          '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css',
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

// --- Mock CVM HTTP API (for the ?api= local-testing path only) ---
let mockSnapshot: any = null;
let mockStatus: number = 200;
let mockDelay: number = 0;
let fetchCount: number = 0;

function startMockCVM(): http.Server {
  return http.createServer((req, res) => {
    fetchCount++;
    const url = req.url || '';
    setTimeout(() => {
      if (url.includes('/snapshot')) {
        if (mockStatus !== 200) { res.writeHead(mockStatus); res.end('{}'); return; }
        res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
        res.end(JSON.stringify(mockSnapshot || {}));
      } else if (url.includes('/price-history')) {
        res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
        res.end(JSON.stringify({ points: [] }));
      } else {
        res.writeHead(404); res.end();
      }
    }, mockDelay);
  });
}

let mockCVM: http.Server;
let mockCVMPort: number;

test.beforeAll(async () => {
  await startServer();
  mockCVM = startMockCVM();
  await new Promise<void>((r) => mockCVM.listen(0, () => {
    mockCVMPort = (mockCVM.address() as any).port; r();
  }));
});

test.afterAll(async () => {
  server.close();
  mockCVM.close();
});

test.beforeEach(() => { fetchCount = 0; mockStatus = 200; mockDelay = 0; mockSnapshot = null; });

/* ───────────────────────── TASK 1: Nostr-native transport ─────────────────────────
 * When no ?api= param is given, the display must NOT issue HTTP requests to a
 * localhost CVM — it must use Nostr (WebSocket) transport instead. We verify by
 * intercepting window.fetch and asserting zero HTTP fetches occurred. */
test('display uses Nostr transport when no api param provided', async ({ page }) => {
  await page.addInitScript(() => {
    (window as any).__nostr_fetches = 0;
    (window as any).__http_fetches = 0;
    const origFetch = window.fetch;
    window.fetch = function (...args: any[]) {
      const url = String(args[0]);
      if (url.includes('wss://') || url.includes('nostr')) (window as any).__nostr_fetches++;
      else (window as any).__http_fetches++;
      return (origFetch as any).apply(this, args as any);
    };
  });

  // Load WITHOUT ?api= param — must use Nostr
  await page.goto(`${baseUrl}/display-deploy/index.html`);
  await page.waitForTimeout(3000);

  const httpFetches = await page.evaluate(() => (window as any).__http_fetches);
  expect(httpFetches).toBe(0);
});

/* ───────────────────────── TASK 2: no badge flicker between polls ─────────────────────────
 * After the first successful load the badge is LIVE. On every subsequent poll
 * the badge must NOT drop back to "connecting" while the fetch is in flight —
 * it should stay LIVE until the result is known. We record every conn-text
 * mutation via a MutationObserver and assert "connecting" never reappears. */
test('connection badge stays LIVE between polls, does not flicker to connecting', async ({ page }) => {
  await page.addInitScript(() => {
    (window as any).__conn_history = [];
    const record = () => {
      const el = document.getElementById('conn-text');
      if (el) (window as any).__conn_history.push(el.textContent);
    };
    const start = () => {
      record();
      new MutationObserver(record).observe(document.getElementById('conn-text')!, {
        childList: true, characterData: true, subtree: true,
      });
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
  });

  const now = Date.now() / 1000;
  mockSnapshot = {
    ts: now,
    quota: { ours: { used_pct: 30, remaining: 1400000, locked: false } },
    pricing: {},
    routing_decisions: [{ ts: now, provider: 'ours', model: 'glm-5.2', tokens: 1000, cost: 0.001, reason: 'test' }],
    dispatch_gate: { can_dispatch: true, recommended_model: 'glm-5.2', reason: 'clear', effective_price_per_m: 0.03, safety_margin: 2 },
    cost_today: 1.5,
    cost_hour: 0.3,
    participants: { count: 1, total_prompts: 5, total_tokens: 10000 },
    scarcity: { factor: 1, level: 'low', budget_used_pct: 20 },
    system: {},
    pricing_meta: {},
    provider_dist: {},
    ledger: [],
  };

  await page.goto(`${baseUrl}/display-deploy/index.html?api=http://localhost:${mockCVMPort}`);

  // Wait for the first LIVE badge.
  await expect(page.locator('#conn-text')).toContainText('LIVE', { timeout: 5000 });

  // Watch across a full poll cycle (5s) + margin.
  await page.waitForTimeout(7000);

  const hist: string[] = await page.evaluate(() => (window as any).__conn_history);
  // Everything after the first LIVE must never read "connecting".
  const firstLiveIdx = hist.findIndex((t) => t && t.includes('LIVE'));
  const afterLive = hist.slice(firstLiveIdx + 1);
  const flickered = afterLive.some((t) => t && t.toLowerCase().includes('connecting'));
  expect(flickered, `badge flickered to connecting; history=${JSON.stringify(hist)}`).toBe(false);
});

/* ───────────────────────── TASK 3: STALE keeps data visible ─────────────────────────
 * When the CVM goes offline AFTER data has loaded, panels must keep showing the
 * last-known data (not blank) and the badge must read STALE, not "connecting". */
test('panels keep showing data with STALE badge after CVM goes offline', async ({ page }) => {
  const now = Date.now() / 1000;
  mockSnapshot = {
    ts: now,
    quota: { ours: { used_pct: 32, remaining: 1360000, locked: false } },
    pricing: {},
    routing_decisions: [{ ts: now, provider: 'ours', model: 'glm-5.2', tokens: 1000, cost: 0.001, reason: 'test' }],
    dispatch_gate: { can_dispatch: true, recommended_model: 'glm-5.2', reason: 'clear', effective_price_per_m: 0.03, safety_margin: 2 },
    cost_today: 1.72,
    cost_hour: 0.45,
    participants: { count: 1, total_prompts: 6, total_tokens: 16247 },
    scarcity: { factor: 1, level: 'low', budget_used_pct: 26 },
    system: {},
    pricing_meta: {},
    provider_dist: {},
    ledger: [],
  };

  await page.goto(`${baseUrl}/display-deploy/index.html?api=http://localhost:${mockCVMPort}`);

  // Data must have loaded.
  await expect(page.locator('#cost-big')).toContainText('$1', { timeout: 5000 });
  await expect(page.locator('#conn-text')).toContainText('LIVE');

  // Simulate the CVM restarting.
  mockStatus = 503;

  // Wait for at least one failed poll cycle.
  await page.waitForTimeout(7000);

  // Last-known data must still be visible (not blanked to $0.00).
  const costText = await page.locator('#cost-big').textContent();
  expect(costText, 'cost panel blanked during outage').toContain('$1');

  // Badge must show STALE (we have prior data), not "connecting" or blank.
  const badge = await page.locator('#conn-text').textContent();
  expect(badge, `badge was ${badge} during outage`).toContain('STALE');
});

/* ───────────────────────── TASK 4: exponential backoff ─────────────────────────
 * During an extended outage the poll interval must grow (5s→10s→20s→30s) and
 * reset to 5s on recovery. Rather than count fetches inside racy fixed time
 * windows, we timestamp every /snapshot fetch and assert the gap between
 * consecutive polls widens — the most direct proof of backoff. */
test('poll interval backs off exponentially during extended outage', async ({ page }) => {
  mockStatus = 503;   // CVM down from the very first poll

  await page.addInitScript(() => {
    (window as any).__snap_times = [];
    const of = window.fetch;
    window.fetch = function (...a: any[]) {
      const u = String(a[0]);
      if (u.includes('/snapshot')) (window as any).__snap_times.push(performance.now());
      return (of as any).apply(this, a);
    };
  });

  await page.goto(`${baseUrl}/display-deploy/index.html?api=http://localhost:${mockCVMPort}`);

  // Wait for ≥3 snapshot fetches: poll1 (load), poll2 (~5s), poll3 (~15s).
  for (let i = 0; i < 60; i++) {
    const n = await page.evaluate(() => (window as any).__snap_times.length);
    if (n >= 3) break;
    await page.waitForTimeout(500);
  }

  const times: number[] = await page.evaluate(() => (window as any).__snap_times);
  expect(times.length, `only ${times.length} snapshot fetches recorded`).toBeGreaterThanOrEqual(3);

  const gap1 = times[1] - times[0];   // expect ~5000ms
  const gap2 = times[2] - times[1];   // expect ~10000ms
  // The second gap must be noticeably larger than the first — backoff is active.
  expect(gap2, `gaps not widening: ${gap1.toFixed(0)}ms then ${gap2.toFixed(0)}ms`).toBeGreaterThan(gap1);
  // And clearly in the ~10s band (not still ~5s).
  expect(gap2).toBeGreaterThanOrEqual(8000);
});

/* ───────────────────────── TASK 5: all panels populate with real data ─────────────────────────
 * Comprehensive integration test: after a successful CVM connection EVERY panel
 * must show real data — cost meter, burn rate, quota bars, dispatch gate,
 * request flow, token economy, connection badge. The mock snapshot exercises
 * the full normalizeSnapshot() → render pipeline. routing_decisions is mapped to
 * `requests` by normalizeSnapshot, so two flow-cards must render. */
test('all panels populate with real data on successful connection', async ({ page }) => {
  const now = Date.now() / 1000;
  mockSnapshot = {
    ts: now,
    quota: {
      ours:   { used_pct: 32.3, remaining: 1354000, locked: false },
      friend: { used_pct: 91.0, remaining: 180000, locked: true },
    },
    pricing: {
      ours:   { cost_basis: 0.020, your_price: 0.030, margin_pct: 33 },
      friend: { cost_basis: 0.018, your_price: 0.025, margin_pct: 28 },
      ollama: { cost_basis: 0.015, your_price: 0.022, margin_pct: 32 },
      ppq:    { cost_basis: 0.040, your_price: 0.060, margin_pct: 33 },
    },
    routing_decisions: [
      { ts: now - 5,  provider: 'ours',   model: 'glm-5.2',       tokens: 3500, cost: 0.003, reason: 'flat-rate preferred' },
      { ts: now - 10, provider: 'friend', model: 'glm-4.5-flash', tokens: 2000, cost: 0.002, reason: 'sufficient headroom' },
    ],
    dispatch_gate: {
      can_dispatch: true,
      recommended_model: 'glm-5.2',
      reason: 'sufficient headroom (ours key)',
      effective_price_per_m: 0.03,
      safety_margin: 2,
      scarcity_factor: 1.0,
    },
    cost_today: 1.72,
    cost_hour: 0.45,
    participants: { count: 1, total_prompts: 6, total_tokens: 16247 },
    scarcity: { factor: 1.0, level: 'low', budget_used_pct: 26 },
    system: { cpu_pct: 15, mem_pct: 50, load_per_core: 0.8 },
    pricing_meta: { margin: 0.41 },
    provider_dist: { ours: 60, friend: 40 },
    ledger: [{ npub: 'npub1test', prompts: 6, tokens: 16247 }],
  };

  await page.goto(`${baseUrl}/display-deploy/index.html?api=http://localhost:${mockCVMPort}`);

  // Panel 5: cost meter — cost_today 1.72 renders "$1.72"; burn from cost_hour 0.45 → "$0.45".
  await expect(page.locator('#cost-big')).toContainText('$1', { timeout: 5000 });
  await expect(page.locator('#burn-rate')).toContainText('$0.45');

  // Panel 7: dispatch gate — CLEAR light + recommended model populated.
  await expect(page.locator('#gate-status')).toContainText('CLEAR');
  await expect(page.locator('#gate-model')).toContainText('glm-5.2');

  // Panel 4: quota bars — both provider keys render with their labels.
  await expect(page.locator('#quota-wrap')).toContainText('OURS');
  await expect(page.locator('#quota-wrap')).toContainText('FRIEND');

  // Panel 6: request flow — routing_decisions maps to two flow cards.
  await expect(page.locator('#flow-list .flow-card')).toHaveCount(2);

  // Panel 8: token economy — participant count populated.
  await expect(page.locator('#econ-participants')).toContainText('1');

  // Connection badge — live, real data.
  await expect(page.locator('#conn-text')).toContainText('LIVE');
});

/* ───────────────────────── TASK 6: QR points to participant nsite, not localhost ─────────────────────────
 * The QR a participant scans must resolve to the live participant nsite
 * (npub13h0…nsite.lol) — never localhost, a private IP, or the old dead
 * participant.nsite.lol URL. renderQR() truncates the visible #qr-url label to
 * 42 chars, so the trailing ".nsite.lol" never appears in the visible text;
 * we therefore verify the full resolved URL via resolveParticipantUrl() (the
 * value the QR actually encodes) in addition to the visible label, and confirm
 * the QR <svg> rendered (qrcode lib is vendored locally, not a CDN dep). */
test('QR code points to participant nsite URL, not localhost', async ({ page }) => {
  mockSnapshot = {
    ts: Date.now() / 1000, quota: {}, pricing: {}, routing_decisions: [],
    dispatch_gate: { can_dispatch: true }, cost_today: 0, cost_hour: 0,
    participants: { count: 0 }, scarcity: {}, system: {}, pricing_meta: {},
    provider_dist: {}, ledger: [],
  };

  await page.goto(`${baseUrl}/display-deploy/index.html?api=http://localhost:${mockCVMPort}`);

  const PARTICIPANT_NPUB = 'npub13h0eushvdzdrygm545zr5zxr3qvk7zaqhrzyyepprflcjdu3u0yskxz53m';
  const PARTICIPANT_URL = `https://${PARTICIPANT_NPUB}.nsite.lol`;

  // The QR <svg> must have rendered (qrcode lib is vendored, not a CDN fetch).
  await expect(page.locator('#qr-box svg')).toHaveCount(1, { timeout: 5000 });

  // Visible label: truncated to 42 chars, so it shows https:// + npub prefix but
  // NOT the trailing domain. It must identify the participant npub and must never
  // leak a localhost / private-IP / old-dead-url string.
  const label = (await page.locator('#qr-url').textContent()) || '';
  expect(label, 'qr-url label still at placeholder').not.toBe('—');
  expect(label).toContain('npub13h0eushvdzdrygm545zr5zxr3qvk'); // unique participant prefix (within 42-char window)
  expect(label).not.toContain('localhost');
  expect(label).not.toContain('127.0.0.1');
  expect(label).not.toContain('192.168');
  expect(label).not.toContain('participant.nsite.lol');         // old dead URL (no npub prefix)

  // Full resolved URL — what the QR actually encodes. resolveParticipantUrl is a
  // top-level function declaration, so it is exposed on window. Must be the exact
  // participant nsite, never localhost.
  const resolved = await page.evaluate(() =>
    typeof (window as any).resolveParticipantUrl === 'function'
      ? (window as any).resolveParticipantUrl()
      : null,
  );
  expect(resolved, 'resolveParticipantUrl not exposed on window').toBeTruthy();
  expect(resolved).toBe(PARTICIPANT_URL);
  expect(resolved).toContain('nsite.lol');
  expect(resolved).not.toContain('localhost');
});

/* ───────────────────────── TASK 9: Nostr freshness watchdog ─────────────────────────
 * In Nostr mode the display subscribes to kind-30315 events. If the CVM dies the
 * events stop arriving, but (before this fix) the badge stayed LIVE forever. The
 * freshness watchdog must transition LIVE → STALE once no event has arrived
 * within the staleness window. We mock SimplePool so the test is deterministic:
 * the fake pool delivers exactly ONE event (so the display reaches LIVE), then
 * goes silent. The display must then mark itself STALE within the (shortened)
 * threshold. ?stale_ms= is a test seam that shrinks the 15s production threshold. */
test('Nostr freshness watchdog marks badge STALE when events stop (Task 9)', async ({ page }) => {
  await page.addInitScript(() => {
    (window as any).__sub_calls = 0;
    let delivered = false;
    (window as any).__testNostrPool = class {
      subscribeMany(_relays: any, _filter: any, opts: any) {
        (window as any).__sub_calls++;
        if (!delivered) {
          delivered = true;
          const snap = {
            ts: Math.floor(Date.now() / 1000),
            quota: { ours: { used_pct: 30, remaining: 1400000, locked: false } },
            pricing: {}, routing_decisions: [],
            dispatch_gate: { can_dispatch: true, recommended_model: 'glm-5.2', reason: 'clear', effective_price_per_m: 0.03, safety_margin: 2 },
            cost_today: 1.5, cost_hour: 0.3,
            participants: { count: 1, total_prompts: 5, total_tokens: 10000 },
            scarcity: { factor: 1, level: 'low', budget_used_pct: 20 },
            system: {}, pricing_meta: {}, provider_dist: {}, ledger: [],
          };
          setTimeout(() => opts.onevent({
            kind: 30315, content: JSON.stringify(snap), tags: [['d', 'cvm-snapshot']],
          }), 50);
        }
        return { close() { /* noop */ } };
      }
    };
  });

  // stale_ms=1500 shrinks the 15s threshold; reconnect_ms left large so it
  // doesn't revive the sub before we observe STALE.
  await page.goto(`${baseUrl}/display-deploy/index.html?stale_ms=1500&reconnect_ms=600000`);

  // First the badge must reach LIVE on the one delivered event.
  await expect(page.locator('#conn-text')).toContainText('LIVE', { timeout: 5000 });

  // No further events → must transition to STALE within the threshold window.
  await expect(page.locator('#conn-text')).toContainText('STALE', { timeout: 6000 });

  // Last-known data must still be visible (not blanked).
  const cost = (await page.locator('#cost-big').textContent()) || '';
  expect(cost, 'cost panel blanked after going stale').toContain('$1');
});

/* ───────────────────────── TASK 10: Nostr reconnect logic ─────────────────────────
 * If a relay WebSocket drops, the subscription silently dies and no events ever
 * arrive again. The display must detect the silence and re-subscribe
 * (re-call initNostr). We prove it by having the fake pool deliver ONE event
 * (reaching LIVE), then never again. After the reconnect window elapses with no
 * events, subscribeMany must be called a second time. ?reconnect_ms= is the
 * test seam that shrinks the 60s production window. */
test('Nostr display re-subscribes after relay goes silent (Task 10)', async ({ page }) => {
  await page.addInitScript(() => {
    (window as any).__sub_calls = 0;
    let delivered = false;
    (window as any).__testNostrPool = class {
      subscribeMany(_relays: any, _filter: any, opts: any) {
        (window as any).__sub_calls++;
        if (!delivered) {
          delivered = true;
          const snap = {
            ts: Math.floor(Date.now() / 1000),
            quota: {}, pricing: {}, routing_decisions: [],
            dispatch_gate: { can_dispatch: true }, cost_today: 0, cost_hour: 0,
            participants: { count: 0 }, scarcity: {}, system: {},
            pricing_meta: {}, provider_dist: {}, ledger: [],
          };
          setTimeout(() => opts.onevent({
            kind: 30315, content: JSON.stringify(snap), tags: [['d', 'cvm-snapshot']],
          }), 50);
        }
        return { close() { /* noop */ } };
      }
    };
  });

  // reconnect_ms=2000 → reconnect check every 2s; stale_ms large so STALE
  // doesn't mask the reconnect behaviour.
  await page.goto(`${baseUrl}/display-deploy/index.html?reconnect_ms=2000&stale_ms=600000`);

  await expect(page.locator('#conn-text')).toContainText('LIVE', { timeout: 5000 });
  const callsAfterLive = await page.evaluate(() => (window as any).__sub_calls);

  // After the reconnect window with no events, subscribeMany must fire again.
  await expect.poll(
    async () => await page.evaluate(() => (window as any).__sub_calls),
    { timeout: 10000, intervals: [500] },
  ).toBeGreaterThan(callsAfterLive);
});

/* ───────────────────────── TASK 13: price history bootstrap in Nostr mode ─────────────────────────
 * In Nostr mode the display used to skip the /price-history fetch, so Panel 3
 * charts started empty and only grew 1 point per snapshot. The fix: the CVM
 * includes a price_history array in the kind-30315 snapshot payload, and the
 * display seeds its charts from it on the first snapshot. We deliver a snapshot
 * carrying a 3-point ours history and assert the OURS mini-chart rendered
 * multiple points (read straight off the Plotly figure on the DOM node). */
test('Nostr snapshot bootstraps price-history charts on load (Task 13)', async ({ page }) => {
  await page.addInitScript(() => {
    (window as any).__testNostrPool = class {
      subscribeMany(_relays: any, _filter: any, opts: any) {
        const now = Math.floor(Date.now() / 1000);
        const snap = {
          ts: now,
          quota: { ours: { used_pct: 30, remaining: 1400000, locked: false } },
          pricing: { ours: { cost_basis: 0.020, your_price: 0.030, margin_pct: 33 } },
          routing_decisions: [],
          dispatch_gate: { can_dispatch: true },
          cost_today: 0, cost_hour: 0,
          participants: { count: 0 }, scarcity: {}, system: {},
          pricing_meta: {}, provider_dist: {}, ledger: [],
          price_history: [
            { ts: now - 7200, key: 'ours', cost_basis: 0.020, your_price: 0.030, margin_pct: 33 },
            { ts: now - 3600, key: 'ours', cost_basis: 0.021, your_price: 0.031, margin_pct: 32 },
            { ts: now - 1800, key: 'ours', cost_basis: 0.019, your_price: 0.029, margin_pct: 34 },
          ],
        };
        setTimeout(() => opts.onevent({
          kind: 30315, content: JSON.stringify(snap), tags: [['d', 'cvm-snapshot']],
        }), 50);
        return { close() { /* noop */ } };
      }
    };
  });

  await page.goto(`${baseUrl}/display-deploy/index.html`);

  // Snapshot must have landed.
  await expect(page.locator('#conn-text')).toContainText('LIVE', { timeout: 5000 });

  // The OURS mini-chart must carry ≥3 bootstrapped points (Plotly stores the
  // figure on the DOM node as .data; trace 0 is the cost-basis line).
  const oursPoints = await page.evaluate(() => {
    const el = document.getElementById('mc-ours') as any;
    return el && Array.isArray(el.data) && el.data[0] ? el.data[0].x.length : 0;
  });
  expect(oursPoints, 'OURS chart did not bootstrap from price_history').toBeGreaterThanOrEqual(3);
});
