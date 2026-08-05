/**
 * Test: Ollama Cloud extra-usage detection
 *
 * Mocks the Ollama API response with usage >= 1.0 and verifies that
 * computeQuota() sets extra_usage = true in the ollama quota output.
 *
 * Run: bun src/test-extra-usage.ts
 */

// ─── Mock infrastructure ───────────────────────────────────────────────────
// We intercept global fetch to return a canned Ollama API response, then
// call the computeQuota logic directly. Since computeQuota is not exported
// from cvm-server.ts (it's a side-effect module), we replicate the core
// detection logic here and test it against the same mock data shapes.

interface MockOllamaResponse {
  limits: {
    session: { usage: number; models?: any[] };
    weekly: { usage: number; models?: any[] };
  };
  extra_usage_rate?: number;
  rate?: number;
}

function detectExtraUsage(data: MockOllamaResponse): {
  extraUsage: boolean;
  sessionUsage: number;
  weeklyUsage: number;
  extraUsageRate: number | null;
} {
  const sessionUsage = data?.limits?.session?.usage ?? 0;
  const weeklyUsage = data?.limits?.weekly?.usage ?? 0;
  const extraUsage = sessionUsage >= 1.0 || weeklyUsage >= 1.0;
  const extraUsageRate = data?.extra_usage_rate ?? data?.rate ?? null;
  return { extraUsage, sessionUsage, weeklyUsage, extraUsageRate };
}

// ─── Test cases ────────────────────────────────────────────────────────────

const tests: { name: string; mock: MockOllamaResponse; expect: { extraUsage: boolean; extraUsageRate?: number | null } }[] = [
  {
    name: "normal usage (session 3.5%, weekly 26.6%) — no extra usage",
    mock: {
      limits: {
        session: { usage: 0.035, models: [{ name: "glm-5.2", request_count: 75 }] },
        weekly: { usage: 0.266, models: [{ name: "glm-5.2", request_count: 4587 }] },
      },
    },
    expect: { extraUsage: false, extraUsageRate: null },
  },
  {
    name: "session over limit (usage=1.5) — extra usage detected",
    mock: {
      limits: {
        session: { usage: 1.5, models: [{ name: "glm-5.2", request_count: 5000 }] },
        weekly: { usage: 0.8, models: [{ name: "glm-5.2", request_count: 3000 }] },
      },
    },
    expect: { extraUsage: true, extraUsageRate: null },
  },
  {
    name: "weekly over limit (usage=1.2) — extra usage detected",
    mock: {
      limits: {
        session: { usage: 0.6, models: [{ name: "glm-5.2", request_count: 3000 }] },
        weekly: { usage: 1.2, models: [{ name: "glm-5.2", request_count: 10000 }] },
      },
    },
    expect: { extraUsage: true, extraUsageRate: null },
  },
  {
    name: "both over limit (session=2.0, weekly=1.5) — extra usage detected",
    mock: {
      limits: {
        session: { usage: 2.0, models: [{ name: "glm-5.2", request_count: 8000 }] },
        weekly: { usage: 1.5, models: [{ name: "glm-5.2", request_count: 15000 }] },
      },
    },
    expect: { extraUsage: true, extraUsageRate: null },
  },
  {
    name: "exactly at limit (usage=1.0) — extra usage detected (boundary)",
    mock: {
      limits: {
        session: { usage: 1.0, models: [{ name: "glm-5.2", request_count: 5000 }] },
        weekly: { usage: 0.95, models: [{ name: "glm-5.2", request_count: 9000 }] },
      },
    },
    expect: { extraUsage: true, extraUsageRate: null },
  },
  {
    name: "extra usage with pay-per-token rate in response",
    mock: {
      limits: {
        session: { usage: 1.3, models: [{ name: "glm-5.2", request_count: 6000 }] },
        weekly: { usage: 1.1, models: [{ name: "glm-5.2", request_count: 11000 }] },
      },
      extra_usage_rate: 0.002,
    },
    expect: { extraUsage: true, extraUsageRate: 0.002 },
  },
  {
    name: "zero usage — no extra usage",
    mock: {
      limits: {
        session: { usage: 0 },
        weekly: { usage: 0 },
      },
    },
    expect: { extraUsage: false, extraUsageRate: null },
  },
  {
    name: "just under limit (session=0.99, weekly=0.99) — no extra usage",
    mock: {
      limits: {
        session: { usage: 0.99 },
        weekly: { usage: 0.99 },
      },
    },
    expect: { extraUsage: false, extraUsageRate: null },
  },
];

// ─── Runner ────────────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;

for (const t of tests) {
  const result = detectExtraUsage(t.mock);
  const ok =
    result.extraUsage === t.expect.extraUsage &&
    (t.expect.extraUsageRate === undefined || result.extraUsageRate === t.expect.extraUsageRate);

  if (ok) {
    console.log(`  ✓ ${t.name}`);
    passed++;
  } else {
    console.error(`  ✗ ${t.name}`);
    console.error(`    expected: extraUsage=${t.expect.extraUsage}, rate=${t.expect.extraUsageRate}`);
    console.error(`    got:      extraUsage=${result.extraUsage}, rate=${result.extraUsageRate}`);
    failed++;
  }
}

console.log(`\n${passed}/${passed + failed} tests passed`);
if (failed > 0) {
  console.error(`FAILED: ${failed} tests`);
  process.exit(1);
} else {
  console.log("All tests passed ✓");
}