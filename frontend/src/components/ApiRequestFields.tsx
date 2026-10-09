import { Plus, Trash2 } from 'lucide-react';

export type RequestField = { id: string; enabled: boolean; key: string; value: string };
export const blankFields = (): RequestField[] => [{ id: crypto.randomUUID(), enabled: true, key: '', value: '' }];
export function requestFields(rows: RequestField[]): Record<string, string> {
  const result: Record<string, string> = Object.create(null);
  for (const row of rows) {
    if (!row.enabled || (!row.key.trim() && !row.value)) continue;
    const key = row.key.trim();
    if (!key) throw new Error('Enter a name for every enabled request field.');
    if (Object.prototype.hasOwnProperty.call(result, key)) throw new Error(`Duplicate request field: ${key}`);
    result[key] = row.value;
  }
  return result;
}

export default function ApiRequestFields({ rows, onChange, label }: {
  rows: RequestField[]; onChange: (rows: RequestField[]) => void; label: string;
}) {
  const update = (id: string, patch: Partial<RequestField>) => onChange(rows.map(r => r.id === id ? { ...r, ...patch } : r));
  return <div className="space-y-3">
    <div className="api-field-row text-xs text-slate-500 px-1"><span /><span>Name</span><span>Value</span><span /></div>
    {rows.map((row, index) => <div key={row.id} className="api-field-row">
      <input type="checkbox" aria-label={`Enable ${label} ${index + 1}`} checked={row.enabled} onChange={e => update(row.id, { enabled: e.target.checked })} />
      <input className="api-field-input" aria-label={`${label} name ${index + 1}`} placeholder="Name" value={row.key} onChange={e => update(row.id, { key: e.target.value })} />
      <input className="api-field-input" aria-label={`${label} value ${index + 1}`} placeholder="Value" value={row.value} onChange={e => update(row.id, { value: e.target.value })} />
      <button className="api-icon-button" title={`Remove ${label} ${index + 1}`} aria-label={`Remove ${label} ${index + 1}`} onClick={() => onChange(rows.filter(r => r.id !== row.id))}><Trash2 size={15} /></button>
    </div>)}
    <button disabled={rows.length >= 40} className="inline-flex items-center gap-1.5 text-xs font-semibold text-pipe-700 disabled:opacity-40" onClick={() => onChange([...rows, ...blankFields()])}><Plus size={14} />Add {label}</button>
  </div>;
}
