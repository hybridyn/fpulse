# Editor E2E (Playwright)

Functional + smoke tests that open every visible node in the editor, render its
config, and (functionally) run nodes through the real backend/DuckDB engine.

This is the durable answer to "check each node's frontend / each functionality
with a sample connection" — it drives the **real** input pipeline, so React's
`onChange` fires (which programmatic `page.evaluate` value-setting can't do).

## What it covers
- **`node-configs.spec.ts`** — opens the config of every visible palette node
  (~40), asserts the config body mounts, a Parameters tab exists, and there's no
  React error boundary. Catches missing dispatches, crashing configs, and
  registry/palette drift.
- **`functional.spec.ts`** — runs the full FE → backend → engine path:
  - DB Source `SELECT 1` against the in-memory engine (no connection) → expects a row.
  - Source via a **sample DB connection** (skips if none exists) → Test Node → expects rows.

## Prerequisites
- A running F-Pulse app (frontend + backend). The config defaults to
  `http://localhost:5174` and **reuses** an already-running dev server.
- Chromium for Playwright: `npm run test:e2e:install` (first time only).

## Run it
```powershell
# point at your app + provide login creds (nothing is committed)
$env:FPULSE_URL = "http://localhost:5174"
$env:FPULSE_E2E_EMAIL = "admin@fpulse.local"
$env:FPULSE_E2E_PASSWORD = "<your password>"

npm run test:e2e            # headless
npm run test:e2e:ui         # interactive UI mode
npx playwright show-report  # last HTML report
```

`global-setup.ts` logs in once and caches the session in `e2e/.auth/state.json`
(gitignored); specs reuse it. Set `NO_SERVER=1` to skip the auto dev-server.

## Extending
Add a node to the `NODES` list in `node-configs.spec.ts` to include it in the
smoke net. Add deterministic runs (fill required fields → **Test Node** →
assert output) to `functional.spec.ts` for nodes whose execution doesn't need
external state. Helpers (`addNode`, `openConfig`, …) are in `helpers.ts`.
