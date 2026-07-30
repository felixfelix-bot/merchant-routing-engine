import { test, expect, type Page } from '@playwright/test';
import * as path from 'path';
import * as http from 'http';
import * as fs from 'fs';
import { fileURLToPath } from 'url';

// ═══════════════════════════════════════════════════════════════════════════
// STATIC FILE SERVER — serves participant/index.html for tests
// ═══════════════════════════════════════════════════════════════════════════

const __filename_esm = fileURLToPath(import.meta.url);
const __dirname_esm = path.dirname(__filename_esm);
const DEMO_ROOT = path.resolve(__dirname_esm, '..');
let server: http.Server;
let baseUrl: string;

function startServer(): Promise<void> {
  return new Promise((resolve) => {
    server = http.createServer((req: http.IncomingMessage, res: http.ServerResponse) => {
      let urlPath = req.url || '/';
      const filePath = path.join(DEMO_ROOT, urlPath.split('?')[0]);
      const safePath = path.resolve(filePath);
      if (!safePath.startsWith(DEMO_ROOT)) {
        res.writeHead(403); res.end('Forbidden'); return;
      }
      fs.readFile(safePath, (err: NodeJS.ErrnoException | null, data: Buffer) => {
        if (err) {
          res.writeHead(404); res.end('Not found'); return;
        }
        const ext = path.extname(safePath);
        const types: Record<string, string> = {
          '.html': 'text/html', '.js': 'application/javascript',
          '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml',
        };
        res.writeHead(200, { 'Content-Type': types[ext] || 'application/octet-stream' });
        res.end(data);
      });
    });
    server.listen(0, () => {
      const addr = server.address();
      if (addr && typeof addr === 'object') {
        baseUrl = `http://localhost:${addr.port}`;
      }
      resolve();
    });
  });
}

function stopServer(): Promise<void> {
  return new Promise((resolve) => server.close(() => resolve()));
}

// ═══════════════════════════════════════════════════════════════════════════
// MOCK HELPERS
// ═══════════════════════════════════════════════════════════════════════════

const TEST_PUBKEY = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef';

// NIP07 mock — simulates Alby/nostr browser extension
function injectNIP07(page: Page, pubkey: string = TEST_PUBKEY) {
  return page.addInitScript((pk: string) => {
    (window as any).nostr = {
      getPublicKey: async () => pk,
      signEvent: async (event: any) => ({ ...event, sig: 'mock-sig' }),
      getRelays: async () => ({}),
      nip04: { encrypt: async () => '', decrypt: async () => '' },
      nip44: { encrypt: async () => '', decrypt: async () => '' },
    };
  }, pubkey);
}

// CVM mock — injects a mock CVM client via window.__testCVM (checked by page init)
// This runs via addInitScript so it's in place BEFORE the page's init() runs.
function injectCVM(page: Page, responses: Record<string, any>) {
  return page.addInitScript((mocks: Record<string, any>) => {
    (window as any).__testCVM = {
      connected: true,
      pk: 'aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899',
      callTool: async function (name: string, args: any) {
        const m = (mocks as any)[name];
        if (m instanceof Function) return m(args);
        if (m) return m;
        return { ok: false, error: 'No mock for: ' + name };
      },
      connect: async () => true,
      close: () => {},
    };
  }, responses);
}

// ═══════════════════════════════════════════════════════════════════════════
// TESTS
// ═══════════════════════════════════════════════════════════════════════════

test.describe('Participant Nsite — Full Flow', () => {

  test.beforeAll(async () => {
    await startServer();
  });

  test.afterAll(async () => {
    await stopServer();
  });

  // ── Test 1: Page loads on mobile viewport (375px) ───────────────────────
  test('page loads on mobile viewport (375px)', async ({ page }) => {
    await page.goto(`${baseUrl}/participant/index.html`);
    await expect(page).toHaveTitle(/Sovereign Demo/);
    await expect(page.locator('#login-card')).toBeVisible();
    await expect(page.locator('#login-card h2')).toContainText('Login with Nostr');

    const vw = await page.evaluate(() => window.innerWidth);
    expect(vw).toBe(375);

    await page.screenshot({ path: 'test-results/screenshots/01-page-load.png' });
  });

  // ── Test 2: NIP07 login flow (mock window.nostr) ─────────────────────────
  test('NIP07 login flow (mock window.nostr)', async ({ page }) => {
    await injectNIP07(page);
    await page.goto(`${baseUrl}/participant/index.html`);

    const btn = page.locator('#btn-nip07');
    await expect(btn).toBeVisible();
    await expect(btn).not.toBeDisabled();
    await btn.click();

    await expect(page.locator('#login-card')).toHaveClass(/hidden/);
    await expect(page.locator('#status-card')).toBeVisible();
    await expect(page.locator('#user-npub')).not.toBeEmpty();

    // Demo mode (no CVM) should show success
    await expect(page.locator('#register-status')).toContainText(/Demo mode|Registered/i, { timeout: 5000 });

    await page.screenshot({ path: 'test-results/screenshots/02-nip07-login.png' });
  });

  // ── Test 3: npub manual entry fallback ──────────────────────────────────
  test('npub manual entry fallback', async ({ page }) => {
    await page.goto(`${baseUrl}/participant/index.html`);

    // No NIP07 — button should say "No Wallet Found"
    await expect(page.locator('#btn-nip07')).toContainText(/No Wallet/i);

    await page.locator('#npub-input').fill(TEST_PUBKEY);
    await page.locator('#btn-npub').click();

    await expect(page.locator('#login-card')).toHaveClass(/hidden/);
    await expect(page.locator('#status-card')).toBeVisible();
    await expect(page.locator('#user-npub')).not.toBeEmpty();

    await page.screenshot({ path: 'test-results/screenshots/03-npub-login.png' });
  });

  // ── Test 4: register_participant CVM call creates account ───────────────
  test('register_participant CVM call creates account', async ({ page }) => {
    await injectNIP07(page);
    await injectCVM(page, {
      register_participant: { ok: true, balance: 50000, message: 'Welcome to the Sovereign Engineering demo!' },
      get_snapshot: {
        ts: Date.now() / 1000,
        pricing: { ours: { your_price: 0.026, cost_basis: 0.02, margin_pct: 23 } },
        scarcity: { factor: 1.0, level: 'low' },
        ledger: [{ npub_short: '01234567…cdef', balance: 50000, prompts_sent: 0, tokens_spent: 0 }],
        participants: { count: 1, total_prompts: 0, total_tokens: 0 },
      },
    });

    await page.goto(`${baseUrl}/participant/index.html`);
    await page.locator('#btn-nip07').click();

    await expect(page.locator('#register-status')).toContainText(/Welcome|Registered/i, { timeout: 5000 });
    await expect(page.locator('#balance-big')).toContainText(/50\.?0?K/i, { timeout: 5000 });

    // All panels visible
    await expect(page.locator('#balance-card')).toBeVisible();
    await expect(page.locator('#prompt-card')).toBeVisible();
    await expect(page.locator('#spend-card')).toBeVisible();
    await expect(page.locator('#price-card')).toBeVisible();

    await page.screenshot({ path: 'test-results/screenshots/04-register.png' });
  });

  // ── Test 5: non-whitelisted npub rejected ──────────────────────────────
  test('non-whitelisted npub rejected', async ({ page }) => {
    await injectNIP07(page);
    await injectCVM(page, {
      register_participant: { ok: false, error: 'npub not whitelisted' },
      get_snapshot: { pricing: { ours: { your_price: 0.026 } }, scarcity: { factor: 1.0, level: 'low' }, ledger: [] },
    });

    await page.goto(`${baseUrl}/participant/index.html`);
    await page.locator('#btn-nip07').click();

    await expect(page.locator('#register-status')).toContainText(/not whitelisted/i, { timeout: 5000 });
    // Should still show panels in demo fallback
    await expect(page.locator('#balance-card')).toBeVisible({ timeout: 5000 });

    await page.screenshot({ path: 'test-results/screenshots/05-rejected.png' });
  });

  // ── Test 6: prompt input + send button visible and functional ──────────
  test('prompt input + send button visible and functional', async ({ page }) => {
    await injectNIP07(page);
    await page.goto(`${baseUrl}/participant/index.html`);
    await page.locator('#btn-nip07').click();

    await expect(page.locator('#prompt-input')).toBeVisible();
    await expect(page.locator('#btn-send')).toBeVisible();

    await page.locator('#prompt-input').fill('Hello, what is 2+2?');
    await expect(page.locator('#prompt-input')).toHaveValue('Hello, what is 2+2?');
    await expect(page.locator('#cost-value')).not.toHaveText('— tokens');

    await page.screenshot({ path: 'test-results/screenshots/06-prompt-input.png' });
  });

  // ── Test 7: send_prompt CVM call routes, shows response ─────────────────
  test('send_prompt CVM call routes and shows response', async ({ page }) => {
    await injectNIP07(page);
    await injectCVM(page, {
      register_participant: { ok: true, balance: 50000, message: 'Welcome!' },
      send_prompt: {
        ok: true, response: '2 + 2 = 4', provider: 'ours', model: 'glm-5.2',
        tokens_used: 150, cost_usd: 0.002, token_cost: 150, new_balance: 49850, scarcity_factor: 1.0,
      },
      get_snapshot: {
        pricing: { ours: { your_price: 0.026, cost_basis: 0.02, margin_pct: 23 } },
        scarcity: { factor: 1.0, level: 'low' },
        ledger: [{ npub_short: '01234567…cdef', balance: 49850, prompts_sent: 1, tokens_spent: 150 }],
        participants: { count: 1, total_prompts: 1, total_tokens: 150 },
      },
    });

    await page.goto(`${baseUrl}/participant/index.html`);
    await page.locator('#btn-nip07').click();
    await page.waitForSelector('#btn-send', { state: 'visible' });

    await page.locator('#prompt-input').fill('What is 2+2?');
    await page.locator('#btn-send').click();

    await expect(page.locator('#response-card')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('#resp-body')).toContainText('2 + 2 = 4', { timeout: 5000 });
    await expect(page.locator('#resp-prov')).toContainText('OURS');
    await expect(page.locator('#resp-model')).toContainText('glm-5.2');

    await page.screenshot({ path: 'test-results/screenshots/07-send-prompt.png' });
  });

  // ── Test 8: token balance updates after prompt ─────────────────────────
  test('token balance updates after prompt', async ({ page }) => {
    await injectNIP07(page);
    await injectCVM(page, {
      register_participant: { ok: true, balance: 50000, message: 'Welcome!' },
      send_prompt: {
        ok: true, response: 'The answer is here.', provider: 'ours', model: 'glm-5.2',
        tokens_used: 100, cost_usd: 0.001, token_cost: 100, new_balance: 49900, scarcity_factor: 1.0,
      },
      get_snapshot: {
        pricing: { ours: { your_price: 0.026 } },
        scarcity: { factor: 1.0, level: 'low' },
        ledger: [{ npub_short: '01234567…cdef', balance: 50000, prompts_sent: 0, tokens_spent: 0 }],
        participants: { count: 1, total_prompts: 0, total_tokens: 0 },
      },
    });

    await page.goto(`${baseUrl}/participant/index.html`);
    await page.locator('#btn-nip07').click();

    // Balance should show ~50K (may be 50.0K depending on formatting)
    await expect(page.locator('#balance-big')).toContainText(/50\.?0?K/i, { timeout: 5000 });
    // Spend should start at 0 prompts
    await expect(page.locator('#spend-prompts')).toContainText('0');

    await page.locator('#prompt-input').fill('Tell me something');
    await page.locator('#btn-send').click();

    // After sending: response should appear with new_balance
    await expect(page.locator('#response-card')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('#resp-balance')).toContainText(/49900|49\.9K/i, { timeout: 5000 });
    // Spend should show 1 prompt
    await expect(page.locator('#spend-prompts')).toContainText('1');

    await page.screenshot({ path: 'test-results/screenshots/08-balance-update.png' });
  });

  // ── Test 9: cost preview shows before sending ──────────────────────────
  test('cost preview shows before sending', async ({ page }) => {
    await injectNIP07(page);
    await page.goto(`${baseUrl}/participant/index.html`);
    await page.locator('#btn-nip07').click();
    await page.waitForSelector('#prompt-input', { state: 'visible' });

    // Initially should show —
    await expect(page.locator('#cost-value')).toContainText('—');

    // Type a prompt
    await page.locator('#prompt-input').fill('This is a test prompt that is long enough to estimate tokens properly.');

    const costText = await page.locator('#cost-value').textContent();
    expect(costText).toBeTruthy();
    expect(costText!).not.toContain('—');
    expect(costText!).toMatch(/\d+\s*tokens/i);

    await page.screenshot({ path: 'test-results/screenshots/09-cost-preview.png' });
  });

  // ── Test 10: insufficient balance rejection shows error ────────────────
  test('insufficient balance rejection shows error', async ({ page }) => {
    await injectNIP07(page);
    await injectCVM(page, {
      register_participant: { ok: true, balance: 10, message: 'Low balance test' },
      send_prompt: { ok: false, error: 'insufficient tokens' },
      get_snapshot: {
        pricing: { ours: { your_price: 0.026 } },
        scarcity: { factor: 1.0, level: 'low' },
        ledger: [{ npub_short: '01234567…cdef', balance: 10, prompts_sent: 0, tokens_spent: 0 }],
        participants: { count: 1, total_prompts: 0, total_tokens: 0 },
      },
    });

    await page.goto(`${baseUrl}/participant/index.html`);
    await page.locator('#btn-nip07').click();
    await page.waitForSelector('#prompt-input', { state: 'visible' });

    // Type a prompt that costs more than 10 tokens
    await page.locator('#prompt-input').fill('This prompt will cost more than 10 tokens because it has many characters.');
    await page.locator('#btn-send').click();

    await expect(page.locator('#prompt-error')).toBeVisible({ timeout: 5000 });
    const errText = await page.locator('#prompt-error').textContent();
    expect(errText!).toMatch(/insufficient|not enough|balance/i);

    await page.screenshot({ path: 'test-results/screenshots/10-insufficient.png' });
  });

  // ── Test 11: rate limit (2 prompts in 5s = blocked) ────────────────────
  test('rate limit (2 prompts in 5s = blocked)', async ({ page }) => {
    await injectNIP07(page);
    await page.goto(`${baseUrl}/participant/index.html`);
    await page.locator('#btn-nip07').click();
    await page.waitForSelector('#prompt-input', { state: 'visible' });

    // Send first prompt (demo mode — ~1s response time)
    await page.locator('#prompt-input').fill('First prompt here');
    await page.locator('#btn-send').click();

    // Wait for first prompt to complete
    await expect(page.locator('#response-card')).toBeVisible({ timeout: 5000 });

    // Immediately try second prompt
    await page.locator('#prompt-input').fill('Second prompt here');
    await page.locator('#btn-send').click();

    // Should show rate limit error
    await expect(page.locator('#prompt-error')).toBeVisible({ timeout: 3000 });
    const errText = await page.locator('#prompt-error').textContent();
    expect(errText!).toMatch(/rate limit|wait/i);

    await page.screenshot({ path: 'test-results/screenshots/11-rate-limit.png' });
  });

});