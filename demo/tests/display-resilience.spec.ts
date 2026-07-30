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
