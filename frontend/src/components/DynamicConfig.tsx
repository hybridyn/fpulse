import { useCallback, useState, useEffect, useMemo } from 'react';
import { autoMapSchema } from '../utils/schemaAutoMap';
import { authHeaders } from '../api/client';
import ExpressionPreview from './ExpressionPreview';

/**
 * DynamicConfig — Schema-driven node config renderer.
 *
 * Reads `param_schema` from the backend node-types registry and renders
 * a complete config form with tabs, conditional visibility (show_when),
 * and all standard field types. Used as a fallback for node types that
 * don't have a hardcoded config function in ConfigPanel.
 */

// ── Types ────────────────────────────────────────────────────────────────────

// A select option may be a plain string (legacy form) OR an object
// carrying value/label/description so the renderer can show a tooltip
// per option. ``schema_policy`` (2026-05-27) is the first field to need
// per-option help text — adding the object form lets other fields use
// it without a second schema change.
type ParamFieldOption = string | { value: string; label?: string; description?: string };

interface ParamField {
  name: string;
  type: string;
  label?: string;
  required?: boolean;
  default?: any;
  placeholder?: string;
  description?: string;
  options?: ParamFieldOption[];
  tab?: string;
  // tier: progressive disclosure for the config panel.
  //   "required"      — always visible, blocks save until filled
  //   "smart-default" — hidden by default; collapsed under a defaults chip
  //   "optional"      — hidden until added via "+ Add setting"
  // If absent, inferred: required=true → required; has default → smart-default; else → optional.
  tier?: 'required' | 'smart-default' | 'optional';
  show_when?: Record<string, any>;
  filter?: string[];
  ops?: string[];
  types?: string[];
  rows?: number;
  min?: number;
  max?: number;
  step?: number;
}

type FieldTier = 'required' | 'smart-default' | 'optional';

// 2026-06-11 — humanize a raw param name for display when the backend
// param_schema didn't supply a `label`. Many schemas omit it, which is
// why the config showed raw keys like `effective_from_column` /
// `sample_rows`. Turns "effective_from_column" → "Effective From Column".
function humanizeName(name: string): string {
  return String(name || '')
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim();
}

function inferTier(field: ParamField): FieldTier {
  if (field.tier) return field.tier;
  if (field.required) return 'required';
  // Has a meaningful default (not undefined / null / empty array / empty string)
  const d = field.default;
  const hasDefault = d !== undefined && d !== null
    && !(Array.isArray(d) && d.length === 0)
    && !(typeof d === 'string' && d === '');
  return hasDefault ? 'smart-default' : 'optional';
}

function isFieldOverridden(field: ParamField, params: Record<string, any>): boolean {
  const cur = params[field.name];
  if (cur === undefined || cur === null) return false;
  const def = field.default;
  if (Array.isArray(cur) && Array.isArray(def)) {
    if (cur.length !== def.length) return true;
    return JSON.stringify(cur) !== JSON.stringify(def);
  }
  return cur !== def && !(cur === '' && (def === undefined || def === null));
}

interface DynamicConfigProps {
  stepType: string;
  params: Record<string, any>;
  nodeId: string;
  onChange: (nodeId: string, params: Record<string, any>) => void;
  columns?: string[];
}

// ── Shared CSS classes (matches ConfigPanel primitives) ──────────────────────

const INPUT_CLS =
  'w-full px-2.5 py-1.5 text-xs text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 focus:border-pipe-300 placeholder:text-slate-400';
const TEXTAREA_CLS = `${INPUT_CLS} font-mono resize-none`;
const SELECT_CLS =
  'w-full px-2.5 py-1.5 text-xs text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300';
const LABEL_CLS = 'block text-[12px] font-semibold text-slate-700 mb-1';
const DESC_CLS = 'text-xs text-slate-400 -mt-1 mb-1';
const REQUIRED_CLS = 'text-[11px] text-red-500 mt-0.5';

/** True when a field's value counts as "not filled in" — covers empty
 *  strings, null/undefined, and empty arrays (list-type fields). */
function isFieldEmpty(value: any): boolean {
  if (value == null) return true;
  if (typeof value === 'string') return value.trim() === '';
  if (Array.isArray(value)) return value.length === 0;
  return false;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function getNodeMeta(stepType: string): { param_schema: ParamField[]; default_params: Record<string, any> } | null {
  try {
    const all: any[] = (window as any).__fpulse_node_types || [];
    const meta = all.find((t) => t.type === stepType);
    if (meta) return { param_schema: meta.param_schema || [], default_params: meta.default_params || {} };
  } catch {}
  return null;
}

/** Check if a field's show_when condition is met */
function isVisible(field: ParamField, params: Record<string, any>): boolean {
  if (!field.show_when) return true;
  return Object.entries(field.show_when).every(([key, expected]) => {
    const val = params[key];
    if (Array.isArray(expected)) return expected.includes(val);
    return val === expected;
  });
}

// ── Connection Picker (inline — same as ConfigPanel) ─────────────────────────

function DynConnectionPicker({ value, onChange, filter }: { value: string; onChange: (v: string) => void; filter?: string[] }) {
  const [connections, setConnections] = useState<any[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!loaded) {
      fetch('/api/connections')
        .then((r) => r.ok ? r.json() : [])
        .then((data) => {
          const list = Array.isArray(data) ? data : data?.items || [];
          const filtered = filter ? list.filter((c: any) => filter.includes(c.type)) : list;
          setConnections(filtered);
          setLoaded(true);
        })
        .catch(() => setLoaded(true));
    }
  }, [loaded, filter]);

  return (
    <select value={value || ''} onChange={(e) => onChange(e.target.value)} className={SELECT_CLS}>
      <option value="">— No connection —</option>
      {connections.map((c: any) => (
        <option key={c.id} value={c.id}>{c.name} ({c.type})</option>
      ))}
    </select>
  );
}

// ── Column Picker (checkboxes for column_list, select for single column) ────

function DynColumnSelect({ value, onChange, columns }: { value: string; onChange: (v: string) => void; columns: string[] }) {
  return (
    <select value={value || ''} onChange={(e) => onChange(e.target.value)} className={SELECT_CLS}>
      <option value="">— Select column —</option>
      {columns.map((c) => <option key={c} value={c}>{c}</option>)}
    </select>
  );
}

function DynColumnList({ value, onChange, columns }: { value: string[]; onChange: (v: string[]) => void; columns: string[] }) {
  const selected = new Set(value || []);
  const toggle = (col: string) => {
    const next = new Set(selected);
    if (next.has(col)) next.delete(col); else next.add(col);
    onChange(Array.from(next));
  };

  if (!columns.length) {
    return <div className="text-xs text-slate-400 italic">Run upstream node first to see columns.</div>;
  }

  return (
    <div className="flex flex-wrap gap-1">
      {columns.map((col) => (
        <button
          key={col}
          type="button"
          onClick={() => toggle(col)}
          className={`text-[9px] px-1.5 py-0.5 rounded border font-mono transition-colors ${
            selected.has(col)
              ? 'bg-pipe-100 text-pipe-700 border-pipe-300'
              : 'bg-white text-slate-500 border-slate-200 hover:border-pipe-200'
          }`}
        >
          {col}
        </button>
      ))}
    </div>
  );
}

// ── Key-Value List ───────────────────────────────────────────────────────────

function DynKeyValueList({ value, onChange }: { value: Array<{ key: string; value: string }>; onChange: (v: any[]) => void }) {
  const items = Array.isArray(value) ? value : [];

  const update = (idx: number, field: string, val: string) => {
    const next = items.map((item, i) => i === idx ? { ...item, [field]: val } : item);
    onChange(next);
  };
  const add = () => onChange([...items, { key: '', value: '' }]);
  const remove = (idx: number) => onChange(items.filter((_, i) => i !== idx));

  return (
    <div className="space-y-1.5">
      {items.map((item, idx) => (
        <div key={idx} className="flex gap-1 items-center">
          <input
            value={item.key || ''}
            onChange={(e) => update(idx, 'key', e.target.value)}
            placeholder="Key"
            className={`flex-1 ${INPUT_CLS}`}
          />
          <span className="text-slate-300 text-xs">=</span>
          <input
            value={item.value || ''}
            onChange={(e) => update(idx, 'value', e.target.value)}
            placeholder="Value"
            className={`flex-1 ${INPUT_CLS}`}
          />
          <button type="button" onClick={() => remove(idx)}
            className="text-slate-400 hover:text-red-500 text-xs px-1">×</button>
        </div>
      ))}
      <button type="button" onClick={add}
        className="text-xs text-pipe-600 hover:text-pipe-800 hover:underline">
        + Add entry
      </button>
    </div>
  );
}

// ── Rule List (for Data Quality) ─────────────────────────────────────────────

function DynRuleList({ value, onChange, ops, columns }: {
  value: Array<Record<string, any>>; onChange: (v: any[]) => void; ops?: string[]; columns: string[];
}) {
  const rules = Array.isArray(value) ? value : [];
  const operators = ops || ['not_null', 'is_null', 'eq', 'ne', 'gt', 'lt', 'gte', 'lte', 'in', 'not_in', 'regex', 'between'];

  const update = (idx: number, patch: Record<string, any>) => {
    onChange(rules.map((r, i) => i === idx ? { ...r, ...patch } : r));
  };
  const add = () => onChange([...rules, { column: '', op: 'not_null', value: '' }]);
  const remove = (idx: number) => onChange(rules.filter((_, i) => i !== idx));

  return (
    <div className="space-y-2">
      {rules.map((rule, idx) => (
        <div key={idx} className="p-2 bg-slate-50 rounded-lg border border-slate-100 space-y-1.5">
          <div className="flex items-center gap-1">
            <select value={rule.column || ''} onChange={(e) => update(idx, { column: e.target.value })} className={`flex-1 ${SELECT_CLS}`}>
              <option value="">— Column —</option>
              {columns.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
            <select value={rule.op || 'not_null'} onChange={(e) => update(idx, { op: e.target.value })} className={`w-28 ${SELECT_CLS}`}>
              {operators.map((op) => <option key={op} value={op}>{op}</option>)}
            </select>
            <button type="button" onClick={() => remove(idx)}
              className="text-slate-400 hover:text-red-500 text-xs px-1 shrink-0">×</button>
          </div>
          {!['not_null', 'is_null'].includes(rule.op) && (
            <input
              value={rule.value ?? ''}
              onChange={(e) => update(idx, { value: e.target.value })}
              placeholder="Value"
              className={INPUT_CLS}
            />
          )}
        </div>
      ))}
      <button type="button" onClick={add}
        className="text-xs text-pipe-600 hover:text-pipe-800 hover:underline">
        + Add rule
      </button>
    </div>
  );
}

// ── Schema Map (for SchemaMapper) ────────────────────────────────────────────

function DynSchemaMap({ value, onChange, types, columns }: {
  value: Array<Record<string, any>>; onChange: (v: any[]) => void; types?: string[]; columns: string[];
}) {
  const mappings = Array.isArray(value) ? value : [];
  const typeOptions = types || ['string', 'int', 'float', 'bool', 'date', 'datetime', 'json'];

  const update = (idx: number, patch: Record<string, any>) => {
    onChange(mappings.map((m, i) => i === idx ? { ...m, ...patch } : m));
  };
  const add = () => onChange([...mappings, { source: '', target: '', type: 'string', default: '' }]);
  const remove = (idx: number) => onChange(mappings.filter((_, i) => i !== idx));
  // B1: auto-map — bootstrap an empty grid straight-through, or fill blank
  // sources on a declared target schema by fuzzy name match.
  const autoMap = () => onChange(autoMapSchema(mappings, columns));
  const canAutoMap = columns.length > 0;

  return (
    <div className="space-y-2">
      <div className="flex gap-1 text-[9px] font-semibold text-slate-400 uppercase tracking-wider">
        <span className="flex-1">Source</span>
        <span className="flex-1">Target</span>
        <span className="w-20">Type</span>
        <span className="w-5" />
      </div>
      {mappings.map((m, idx) => (
        <div key={idx} className="flex gap-1 items-center">
          <select value={m.source || ''} onChange={(e) => update(idx, { source: e.target.value })} className={`flex-1 ${SELECT_CLS}`}>
            <option value="">— Source —</option>
            {columns.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <input value={m.target || ''} onChange={(e) => update(idx, { target: e.target.value })}
            placeholder="target_col" className={`flex-1 ${INPUT_CLS}`} />
          <select value={m.type || 'string'} onChange={(e) => update(idx, { type: e.target.value })} className={`w-20 ${SELECT_CLS}`}>
            {typeOptions.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <button type="button" onClick={() => remove(idx)}
            className="text-slate-400 hover:text-red-500 text-xs px-1 shrink-0">×</button>
        </div>
      ))}
      <div className="flex items-center gap-3">
        <button type="button" onClick={add}
          className="text-xs text-pipe-600 hover:text-pipe-800 hover:underline">
          + Add mapping
        </button>
        <button type="button" onClick={autoMap} disabled={!canAutoMap}
          title={canAutoMap
            ? 'Match source columns to targets by name (fills blanks; bootstraps an empty grid)'
            : 'Run a sample first so the upstream columns are known'}
          className="text-xs text-pipe-600 hover:text-pipe-800 hover:underline disabled:text-slate-300 disabled:no-underline disabled:cursor-not-allowed">
          ⚡ Auto-map
        </button>
      </div>
    </div>
  );
}

// ── Aggregate List ───────────────────────────────────────────────────────────

function DynAggregateList({ value, onChange, columns }: {
  value: Array<Record<string, any>>; onChange: (v: any[]) => void; columns: string[];
}) {
  const aggs = Array.isArray(value) ? value : [];
  const funcs = ['COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'COUNT_DISTINCT', 'MEDIAN',
    'PERCENTILE_CONT', 'PERCENTILE_DISC', 'STRING_AGG', 'FIRST', 'LAST', 'CUSTOM'];

  const update = (idx: number, patch: Record<string, any>) => {
    onChange(aggs.map((a, i) => i === idx ? { ...a, ...patch } : a));
  };
  const add = () => onChange([...aggs, { function: 'COUNT', column: '*', alias: '' }]);
  const remove = (idx: number) => onChange(aggs.filter((_, i) => i !== idx));

  return (
    <div className="space-y-2">
      {aggs.map((agg, idx) => (
        <div key={idx} className="p-2 bg-slate-50 rounded-lg border border-slate-100 space-y-1.5">
          <div className="flex items-center gap-1">
            <select value={agg.function || 'COUNT'} onChange={(e) => update(idx, { function: e.target.value })} className={`w-32 ${SELECT_CLS}`}>
              {funcs.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
            <select value={agg.column || '*'} onChange={(e) => update(idx, { column: e.target.value })} className={`flex-1 ${SELECT_CLS}`}>
              <option value="*">*</option>
              {columns.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
            <button type="button" onClick={() => remove(idx)}
              className="text-slate-400 hover:text-red-500 text-xs px-1 shrink-0">×</button>
          </div>
          <input value={agg.alias || ''} onChange={(e) => update(idx, { alias: e.target.value })}
            placeholder="Alias (optional)" className={INPUT_CLS} />
        </div>
      ))}
      <button type="button" onClick={add}
        className="text-xs text-pipe-600 hover:text-pipe-800 hover:underline">
        + Add aggregation
      </button>
    </div>
  );
}

// ── Single Field Renderer ────────────────────────────────────────────────────

function DynField({ field, value, onChange, columns }: {
  field: ParamField; value: any; onChange: (v: any) => void; columns: string[];
}) {
  const label = `${field.label || humanizeName(field.name)}${field.required ? ' *' : ''}`;
  // Auto-derived from param_schema `required`: flag empty required fields
  // inline (the up-front `*` says it's required; this says it's not filled).
  const emptyReq = field.required === true && isFieldEmpty(value ?? field.default);

  const body = (() => {
  switch (field.type) {
    case 'text':
    case 'password':
      return (
        <div>
          <label className={LABEL_CLS}>{label}</label>
          <input
            type={field.type === 'password' ? 'password' : 'text'}
            value={value ?? ''}
            onChange={(e) => onChange(e.target.value)}
            placeholder={field.placeholder}
            className={INPUT_CLS}
          />
          {field.type === 'text' && <ExpressionPreview value={value ?? ''} />}
          {field.description && <div className={DESC_CLS}>{field.description}</div>}
        </div>
      );

    case 'number':
      return (
        <div>
          <label className={LABEL_CLS}>{label}</label>
          <input
            type="number"
            value={value ?? field.default ?? ''}
            onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))}
            placeholder={field.placeholder}
            min={field.min}
            max={field.max}
            step={field.step}
            className={INPUT_CLS}
          />
          {field.description && <div className={DESC_CLS}>{field.description}</div>}
        </div>
      );

    case 'textarea':
    case 'sql':
    case 'code':
      return (
        <div>
          <label className={LABEL_CLS}>{label}</label>
          <textarea
            value={value ?? ''}
            onChange={(e) => onChange(e.target.value)}
            placeholder={field.placeholder}
            rows={field.rows || (field.type === 'sql' ? 6 : 3)}
            className={TEXTAREA_CLS}
          />
          {field.type !== 'code' && <ExpressionPreview value={value ?? ''} />}
          {field.description && <div className={DESC_CLS}>{field.description}</div>}
        </div>
      );

    case 'select': {
      // Options support two shapes:
      //   1. legacy: ['a', 'b']      — value === label, no tooltip
      //   2. object: [{value, label, description}] — drives per-option <option title> tooltip
      //                                                  and an inline help row beneath the
      //                                                  select for the *currently selected* item.
      const opts = (field.options || []).map((o) =>
        typeof o === 'string' ? { value: o, label: o } : o,
      );
      const currentValue = value ?? field.default ?? '';
      const currentOpt = opts.find((o) => o.value === currentValue);
      return (
        <div>
          <label className={LABEL_CLS}>{label}</label>
          <select
            value={currentValue}
            onChange={(e) => onChange(e.target.value)}
            className={SELECT_CLS}
          >
            {!field.required && <option value="">— Select —</option>}
            {opts.map((opt) => (
              <option key={opt.value} value={opt.value} title={opt.description || undefined}>
                {opt.label || opt.value}
              </option>
            ))}
          </select>
          {/* Per-option help: shows the description for the currently selected option
              right under the select, so users discover policy semantics without
              hunting through native option tooltips. Falls back to the field-level
              description for legacy fields. */}
          {currentOpt?.description ? (
            <div className={DESC_CLS}>{currentOpt.description}</div>
          ) : field.description ? (
            <div className={DESC_CLS}>{field.description}</div>
          ) : null}
        </div>
      );
    }

    case 'boolean':
      return (
        <div>
          <label className="flex items-center gap-2 cursor-pointer">
            <div
              onClick={() => onChange(!value)}
              className={`w-8 h-4 rounded-full transition-colors ${value ? 'bg-pipe-500' : 'bg-slate-200'} relative`}
            >
              <div className={`w-3 h-3 bg-white rounded-full absolute top-0.5 transition-transform ${value ? 'translate-x-4' : 'translate-x-0.5'}`} />
            </div>
            <span className="text-xs text-slate-500">{field.label || humanizeName(field.name)}</span>
          </label>
          {field.description && <div className={DESC_CLS}>{field.description}</div>}
        </div>
      );

    case 'connection':
      return (
        <div>
          <label className={LABEL_CLS}>{label}</label>
          <DynConnectionPicker value={value || ''} onChange={onChange} filter={field.filter} />
          {field.description && <div className={DESC_CLS}>{field.description}</div>}
        </div>
      );

    case 'column':
      return (
        <div>
          <label className={LABEL_CLS}>{label}</label>
          <DynColumnSelect value={value || ''} onChange={onChange} columns={columns} />
          {field.description && <div className={DESC_CLS}>{field.description}</div>}
        </div>
      );

    case 'column_list':
      return (
        <div>
          <label className={LABEL_CLS}>{label}</label>
          <DynColumnList value={value || []} onChange={onChange} columns={columns} />
          {field.description && <div className={DESC_CLS}>{field.description}</div>}
        </div>
      );

    case 'key_value_list':
      return (
        <div>
          <label className={LABEL_CLS}>{label}</label>
          <DynKeyValueList value={value || []} onChange={onChange} />
          {field.description && <div className={DESC_CLS}>{field.description}</div>}
        </div>
      );

    case 'rule_list':
      return (
        <div>
          <label className={LABEL_CLS}>{label}</label>
          <DynRuleList value={value || []} onChange={onChange} ops={field.ops} columns={columns} />
          {field.description && <div className={DESC_CLS}>{field.description}</div>}
        </div>
      );

    case 'schema_map':
      return (
        <div>
          <label className={LABEL_CLS}>{label}</label>
          <DynSchemaMap value={value || []} onChange={onChange} types={field.types} columns={columns} />
          {field.description && <div className={DESC_CLS}>{field.description}</div>}
        </div>
      );

    case 'aggregate_list':
      return (
        <div>
          <label className={LABEL_CLS}>{label}</label>
          <DynAggregateList value={value || []} onChange={onChange} columns={columns} />
          {field.description && <div className={DESC_CLS}>{field.description}</div>}
        </div>
      );

    case 'json':
      return (
        <div>
          <label className={LABEL_CLS}>{label}</label>
          <textarea
            value={typeof value === 'string' ? value : JSON.stringify(value ?? '', null, 2)}
            onChange={(e) => {
              try { onChange(JSON.parse(e.target.value)); } catch { onChange(e.target.value); }
            }}
            rows={field.rows || 4}
            placeholder={field.placeholder || '{}'}
            className={TEXTAREA_CLS}
          />
          {field.description && <div className={DESC_CLS}>{field.description}</div>}
        </div>
      );

    default:
      // Fallback: render as text input
      return (
        <div>
          <label className={LABEL_CLS}>{label}</label>
          <input
            type="text"
            value={value ?? ''}
            onChange={(e) => onChange(e.target.value)}
            placeholder={field.placeholder}
            className={INPUT_CLS}
          />
          {field.description && <div className={DESC_CLS}>{field.description}</div>}
        </div>
      );
  }
  })();

  return (
    <>
      {body}
      {emptyReq && <p className={REQUIRED_CLS}>Required — please fill this in.</p>}
    </>
  );
}

// ── Main Component ───────────────────────────────────────────────────────────

// Source step types that the backend's `/api/execute/preview-source`
// endpoint will accept. Kept in sync with the SOURCE_PREVIEW_ALLOWED
// set in backend/fpulse/api/execution.py — adding to this list without
// adding to that one will silently 400.
const PREVIEW_ELIGIBLE_SOURCES = new Set([
  'csv_source', 'json_source', 'excel_source', 'parquet_source',
  'db_source', 'api_source', 's3_source',
]);

interface PreviewResult {
  total_rows?: number;
  columns?: string[];
  sample_data?: any[];
  error?: string;
  duration_ms?: number;
}

export default function DynamicConfig({ stepType, params, nodeId, onChange, columns = [] }: DynamicConfigProps) {
  const meta = useMemo(() => getNodeMeta(stepType), [stepType]);
  const stepTypeLower = (stepType || '').toLowerCase();
  const previewEligible = PREVIEW_ELIGIBLE_SOURCES.has(stepTypeLower);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewResult, setPreviewResult] = useState<PreviewResult | null>(null);

  const handlePreview = useCallback(async () => {
    setPreviewLoading(true);
    setPreviewResult(null);
    try {
      const r = await fetch('/api/execute/preview-source', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ step_type: stepTypeLower, params, limit: 20 }),
      });
      if (!r.ok) {
        let detail = `HTTP ${r.status}`;
        try { detail = (await r.json()).detail || detail; } catch { /* ignore */ }
        setPreviewResult({ error: detail });
      } else {
        setPreviewResult(await r.json());
      }
    } catch (e) {
      setPreviewResult({ error: String(e) });
    } finally {
      setPreviewLoading(false);
    }
  }, [stepTypeLower, params]);

  if (!meta || !meta.param_schema.length) {
    return (
      <div className="text-xs text-slate-400 italic">
        No configurable parameters for this node type.
      </div>
    );
  }

  const schema = meta.param_schema;

  // Group fields by tab
  const tabs = useMemo(() => {
    const tabMap = new Map<string, ParamField[]>();
    for (const field of schema) {
      const tab = field.tab || 'General';
      if (!tabMap.has(tab)) tabMap.set(tab, []);
      tabMap.get(tab)!.push(field);
    }
    return tabMap;
  }, [schema]);

  const tabNames = Array.from(tabs.keys());
  const [activeTab, setActiveTab] = useState(tabNames[0] || 'General');

  // Reset tab if current tab disappears
  useEffect(() => {
    if (!tabNames.includes(activeTab)) setActiveTab(tabNames[0] || 'General');
  }, [tabNames, activeTab]);

  const visibleFields = (tabs.get(activeTab) || []).filter((f) => isVisible(f, params));

  // Tier-based grouping. Optional fields are hidden until the user
  // explicitly adds them; smart-defaults collapse under a chip; only
  // required fields show up-front. "Show all" toggle (per-node, persisted)
  // flattens everything for power users who want the old behavior.
  const advancedKey = `fpulse.cfg.advanced.${stepType}`;
  const [advanced, setAdvanced] = useState<boolean>(() => {
    try { return localStorage.getItem(advancedKey) === '1'; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem(advancedKey, advanced ? '1' : '0'); } catch {}
  }, [advanced, advancedKey]);

  // Track which optional fields the user explicitly added this session +
  // any optional that already has a non-default value (loaded from saved state).
  const [addedOptionals, setAddedOptionals] = useState<Set<string>>(() => new Set());

  const required = visibleFields.filter((f) => inferTier(f) === 'required');
  const smartDefaults = visibleFields.filter((f) => inferTier(f) === 'smart-default');
  const optionals = visibleFields.filter((f) => inferTier(f) === 'optional');

  // Optionals already populated should be visible even if not "added" this session.
  const visibleOptionals = optionals.filter((f) => {
    if (addedOptionals.has(f.name)) return true;
    const v = params[f.name];
    if (v === undefined || v === null || v === '') return false;
    if (Array.isArray(v) && v.length === 0) return false;
    if (typeof v === 'object' && Object.keys(v).length === 0) return false;
    return true;
  });
  const availableOptionals = optionals.filter((f) => !visibleOptionals.includes(f));

  const overriddenDefaults = smartDefaults.filter((f) => isFieldOverridden(f, params));

  // 2026-06-11 — auto-expand the defaults section for small sets, or when
  // the user has already customised something. This was the root cause of
  // the "node hides its settings" reaction: every default-bearing field
  // (SCD2's version columns, Sample's options, …) sat collapsed behind an
  // opaque "N defaults applied" chip. Small nodes now show their settings
  // up front; larger ones stay collapsed but with human-readable labels.
  const [defaultsExpanded, setDefaultsExpanded] = useState(
    () => smartDefaults.length > 0 && (smartDefaults.length <= 5 || overriddenDefaults.length > 0),
  );

  const renderField = (field: ParamField) => (
    <DynField
      key={field.name}
      field={field}
      value={params[field.name]}
      onChange={(v) => onChange(nodeId, { [field.name]: v })}
      columns={columns}
    />
  );

  return (
    <div className="space-y-3">
      {/* Tab bar (only if >1 tab) */}
      {tabNames.length > 1 && (
        <div className="flex gap-0.5 border-b border-slate-100 -mx-4 px-4">
          {tabNames.map((tab) => (
            <button
              key={tab}
              type="button"
              onClick={() => setActiveTab(tab)}
              className={`px-3 py-1.5 text-[12px] font-semibold border-b-2 transition-colors ${
                activeTab === tab
                  ? 'border-pipe-500 text-pipe-700'
                  : 'border-transparent text-slate-500 hover:text-slate-700'
              }`}
            >
              {tab}
            </button>
          ))}
        </div>
      )}

      {/* Fields */}
      <div className="space-y-3">
        {advanced ? (
          // Power-user view: flat render of every field, original behavior.
          <>
            {visibleFields.map(renderField)}
            {visibleFields.length === 0 && (
              <div className="text-xs text-slate-400 italic">No visible fields for current settings.</div>
            )}
          </>
        ) : (
          <>
            {/* Required first */}
            {required.map(renderField)}

            {/* Smart-defaults chip — shows summary, expands to editable fields */}
            {smartDefaults.length > 0 && (
              <div className="rounded-lg border border-slate-200 bg-slate-50/60">
                <button
                  type="button"
                  onClick={() => setDefaultsExpanded((v) => !v)}
                  className="w-full flex items-center justify-between gap-2 px-3 py-2 text-left hover:bg-slate-50 transition-colors rounded-lg"
                >
                  <div className="flex-1 min-w-0">
                    <div className="text-xs font-semibold text-slate-700">
                      {overriddenDefaults.length > 0
                        ? `${overriddenDefaults.length} of ${smartDefaults.length} setting${smartDefaults.length === 1 ? '' : 's'} customized`
                        : `Optional settings (${smartDefaults.length}) — using defaults`}
                    </div>
                    <div className="text-xs text-slate-500 truncate">
                      {smartDefaults.slice(0, 4).map((f) => {
                        const cur = params[f.name];
                        const v = cur !== undefined && cur !== null && cur !== '' ? cur : f.default;
                        const label = f.label || humanizeName(f.name);
                        const shown = Array.isArray(v)
                          ? (v.length ? `${v.length} selected` : 'none')
                          : (v === true ? 'on' : v === false ? 'off' : String(v ?? ''));
                        return `${label}: ${shown}`;
                      }).join(' · ')}
                      {smartDefaults.length > 4 ? ' · …' : ''}
                    </div>
                  </div>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
                       className={`text-slate-400 shrink-0 transition-transform ${defaultsExpanded ? 'rotate-90' : ''}`}>
                    <polyline points="9 18 15 12 9 6" />
                  </svg>
                </button>
                {defaultsExpanded && (
                  <div className="px-3 pb-3 pt-1 space-y-3 border-t border-slate-100">
                    {smartDefaults.map(renderField)}
                  </div>
                )}
              </div>
            )}

            {/* Visible optionals (added this session or non-empty in saved state) */}
            {visibleOptionals.map((f) => (
              <div key={f.name} className="relative group">
                {renderField(f)}
                <button
                  type="button"
                  onClick={() => {
                    onChange(nodeId, { [f.name]: undefined });
                    setAddedOptionals((s) => { const n = new Set(s); n.delete(f.name); return n; });
                  }}
                  className="absolute top-0 right-0 text-xs text-slate-400 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity"
                  title="Remove this optional setting"
                >
                  remove
                </button>
              </div>
            ))}

            {/* + Add setting picker */}
            {availableOptionals.length > 0 && (
              <details className="rounded-lg border border-dashed border-slate-200 bg-white">
                <summary className="cursor-pointer px-3 py-2 text-xs font-semibold text-pipe-600 hover:text-pipe-800 select-none">
                  + Add setting <span className="text-slate-400 font-normal">({availableOptionals.length} available)</span>
                </summary>
                <div className="px-3 pb-3 pt-1 space-y-1">
                  {availableOptionals.map((f) => (
                    <button
                      key={f.name}
                      type="button"
                      onClick={() => setAddedOptionals((s) => { const n = new Set(s); n.add(f.name); return n; })}
                      className="w-full text-left px-2 py-1.5 rounded hover:bg-slate-50 transition-colors"
                    >
                      <div className="text-xs font-semibold text-slate-700">{f.label || humanizeName(f.name)}</div>
                      {f.description && (
                        <div className="text-xs text-slate-500 leading-snug">{f.description}</div>
                      )}
                    </button>
                  ))}
                </div>
              </details>
            )}

            {required.length === 0 && smartDefaults.length === 0 && visibleOptionals.length === 0 && availableOptionals.length === 0 && (
              <div className="text-xs text-slate-400 italic">No visible fields for current settings.</div>
            )}
          </>
        )}

        {/* Advanced toggle */}
        {visibleFields.length > 0 && (
          <div className="pt-2 border-t border-slate-100">
            <label className="flex items-center gap-2 cursor-pointer w-fit">
              <input
                type="checkbox"
                checked={advanced}
                onChange={(e) => setAdvanced(e.target.checked)}
                className="w-3 h-3 cursor-pointer"
              />
              <span className="text-xs text-slate-500">Show all settings (advanced)</span>
            </label>
          </div>
        )}
      </div>

      {/* Preview button — appears on source nodes (F11 audit gap fix).
          POSTs the current params to /api/execute/preview-source which
          builds a synthetic 1-step workflow and returns sample rows. */}
      {previewEligible && (
        <div className="pt-3 border-t border-slate-100 space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-xs uppercase tracking-wider font-bold text-slate-500">
              Preview source
            </div>
            <button
              type="button"
              onClick={handlePreview}
              disabled={previewLoading}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg bg-gradient-to-r from-indigo-500 to-purple-500 text-white shadow-sm hover:shadow-md hover:from-indigo-600 hover:to-purple-600 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
            >
              {previewLoading ? (
                <>
                  <svg className="animate-spin" width="11" height="11" viewBox="0 0 24 24">
                    <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" fill="none" opacity="0.3" />
                    <path d="M4 12a8 8 0 018-8" stroke="currentColor" strokeWidth="3" fill="none" strokeLinecap="round" />
                  </svg>
                  Reading…
                </>
              ) : (
                <>
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M5 3l14 9-14 9V3z" />
                  </svg>
                  Preview rows
                </>
              )}
            </button>
          </div>
          {previewResult && previewResult.error && (
            <div className="text-xs text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
              <div className="font-semibold mb-0.5">Preview failed</div>
              <div className="font-mono text-xs whitespace-pre-wrap break-all">{previewResult.error}</div>
            </div>
          )}
          {previewResult && !previewResult.error && previewResult.columns && (
            <div className="bg-slate-50 border border-slate-200 rounded-lg overflow-hidden">
              <div className="px-3 py-1.5 bg-white border-b border-slate-200 flex items-center justify-between text-xs">
                <span className="font-semibold text-slate-700">
                  {previewResult.total_rows ?? 0} row{previewResult.total_rows === 1 ? '' : 's'} · {previewResult.columns.length} columns
                </span>
                {typeof previewResult.duration_ms === 'number' && (
                  <span className="text-slate-400">{Math.round(previewResult.duration_ms)} ms</span>
                )}
              </div>
              <div className="overflow-x-auto max-h-64 overflow-y-auto">
                <table className="w-full text-xs">
                  <thead className="bg-slate-100/60 sticky top-0">
                    <tr>
                      {previewResult.columns.map((c) => (
                        <th key={c} className="text-left px-2 py-1 font-semibold text-slate-700 border-b border-slate-200">{c}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(previewResult.sample_data || []).slice(0, 20).map((row, ri) => (
                      <tr key={ri} className="hover:bg-white">
                        {previewResult.columns!.map((c) => {
                          const v = row[c];
                          const display = v == null ? <span className="text-slate-400 italic">NULL</span> : String(v);
                          return (
                            <td key={c} className="px-2 py-1 border-b border-slate-100 text-slate-700 font-mono truncate max-w-[160px]" title={String(v ?? '')}>
                              {display}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Check if a given step type has a dynamic schema available */
export function hasDynamicSchema(stepType: string): boolean {
  const meta = getNodeMeta(stepType);
  return !!meta && meta.param_schema.length > 0;
}
