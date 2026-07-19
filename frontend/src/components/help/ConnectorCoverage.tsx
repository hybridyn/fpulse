import { useEffect, useState } from 'react';

/**
 * Live connector-coverage strip for the Node Reference page.
 *
 * The Source / Destination cards on that page are a curated, user-facing
 * teaching list (CSV / JSON / Database / REST …) — intentionally NOT a raw
 * dump of the 100+ internal step-types or connector providers. This strip
 * pulls the *live* connector catalog (`/api/v1/catalog/connectors`) so the
 * headline numbers + certification mix never drift from what actually
 * ships, and links to the full catalog + cert matrix.
 *
 * Fails closed: on any fetch / shape error it renders nothing, so the
 * curated cards always stand on their own.
 */

type Maturity = Partial<
  Record<'production' | 'certified' | 'configurable' | 'form_only' | 'declared_only', number>
>;

interface ConnectorCatalog {
  count: number;
  categories: string[];
  maturity_summary: Maturity;
}

const TIERS: Array<{ key: keyof Maturity; label: string; cls: string }> = [
  { key: 'production',    label: 'Production',   cls: 'bg-emerald-50 text-emerald-700 border-emerald-200' },
  { key: 'certified',     label: 'Certified',    cls: 'bg-blue-50 text-blue-700 border-blue-200' },
  { key: 'configurable',  label: 'Configurable', cls: 'bg-violet-50 text-violet-700 border-violet-200' },
  { key: 'form_only',     label: 'Form-only',    cls: 'bg-amber-50 text-amber-700 border-amber-200' },
  { key: 'declared_only', label: 'Declared',     cls: 'bg-slate-50 text-slate-600 border-slate-200' },
];

export default function ConnectorCoverage() {
  const [data, setData] = useState<ConnectorCatalog | null>(null);

  useEffect(() => {
    let alive = true;
    fetch('/api/v1/catalog/connectors')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d: ConnectorCatalog) => { if (alive) setData(d); })
      .catch(() => { /* fail closed — the curated cards remain */ });
    return () => { alive = false; };
  }, []);

  if (!data || typeof data.count !== 'number') return null;

  const maturity = data.maturity_summary || {};
  const categoryCount = Array.isArray(data.categories) ? data.categories.length : 0;

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <div className="text-sm font-bold text-slate-800">
            {data.count} connector types across {categoryCount} categories
          </div>
          <div className="text-xs text-slate-500 mt-0.5">
            The Source / Destination cards below are the user-facing families. Live certification mix:
          </div>
        </div>
        <a
          href="#cert-matrix"
          className="text-xs font-semibold text-pipe-600 hover:text-pipe-700 whitespace-nowrap"
        >
          Full catalog + cert matrix &rarr;
        </a>
      </div>
      <div className="flex items-center gap-2 mt-2 flex-wrap">
        {TIERS.map((t) => (
          <span key={t.key} className={`text-xs font-bold px-2 py-1 rounded border ${t.cls}`}>
            {maturity[t.key] ?? 0} {t.label}
          </span>
        ))}
      </div>
    </div>
  );
}
