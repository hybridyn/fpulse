/**
 * Promote-to-table dialog (2026-05-23, Y5 + Y7 restyle).
 *
 * Modal flow: pick target schema (existing or new) + table name, then
 * POST /api/storage/promote-to-table. The backend reads the source
 * file with DuckDB, writes a Parquet table under
 * tables/{ws}/{schema}/{name}/part-000.parquet, and creates the
 * storage_tables + storage_columns metadata rows.
 *
 * Theme match — rounded-2xl card, slate-200 border, shadow-2xl
 * backdrop. Primary action uses the canonical blue gradient
 * (#3B7DD8 → #1E5AAF). Field inputs follow the slate-300 border +
 * slate-50/70 focus-ring pattern shared with ConnectionsPage and
 * SettingsPage.
 */

import { useState } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';

interface StorageObject {
  id: string;
  name: string;
  format: string | null;
}

interface StorageTable {
  id: string;
  schema_name: string;
  name: string;
  row_count: number;
  column_count: number;
}

const INPUT_CLASS =
  'w-full text-sm rounded-lg border border-slate-300 bg-white px-3 py-2 ' +
  'focus:outline-none focus:ring-2 focus:ring-blue-500/40 focus:border-blue-500 ' +
  'transition-colors';

export default function StoragePromoteDialog({
  object,
  existingSchemas,
  onClose,
  onPromoted,
}: {
  object: StorageObject;
  existingSchemas: string[];
  onClose: () => void;
  onPromoted: (table: StorageTable) => void;
}) {
  const schemaOptions = existingSchemas.length ? existingSchemas : ['default'];
  const [schema, setSchema] = useState(schemaOptions[0]);
  const [creatingNewSchema, setCreatingNewSchema] = useState(false);
  const [newSchema, setNewSchema] = useState('');
  const [tableName, setTableName] = useState(() => {
    const base = (object.name || '').replace(/\.[^.]+$/, '').trim();
    return base
      .toLowerCase()
      .replace(/[^a-z0-9_]+/g, '_')
      .replace(/^_+|_+$/g, '');
  });
  const [columnRenames, setColumnRenames] = useState('');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);

  const targetSchema = creatingNewSchema ? newSchema.trim() : schema;
  const canSubmit = Boolean(targetSchema && tableName.trim() && !busy);

  const onSubmit = async () => {
    if (!targetSchema) {
      toast.error('Schema is required.');
      return;
    }
    if (!tableName.trim()) {
      toast.error('Table name is required.');
      return;
    }
    const columnMap: Record<string, string> = {};
    for (const pair of columnRenames.split(',')) {
      const trimmed = pair.trim();
      if (!trimmed) continue;
      const [src, dst] = trimmed.split(':').map((s) => s.trim());
      if (!src || !dst) {
        toast.error(`Rename pair must be "old:new", got "${trimmed}"`);
        return;
      }
      columnMap[src] = dst;
    }
    setBusy(true);
    try {
      const result = await api.post<StorageTable>('/api/storage/promote-to-table', {
        object_id: object.id,
        schema_name: targetSchema,
        table_name: tableName.trim(),
        description,
        column_map: columnMap,
        tags: [],
      });
      toast.success(
        `Created ${result.schema_name}.${result.name} (${result.row_count.toLocaleString()} rows)`,
      );
      onPromoted(result);
    } catch (err) {
      toast.error(`Promote failed: ${(err as Error).message || err}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="bg-white rounded-2xl shadow-2xl border border-slate-200/60 w-[540px] max-w-[95vw] overflow-hidden">
        {/* Header */}
        <div className="px-6 py-4 border-b border-slate-200/70 bg-gradient-to-b from-slate-50 to-white">
          <h2 className="text-lg font-bold text-slate-900 flex items-center gap-2">
            <svg
              width="20" height="20" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
              className="text-blue-500"
            >
              <ellipse cx="12" cy="5" rx="9" ry="3" />
              <path d="M3 5v6c0 1.66 4.03 3 9 3s9-1.34 9-3V5" />
              <path d="M3 11v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6" />
            </svg>
            Promote to managed table
          </h2>
          <p className="text-xs text-slate-500 mt-1">
            From <span className="font-mono text-slate-700">{object.name}</span>
          </p>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-4">
          <Field label="Schema">
            {!creatingNewSchema ? (
              <div className="flex gap-2">
                <select
                  value={schema}
                  onChange={(e) => setSchema(e.target.value)}
                  className={INPUT_CLASS + ' flex-1'}
                >
                  {schemaOptions.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() => setCreatingNewSchema(true)}
                  className="px-3 py-2 text-xs font-medium rounded-lg border border-slate-200 bg-white text-slate-600 hover:bg-slate-50 whitespace-nowrap transition-colors"
                >
                  + New schema
                </button>
              </div>
            ) : (
              <div className="flex gap-2">
                <input
                  value={newSchema}
                  onChange={(e) => setNewSchema(e.target.value)}
                  placeholder="e.g. sales, hr, finance"
                  className={INPUT_CLASS + ' flex-1'}
                  autoFocus
                />
                <button
                  type="button"
                  onClick={() => setCreatingNewSchema(false)}
                  className="px-3 py-2 text-xs font-medium rounded-lg text-slate-500 hover:bg-slate-100 transition-colors"
                >
                  Cancel
                </button>
              </div>
            )}
          </Field>

          <Field label="Table name">
            <input
              value={tableName}
              onChange={(e) => setTableName(e.target.value)}
              placeholder="customers"
              className={INPUT_CLASS}
            />
            <p className="text-xs text-slate-500 mt-1">
              Reference as{' '}
              <code className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-700 text-xs font-mono">
                {targetSchema || 'default'}.{tableName || 'name'}
              </code>{' '}
              from local_table_source / sink.
            </p>
          </Field>

          <Field label="Description (optional)">
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Notes about this table"
              className={INPUT_CLASS}
            />
          </Field>

          <Field label="Column renames (optional)">
            <input
              value={columnRenames}
              onChange={(e) => setColumnRenames(e.target.value)}
              placeholder="Customer Id:customer_id, Order Date:order_date"
              className={INPUT_CLASS + ' font-mono'}
            />
            <p className="text-xs text-slate-500 mt-1">
              Comma-separated{' '}
              <code className="px-1 rounded bg-slate-100 text-slate-700">old:new</code> pairs. Empty
              = keep source column names verbatim.
            </p>
          </Field>
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-slate-200/70 flex justify-end gap-2 bg-slate-50/60">
          <button
            onClick={onClose}
            disabled={busy}
            className="px-4 py-2 text-sm font-medium rounded-lg text-slate-600 hover:bg-slate-200/70 transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={onSubmit}
            disabled={!canSubmit}
            className="px-4 py-2 text-white text-sm font-bold rounded-lg transition-all shadow-sm hover:shadow-md disabled:opacity-50 disabled:cursor-not-allowed"
            style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
          >
            {busy ? 'Promoting…' : 'Promote to table'}
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-sm font-semibold text-slate-700 mb-1.5">{label}</label>
      {children}
    </div>
  );
}
