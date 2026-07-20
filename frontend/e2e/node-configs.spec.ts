import { test, expect } from '@playwright/test';
import { openEditor, addNode, openConfig, closeConfig, configBody, assertNoErrorBoundary } from './helpers';

/**
 * Per-node config smoke: open EVERY visible palette node's config and assert it
 * renders cleanly (config body mounts, a Parameters tab exists, no React error
 * boundary). This is the comprehensive "check each node frontend" net — it
 * catches missing dispatches, crashing configs, and registry drift (a type
 * that can't be dropped fails the `id` assertion).
 *
 * Mirrors the visible palette (ModulesPanel MODULES, minus HIDDEN_TYPES).
 */
const NODES: string[] = [
  // Data Movement
  'source', 'destination', 'copy_data', 'local_table_source', 'local_table_sink',
  // Transform / Prepare
  'filter', 'derived_column', 'sort', 'sample', 'deduplicate', 'schema_mapper',
  'data_quality', 'flatten_explode', 'transform', 'data_wrangler', 'scd2',
  // Combine / Reshape
  'join', 'lookup', 'union', 'pivot', 'unpivot', 'window', 'aggregate',
  // Control Flow
  'if_condition', 'conditional_split', 'foreach_pipeline', 'foreach_loop',
  'execute_pipeline', 'lookup_activity', 'set_variable', 'wait_delay', 'fail', 'retry_handler',
  // Action
  'http_request', 'send_email', 'slack_notify', 'get_metadata',
  // AI / Semantic
  'embedder', 'semantic_router', 'llm_guardrail',
];

/** Full-bleed nodes own their whole surface and don't render the standard
 *  Data In / Data Out bands (Data Wrangler has its own 3-pane workspace). */
const FULL_BLEED = new Set<string>(['data_wrangler']);

test.describe('node config renders for every visible node', () => {
  for (const type of NODES) {
    test(`config: ${type}`, async ({ page }) => {
      await openEditor(page);

      const id = await addNode(page, type);
      expect(id, `"${type}" should drop onto the canvas (registry/palette drift if not)`).toBeTruthy();

      await openConfig(page, id);

      const body = configBody(page);
      await expect(body, `"${type}" config body should mount`).toBeVisible();
      // The tab bar is always present; Parameters is the first tab. (.first()
      // because DynamicConfig's smart-defaults toggle can also contain the word.)
      await expect(body.getByRole('button', { name: 'Parameters' }).first()).toBeVisible();

      // NodeConfigFrame standard shell (2026-06-16): every node except the
      // full-bleed Data Wrangler renders "Data In" + "Data Out" bands around
      // its Processing form. This asserts the shell is present and consistent —
      // not just that the modal mounts.
      if (!FULL_BLEED.has(type)) {
        await expect(
          body.getByText('Data In', { exact: true }).first(),
          `"${type}" should render the Data In band`,
        ).toBeVisible();
        await expect(
          body.getByText('Data Out', { exact: true }).first(),
          `"${type}" should render the Data Out band`,
        ).toBeVisible();
        // Multi-input nodes must name their input roles in the Data In band —
        // the core IO-clarity fix (Join Left/Right was the #1 confusion).
        if (type === 'join') {
          await expect(body.getByText('Left', { exact: false }).first()).toBeVisible();
          await expect(body.getByText('Right', { exact: false }).first()).toBeVisible();
        }
      }

      await assertNoErrorBoundary(page);

      await closeConfig(page);
    });
  }
});
