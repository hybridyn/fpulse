import { test as base, expect, Page, APIRequestContext } from '@playwright/test';

/**
 * Programmatic auth for E2E.
 *
 * The previous fixture drove the login *form* with `password: 'admin'` —
 * which is not the real bootstrap password — and the app is hash-routed, so
 * the old `page.goto('/connections')` path navigations never reached the
 * intended page. This version logs in via the real API
 * (`POST /api/auth/login` -> `{token,user}`) and injects the session into
 * localStorage before the SPA boots, exactly the way App.tsx restores a
 * session on mount (reads `fpulse_token` + `fpulse_user`, then validates via
 * `/api/auth/me`). Robust against login markup changes and far faster than
 * typing into a form per test.
 *
 * Credentials come from the environment so no secret is committed:
 *   FPULSE_E2E_EMAIL     (default: admin@fpulse.local)
 *   FPULSE_E2E_PASSWORD  (the bootstrap admin password)
 */

const EMAIL = process.env.FPULSE_E2E_EMAIL || 'admin@fpulse.local';
const PASSWORD = process.env.FPULSE_E2E_PASSWORD || '';

export type Auth = { token: string; user: unknown };

export async function apiLogin(request: APIRequestContext): Promise<Auth> {
  const res = await request.post('/api/auth/login', {
    data: { email: EMAIL, password: PASSWORD },
  });
  if (!res.ok()) {
    throw new Error(
      `E2E login failed (${res.status()}). Set FPULSE_E2E_PASSWORD to the ` +
      `bootstrap admin password (see data/.../INITIAL_ADMIN_PASSWORD.txt). ` +
      `Body: ${await res.text()}`,
    );
  }
  const body = await res.json();
  return { token: body.token, user: body.user };
}

/** Inject a valid session into localStorage before the SPA boots. */
export async function injectAuth(page: Page, auth: Auth): Promise<void> {
  await page.addInitScript((a) => {
    const typed = a as { token: string; user: unknown };
    localStorage.setItem('fpulse_token', typed.token);
    localStorage.setItem('fpulse_user', JSON.stringify(typed.user));
    localStorage.setItem('fpulse_env', 'dev');
  }, auth);
}

export const test = base.extend<{ auth: Auth; authedPage: Page }>({
  auth: async ({ request }, use) => {
    const a = await apiLogin(request);
    await use(a);
  },
  authedPage: async ({ page, auth }, use) => {
    await injectAuth(page, auth);
    await use(page);
  },
});

export { expect };
