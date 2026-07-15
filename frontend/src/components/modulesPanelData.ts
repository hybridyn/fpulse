/**
 * Non-component exports extracted from ModulesPanel.tsx so React Fast
 * Refresh can hot-reload ModulesPanel cleanly. Vite's Fast Refresh
 * requires component-only exports per file; mixing constants/hooks/types
 * with React components forces full HMR invalidation, which wipes
 * module-level state (e.g. the license cache in api/client.ts) on every
 * file change → backend log spam + brief tier flicker.
 *
 * Anything imported by both ModulesPanel and other surfaces (ActivitiesRibbon)
 * lives here. Components stay in ModulesPanel.tsx.
 */
import { useEffect, useMemo, useState } from 'react';
import { HIDDEN_TYPES } from './hiddenNodeTypes';

/**
 * Progressive disclosure level for the palette (D11/12/13, 2026-05-18).
 *   - 'beginner'     — must-have nodes everyone needs from day one.
 *   - 'intermediate' — common transforms most users reach for within
 *                     a couple of pipelines.
 *   - 'advanced'     — specialist primitives (retry_handler, materialize,
 *                     conditional_split, SCD2, semantic_router, etc.).
 *                     Hidden by default; revealed when the user picks
 *                     "All" in the palette level filter.
 *
 * Items without an explicit level default to 'intermediate' so adding
 * a new node doesn't accidentally bury it in Advanced.
 */
export type ModuleLevel = 'beginner' | 'intermediate' | 'advanced';

export interface ModuleItem {
  type: string;
  label: string;
  description?: string;
  level?: ModuleLevel;
}

export interface ModuleCategory {
  name: string;
  items: ModuleItem[];
}

/**
 * C1 — User-intent categorization (2026-05-18).
 *
 * The engine-categorization (Data Movement / Transform / Combine /
 * Control Flow / Action / AI / Semantic) is internally accurate but
 * mentally awkward for first-time users — "is HTTP Request a transform
 * or an action?" Reviewer 1 strongly recommended re-grouping the
 * palette by what the user is TRYING TO DO instead:
 *
 *   Import   — read data in
 *   Prepare  — clean, shape, validate
 *   Analyze  — combine, aggregate, classify
 *   Automate — call APIs, send messages, branch on conditions
 *   Publish  — write data out
 *
 * The Basic + Standard palette levels use this grouping; All keeps the
 * engine grouping for power users who think in primitives.
 */
export type UserIntent = 'Import' | 'Prepare' | 'Analyze' | 'Automate' | 'Publish';

export const INTENT_FOR_STEP_TYPE: Record<string, UserIntent> = {
  // ── Import (sources) ──
  source: 'Import', csv_source: 'Import', json_source: 'Import',
  parquet_source: 'Import', excel_source: 'Import', xml_source: 'Import',
  db_source: 'Import', api_source: 'Import', s3_source: 'Import',
  kafka_source: 'Import', ftp_source: 'Import', gsheet_source: 'Import',
  delta_source: 'Import', sharepoint_source: 'Import', onedrive_source: 'Import',
  gdrive_source: 'Import', dropbox_source: 'Import', box_source: 'Import',
  adls_gen2_source: 'Import', azure_blob_source: 'Import', gcs_source: 'Import',
  webhook_trigger: 'Import',
  // 2026-06-10: managed local-table nodes were missing here, so the
  // intent regrouper dropped them into the default 'Automate' bucket —
  // a table READER filed under Automate while every other reader sits
  // under Import.
  local_table_source: 'Import',
  // ── Prepare (single-relation transforms + quality) ──
  data_wrangler: 'Prepare', filter: 'Prepare', derived_column: 'Prepare',
  schema_mapper: 'Prepare', sort: 'Prepare', deduplicate: 'Prepare',
  sample: 'Prepare', data_quality: 'Prepare', flatten_explode: 'Prepare', split_out: 'Prepare',
  upsert: 'Prepare', rename: 'Prepare', typecast: 'Prepare',
  validate: 'Prepare', llm_guardrail: 'Prepare',
  // ── Analyze (multi-relation + semantic classification) ──
  transform: 'Analyze', join: 'Analyze', lookup: 'Analyze', union: 'Analyze',
  aggregate: 'Analyze', pivot: 'Analyze', unpivot: 'Analyze', window: 'Analyze',
  embedder: 'Analyze', semantic_router: 'Analyze',
  scd2: 'Analyze',
  // ── Automate (calls, messages, control flow) ──
  http_request: 'Automate', send_email: 'Automate', slack_notify: 'Automate',
  code_script: 'Automate', get_metadata: 'Automate', copy_data: 'Automate',
  execute_pipeline: 'Automate', execute_sql_task: 'Automate',
  file_system: 'Automate', delete_data: 'Automate',
  set_variable: 'Automate', append_variable: 'Automate', filter_array: 'Automate',
  if_condition: 'Automate', switch_case: 'Automate', lookup_activity: 'Automate',
  conditional_split: 'Automate',
  foreach_loop: 'Automate', foreach_pipeline: 'Automate', until_loop: 'Automate', wait_delay: 'Automate',
  retry_handler: 'Automate', fail: 'Automate', validation: 'Automate',
  // ── Publish (sinks) ──
  destination: 'Publish', csv_sink: 'Publish', json_sink: 'Publish',
  excel_sink: 'Publish', parquet_sink: 'Publish', db_sink: 'Publish',
  s3_sink: 'Publish', kafka_sink: 'Publish', api_sink: 'Publish',
  email_sink: 'Publish', delta_sink: 'Publish', warehouse_sink: 'Publish',
  file_sink: 'Publish', sharepoint_sink: 'Publish', onedrive_sink: 'Publish',
  gdrive_sink: 'Publish', dropbox_sink: 'Publish', box_sink: 'Publish',
  adls_gen2_sink: 'Publish', azure_blob_sink: 'Publish', gcs_sink: 'Publish',
  output: 'Publish',
  local_table_sink: 'Publish',  // 2026-06-10 — see local_table_source note above
};

/** Re-groups a list of engine-categorized modules into user-intent groups. */
export function regroupByIntent(modules: ModuleCategory[]): ModuleCategory[] {
  const order: UserIntent[] = ['Import', 'Prepare', 'Analyze', 'Automate', 'Publish'];
  const buckets = new Map<UserIntent, ModuleItem[]>();
  for (const intent of order) buckets.set(intent, []);
  for (const cat of modules) {
    for (const item of cat.items) {
      const intent = INTENT_FOR_STEP_TYPE[item.type] || 'Automate';
      buckets.get(intent)!.push(item);
    }
  }
  return order
    .map((name) => ({ name, items: buckets.get(name) || [] }))
    .filter((c) => c.items.length > 0);
}

/** Backend node registry — fetched once, cached on window for the app lifetime. */
export function useBackendNodeTypes(): { type: string; label: string; category: string; description?: string }[] | null {
  const [types, setTypes] = useState<{ type: string; label: string; category: string; description?: string }[] | null>(
    () => (window as any).__fpulse_node_types ?? null
  );
  useEffect(() => {
    if (types) return;
    fetch('/api/node-types')
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (Array.isArray(data)) {
          (window as any).__fpulse_node_types = data;
          setTypes(data);
        }
      })
      .catch(() => {});
  }, [types]);
  return types;
}

// `icon` values are IconName strings from `shared/Icon.tsx`. Consumers
// render them as line-art SVGs so palette categories render identically
// across OS / browser fonts (the previous emoji values degraded to
// monochrome boxes on systems without an emoji font).
import type { IconName } from './shared/Icon';

export const CATEGORY_ICONS: Record<string, { icon: IconName; color: string; }> = {
  'Data Movement': { icon: 'package', color: '#3b82f6' },
  Transform: { icon: 'zap', color: '#10b981' },
  Combine: { icon: 'link', color: '#f97316' },
  'Control Flow': { icon: 'shuffle', color: '#eab308' },
  Action: { icon: 'globe', color: '#0ea5e9' },
  'AI / Semantic': { icon: 'activity', color: '#8b5cf6' },
  // Legacy keys — kept for reconciliation fallback
  Sources: { icon: 'download', color: '#3b82f6' },
  Outputs: { icon: 'upload', color: '#6366f1' },
  Flow: { icon: 'shuffle', color: '#eab308' },
  SaaS: { icon: 'globe', color: '#a855f7' },
};

const CATEGORY_ALIAS: Record<string, string> = {
  source: 'Data Movement',
  sources: 'Data Movement',
  output: 'Data Movement',
  outputs: 'Data Movement',
  transform: 'Transform',
  combine: 'Combine',
  flow: 'Control Flow',
  flow_control: 'Control Flow',
  action: 'Action',
  saas: 'Action',
  ai: 'AI / Semantic',
  semantic: 'AI / Semantic',
  general: 'Transform',
};

const aliasFor = (raw: string) => {
  const key = (raw || '').toLowerCase();
  return CATEGORY_ALIAS[key] || raw.replace(/^./, (c) => c.toUpperCase());
};

/** Reconciles the hardcoded MODULES palette against the backend node
 *  registry. Lifted out of ModulesPanel so ActivitiesRibbon (and any
 *  future picker surface) reuses the exact same source-of-truth.
 *  MODULES is passed in to avoid a circular import — ModulesPanel.tsx
 *  owns the canonical MODULES array and re-exports useReconciledModules
 *  bound to its own MODULES via a tiny wrapper. */
export function makeReconciledModulesHook(modules: ModuleCategory[]) {
  return function useReconciledModules(): ModuleCategory[] {
    const backendTypes = useBackendNodeTypes();
    return useMemo<ModuleCategory[]>(() => {
      if (!backendTypes) return modules;

      const descByType = new Map(
        backendTypes.map((t) => [t.type, t.description || ''])
      );

      const filtered: ModuleCategory[] = modules.map((cat) => ({
        ...cat,
        items: cat.items
          .filter((item) => !HIDDEN_TYPES.has(item.type))
          .map((item): ModuleItem => ({
            ...item,
            description: descByType.get(item.type) || item.description,
          })),
      })).filter((cat) => cat.items.length > 0);

      const knownTypes = new Set(modules.flatMap((c) => c.items.map((i) => i.type)));
      const extras = backendTypes.filter(
        (t) => !knownTypes.has(t.type) && !HIDDEN_TYPES.has(t.type)
      );
      if (extras.length > 0) {
        const byCat: Record<string, ModuleItem[]> = {};
        for (const t of extras) {
          const cat = aliasFor(t.category || 'More');
          (byCat[cat] ||= []).push({ type: t.type, label: t.label || t.type, description: t.description });
        }
        for (const [name, items] of Object.entries(byCat)) {
          const existing = filtered.find((c) => c.name === name);
          if (existing) existing.items.push(...items);
          else filtered.push({ name, items });
        }
      }
      return filtered;
    }, [backendTypes]);
  };
}
