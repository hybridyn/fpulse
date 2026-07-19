/**
 * Managed table — Edit + Rename dialog (Z22 + Z23, 2026-05-23).
 *
 * Lets users update metadata AND rename managed Parquet tables. Backed
 * by PATCH /api/storage/tables/{id} which handles both:
 *
 *   description / tags          → pure metadata (no disk touch)
 *   schema_name / table_name    → rename. Moves bytes on disk +
 *                                 updates the index row + returns
 *                                 a stale_consumers list of pipelines
 *                                 still pointing at the OLD name.
 *
 * Rename pre-flight: when the user types a new name AND the table has
 * consumer pipelines, an amber warning appears listing them. The
 * consumers do NOT auto-rewrite — user opens each one and updates the
 * sink target manually. The list lets them not miss any.
 */

import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';

interface StorageTable {
  id: string;
  schema_name: string;
  name: string;
  description: string;
  tags: string[];
}

interface UsageRef {
  workflow_id: string;
  name: string;
  role?: string;
}

interface SourceFile {
  id: string;
  name: string;
  format: string | null;
  size_bytes: number;
}

type NavTarget =
  | { kind: 'file'; id: string }
  | { kind: 'pipeline'; workflow_id: string };

interface Props {
  table: StorageTable;
  /** Pipelines that currently reference this table (any role). Used to
   *  surface a warning when the user changes schema/name — those refs
   *  will fail on next run until updated. Z24: also used for the
   *  "Provenance → written by" line (filtered to role=sink). */
  consumers?: UsageRef[];
  /** Z24 (2026-05-23) — Source file resolved from table.created_from_object_id.
   *  Set when the table was created via Storage → Files → Promote.
   *  Null when the table was written by a pipeline's local_table_sink. */
  sourceFile?: SourceFile | null;
  onClose: () => void;
  onSaved: (updated: StorageTable) => void;
  /** Optional navigation callback so Provenance links can take the user
   *  to the source file row OR the writer pipeline in the Editor. */
  onNavigate?: (target: NavTarget) => void;
}

// Identifier validation — mirrors the backend's safe_schema_or_table_name.
// Lowercase letters, digits, underscore. Frontend pre-check so the user
// gets immediate feedback before they hit Save.
const VALID_IDENT = /^[a-z][a-z0-9_]*$/;

function validateIdent(value: string, label: string): string | null {
  const v = value.trim();
  if (!v) return `${label} is required`;
  if (!VALID_IDENT.test(v)) {
    return `${label} must start with a lowercase letter and contain only lowercase letters, digits, or underscores`;
  }
  return null;
}

export default function StorageTableEditDialog({
  table,
  consumers = [],
  sourceFile = null,
  onClose,
  onSaved,
  onNavigate,
}: Props) {
  // Z24 — derive writer pipelines from the consumers list. Sinks are
  // the workflows that PRODUCED this table; sources are downstream
  // readers. Both directions are useful but Provenance is about origin,
  // so we list sinks first under "Written by" and dim the rest.
  const writerPipelines = consumers.filter((c) => c.role === 'sink');
  const [description, setDescription] = useState(table.description || '');
  const [tagsText, setTagsText] = useState((table.tags || []).join(', '));
  const [schemaName, setSchemaName] = useState(table.schema_name);
  const [tableName, setTableName] = useState(table.name);
  const [saving, setSaving] = useState(false);

  // Escape closes
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  const willRename =
    schemaName.trim() !== table.schema_name ||
    tableName.trim() !== table.name;

  const schemaError = willRename ? validateIdent(schemaName, 'Schema') : null;
  const nameError = willRename ? validateIdent(tableName, 'Table name') : null;
  const hasIdentError = !!(schemaError || nameError);

  const onSave = async () => {
    if (hasIdentError) return;
    setSaving(true);
    try {
      // Parse tags from the comma-separated input
      const tags = tagsText
        .split(',')
        .map((t) => t.trim())
        .filter(Boolean);
      const body: Record<string, unknown> = {
        description: description.trim(),
        tags,
      };
      if (willRename) {
        body.schema_name = schemaName.trim();
        body.table_name = tableName.trim();
      }
      const updated = await api.patch<StorageTable & { stale_consumers?: UsageRef[] }>(
        `/api/storage/tables/${table.id}`,
        body,
      );
      const stale = updated.stale_consumers || [];
      if (willRename) {
        toast.success(
          `Renamed to ${updated.schema_name}.${updated.name}`,
          stale.length > 0
            ? `${stale.length} pipeline${stale.length === 1 ? '' : 's'} still reference the old name and will fail on next run until updated: ${stale.slice(0, 3).map((r) => r.name).join(', ')}${stale.length > 3 ? ` (+${stale.length - 3} more)` : ''}`
            : 'No pipelines referenced this table — nothing else to update.',
        );
      } else {
        toast.success(`Updated ${updated.schema_name}.${updated.name}`);
      }
      onSaved(updated);
      onClose();
    } catch (err) {
      toast.error(`Save failed: ${(err as Error).message || err}`);
    } finally {
      setSaving(false);
    }
  };

  const dirty =
    willRename ||
    description.trim() !== (table.description || '').trim() ||
    tagsText.trim() !== (table.tags || []).join(', ').trim();

  return (
    <div
      role="dialog"
      aria-label={`Edit ${table.schema_name}.${table.name}`}
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="w-[520px] max-w-[95vw] bg-white rounded-xl border border-slate-200 shadow-2xl flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start gap-3 px-6 py-4 border-b border-slate-200 bg-gradient-to-r from-emerald-50/60 via-white to-slate-50">
          <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-emerald-100 to-emerald-50 border border-emerald-200/70 flex items-center justify-center shrink-0 shadow-sm">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-emerald-600">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-base font-semibold text-slate-900">Edit table metadata</div>
            <div className="text-xs text-slate-500 mt-0.5 truncate font-mono">
              {table.schema_name}.{table.name}
            </div>
          </div>
          <button
            onClick={onClose}
            className="w-8 h-8 inline-flex items-center justify-center rounded-md text-slate-400 hover:text-slate-700 hover:bg-white border border-transparent hover:border-slate-200 transition-colors"
            aria-label="Close"
            title="Close (Esc)"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-4 space-y-4">
          {/* Z24 — Provenance. Read-only. Shows where the table came
              from so the user can answer "what file or pipeline produced
              this?" without leaving the dialog. Two possible origins:
                (a) Promoted from a file (created_from_object_id link)
                (b) Written by a pipeline's local_table_sink (usage scanner)
              Both can be present (a file promoted to a table, then a
              pipeline overwrites it). Neither = "unknown" with a hint. */}
          {(sourceFile || writerPipelines.length > 0) ? (
            <div className="rounded-lg border border-slate-200 bg-slate-50/60 px-3 py-2.5 space-y-2">
              <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500">
                Provenance
              </div>
              {sourceFile && (
                <div className="flex items-start gap-2 text-xs">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-amber-600 shrink-0 mt-0.5">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                    <polyline points="14 2 14 8 20 8" />
                  </svg>
                  <div className="flex-1 min-w-0">
                    <div className="text-slate-600">
                      Promoted from file{' '}
                      <button
                        type="button"
                        onClick={() => onNavigate?.({ kind: 'file', id: sourceFile.id })}
                        className="font-mono font-semibold text-slate-800 hover:text-amber-700 underline decoration-dotted underline-offset-2"
                      >
                        {sourceFile.name}
                      </button>
                    </div>
                    {sourceFile.format && (
                      <div className="text-[10px] text-slate-400 uppercase tracking-wider mt-0.5">
                        {sourceFile.format}
                      </div>
                    )}
                  </div>
                </div>
              )}
              {writerPipelines.length > 0 && (
                <div className="flex items-start gap-2 text-xs">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-emerald-600 shrink-0 mt-0.5">
                    <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
                  </svg>
                  <div className="flex-1 min-w-0">
                    <div className="text-slate-600 mb-0.5">
                      Written by {writerPipelines.length === 1 ? 'pipeline' : `${writerPipelines.length} pipelines`}
                    </div>
                    <ul className="space-y-0.5">
                      {writerPipelines.map((w) => (
                        <li key={w.workflow_id}>
                          <button
                            type="button"
                            onClick={() => onNavigate?.({ kind: 'pipeline', workflow_id: w.workflow_id })}
                            className="font-semibold text-slate-800 hover:text-emerald-700 underline decoration-dotted underline-offset-2 text-left"
                          >
                            {w.name}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="rounded-lg border border-slate-200 bg-slate-50/60 px-3 py-2.5">
              <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-1">
                Provenance
              </div>
              <div className="text-xs text-slate-500">
                Origin unknown — this table wasn't promoted from a file in this workspace, and no saved pipeline writes to it yet.
              </div>
            </div>
          )}

          {/* Rename — schema + table name. Renames move the bytes on
              disk and update the index row. Consumer pipelines are NOT
              auto-rewritten; the warning panel below appears once the
              user changes either field. */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs font-semibold uppercase tracking-wide text-slate-700">
                Schema
              </label>
              <input
                type="text"
                value={schemaName}
                onChange={(e) => setSchemaName(e.target.value)}
                className={`mt-1 w-full px-3 py-2 text-sm font-mono rounded-md border focus:outline-none focus:ring-2 ${
                  schemaError
                    ? 'border-red-300 focus:ring-red-300 focus:border-red-400'
                    : 'border-slate-300 focus:ring-emerald-300 focus:border-emerald-400'
                }`}
              />
              {schemaError && (
                <div className="mt-1 text-[11px] text-red-600">{schemaError}</div>
              )}
            </div>
            <div>
              <label className="text-xs font-semibold uppercase tracking-wide text-slate-700">
                Table name
              </label>
              <input
                type="text"
                value={tableName}
                onChange={(e) => setTableName(e.target.value)}
                className={`mt-1 w-full px-3 py-2 text-sm font-mono rounded-md border focus:outline-none focus:ring-2 ${
                  nameError
                    ? 'border-red-300 focus:ring-red-300 focus:border-red-400'
                    : 'border-slate-300 focus:ring-emerald-300 focus:border-emerald-400'
                }`}
              />
              {nameError && (
                <div className="mt-1 text-[11px] text-red-600">{nameError}</div>
              )}
            </div>
          </div>

          {/* Rename warning — fires once the user touches schema or name
              AND consumer pipelines exist. Auto-rewriting their sink
              params is too risky (sandbox/published/PROD considerations),
              so the user updates them manually using the list below. */}
          {willRename && consumers.length > 0 && (
            <div className="text-[12px] text-amber-800 leading-relaxed bg-amber-50 border border-amber-300 rounded-lg px-3 py-2.5">
              <div className="font-bold flex items-center gap-1.5 mb-1">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                  <line x1="12" y1="9" x2="12" y2="13" />
                  <line x1="12" y1="17" x2="12.01" y2="17" />
                </svg>
                {consumers.length} pipeline{consumers.length === 1 ? '' : 's'} will break on next run
              </div>
              <div className="mb-1.5">
                Renaming moves the bytes on disk and updates this table's index. Pipelines that reference{' '}
                <span className="font-mono font-semibold">{table.schema_name}.{table.name}</span> still expect the old name and will fail with "table not found":
              </div>
              <ul className="list-disc pl-5 space-y-0.5 max-h-32 overflow-auto">
                {consumers.map((c) => (
                  <li key={c.workflow_id}>
                    <span className="font-medium">{c.name}</span>
                    {c.role ? <span className="text-amber-700 ml-1">({c.role})</span> : null}
                  </li>
                ))}
              </ul>
              <div className="mt-1.5 text-amber-700">
                Update each pipeline's sink to point at <span className="font-mono font-semibold">{schemaName}.{tableName}</span> after saving.
              </div>
            </div>
          )}

          <div>
            <label className="text-xs font-semibold uppercase tracking-wide text-slate-700">
              Description
            </label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What's in this table? Source, refresh cadence, owner…"
              rows={3}
              className="mt-1 w-full px-3 py-2 text-sm rounded-md border border-slate-300 focus:outline-none focus:ring-2 focus:ring-emerald-300 focus:border-emerald-400"
            />
          </div>

          <div>
            <label className="text-xs font-semibold uppercase tracking-wide text-slate-700">
              Tags
            </label>
            <input
              type="text"
              value={tagsText}
              onChange={(e) => setTagsText(e.target.value)}
              placeholder="finance, daily, gold-layer"
              className="mt-1 w-full px-3 py-2 text-sm rounded-md border border-slate-300 focus:outline-none focus:ring-2 focus:ring-emerald-300 focus:border-emerald-400"
            />
            <div className="mt-1 text-[11px] text-slate-500">
              Comma-separated. Whitespace stripped, duplicates removed.
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 px-6 py-3 border-t border-slate-200 bg-slate-50">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium rounded-lg border bg-white text-slate-600 border-slate-200 hover:bg-slate-100 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={onSave}
            disabled={saving || !dirty || hasIdentError}
            className={`px-4 py-2 text-sm font-bold rounded-lg shadow-sm transition-colors ${
              saving || !dirty || hasIdentError
                ? 'bg-slate-200 text-slate-400 cursor-not-allowed'
                : willRename
                  ? 'bg-amber-500 hover:bg-amber-600 text-white'
                  : 'bg-emerald-500 hover:bg-emerald-600 text-white'
            }`}
          >
            {saving ? 'Saving…' : willRename ? 'Rename + Save' : 'Save changes'}
          </button>
        </div>
      </div>
    </div>
  );
}
