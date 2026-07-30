// verify-gates.mjs — Exercises all Task A3 quality gates.
//
//   node demo/tests/verify-gates.mjs
//
// Gates covered (PLAN-sovereign-demo.md §A3 Quality Gates):
//   G0  scarcityFactorForPct band boundaries (pure unit check)
//   G1  Whitelist: whitelisted → register; non-whitelisted → 403;
//       duplicate → 409; admin-added → can register
//   G2  Balance: deduction = est × price_per_token × scarcity;
//       balance/total_spent track correctly; insufficient → 402
//   G3  Scarcity: factor visibly ramps 1.0 → 1.2 → 1.5 → 1.8 → 2.0
//   G4  Rate limit: 1 prompt / 5s / npub (second within window → 429;
//       after window → ok)
//   G5  HTTP surface: /health /register /ledger /snapshot /admin/whitelist
//       /reset all behave over a real socket (cold-review friendly)
//
// Each gate gets a fresh temp dir (isolated DB + whitelist). Deterministic
// clocks are injected via charge({ now }) so the rate-limit gate is fast.
// Exit code is non-zero if any assertion fails.

import { createTokenLedger, scarcityFactorForPct } from '../src/token-ledger.mjs';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

let pass = 0;
let fail = 0;
function ok(name, cond, detail = '') {
  if (cond) {
    pass++;
    console.log(`  PASS  ${name}`);
  } else {
    fail++;
    console.log(`  FAIL  ${name}${detail ? ' :: ' + detail : ''}`);
  }
}

// Build an isolated ledger for one gate.
async function fresh(overrides = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'tledger-'));
  const whitelistPath = join(dir, 'wl.json');
  writeFileSync(
    whitelistPath,
    JSON.stringify(['npub_alice', 'npub_bob']),
  );
  return createTokenLedger({
    dbPath: join(dir, 't.db'),
    whitelistPath,
    adminPassword: 'pw',
    startingBalance: 1000,
    rateLimitMs: 5000,
    basePricePerToken: 1.0,
    ...overrides,
  });
}

console.log('Token-ledger gate verification\n');

// ── G0: scarcity bands (pure) ───────────────────────────────────────────────
console.log('[G0] scarcity band boundaries');
ok('pct 0   → 1.0', scarcityFactorForPct(0) === 1.0);
ok('pct 19  → 1.0', scarcityFactorForPct(19) === 1.0);
ok('pct 20  → 1.2', scarcityFactorForPct(20) === 1.2, `got ${scarcityFactorForPct(20)}`);
ok('pct 39  → 1.2', scarcityFactorForPct(39) === 1.2);
ok('pct 40  → 1.5', scarcityFactorForPct(40) === 1.5, `got ${scarcityFactorForPct(40)}`);
ok('pct 60  → 1.8', scarcityFactorForPct(60) === 1.8, `got ${scarcityFactorForPct(60)}`);
ok('pct 80  → 2.0', scarcityFactorForPct(80) === 2.0, `got ${scarcityFactorForPct(80)}`);
ok('pct 100 → 2.0', scarcityFactorForPct(100) === 2.0);

// ── G1: whitelist / access control ─────────────────────────────────────────
console.log('\n[G1] whitelist + access control');
{
  const l = await fresh();
  const alice = l.register('npub_alice');
  ok('whitelisted npub registers', !!alice);
  ok('starting balance = 1000', alice.balance === 1000, `got ${alice?.balance}`);

  let rej = null;
  try { l.register('npub_eve'); } catch (e) { rej = e; }
  ok('non-whitelisted npub rejected (403)', rej?.httpStatus === 403);

  let dup = null;
  try { l.register('npub_alice'); } catch (e) { dup = e; }
  ok('duplicate registration rejected (409)', dup?.httpStatus === 409);

  // admin add via the public method, then register works
  const added = l.addWhitelist('npub_eve');
  ok('admin.addWhitelist reports added=true', added.added === true);
  const eve = l.register('npub_eve');
  ok('admin-added npub can register', eve?.balance === 1000);

  // re-adding is idempotent
  const again = l.addWhitelist('npub_eve');
  ok('re-adding same npub is idempotent (added=false)', again.added === false);
  l.close();
}

// ── G2: balance correctness ────────────────────────────────────────────────
console.log('\n[G2] balance correctness');
{
  const l = await fresh({ startingBalance: 1000 });
  l.register('npub_alice');
  const c = l.charge({ npub: 'npub_alice', est_tokens: 100, price_per_token: 1.0, now: 1000 });
  ok('deduction = 100×1.0×1.0 = 100', c.deduction === 100, `got ${c.deduction}`);
  ok('balance_after = 900', c.balance_after === 900, `got ${c.balance_after}`);
  ok('total_spent = 100', c.total_spent === 100, `got ${c.total_spent}`);
  ok('prompt_count = 1', c.prompt_count === 1, `got ${c.prompt_count}`);

  // second charge at ppt 2.0
  const c2 = l.charge({ npub: 'npub_alice', est_tokens: 50, price_per_token: 2.0, now: 7000 });
  // spent before c2 = 100, pct = 10% → scarcity 1.0 → deduction 50*2*1.0 = 100
  ok('second deduction = 50×2.0×1.0 = 100', c2.deduction === 100, `got ${c2.deduction}`);
  ok('balance_after = 800', c2.balance_after === 800, `got ${c2.balance_after}`);
  ok('total_spent = 200', c2.total_spent === 200, `got ${c2.total_spent}`);

  // insufficient balance
  let poor = null;
  try {
    l.charge({ npub: 'npub_alice', est_tokens: 10_000, price_per_token: 1.0, now: 13000 });
  } catch (e) { poor = e; }
  ok('over-budget charge rejected (402)', poor?.httpStatus === 402);
  ok('402 reports deficit', !!poor?.detail?.deficit);

  // charging unregistered npub
  let ghost = null;
  try { l.charge({ npub: 'npub_nobody', est_tokens: 1, price_per_token: 1, now: 19000 }); }
  catch (e) { ghost = e; }
  ok('charge to unregistered npub → 404', ghost?.httpStatus === 404);

  // ledger sorted by spend
  l.register('npub_bob'); // bob: 1000, 0 spent
  const led = l.getLedger();
  ok('ledger sorted by spend desc (alice first)', led[0].npub === 'npub_alice' && led[1].npub === 'npub_bob');
  l.close();
}

// ── G3: scarcity ramp visibility ───────────────────────────────────────────
console.log('\n[G3] scarcity ramp visibility');
{
  // Small fixed budget so bands flip quickly; big starting balance so the
  // account never runs dry while we watch scarcity climb.
  const l = await fresh({ startingBalance: 1000, totalDemoBudget: 100 });
  l.register('npub_alice');
  const factors = [];
  let now = 1000;
  for (let i = 0; i < 8; i++) {
    const c = l.charge({ npub: 'npub_alice', est_tokens: 10, price_per_token: 1.0, now });
    factors.push(c.scarcity_factor);
    now += 6000; // > rateLimitMs
  }
  ok('scarcity starts at 1.0', factors[0] === 1.0, `got ${factors[0]}`);
  ok('scarcity reaches 1.2', factors.includes(1.2), `seen: ${factors.join(',')}`);
  ok('scarcity reaches 1.5', factors.includes(1.5), `seen: ${factors.join(',')}`);
  ok('scarcity reaches 1.8', factors.includes(1.8), `seen: ${factors.join(',')}`);
  ok('scarcity reaches 2.0', factors.includes(2.0), `seen: ${factors.join(',')}`);
  // monotonic non-decreasing
  const mono = factors.every((f, i) => i === 0 || f >= factors[i - 1]);
  ok('scarcity is non-decreasing as budget drains', mono, `seen: ${factors.join(',')}`);
  const snap = l.snapshot();
  ok('snapshot exposes scarcity_factor + band', snap.scarcity_factor >= 1.0 && typeof snap.scarcity_band === 'string');
  ok('snapshot exposes budget_used_pct', typeof snap.budget_used_pct === 'number');
  l.close();
}

// ── G4: rate limit ─────────────────────────────────────────────────────────
console.log('\n[G4] rate limit (1 prompt / 5s / npub)');
{
  const l = await fresh({ rateLimitMs: 5000 });
  l.register('npub_alice');
  const c1 = l.charge({ npub: 'npub_alice', est_tokens: 10, price_per_token: 1.0, now: 1000 });
  ok('first charge succeeds', !!c1.ok);

  let rl = null;
  try { l.charge({ npub: 'npub_alice', est_tokens: 10, price_per_token: 1.0, now: 1500 }); }
  catch (e) { rl = e; }
  ok('second charge within 5s → 429', rl?.httpStatus === 429);
  ok('429 reports retry_after_s', !!rl?.detail?.retry_after_s);

  // different npub is NOT rate-limited by alice
  l.register('npub_bob');
  const bob = l.charge({ npub: 'npub_bob', est_tokens: 10, price_per_token: 1.0, now: 1500 });
  ok('rate limit is per-npub (bob not blocked by alice)', !!bob.ok);

  // after the window, alice can charge again
  const c3 = l.charge({ npub: 'npub_alice', est_tokens: 10, price_per_token: 1.0, now: 6500 });
  ok('charge after 5s window succeeds', !!c3.ok);

  // topUp: bypasses rate limit + does not increment prompt_count (same prompt)
  const aliceBefore = l.getLedger().find((p) => p.npub === 'npub_alice');
  const tu = l.charge({ npub: 'npub_alice', est_tokens: 5, price_per_token: 1.0, now: 6700, topUp: true });
  ok('topUp succeeds inside the rate window', !!tu.ok && tu.topUp === true);
  ok('topUp does NOT increment prompt_count', tu.prompt_count === aliceBefore.prompt_count, `before=${aliceBefore.prompt_count} after=${tu.prompt_count}`);
  ok('topUp still deducts tokens', tu.deduction === 5 && tu.balance_after < aliceBefore.balance, `deduction=${tu.deduction}`);

  // reset clears rate-limit window
  l.reset();
  l.register('npub_alice');
  const c4 = l.charge({ npub: 'npub_alice', est_tokens: 10, price_per_token: 1.0, now: 6600 });
  ok('rate-limit window cleared after reset', !!c4.ok);
  l.close();
}

// ── G5: HTTP surface (real socket) ─────────────────────────────────────────
console.log('\n[G5] HTTP surface over a real socket');
{
  const l = await fresh({ startingBalance: 5000 });
  const server = l.listen({ port: 0, host: '127.0.0.1' });
  const port = await new Promise((res) => server.on('listening', () => res(server.address().port)));
  const base = `http://127.0.0.1:${port}`;
  const j = (r) => r.json();

  const health = await j(await fetch(`${base}/health`));
  ok('GET /health → ok', health.ok === true, JSON.stringify(health));

  const snap = await j(await fetch(`${base}/snapshot`));
  ok('GET /snapshot → scarcity_factor present', typeof snap.scarcity_factor === 'number', JSON.stringify(snap));

  // register whitelisted
  let r = await fetch(`${base}/register`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ npub: 'npub_alice' }),
  });
  let body = await j(r);
  ok('POST /register whitelisted → 200 + balance 5000', r.status === 200 && body.participant?.balance === 5000);

  // register non-whitelisted
  r = await fetch(`${base}/register`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ npub: 'npub_eve' }),
  });
  ok('POST /register non-whitelisted → 403', r.status === 403);

  // admin whitelist wrong password
  r = await fetch(`${base}/admin/whitelist`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ password: 'wrong', npub: 'npub_eve' }),
  });
  ok('POST /admin/whitelist wrong password → 401', r.status === 401);

  // admin whitelist correct
  r = await fetch(`${base}/admin/whitelist`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ password: 'pw', npub: 'npub_eve' }),
  });
  body = await j(r);
  ok('POST /admin/whitelist correct → 200 + added', r.status === 200 && body.added === true);

  // prompt (standalone est from text)
  r = await fetch(`${base}/prompt`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ npub: 'npub_alice', prompt: 'hello world this is a demo prompt' }),
  });
  body = await j(r);
  ok('POST /prompt deducts tokens', r.status === 200 && body.balance_after < 5000, `balance_after=${body.balance_after}`);

  // ledger
  r = await fetch(`${base}/ledger`);
  body = await j(r);
  ok('GET /ledger lists participants', r.status === 200 && body.participants?.length === 1, `len=${body.participants?.length}`);

  // reset
  r = await fetch(`${base}/reset`, { method: 'POST' });
  body = await j(r);
  ok('POST /reset → cleared', r.status === 200 && body.cleared === true);
  const after = await j(await fetch(`${base}/ledger`));
  ok('reset emptied participants', after.participants?.length === 0, `len=${after.participants?.length}`);

  server.close();
  l.close();
}

// ── summary ────────────────────────────────────────────────────────────────
console.log(`\n${'='.repeat(50)}`);
console.log(`RESULT: ${pass} passed, ${fail} failed`);
if (fail > 0) {
  console.log('SOME GATES FAILED');
  process.exit(1);
} else {
  console.log('ALL GATES PASSED ✓');
}
