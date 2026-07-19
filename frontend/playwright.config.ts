import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 12_000 },
  fullyParallel: false,   // tests share DEV/PROD state
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  // Logs in once via the UI and caches the session in e2e/.auth/state.json
  // so every spec starts authenticated. Set FPULSE_E2E_PASSWORD (+ optionally
  // FPULSE_E2E_EMAIL) before running. See e2e/README.md.
  globalSetup: './e2e/global-setup.ts',
  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report', open: 'never' }],
    ['junit', { outputFile: 'playwright-report/junit.xml' }],
  ],
  use: {
    baseURL: process.env.FPULSE_URL ?? 'http://localhost:5174',
    storageState: 'e2e/.auth/state.json',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    viewport: { width: 1440, height: 900 },
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: process.env.NO_SERVER ? undefined : {
    command: 'npm run dev',
    url: 'http://localhost:5174',
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
