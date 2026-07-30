import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  outputDir: './test-results',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [['list'], ['html', { outputFolder: 'test-report' }]],
  timeout: 60_000,
  use: {
    headless: true,
    video: 'on',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  projects: [
    {
      // Participant nsite — phone-first, 375px mobile viewport
      name: 'mobile-chrome',
      testMatch: /participant-.*\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        channel: 'chromium',
        viewport: { width: 375, height: 667 },
        isMobile: true,
        hasTouch: true,
        userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
      },
    },
    {
      // Display nsite — desktop dashboard, 1440×900
      name: 'desktop-chrome',
      testMatch: /display-.*\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        channel: 'chromium',
        viewport: { width: 1440, height: 900 },
        isMobile: false,
        hasTouch: false,
      },
    },
    {
      // Integration — display ↔ participant via shared CVM (A4).
      // Desktop viewport; the participant page is opened as a mobile page inside the test.
      name: 'integration',
      testMatch: /integration.*\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        channel: 'chromium',
        viewport: { width: 1440, height: 900 },
        isMobile: false,
        hasTouch: false,
      },
    },
    {
      // CVM server — integration tests over Nostr relays (A1b).
      // Desktop viewport; the test manages its own video context.
      name: 'cvm-server',
      testMatch: /cvm-server.*\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        channel: 'chromium',
        viewport: { width: 900, height: 700 },
        isMobile: false,
        hasTouch: false,
      },
    },
  ],
});
