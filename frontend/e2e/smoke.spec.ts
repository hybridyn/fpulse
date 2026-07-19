import { test, expect } from './fixtures';

/**
 * Authenticated page-render smoke — the "missing confidence layer".
 *
 * For each primary surface we inject a real session, navigate to the hash
 * route, and assert:
 *   1. We are authenticated (the login password field is NOT shown).
 *   2. The page mounted real content (#root is non-empty + visible).
 *   3. No uncaught JS exception fired (pageerror) — a real crash signal.
 *
 * Uses only local state (no external DB) so it's deterministic in CI.
 * Console errors are recorded as annotations for visibility but don't fail
 * the test (they're frequently benign dev-server / expected-4xx noise);
 * an uncaught exception is the hard failure signal.
 */

const PAGES: { route: string; name: string }[] = [
  { route: 'dashboard',   name: 'Dashboard' },
  { route: 'storage',     name: 'Storage' },
  { route: 'connections', name: 'Connections' },
  { route: 'editor',      name: 'Pipeline editor' },
  { route: 'pool',        name: 'Execution pool' },
  { route: 'executions',  name: 'Monitor / runs' },
  { route: 'insights',    name: 'AI / Steward (Insights)' },
  { route: 'settings',    name: 'Settings' },
  { route: 'help',        name: 'Help' },
];

const BENIGN = /favicon|ResizeObserver|React DevTools|\bws:\/\/|websocket|401|403|404/i;

test.describe('Authenticated page smoke', () => {
  for (const p of PAGES) {
    test(`renders ${p.name} (#${p.route})`, async ({ authedPage: page }) => {
      const crashes: string[] = [];
      const consoleErrors: string[] = [];
      page.on('pageerror', (e) => crashes.push(String(e)));
      page.on('console', (m) => {
        if (m.type() === 'error' && !BENIGN.test(m.text())) consoleErrors.push(m.text());
      });

      await page.goto(`/#${p.route}`, { waitUntil: 'domcontentloaded' });

      // Authenticated -> the login password field must be absent.
      await expect(page.getByLabel(/password/i)).toHaveCount(0);

      // The page mounted real content.
      const root = page.locator('#root');
      await expect(root).toBeVisible();
      await expect(root).not.toBeEmpty();

      // Give late effects/fetches a beat to surface any uncaught error.
      await page.waitForTimeout(800);

      if (consoleErrors.length) {
        test.info().annotations.push({
          type: 'console-errors',
          description: `#${p.route}:\n${consoleErrors.join('\n')}`,
        });
      }
      expect(crashes, `uncaught errors on #${p.route}:\n${crashes.join('\n')}`).toEqual([]);
    });
  }
});
