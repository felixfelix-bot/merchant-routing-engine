// verify-a1-gates.mjs — Exercises the Task A1 quality gates against a running
// dashboard-server (default http://localhost:3001; override with BASE_URL).
//
//   PORT=3001 node demo/src/dashboard-server.mjs &   # start the server
//   node demo/tests/verify-a1-gates.mjs              # run the gates
//
// Gates (PLAN-sovereign-demo.md §A1):
//   G1  Server responds on /health
//   G2  GET /api/snapshot → valid JSON, has contract keys, server _ms < 50
//   G3  Contract shapes: pricing(4 keys), quota, requests, distribution, gate
//   G4  POST /register (whitelisted) → 200 + balance; /ledger lists it
//   G5  POST /prompt → routes through proxy, returns provider+model+cost+tokens
//   G6  WS /stream handshake + a live 'request' push within 6s (<3s typical)
// Zero dependencies — uses Node 22 built-ins incl. a hand-rolled WS client.

import net from 'node:net';
import { randomBytes } from 'node:crypto';

const BASE = process.env.BASE_URL || 'http://localhost:3001';
const PORT = Number(new URL(BASE).port) || 3001;
const ALICE = 'npub1demo0001alice0000000000000000000000000000000000000000000000alice';

let pass = 0, fail = 0;
function ok(name, cond, detail = '') {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' :: ' + detail : ''}`); }
}
const j = (r) => r.json();
const post = (p, body) => fetch(`${BASE}${p}`, {
  method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body || {}),
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── minimal WebSocket client (server→client unmasked text frames only) ─────
function wsCollect(port, path, waitMs) {
  return new Promise((resolve, reject) => {
    const key = randomBytes(16).toString('base64');
    const sock = net.connect(port, '127.0.0.1');
    let buf = Buffer.alloc(0);
    let upgraded = false;
    const events = []; // { t: performance.now(), msg }
    const timer = setTimeout(() => { try { sock.end(); } catch {} resolve({ upgraded, events }); }, waitMs);
    sock.on('connect', () => {
      sock.write(
        `GET ${path} HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n` +
        `Connection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`,
      );
    });
    sock.on('data', (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      if (!upgraded) {
        const idx = buf.indexOf('\r\n\r\n');
        if (idx === -1) return;
        const head = buf.slice(0, idx).toString();
        buf = buf.slice(idx + 4);
        if (!/^HTTP\/1\.1 101/.test(head)) { clearTimeout(timer); sock.destroy(); return reject(new Error('no 101: ' + head.split('\r\n')[0])); }
        upgraded = true;
      }
      let parsed;
      [parsed, buf] = parseFrames(buf);
      const t = performance.now();
      for (const msg of parsed) events.push({ t, msg });
    });
    sock.on('error', (e) => { clearTimeout(timer); reject(e); });
  });
}
function parseFrames(buf) {
  const out = [];
  let i = 0;
  while (i < buf.length) {
    if (buf.length - i < 2) break;
    const b0 = buf[i], b1 = buf[i + 1];
    const opcode = b0 & 0x0f;
    const masked = (b1 & 0x80) !== 0;
    let len = b1 & 0x7f;
    let off = i + 2;
    if (len === 126) { if (buf.length - off < 2) break; len = buf.readUInt16BE(off); off += 2; }
    else if (len === 127) { if (buf.length - off < 8) break; len = Number(buf.readBigUInt64BE(off)); off += 8; }
    let mask = null;
    if (masked) { if (buf.length - off < 4) break; mask = buf.slice(off, off + 4); off += 4; }
    if (buf.length - off < len) break;
    let data = buf.slice(off, off + len);
    if (mask) for (let k = 0; k < data.length; k++) data[k] ^= mask[k % 4];
    if (opcode === 1 || opcode === 0) out.push(data.toString('utf8'));
    i = off + len;
  }
  return [out, buf.slice(i)];
}

console.log('A1 dashboard-server gate verification\n');

// ── G1: health ─────────────────────────────────────────────────────────────
console.log('[G1] health');
{
  const r = await fetch(`${BASE}/health`);
  const h = await j(r);
  ok('GET /health → 200 ok', r.status === 200 && h.ok === true, `status=${r.status}`);
  ok('health reports proxy_active', typeof h.proxy_active === 'string' || h.proxy_active === null);
}

// ── G2 + G3: snapshot validity, timing, contract shape ─────────────────────
console.log('\n[G2/G3] snapshot validity + timing + contract shape');
let snap;
{
  const t0 = performance.now();
  const r = await fetch(`${BASE}/api/snapshot`);
  const dt = performance.now() - t0;
  snap = await j(r);
  ok('GET /api/snapshot → 200', r.status === 200, `status=${r.status}`);
  ok('snapshot is valid JSON object', snap && typeof snap === 'object');
  ok('snapshot server _ms < 50', typeof snap._ms === 'number' && snap._ms < 50, `_ms=${snap._ms}`);
  ok('snapshot round-trip < 250ms (localhost)', dt < 250, `dt=${dt.toFixed(1)}ms`);
  // contract keys
  for (const k of ['ts', 'pricing', 'quota', 'requests', 'provider_distribution', 'dispatch_gate', 'cost_today', 'system']) {
    ok(`snapshot has .${k}`, k in snap, `missing ${k}`);
  }
  // pricing: four keys each with 4 fields
  for (const key of ['ours', 'friend', 'ollama', 'ppq']) {
    const p = snap.pricing?.[key];
    ok(`pricing.${key} has cost_basis/your_price/margin_pct/effective_rate`,
      p && ['cost_basis', 'your_price', 'margin_pct', 'effective_rate'].every(f => typeof p[f] === 'number'),
      `pricing.${key}=${JSON.stringify(p)}`);
  }
  ok('requests is an array (<=20)', Array.isArray(snap.requests) && snap.requests.length <= 20, `len=${snap.requests?.length}`);
  if (snap.requests.length) {
    const rr = snap.requests[0];
    ok('request item has provider/model/tokens/cost/reason',
      'provider' in rr && 'model' in rr && 'tokens' in rr && 'cost' in rr && 'reason' in rr,
      JSON.stringify(rr));
  }
  ok('dispatch_gate has can_dispatch + scarcity_factor',
    typeof snap.dispatch_gate?.can_dispatch === 'boolean' && typeof snap.dispatch_gate?.scarcity_factor === 'number',
    JSON.stringify(snap.dispatch_gate));
  ok('cost_today is a number', typeof snap.cost_today === 'number', `${snap.cost_today}`);
  ok('system has cpu_pct + mem_pct',
    typeof snap.system?.cpu_pct === 'number' && typeof snap.system?.mem_pct === 'number', JSON.stringify(snap.system));
}

// ── G4: register + ledger ──────────────────────────────────────────────────
console.log('\n[G4] register + ledger');
{
  await post('/reset', {}); // clean slate
  const r = await post('/register', { npub: ALICE });
  const body = await j(r);
  ok('POST /register whitelisted → 200', r.status === 200, `status=${r.status} ${JSON.stringify(body)}`);
  ok('register returns balance = 50000', body.balance === 50000, `balance=${body.balance}`);

  const rej = await j(await post('/register', { npub: 'npub1notonwhitelist'.padEnd(63, '0') }));
  // (non-whitelisted → 403; we just check it didn't grant a balance)
  ok('non-whitelisted register does not return ok:true', rej.ok !== true, JSON.stringify(rej));

  const led = await j(await fetch(`${BASE}/ledger`));
  ok('GET /ledger lists participant', Array.isArray(led.participants) && led.participants.some(p => p.npub === ALICE), JSON.stringify(led.participants));
  ok('ledger exposes scarcity_factor + total_budget', typeof led.scarcity_factor === 'number' && typeof led.total_budget === 'number');
}

// ── G5: prompt routes through proxy ────────────────────────────────────────
console.log('\n[G5] POST /prompt routes through proxy');
{
  const r = await post('/prompt', { prompt: 'Reply with exactly one word: ping', requester_npub: ALICE });
  const body = await j(r);
  ok('POST /prompt → 200', r.status === 200, `status=${r.status} ${JSON.stringify(body)}`);
  ok('prompt returns a provider', typeof body.provider === 'string' && body.provider.length > 0, `provider=${body.provider}`);
  ok('prompt returns a model', typeof body.model === 'string' && body.model.length > 0, `model=${body.model}`);
  ok('prompt returns tokens > 0', typeof body.tokens === 'number' && body.tokens > 0, `tokens=${body.tokens}`);
  ok('prompt returns cost (number)', typeof body.cost === 'number', `cost=${body.cost}`);
  ok('prompt returns price_per_m (number)', typeof body.price_per_m === 'number', `price_per_m=${body.price_per_m}`);
  ok('prompt deducts → balance_after < 50000', typeof body.balance_after === 'number' && body.balance_after < 50000, `balance_after=${body.balance_after}`);
  console.log(`        routed: provider=${body.provider} model=${body.model} tokens=${body.tokens} cost=$${body.cost} price/M=$${body.price_per_m} balance=${body.balance_after}`);
}

// ── G6: WebSocket /stream ──────────────────────────────────────────────────
console.log('\n[G6] WS /stream live push');
{
  // Open the socket, then generate traffic (a real prompt). We measure from the
  // moment the prompt HTTP call completes (== row inserted) to the first WS
  // push that follows — that is exactly the "within 3s of DB insert" gate.
  const collect = wsCollect(PORT, '/stream', 9000);
  await sleep(400); // let the handshake settle
  const postT = performance.now(); // decision row will be inserted during this call
  await post('/prompt', { prompt: 'Reply with exactly: pong', requester_npub: ALICE });
  // The WS poller catches the new key_decisions row within its 2s cycle; the
  // push may arrive during the HTTP round-trip (before the await resolves).
  const { upgraded, events } = await collect;
  ok('WS /stream handshake upgrades (101)', upgraded === true, 'no upgrade');
  const requests = events
    .map((e) => { try { return { t: e.t, m: JSON.parse(e.msg) }; } catch { return null; } })
    .filter((x) => x && x.m && x.m.type === 'request');
  const afterInsert = requests.find((x) => x.t >= postT - 200);
  ok('WS received ≥1 request push after the prompt', !!afterInsert, `total pushes=${requests.length}`);
  if (afterInsert) {
    const latency = afterInsert.t - postT;
    ok('WS push within 3s of DB insert', latency < 3000, `post→push=${latency.toFixed(0)}ms`);
    const d = afterInsert.m.data;
    ok('WS push data has provider/model/tokens',
      d && 'provider' in d && 'model' in d && 'tokens' in d, JSON.stringify(d));
  }
}

// ── summary ────────────────────────────────────────────────────────────────
console.log(`\n${'='.repeat(50)}`);
console.log(`RESULT: ${pass} passed, ${fail} failed`);
if (fail > 0) { console.log('SOME GATES FAILED'); process.exit(1); }
else { console.log('ALL A1 GATES PASSED ✓'); }
