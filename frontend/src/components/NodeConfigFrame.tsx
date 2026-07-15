/**
 * NodeConfigFrame — the standard node-config shell (2026-06-16).
 *
 * Wraps a node's existing Processing form with consistent "Data In" and
 * "Data Out" bands so every node reads the same way:
 *
 *     Data In  →  Processing (children)  →  Data Out   [ Settings = Advanced tab ]
 *
 * The bands are derived in utils/nodeUiContract from the live registry, so
 * they stay truthful without per-node hand-maintenance. This is the rollout
 * shell — apply it node-by-node (piloted on Join) by wrapping the node's
 * config component; the Processing form itself is unchanged.
 */
import { ReactNode } from 'react';
import type { DataInDescriptor, DataOutDescriptor } from '../utils/nodeUiContract';

function ColumnChips({ columns, max = 8 }: { columns: string[]; max?: number }) {
  if (columns.length === 0) return null;
  return (
    <div className="mt-0.5 flex flex-wrap gap-1">
      {columns.slice(0, max).map((c) => (
        <span key={c} className="font-mono text-[10px] px-1 py-0.5 rounded bg-white border border-slate-200 text-slate-500">
          {c}
        </span>
      ))}
      {columns.length > max && (
        <span className="text-[10px] text-slate-400 self-center">+{columns.length - max} more</span>
      )}
    </div>
  );
}

export function DataInBand({ d }: { d: DataInDescriptor }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-slate-50/60 px-3 py-2.5">
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[11px] font-bold uppercase tracking-wider text-slate-500">Data In</span>
        <span className="text-[11px] text-slate-400">{d.note}</span>
      </div>
      {d.ports.length === 0 ? (
        <p className="text-xs text-slate-400">{d.note}</p>
      ) : (
        <div className="space-y-1.5">
          {d.ports.map((p, i) => (
            <div key={`${p.role}-${i}`} className="flex items-start gap-2 text-xs">
              <span className="shrink-0 px-1.5 py-0.5 rounded bg-slate-200 text-slate-600 font-semibold">
                {p.role}{p.required ? ' *' : ''}
              </span>
              {p.connected ? (
                <div className="min-w-0">
                  <span className="font-semibold text-slate-700">{p.label || '(upstream)'}</span>
                  <span className="text-slate-400"> · {p.columns.length} col{p.columns.length === 1 ? '' : 's'}</span>
                  <ColumnChips columns={p.columns} />
                </div>
              ) : (
                <span className="text-slate-400 italic self-center">
                  {p.required ? 'Not connected — connect an upstream node' : 'Optional — not connected'}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

// Output-kind chip styling — gives each disposition a distinct, readable
// color so dataset / variable / report / branch / write / terminal are
// visually separable at a glance (not just prose). Text uses the dark stop
// of each ramp for contrast.
const DISPOSITION_CHIP: Record<string, { label: string; bg: string; fg: string }> = {
  rows:        { label: 'Dataset',          bg: '#E6F1FB', fg: '#0C447C' },
  transformed: { label: 'Dataset (result)', bg: '#E6F1FB', fg: '#0C447C' },
  variable:    { label: 'Variable',         bg: '#E1F5EE', fg: '#0F6E56' },
  report:      { label: 'Report',           bg: '#EEEDFE', fg: '#3C3489' },
  branches:    { label: 'Branches',         bg: '#FBEAF0', fg: '#72243E' },
  passthrough: { label: 'Writes externally', bg: '#FAEEDA', fg: '#633806' },
  terminal:    { label: 'Terminal',         bg: '#FCEBEB', fg: '#791F1F' },
  control:     { label: 'Control flow',     bg: '#F1EFE8', fg: '#444441' },
};

export function DataOutBand({ d }: { d: DataOutDescriptor }) {
  const chip = DISPOSITION_CHIP[d.disposition] || { label: d.disposition, bg: '#F1EFE8', fg: '#444441' };
  return (
    <section className="rounded-lg border border-slate-200 bg-slate-50/60 px-3 py-2.5">
      <div className="flex items-center justify-between mb-1">
        <span className="text-[11px] font-bold uppercase tracking-wider text-slate-500">Data Out</span>
        <span
          className="text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded"
          style={{ background: chip.bg, color: chip.fg }}
        >
          {chip.label}
        </span>
      </div>
      {d.sideEffect && (
        <div className="mb-1.5 flex items-start gap-1.5 rounded-md border border-amber-300 bg-amber-50 px-2 py-1.5">
          <span aria-hidden className="mt-px text-sm leading-none text-amber-600">⚠</span>
          <p className="text-[11px] leading-snug text-amber-800">
            <span className="font-bold uppercase tracking-wider text-amber-700">Side effect</span>
            {d.sideEffectNote ? ` · ${d.sideEffectNote}` : ''} — not a row transform; preview, retry and resume can have real-world consequences.
          </p>
        </div>
      )}
      <p className="text-xs text-slate-600">{d.summary}</p>
      {d.ports.length > 1 && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {d.ports.map((p) => (
            <span
              key={p.id}
              className="text-[11px] px-1.5 py-0.5 rounded font-semibold"
              style={{ background: `${p.color || '#94a3b8'}22`, color: p.color || '#64748b' }}
            >
              {p.label}
            </span>
          ))}
        </div>
      )}
      {d.columns.length > 0 && (
        <div className="mt-1.5">
          <div className="text-[10px] uppercase tracking-wider text-slate-400 mb-0.5">
            Output columns ({d.columns.length})
          </div>
          <ColumnChips columns={d.columns} max={12} />
        </div>
      )}
      {d.columns.length === 0 && d.schemaDynamic && (
        <p className="mt-1.5 text-[11px] italic text-slate-500">
          Output columns are data-dependent — known after the first run.
        </p>
      )}
    </section>
  );
}

export default function NodeConfigFrame({
  dataIn,
  dataOut,
  children,
}: {
  dataIn: DataInDescriptor;
  dataOut: DataOutDescriptor;
  children: ReactNode;
}) {
  return (
    <div className="space-y-3">
      <DataInBand d={dataIn} />
      {/* Processing — the node's own config form, unchanged */}
      <div className="space-y-3">{children}</div>
      <DataOutBand d={dataOut} />
    </div>
  );
}
