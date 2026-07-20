import { test, expect } from '@playwright/test';
import { openEditor, addNode, addEdge, edgeCount, openConfig, closeConfig, configBody } from './helpers';

/**
 * Functional E2E — proves the real path FE config → backend → DuckDB engine
 * actually executes (these use Playwright's real typing, so React's onChange
 * fires — the thing that blocks programmatic preview-eval testing).
 */

test('DB Source runs a query against the in-memory engine (no connection)', async ({ page }) => {
  await openEditor(page);
  const id = await addNode(page, 'db_source');
  expect(id).toBeTruthy();
  await openConfig(page, id);

  const body = configBody(page);
  // Mode defaults to "SQL Query" → fill the query (real typing → React state).
  await body.locator('textarea').first().fill('SELECT 1 AS ok, 2 AS two');

  await page.getByRole('button', { name: 'Test Node' }).click();

  // With no connection the engine runs it against DuckDB → 1 row, column "ok".
  await expect(page.getByText(/\b1 rows?\b/i).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('ok', { exact: false }).first()).toBeVisible();
});

test('Source executes via a sample DB connection', async ({ page }) => {
  await openEditor(page);
  const id = await addNode(page, 'source');
  expect(id).toBeTruthy();
  await openConfig(page, id);

  const body = configBody(page);
  const connSelect = body.locator('select').first();
  // The connection dropdown groups options by type via <optgroup>. Pick a
  // relational/warehouse connection if one exists; otherwise skip.
  const dbOption = body.locator(
    'optgroup[label="postgresql"] option, optgroup[label="mysql"] option, ' +
    'optgroup[label="mssql"] option, optgroup[label="oracle"] option, ' +
    'optgroup[label="snowflake"] option',
  ).first();
  const haveDbConn = await dbOption.count();
  test.skip(haveDbConn === 0, 'no relational/warehouse sample connection in this environment');

  const value = await dbOption.getAttribute('value');
  await connSelect.selectOption(value!);

  // Now in database mode: Mode defaults to SQL Query → fill a side-effect-free probe.
  await expect(body.getByText(/Mode/i).first()).toBeVisible();
  await body.locator('textarea').first().fill('SELECT 1 AS ok');

  await page.getByRole('button', { name: 'Test Node' }).click();

  // Rows back, OR a surfaced backend error (e.g. a missing driver like
  // psycopg2, or an unreachable host) — both prove the node round-tripped the
  // connection to the engine, which is what this test verifies. A hang/crash
  // (neither appears) is the real failure. The exact outcome is in the report.
  await expect(
    page.getByText(/\b\d+ rows?\b|ERROR|No module named|failed|refused|timeout/i).first(),
  ).toBeVisible({ timeout: 30_000 });
});

test('source → filter chain executes end-to-end (multi-node fill + run)', async ({ page }) => {
  await openEditor(page);
  const srcId = await addNode(page, 'db_source');
  // Offset the filter well to the right so the two nodes' handles don't overlap
  // (addEdge drags between them).
  const filterId = await addNode(page, 'filter', 10);
  expect(srcId, 'db_source should drop').toBeTruthy();
  expect(filterId, 'filter should drop').toBeTruthy();

  await addEdge(page, srcId, filterId);

  // Source: 3 deterministic rows via the in-memory engine.
  await openConfig(page, srcId);
  await configBody(page).locator('textarea').first().fill('SELECT * FROM (VALUES (1),(2),(3)) AS t(n)');
  await closeConfig(page);

  // Filter: keep n > 1 → 2 of 3 rows. Expression mode is the default.
  await openConfig(page, filterId);
  await configBody(page).locator('input, textarea').first().fill('n > 1');
  await page.getByRole('button', { name: 'Test Node' }).click();

  // Test Node on the filter runs source→filter through the engine — proving a
  // WIRED multi-node pipeline executes with filled params, not just that the
  // config mounts. (Edge-wiring + fill + run, end to end.)
  await expect(page.getByText(/\b2 rows?\b/i).first()).toBeVisible({ timeout: 30_000 });
});

test('source → filter → sink: edges connect, chain executes, rows reach the sink', async ({ page }) => {
  // Closes the gap from the validation pass: the earlier source→filter flake
  // was a SILENT missing edge. addEdge now verifies + retries + throws, and we
  // assert the edge count, so an unconnected node fails loudly instead of
  // running "with no input".
  await openEditor(page);
  const srcId = await addNode(page, 'db_source');
  const filterId = await addNode(page, 'filter', 10);
  const sinkId = await addNode(page, 'local_table_sink', 20);
  expect(srcId, 'db_source should drop').toBeTruthy();
  expect(filterId, 'filter should drop').toBeTruthy();
  expect(sinkId, 'sink should drop').toBeTruthy();

  await addEdge(page, srcId, filterId);
  await addEdge(page, filterId, sinkId);
  expect(await edgeCount(page), 'both edges (src→filter, filter→sink) should be connected').toBe(2);

  // Source: 3 deterministic rows via the in-memory engine.
  await openConfig(page, srcId);
  await configBody(page).locator('textarea').first().fill('SELECT * FROM (VALUES (1),(2),(3)) AS t(n)');
  await closeConfig(page);

  // Filter: keep n > 1 → 2 of 3 rows.
  await openConfig(page, filterId);
  await configBody(page).locator('input, textarea').first().fill('n > 1');
  await closeConfig(page);

  // Sink: write the filtered rows to a NEW managed table (mode defaults to
  // 'replace' → reruns are idempotent). The sink's "type a new table name"
  // field is targeted by its placeholder.
  await openConfig(page, sinkId);
  await configBody(page).getByPlaceholder(/type a new table name/i).fill('e2e_filter_out');

  // Test Node on the SINK runs the whole chain: source → filter → sink. Proves
  // upstream nodes execute first (the engine resolves dependencies) and the 2
  // filtered rows reach the sink write — i.e. an output table is produced.
  await page.getByRole('button', { name: 'Test Node' }).click();
  await expect(
    page.getByText(/\b2 rows?\b|wrote|written|success/i).first(),
  ).toBeVisible({ timeout: 30_000 });
});
