/**
 * Storage preview panel — anchored to the BOTTOM of the viewport
 * (Y4 + Y5 + Y8 + Y16 restyle, 2026-05-23).
 *
 * Y16 rewrite: previously a right-side slide-in drawer. The Builder's
 * PreviewPanel (components/PreviewPanel.tsx) anchors at the bottom of
 * the viewport so the user can still see + click their canvas while
 * inspecting data. Storage now uses the same shape — file list stays
 * scrollable above; preview pins below.
 *
 * Layout:
 *   ─────────────────────────────────────────────
 *   Page (file list scrolls above, full width)
 *   ─────────────────────────────────────────────
 *   Bottom panel  ← fixed bottom-0 left-0 right-0
 *     Header: file name + format + row count + sub-tabs + close
 *     [Pipeline-detected banner — sticky, when applicable]
 *     Body: Preview rows OR Schema OR Document tree
 *
 * Supports two response shapes (Y8):
 *   - kind="rows"     → DuckDB row preview, schema panel as columns.
 *   - kind="document" → JSON-tree fallback for non-tabular JSON.
 *
 * The component name + filename remain ``StoragePreviewDrawer`` for
 * back-compat with the existing import in StoragePage.tsx. The visible
 * shape is a bottom panel; the internal noun-vocabulary updates
 * accordingly.
 */

import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';
import ResizeHandle from '../shared/ResizeHandle';

interface StorageObject {
  id: string;
  name: string;
  format: string | null;
  size_bytes: number;
  row_count: number | null;
}

interface RowsPreview {
  kind: 'rows';
  columns: Array<{ name: string; type: string }>;
  rows: Array<Record<string, unknown>>;
  row_count: number | null;
  limit: number;
  offset: number;
  format: string;
}

interface DocumentPreview {
  kind: 'document';
  format: string;
  document: unknown;
  document_kind: 'object' | 'array' | 'too_large' | 'invalid';
  size_bytes: number;
  is_pipeline_definition: boolean;
  message?: string;
}

type PreviewResponse = RowsPreview | DocumentPreview;

interface SchemaResponse {
  columns: Array<{
    name: string;
    type: string;
    nullable: boolean;
    ordinal: number;
    sample?: unknown;
  }>;
}

export default function StoragePreviewDrawer({
  object,
  onClose,
  height = 440,
  onHeightChange,
  resourceKind = 'object',
}: {
  object: StorageObject;
  onClose: () => void;
  /** Pixel height of the panel. Owned by the parent (StoragePage) so the
   *  page can keep its bottom-padding in sync. Drag-to-resize fires
   *  ``onHeightChange`` on every move. */
  height?: number;
  onHeightChange?: (h: number) => void;
  resourceKind?: 'object' | 'table';
}) {
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<'preview' | 'schema' | 'tests'>('preview');
  // N3 / V7 — tests tab state. Only relevant when resourceKind ===
  // 'table'; we still declare the state unconditionally so the hooks
  // order stays stable across renders.
  const [tableTests, setTableTests] = useState<Array<{
    test_type: string;
    columns: string[];
    accepted_values?: unknown[];
    to_table?: string;
    to_column?: string;
  }>>([]);
  const [testsRunResult, setTestsRunResult] = useState<{
    passed: number;
    failed: number;
    results: Array<{ test_type: string; columns: string[]; ok: boolean; violations: number; message?: string }>;
    ran_at: string;
  } | null>(null);
  const [testsRunning, setTestsRunning] = useState(false);
  // F2 (2026-05-30) — provenance card data
  const [provenance, setProvenance] = useState<{
    source_file: { id: string; name: string } | null;
    source_workflow: { id: string; name: string } | null;
    source_recipe: unknown[] | null;
    last_run: {
      id: string | null;
      status: string | null;
      started_at: string | null;
      completed_at: string | null;
      rows_written: number | null;
    } | null;
    consumers: Array<{ workflow_id: string; name: string }>;
    consumer_count: number;
  } | null>(null);
  // V7 round 2 — inline test editor state
  const [showAddTest, setShowAddTest] = useState(false);
  const [newTestType, setNewTestType] = useState<'not_null' | 'unique' | 'accepted_values' | 'relationships' | 'freshness' | 'row_count_anomaly'>('not_null');
  const [newTestColumns, setNewTestColumns] = useState('');
  const [newTestAccepted, setNewTestAccepted] = useState('');
  // F4 (2026-05-30) — extended fields for freshness + row_count_anomaly
  // tests (P6 backend). severity defaults to 'fail' to match backend +
  // historic behaviour (test fails ≡ block downstream).
  const [newTestSeverity, setNewTestSeverity] = useState<'fail' | 'warn'>('fail');
  const [newTestMaxAge, setNewTestMaxAge] = useState('60');
  const [newTestMinRows, setNewTestMinRows] = useState('');
  const [newTestMaxRows, setNewTestMaxRows] = useState('');
  const [newTestDriftPct, setNewTestDriftPct] = useState('');
  const [newTestToTable, setNewTestToTable] = useState('');
  const [newTestToColumn, setNewTestToColumn] = useState('');
  const [savingTests, setSavingTests] = useState(false);
  const [opening, setOpening] = useState(false);
  // Z5 / Z9 — drag-to-resize via the shared <ResizeHandle> primitive.
  // The panel is anchored to the bottom of the viewport, so the handle
  // sits on the top edge (edge="top-edge") and dragging UP grows it.

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setPreview(null);
    setSchema(null);
    setTableTests([]);
    setTestsRunResult(null);
    setProvenance(null);
    const previewUrl = resourceKind === 'table'
      ? `/api/storage/tables/${object.id}/preview?limit=100`
      : `/api/storage/preview/${object.id}?limit=100`;
    const schemaUrl = resourceKind === 'table'
      ? `/api/storage/tables/${object.id}/columns`
      : `/api/storage/schema/${object.id}`;
    Promise.all([
      api.get<PreviewResponse>(previewUrl),
      api
        .get<SchemaResponse>(schemaUrl)
        .catch(() => ({ columns: [] })),
    ])
      .then(([p, s]) => {
        if (!alive) return;
        setPreview(p);
        setSchema(s);
      })
      .catch((err) => {
        if (!alive) return;
        toast.error(`Preview failed: ${(err as Error).message || err}`);
      })
      .finally(() => alive && setLoading(false));

    // V7 / N3 — fetch the configured tests when previewing a managed
    // table. Non-fatal: if the endpoint isn't there yet (e.g. backend
    // hasn't restarted), we just leave the tab empty.
    if (resourceKind === 'table') {
      api
        .get<{ tests: Array<{
          test_type: string;
          columns: string[];
          accepted_values?: unknown[];
          to_table?: string;
          to_column?: string;
        }> }>(`/api/storage/tables/${object.id}/tests`)
        .then((data) => { if (alive) setTableTests(data.tests || []); })
        .catch(() => { /* tab stays empty; harmless */ });

      // F2 (2026-05-30) — fetch the unified provenance card. One call
      // answers "where did this table come from?", "what pipeline
      // produced it?", "which run last touched it?", and "what breaks
      // if I delete it?" — all surfaces the prior Storage drawer
      // needed individual fetches for. Non-fatal: if the endpoint
      // isn't there, the card silently stays hidden.
      api
        .get<{
          source_file: { id: string; name: string } | null;
          source_workflow: { id: string; name: string } | null;
          source_recipe: unknown[] | null;
          last_run: {
            id: string | null;
            status: string | null;
            started_at: string | null;
            completed_at: string | null;
            rows_written: number | null;
          } | null;
          consumers: Array<{ workflow_id: string; name: string }>;
          consumer_count: number;
        }>(`/api/storage/tables/${object.id}/provenance`)
        .then((data) => { if (alive) setProvenance(data); })
        .catch(() => { /* card stays hidden; harmless */ });
    }
    return () => {
      alive = false;
    };
  }, [object.id, resourceKind]);

  // Close on Escape — bottom panel doesn't have a backdrop, so the
  // keyboard fallback keeps it dismissable without clicking the X.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Drag-to-resize behaviour lives in the shared ResizeHandle primitive
  // (orientation="vertical", edge="top-edge"). Min height keeps the
  // chrome readable; max cap leaves at least ~15vh for the file list
  // above the panel.

  const isDocument = preview?.kind === 'document';
  const isPipeline =
    isDocument && (preview as DocumentPreview).is_pipeline_definition;

  const onOpenInEditor = async () => {
    setOpening(true);
    try {
      const res = await fetch(`/api/storage/raw/${object.id}`, {
        headers: {
          Authorization: localStorage.getItem('fpulse_token')
            ? `Bearer ${localStorage.getItem('fpulse_token')}`
            : '',
          'X-Workspace-Id': localStorage.getItem('fpulse_workspace_id') || 'default',
        },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const text = await res.text();
      try {
        JSON.parse(text);
      } catch (err) {
        toast.error(`File is not valid JSON: ${(err as Error).message}`);
        return;
      }
      sessionStorage.setItem('fpulse_pending_import', text);
      sessionStorage.setItem(
        'fpulse_pending_import_source',
        `storage:${object.name}`,
      );
      toast.success(`Opening ${object.name} in Editor…`);
      window.location.hash = 'editor';
    } catch (err) {
      toast.error(`Open in Editor failed: ${(err as Error).message || err}`);
    } finally {
      setOpening(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-label={`Preview ${object.name}`}
      className="fixed bottom-0 left-0 right-0 z-40 bg-white border-t-2 border-amber-400/70 shadow-[0_-16px_40px_-12px_rgba(15,23,42,0.20)] flex flex-col"
      style={{ height }}
    >
      <ResizeHandle
        orientation="vertical"
        edge="top-edge"
        value={height}
        onResize={(h) => onHeightChange?.(h)}
        min={220}
        max={() => Math.round(window.innerHeight * 0.85)}
        ariaLabel="Drag to resize preview panel"
        className="absolute -top-1 left-0 right-0 z-50"
      />
      {/* Header — light but structured. A soft gradient strip with a
          file-icon badge, format chip, row-count chip, vertical divider,
          and amber-accented tab pills. Reads as "data viewer chrome"
          without echoing the upper table's dark thead. */}
      <div className="flex items-center px-5 py-3 gap-4 shrink-0 bg-gradient-to-r from-amber-50/60 via-white to-slate-50 border-b border-slate-200">
        {/* Icon badge — circular amber tint, anchors the panel identity. */}
        <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-amber-100 to-amber-50 border border-amber-200/70 flex items-center justify-center shrink-0 shadow-sm">
          <svg
            width="18" height="18" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
            className="text-amber-600"
          >
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
          </svg>
        </div>

        <div className="min-w-0 flex flex-col gap-1">
          <div className="text-sm font-semibold text-slate-900 truncate leading-tight">
            {object.name}
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="inline-flex items-center px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider rounded bg-blue-100 text-blue-800 border border-blue-200/60">
              {object.format ? object.format.toUpperCase() : 'UNKNOWN'}
            </span>
            <span className="inline-flex items-center px-2 py-0.5 text-[10px] font-semibold rounded bg-slate-100 text-slate-700 border border-slate-200">
              {previewSummary(preview)}
            </span>
          </div>
        </div>

        {/* V7 round 3 + 4 — "View lineage" link for managed tables.
            Round 4: deep-links to the pipeline that produced this
            table (via the new ?entity=workflow:<id> query param the
            LineagePage now reads on mount). Falls back to plain
            #lineage when we don't know the source pipeline (e.g.
            legacy tables predating prep_workflow_id). */}
        {resourceKind === 'table' && (
          <a
            href={
              (object as any).pipeline_id
                ? `#lineage?entity=workflow:${(object as any).pipeline_id}`
                : '#lineage'
            }
            className="ml-auto text-xs font-semibold px-3 py-1.5 rounded text-slate-600 hover:text-slate-900 border border-slate-200 hover:bg-slate-50 transition-colors inline-flex items-center gap-1.5"
            title={
              (object as any).pipeline_id
                ? `Open the Lineage page focused on the pipeline that produced ${object.name}`
                : 'Open the Lineage page (no source pipeline tracked for this table)'
            }
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="6" cy="6" r="2" />
              <circle cx="18" cy="6" r="2" />
              <circle cx="12" cy="18" r="2" />
              <path d="M6 8v2a4 4 0 0 0 4 4h4a4 4 0 0 0 4-4V8" />
              <path d="M12 14v2" />
            </svg>
            View lineage
          </a>
        )}

        {/* Vertical divider before the tab cluster — gives the header
            two clear regions (file identity / view controls). */}
        <div className={resourceKind === 'table' ? 'h-9 w-px bg-slate-200' : 'ml-auto h-9 w-px bg-slate-200'} aria-hidden="true" />

        {/* Sub-tabs — pill style with amber accent on active. Tests
            tab only shows for managed tables (V7 / N3). */}
        <div className="flex items-center gap-1 bg-white border border-slate-200 rounded-lg p-0.5 shadow-sm">
          {(resourceKind === 'table'
            ? (['preview', 'schema', 'tests'] as const)
            : (['preview', 'schema'] as const)
          ).map((v) => {
            const active = view === v;
            const disabled = v === 'schema' && isDocument;
            return (
              <button
                key={v}
                onClick={() => !disabled && setView(v)}
                disabled={disabled}
                className={`px-3 py-1.5 text-xs font-semibold rounded-md transition-colors ${
                  active
                    ? 'bg-amber-100 text-amber-900 border border-amber-300 shadow-sm'
                    : disabled
                      ? 'text-slate-300 cursor-not-allowed'
                      : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900 border border-transparent'
                }`}
                title={disabled ? 'Schema view is for tabular files only' : undefined}
              >
                {v === 'preview' ? 'Preview' : v === 'schema' ? 'Schema' : (
                  <>
                    Tests
                    {tableTests.length > 0 && (
                      <span className="ml-1.5 text-[10px] font-bold px-1.5 py-0.5 rounded bg-amber-200 text-amber-900">
                        {tableTests.length}
                      </span>
                    )}
                  </>
                )}
              </button>
            );
          })}
        </div>

        <button
          onClick={onClose}
          className="w-8 h-8 inline-flex items-center justify-center rounded-md text-slate-400 hover:text-slate-700 hover:bg-white border border-transparent hover:border-slate-200 transition-colors shrink-0"
          aria-label="Close preview"
          title="Close (Esc)"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>

      {/* Y8: Pipeline-detected banner — sticky just under the header
          so it stays visible while the user scrolls the preview body. */}
      {isPipeline && (
        <div className="border-b border-amber-200 bg-gradient-to-r from-amber-50 to-yellow-50 px-5 py-2.5 flex items-start gap-3 shrink-0">
          <svg
            width="16" height="16" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
            className="text-amber-600 shrink-0 mt-0.5"
          >
            <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
            <line x1="12" y1="9" x2="12" y2="13" />
            <line x1="12" y1="17" x2="12.01" y2="17" />
          </svg>
          <div className="flex-1 min-w-0">
            <div className="text-xs font-semibold text-amber-900">
              This looks like an F-Pulse pipeline definition.
            </div>
            <div className="text-[11px] text-amber-800 mt-0.5">
              Storage is for data files. Pipelines belong under Workflows.
            </div>
          </div>
          <button
            onClick={onOpenInEditor}
            disabled={opening}
            className="px-3 py-1.5 text-xs font-bold rounded-lg bg-amber-600 hover:bg-amber-700 text-white shadow-sm transition-colors disabled:opacity-50 whitespace-nowrap"
          >
            {opening ? 'Opening…' : 'Open in Editor'}
          </button>
        </div>
      )}

      {/* F2 (2026-05-30) — Provenance card for managed tables. One-line
          summary of source file → workflow → last run + consumer count,
          fetched via /api/storage/tables/{id}/provenance. Hidden when the
          backend doesn't return data (legacy tables, fresh installs). */}
      {resourceKind === 'table' && provenance && (
        provenance.source_file || provenance.source_workflow ||
        provenance.last_run || provenance.consumer_count > 0 ||
        (Array.isArray(provenance.source_recipe) && provenance.source_recipe.length > 0)
      ) && (
        <div className="px-4 py-2 border-b border-slate-200 bg-slate-50 text-[11px] text-slate-700 flex flex-wrap gap-x-4 gap-y-1">
          {provenance.source_file && (
            <span>
              <span className="font-semibold uppercase text-[9px] tracking-wider text-slate-500">From file</span>{' '}
              <code className="px-1 rounded bg-white border border-slate-200 font-mono text-[10px]">{provenance.source_file.name}</code>
            </span>
          )}
          {provenance.source_workflow && (
            <span>
              <span className="font-semibold uppercase text-[9px] tracking-wider text-slate-500">Produced by</span>{' '}
              <a
                href={`#editor/${provenance.source_workflow.id}`}
                className="text-pipe-700 hover:text-pipe-900 font-medium underline-offset-2 hover:underline"
              >{provenance.source_workflow.name}</a>
            </span>
          )}
          {provenance.last_run && provenance.last_run.completed_at && (
            <span>
              <span className="font-semibold uppercase text-[9px] tracking-wider text-slate-500">Last run</span>{' '}
              {new Date(provenance.last_run.completed_at).toLocaleString()}
              {provenance.last_run.status && (
                <span className={`ml-1 inline-block px-1.5 rounded text-[9px] font-bold uppercase ${
                  provenance.last_run.status === 'success' ? 'bg-emerald-100 text-emerald-800' :
                  provenance.last_run.status === 'error' ? 'bg-red-100 text-red-800' :
                  'bg-slate-200 text-slate-700'
                }`}>{provenance.last_run.status}</span>
              )}
            </span>
          )}
          <span title={
            provenance.consumers.length > 0
              ? `Consumed by:\n${provenance.consumers.map((c) => '• ' + c.name).join('\n')}`
              : 'No downstream pipelines depend on this table'
          }>
            <span className="font-semibold uppercase text-[9px] tracking-wider text-slate-500">Consumed by</span>{' '}
            <span className={provenance.consumer_count > 0 ? 'text-amber-700 font-medium' : 'text-slate-400'}>
              {provenance.consumer_count} pipeline{provenance.consumer_count === 1 ? '' : 's'}
            </span>
          </span>
          {/* Transformation recipe (the Data-Wrangler steps that built this
              table). Stored as prep_recipe + returned by /provenance; shown
              here so the "what transformation produced this?" answer is
              visible, not buried. basis-full puts it on its own line since
              a recipe can have many steps. Straight-promote tables have no
              recipe, so this simply doesn't render for them. */}
          {Array.isArray(provenance.source_recipe) && provenance.source_recipe.length > 0 && (
            <span className="flex items-center gap-1.5 basis-full">
              <span className="font-semibold uppercase text-[9px] tracking-wider text-slate-500">Transformation</span>
              <span className="text-slate-600">
                {provenance.source_recipe.length} step{provenance.source_recipe.length === 1 ? '' : 's'}:
              </span>
              <span className="flex flex-wrap gap-1">
                {provenance.source_recipe.slice(0, 8).map((s, i) => (
                  <code key={i} className="px-1 rounded bg-white border border-slate-200 font-mono text-[10px]">
                    {String((s as any)?.label || (s as any)?.op || 'step')}
                  </code>
                ))}
                {provenance.source_recipe.length > 8 && (
                  <span className="text-[10px] text-slate-400">
                    +{provenance.source_recipe.length - 8} more
                  </span>
                )}
              </span>
            </span>
          )}
        </div>
      )}

      {/* Body — fills the remaining height of the panel, scrolls
          independently of the file list above. */}
      <div className="flex-1 overflow-auto bg-slate-50/40">
        {loading ? (
          <div className="p-8 text-center text-sm text-slate-500">Loading preview…</div>
        ) : view === 'preview' ? (
          !preview ? (
            <div className="p-6 text-sm text-slate-500">No preview available.</div>
          ) : preview.kind === 'rows' ? (
            <PreviewTable preview={preview} />
          ) : (
            <DocumentPreviewView preview={preview} />
          )
        ) : view === 'schema' ? (
          schema && schema.columns.length > 0 ? (
            <SchemaTable schema={schema} />
          ) : (
            <div className="p-6 text-sm text-slate-500">
              No schema available — this file isn't tabular.
            </div>
          )
        ) : (
          /* V7 / N3 — Tests tab content. */
          <div className="p-5 space-y-4">
            <div className="flex items-center justify-between">
              <div className="text-sm font-bold text-slate-800">
                {tableTests.length === 0
                  ? 'No tests configured'
                  : `${tableTests.length} test${tableTests.length === 1 ? '' : 's'}`}
              </div>
              {tableTests.length > 0 && (
                <button
                  type="button"
                  onClick={async () => {
                    setTestsRunning(true);
                    setTestsRunResult(null);
                    try {
                      const res = await api.post<typeof testsRunResult>(
                        `/api/storage/tables/${object.id}/tests/run`,
                        {},
                      );
                      setTestsRunResult(res);
                    } catch (err) {
                      toast.error(`Run tests failed: ${(err as Error).message || err}`);
                    } finally {
                      setTestsRunning(false);
                    }
                  }}
                  disabled={testsRunning}
                  className="px-3 py-1.5 text-xs font-semibold rounded bg-amber-100 text-amber-900 border border-amber-300 hover:bg-amber-200 disabled:opacity-50"
                >
                  {testsRunning ? 'Running…' : 'Run tests'}
                </button>
              )}
            </div>

            {/* V7 round 2 — inline editor. "+ Add test" toggles a
                small form below; the existing list re-renders after
                save with a fresh fetch. PUT replaces the whole list,
                so add + delete both call the same endpoint with the
                full updated array. */}
            {!showAddTest && (
              <button
                type="button"
                onClick={() => {
                  setShowAddTest(true);
                  setNewTestType('not_null');
                  setNewTestColumns('');
                  setNewTestAccepted('');
                  setNewTestToTable('');
                  setNewTestToColumn('');
                }}
                className="text-xs font-semibold px-3 py-1.5 rounded border border-slate-300 text-slate-700 hover:bg-slate-50"
              >
                + Add test
              </button>
            )}

            {showAddTest && (
              <div className="border border-slate-200 rounded-lg p-3 space-y-2 bg-white">
                <div className="flex gap-2">
                  <label className="text-xs font-semibold text-slate-700 w-20 mt-1.5">Type</label>
                  <select
                    value={newTestType}
                    onChange={(e) => setNewTestType(e.target.value as typeof newTestType)}
                    className="flex-1 px-2 py-1 text-xs rounded border border-slate-300"
                  >
                    <option value="not_null">not_null — column has no NULLs</option>
                    <option value="unique">unique — column has no duplicates</option>
                    <option value="accepted_values">accepted_values — column ∈ list</option>
                    <option value="relationships">relationships — FK to another table</option>
                    <option value="freshness">freshness — table updated recently</option>
                    <option value="row_count_anomaly">row_count_anomaly — count within bounds</option>
                  </select>
                </div>
                <div className="flex gap-2">
                  <label className="text-xs font-semibold text-slate-700 w-20 mt-1.5">Severity</label>
                  <select
                    value={newTestSeverity}
                    onChange={(e) => setNewTestSeverity(e.target.value as 'fail' | 'warn')}
                    className="flex-1 px-2 py-1 text-xs rounded border border-slate-300"
                  >
                    <option value="fail">fail — block the run on violation</option>
                    <option value="warn">warn — log only, continue</option>
                  </select>
                </div>
                {newTestType !== 'freshness' && newTestType !== 'row_count_anomaly' && (
                  <div className="flex gap-2">
                    <label className="text-xs font-semibold text-slate-700 w-20 mt-1.5">Column(s)</label>
                    <input
                      value={newTestColumns}
                      onChange={(e) => setNewTestColumns(e.target.value)}
                      placeholder={newTestType === 'unique' ? 'e.g. id  (or composite: id, region)' : 'e.g. id'}
                      className="flex-1 px-2 py-1 text-xs rounded border border-slate-300 font-mono"
                    />
                  </div>
                )}
                {newTestType === 'freshness' && (
                  <div className="flex gap-2">
                    <label className="text-xs font-semibold text-slate-700 w-20 mt-1.5">Max age</label>
                    <input
                      type="number" min="1"
                      value={newTestMaxAge}
                      onChange={(e) => setNewTestMaxAge(e.target.value)}
                      placeholder="60"
                      className="flex-1 px-2 py-1 text-xs rounded border border-slate-300 font-mono"
                    />
                    <span className="text-xs text-slate-500 self-center">minutes</span>
                  </div>
                )}
                {newTestType === 'row_count_anomaly' && (
                  <>
                    <div className="flex gap-2">
                      <label className="text-xs font-semibold text-slate-700 w-20 mt-1.5">Min rows</label>
                      <input
                        type="number" min="0"
                        value={newTestMinRows}
                        onChange={(e) => setNewTestMinRows(e.target.value)}
                        placeholder="(optional)"
                        className="flex-1 px-2 py-1 text-xs rounded border border-slate-300 font-mono"
                      />
                    </div>
                    <div className="flex gap-2">
                      <label className="text-xs font-semibold text-slate-700 w-20 mt-1.5">Max rows</label>
                      <input
                        type="number" min="0"
                        value={newTestMaxRows}
                        onChange={(e) => setNewTestMaxRows(e.target.value)}
                        placeholder="(optional)"
                        className="flex-1 px-2 py-1 text-xs rounded border border-slate-300 font-mono"
                      />
                    </div>
                    <div className="flex gap-2">
                      <label className="text-xs font-semibold text-slate-700 w-20 mt-1.5">Drift %</label>
                      <input
                        type="number" min="0" step="0.5"
                        value={newTestDriftPct}
                        onChange={(e) => setNewTestDriftPct(e.target.value)}
                        placeholder="e.g. 10 = alert on >10% change vs last run"
                        className="flex-1 px-2 py-1 text-xs rounded border border-slate-300 font-mono"
                      />
                    </div>
                  </>
                )}
                {newTestType === 'accepted_values' && (
                  <div className="flex gap-2">
                    <label className="text-xs font-semibold text-slate-700 w-20 mt-1.5">Values</label>
                    <input
                      value={newTestAccepted}
                      onChange={(e) => setNewTestAccepted(e.target.value)}
                      placeholder='active, inactive, pending'
                      className="flex-1 px-2 py-1 text-xs rounded border border-slate-300 font-mono"
                    />
                  </div>
                )}
                {newTestType === 'relationships' && (
                  <>
                    <div className="flex gap-2">
                      <label className="text-xs font-semibold text-slate-700 w-20 mt-1.5">To table</label>
                      <input
                        value={newTestToTable}
                        onChange={(e) => setNewTestToTable(e.target.value)}
                        placeholder="schema.name"
                        className="flex-1 px-2 py-1 text-xs rounded border border-slate-300 font-mono"
                      />
                    </div>
                    <div className="flex gap-2">
                      <label className="text-xs font-semibold text-slate-700 w-20 mt-1.5">To column</label>
                      <input
                        value={newTestToColumn}
                        onChange={(e) => setNewTestToColumn(e.target.value)}
                        placeholder="id"
                        className="flex-1 px-2 py-1 text-xs rounded border border-slate-300 font-mono"
                      />
                    </div>
                  </>
                )}
                <div className="flex justify-end gap-2 pt-1">
                  <button
                    type="button"
                    onClick={() => setShowAddTest(false)}
                    className="px-3 py-1 text-xs font-semibold rounded text-slate-600 hover:bg-slate-100"
                    disabled={savingTests}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={async () => {
                      const cols = newTestColumns
                        .split(',').map((s) => s.trim()).filter(Boolean);
                      // F4 — freshness + row_count_anomaly are whole-table
                      // assertions that don't take columns. The other 4 do.
                      const needsColumns = newTestType !== 'freshness' && newTestType !== 'row_count_anomaly';
                      if (needsColumns && !cols.length) {
                        toast.error('At least one column is required');
                        return;
                      }
                      const newTest: {
                        test_type: string;
                        columns: string[];
                        severity: 'fail' | 'warn';
                        accepted_values?: unknown[];
                        to_table?: string;
                        to_column?: string;
                        max_age_minutes?: number;
                        expected_min_rows?: number;
                        expected_max_rows?: number;
                        drift_pct?: number;
                      } = {
                        test_type: newTestType,
                        columns: cols,
                        severity: newTestSeverity,
                      };
                      if (newTestType === 'accepted_values') {
                        const vals = newTestAccepted
                          .split(',').map((s) => s.trim()).filter(Boolean);
                        if (!vals.length) {
                          toast.error('Accepted values list cannot be empty');
                          return;
                        }
                        newTest.accepted_values = vals;
                      }
                      if (newTestType === 'relationships') {
                        if (!newTestToTable.trim() || !newTestToColumn.trim()) {
                          toast.error('Relationships test needs to_table and to_column');
                          return;
                        }
                        newTest.to_table = newTestToTable.trim();
                        newTest.to_column = newTestToColumn.trim();
                      }
                      if (newTestType === 'freshness') {
                        const age = parseInt(newTestMaxAge, 10);
                        if (!age || age <= 0) {
                          toast.error('Freshness needs max_age_minutes > 0');
                          return;
                        }
                        newTest.max_age_minutes = age;
                      }
                      if (newTestType === 'row_count_anomaly') {
                        const minR = newTestMinRows ? parseInt(newTestMinRows, 10) : undefined;
                        const maxR = newTestMaxRows ? parseInt(newTestMaxRows, 10) : undefined;
                        const drift = newTestDriftPct ? parseFloat(newTestDriftPct) : undefined;
                        if (minR === undefined && maxR === undefined && drift === undefined) {
                          toast.error('Row count anomaly needs at least one of min / max / drift %');
                          return;
                        }
                        if (minR !== undefined) newTest.expected_min_rows = minR;
                        if (maxR !== undefined) newTest.expected_max_rows = maxR;
                        if (drift !== undefined) newTest.drift_pct = drift;
                      }
                      setSavingTests(true);
                      try {
                        const updated = [...tableTests, newTest];
                        await api.put<{ tests: typeof tableTests }>(
                          `/api/storage/tables/${object.id}/tests`,
                          { tests: updated },
                        );
                        setTableTests(updated);
                        setShowAddTest(false);
                      } catch (err) {
                        toast.error(`Save failed: ${(err as Error).message || err}`);
                      } finally {
                        setSavingTests(false);
                      }
                    }}
                    className="px-3 py-1 text-xs font-semibold rounded bg-amber-500 text-white hover:bg-amber-600 disabled:opacity-50"
                    disabled={savingTests}
                  >
                    {savingTests ? 'Saving…' : 'Save test'}
                  </button>
                </div>
              </div>
            )}

            {tableTests.length === 0 ? (
              <div className="text-xs text-slate-500 leading-relaxed">
                No tests yet. Click <strong>+ Add test</strong> above to declare a
                {' '}<code className="px-1 rounded bg-slate-100 text-slate-700">not_null</code>,
                {' '}<code className="px-1 rounded bg-slate-100 text-slate-700">unique</code>,
                {' '}<code className="px-1 rounded bg-slate-100 text-slate-700">accepted_values</code>, or
                {' '}<code className="px-1 rounded bg-slate-100 text-slate-700">relationships</code> assertion on this managed table.
                Tests run via the <strong>Run tests</strong> button and report pass/fail with violation counts.
              </div>
            ) : (
              <ul className="space-y-1.5">
                {tableTests.map((t, i) => {
                  const runResult = testsRunResult?.results[i];
                  return (
                    <li
                      key={i}
                      className={`flex items-start gap-3 px-3 py-2 rounded-md border text-xs ${
                        runResult
                          ? runResult.ok
                            ? 'bg-emerald-50 border-emerald-200'
                            : 'bg-red-50 border-red-200'
                          : 'bg-slate-50 border-slate-200'
                      }`}
                    >
                      <span className={`mt-0.5 inline-flex items-center justify-center w-4 h-4 rounded-full ${
                        runResult
                          ? runResult.ok
                            ? 'bg-emerald-500 text-white'
                            : 'bg-red-500 text-white'
                          : 'bg-slate-300 text-slate-600'
                      }`}>
                        {runResult ? (runResult.ok ? '✓' : '✗') : '·'}
                      </span>
                      <div className="flex-1 min-w-0">
                        <div className="font-semibold text-slate-800 flex items-center gap-2">
                          <span>
                            {t.test_type} <span className="font-mono text-slate-500">({t.columns.join(', ')})</span>
                          </span>
                          <button
                            type="button"
                            onClick={async () => {
                              setSavingTests(true);
                              try {
                                const updated = tableTests.filter((_, j) => j !== i);
                                await api.put<{ tests: typeof tableTests }>(
                                  `/api/storage/tables/${object.id}/tests`,
                                  { tests: updated },
                                );
                                setTableTests(updated);
                                // If a run result was attached, clear it
                                // (indices have shifted; safer to refetch
                                // the next time the user runs tests).
                                setTestsRunResult(null);
                              } catch (err) {
                                toast.error(`Delete failed: ${(err as Error).message || err}`);
                              } finally {
                                setSavingTests(false);
                              }
                            }}
                            disabled={savingTests}
                            className="ml-auto text-[10px] text-slate-400 hover:text-red-500 disabled:opacity-30"
                            title="Remove this test"
                          >
                            Remove
                          </button>
                        </div>
                        {t.test_type === 'accepted_values' && t.accepted_values && (
                          <div className="text-[11px] text-slate-500 mt-0.5">
                            Accepted: {t.accepted_values.map((v) => JSON.stringify(v)).join(', ')}
                          </div>
                        )}
                        {t.test_type === 'relationships' && (
                          <div className="text-[11px] text-slate-500 mt-0.5">
                            References {t.to_table}.{t.to_column}
                          </div>
                        )}
                        {runResult && !runResult.ok && (
                          <div className="text-[11px] text-red-700 mt-0.5">
                            {runResult.message
                              ? runResult.message
                              : `${runResult.violations} violation${runResult.violations === 1 ? '' : 's'}`}
                          </div>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}

            {testsRunResult && (
              <div className={`text-xs px-3 py-2 rounded ${
                testsRunResult.failed === 0
                  ? 'bg-emerald-50 text-emerald-800 border border-emerald-200'
                  : 'bg-amber-50 text-amber-800 border border-amber-200'
              }`}>
                Ran at {new Date(testsRunResult.ran_at).toLocaleTimeString()} ·{' '}
                {testsRunResult.passed} passed · {testsRunResult.failed} failed
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function previewSummary(preview: PreviewResponse | null): string {
  if (!preview) return '100-row preview';
  if (preview.kind === 'rows') {
    return preview.row_count != null
      ? `${preview.row_count.toLocaleString()} rows`
      : '100-row preview';
  }
  if (preview.document_kind === 'too_large') return 'Too large to preview';
  if (preview.document_kind === 'invalid') return 'Invalid JSON';
  return preview.document_kind === 'array' ? 'JSON array' : 'JSON object';
}

// ── Rows preview ───────────────────────────────────────────────────────

// Numeric column types render right-aligned + monospace tabular so the
// digits line up; text / date columns use the normal proportional font,
// which reads far better than monospace for names, emails, and titles.
function isNumericType(type: string): boolean {
  // No word boundaries on purpose: DuckDB reports "INTEGER", "BIGINT",
  // "HUGEINT", etc. — `\bINT\b` would MISS "INTEGER". Substring match,
  // same as TypeChip's numeric branch, so colour + alignment agree.
  return /INT|DOUBLE|FLOAT|DECIMAL|NUMERIC|REAL|SERIAL/.test(
    (type || '').toUpperCase(),
  );
}

function PreviewTable({ preview }: { preview: RowsPreview }) {
  if (preview.rows.length === 0) {
    return <div className="p-8 text-sm text-slate-500 text-center">File is empty.</div>;
  }
  const lastColIdx = preview.columns.length - 1;
  return (
    <div className="overflow-auto">
      <table className="text-xs w-full border-collapse">
        {/* Sticky thead — light but with structure. Vertical column rules
            on every cell except the last give the spreadsheet feel; the
            soft gradient + border-b-2 keep the depth from going dark. */}
        <thead className="sticky top-0 z-10 bg-gradient-to-b from-slate-100 to-slate-50 border-b-2 border-slate-200 shadow-[0_2px_4px_-2px_rgba(15,23,42,0.08)]">
          <tr>
            {preview.columns.map((c, idx) => (
              <th
                key={c.name}
                className={`px-4 py-2.5 whitespace-nowrap align-bottom ${
                  isNumericType(c.type) ? 'text-right w-[1%]' : 'text-left'
                } ${idx < lastColIdx ? 'border-r border-slate-300' : ''}`}
              >
                <div className="text-[11px] font-bold text-slate-800 uppercase tracking-wide">
                  {c.name}
                </div>
                <div className="mt-1">
                  <TypeChip type={c.type} />
                </div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="bg-white">
          {preview.rows.map((row, i) => (
            <tr
              key={i}
              className={`border-b border-slate-200 last:border-b-0 hover:bg-amber-50 transition-colors ${
                i % 2 === 1 ? 'bg-slate-50' : 'bg-white'
              }`}
            >
              {preview.columns.map((c, idx) => (
                <td
                  key={c.name}
                  className={`px-4 py-2 whitespace-nowrap text-[12px] max-w-[280px] truncate ${
                    isNumericType(c.type)
                      ? 'text-right tabular-nums font-mono text-slate-700 w-[1%]'
                      : 'text-left text-slate-800'
                  } ${idx < lastColIdx ? 'border-r border-slate-200' : ''}`}
                  title={
                    row[c.name] === null || row[c.name] === undefined
                      ? ''
                      : String(row[c.name])
                  }
                >
                  {renderCell(row[c.name])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Small colour-coded chip for a DuckDB/Parquet column type. Mapping
// targets readability over exhaustiveness — numeric / text / time /
// boolean / json get distinct tints; anything else falls back to slate.
function TypeChip({ type }: { type: string }) {
  const t = (type || '').toUpperCase();
  let tone = 'bg-slate-100 text-slate-600 border-slate-200';
  if (/INT|BIGINT|DOUBLE|FLOAT|DECIMAL|NUMERIC|SMALLINT|TINYINT/.test(t)) {
    tone = 'bg-blue-50 text-blue-700 border-blue-200';
  } else if (/VARCHAR|STRING|TEXT|CHAR/.test(t)) {
    tone = 'bg-violet-50 text-violet-700 border-violet-200';
  } else if (/DATE|TIME|TIMESTAMP/.test(t)) {
    tone = 'bg-emerald-50 text-emerald-700 border-emerald-200';
  } else if (/BOOL/.test(t)) {
    tone = 'bg-amber-50 text-amber-800 border-amber-200';
  } else if (/JSON|STRUCT|MAP|LIST|ARRAY/.test(t)) {
    tone = 'bg-rose-50 text-rose-700 border-rose-200';
  }
  return (
    <span
      className={`inline-flex items-center px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider rounded border ${tone}`}
    >
      {t || 'UNKNOWN'}
    </span>
  );
}

// ── Schema panel ───────────────────────────────────────────────────────

function SchemaTable({ schema }: { schema: SchemaResponse }) {
  return (
    <table className="text-sm w-full bg-white border-collapse">
      <thead className="sticky top-0 z-10 bg-gradient-to-b from-slate-100 to-slate-50 border-b-2 border-slate-200 shadow-[0_2px_4px_-2px_rgba(15,23,42,0.08)]">
        <tr>
          <th className="text-left px-4 py-3 text-[11px] font-bold uppercase tracking-wide text-slate-800 w-12 border-r border-slate-300">
            #
          </th>
          <th className="text-left px-4 py-3 text-[11px] font-bold uppercase tracking-wide text-slate-800 border-r border-slate-300">
            Column
          </th>
          <th className="text-left px-4 py-3 text-[11px] font-bold uppercase tracking-wide text-slate-800 border-r border-slate-300">
            Type
          </th>
          <th className="text-left px-4 py-3 text-[11px] font-bold uppercase tracking-wide text-slate-800">
            Sample
          </th>
        </tr>
      </thead>
      <tbody>
        {schema.columns.map((c, i) => (
          <tr
            key={c.name}
            className={`border-b border-slate-200 last:border-b-0 hover:bg-amber-50/50 transition-colors ${
              i % 2 === 1 ? 'bg-slate-50/50' : 'bg-white'
            }`}
          >
            <td className="px-4 py-2 text-xs text-slate-400 tabular-nums border-r border-slate-200">{c.ordinal}</td>
            <td className="px-4 py-2 font-semibold text-slate-900 font-mono text-xs border-r border-slate-200">{c.name}</td>
            <td className="px-4 py-2 border-r border-slate-200">
              <TypeChip type={c.type} />
            </td>
            <td className="px-4 py-2 text-xs text-slate-600 font-mono truncate max-w-[280px]">
              {renderCell(c.sample)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ── JSON document tree ────────────────────────────────────────────────

function DocumentPreviewView({ preview }: { preview: DocumentPreview }) {
  if (preview.document_kind === 'too_large' || preview.document_kind === 'invalid') {
    return (
      <div className="p-8">
        <div className="bg-white rounded-lg border border-slate-200/60 px-4 py-6 text-center">
          <div className="text-sm font-medium text-slate-700">
            {preview.document_kind === 'too_large'
              ? 'File is too large to preview as a JSON document.'
              : 'JSON could not be parsed.'}
          </div>
          {preview.message && (
            <div className="text-xs text-slate-500 mt-2 font-mono">{preview.message}</div>
          )}
        </div>
      </div>
    );
  }
  return (
    <div className="p-4">
      <div className="bg-white rounded-lg border border-slate-200/60 p-4 font-mono text-[12px] leading-relaxed text-slate-800 overflow-auto">
        <JsonNode value={preview.document} depth={0} />
      </div>
    </div>
  );
}

function JsonNode({ value, depth }: { value: unknown; depth: number }) {
  const [open, setOpen] = useState(depth < 2);
  if (value === null) return <span className="text-slate-400">null</span>;
  if (typeof value === 'string')
    return <span className="text-emerald-700">"{value}"</span>;
  if (typeof value === 'number')
    return <span className="text-blue-700">{value}</span>;
  if (typeof value === 'boolean')
    return <span className="text-purple-700">{String(value)}</span>;

  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-slate-400">[]</span>;
    return (
      <span>
        <button
          onClick={() => setOpen((o) => !o)}
          className="text-slate-500 hover:text-slate-900 mr-1 font-mono text-[10px] align-middle"
        >
          {open ? '▼' : '▶'}
        </button>
        <span className="text-slate-400">
          [ {value.length} item{value.length === 1 ? '' : 's'} ]
        </span>
        {open && (
          <ul className="border-l border-slate-200 pl-3 ml-2 mt-1 space-y-1">
            {value.map((v, i) => (
              <li key={i}>
                <span className="text-slate-400 mr-1">{i}:</span>
                <JsonNode value={v} depth={depth + 1} />
              </li>
            ))}
          </ul>
        )}
      </span>
    );
  }

  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return <span className="text-slate-400">{'{}'}</span>;
    return (
      <span>
        <button
          onClick={() => setOpen((o) => !o)}
          className="text-slate-500 hover:text-slate-900 mr-1 font-mono text-[10px] align-middle"
        >
          {open ? '▼' : '▶'}
        </button>
        <span className="text-slate-400">
          {'{ '}{entries.length} key{entries.length === 1 ? '' : 's'}{' }'}
        </span>
        {open && (
          <ul className="border-l border-slate-200 pl-3 ml-2 mt-1 space-y-1">
            {entries.map(([k, v]) => (
              <li key={k}>
                <span className="text-rose-700 mr-1">"{k}":</span>
                <JsonNode value={v} depth={depth + 1} />
              </li>
            ))}
          </ul>
        )}
      </span>
    );
  }

  return <span>{String(value)}</span>;
}

function renderCell(v: unknown): string {
  if (v === null || v === undefined) return '∅';
  if (typeof v === 'string') return v.length > 100 ? `${v.slice(0, 100)}…` : v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  if (Array.isArray(v) || typeof v === 'object') {
    try {
      const s = JSON.stringify(v);
      return s.length > 100 ? `${s.slice(0, 100)}…` : s;
    } catch {
      return String(v);
    }
  }
  return String(v);
}
