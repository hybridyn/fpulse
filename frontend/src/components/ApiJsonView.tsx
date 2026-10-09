import { useMemo, useState } from 'react';
import { ChevronRight, ChevronDown } from 'lucide-react';

/**
 * Collapsible, type-coloured JSON viewer for the API Explorer response pane.
 *
 * Renders parsed JSON that the backend already sampled and redacted; it never
 * fetches, parses or evaluates anything itself. Nodes below `autoExpandDepth`
 * start collapsed so a 5,000-row payload does not paint 5,000 rows at once.
 */

type Props = { value: unknown; filter?: string; autoExpandDepth?: number };

const isContainer = (v: unknown) => v !== null && typeof v === 'object';

function summarise(value: unknown): string {
  if (Array.isArray(value)) return `[ ${value.length} ${value.length === 1 ? 'item' : 'items'} ]`;
  const keys = Object.keys(value as object);
  return `{ ${keys.length} ${keys.length === 1 ? 'key' : 'keys'} }`;
}

function Scalar({ value }: { value: unknown }) {
  if (value === null) return <span className="jv-null">null</span>;
  switch (typeof value) {
    case 'string': return <span className="jv-string">"{value}"</span>;
    case 'number': return <span className="jv-number">{String(value)}</span>;
    case 'boolean': return <span className="jv-boolean">{String(value)}</span>;
    default: return <span>{String(value)}</span>;
  }
}

/** True when this subtree contains the filter text in any key or scalar value. */
function matches(value: unknown, needle: string): boolean {
  if (!needle) return true;
  if (!isContainer(value)) return String(value).toLowerCase().includes(needle);
  const entries = Array.isArray(value)
    ? (value as unknown[]).map((v, i) => [String(i), v] as const)
    : Object.entries(value as object);
  return entries.some(([k, v]) => k.toLowerCase().includes(needle) || matches(v, needle));
}

function Node({ name, value, depth, filter, autoExpandDepth }: {
  name?: string; value: unknown; depth: number; filter: string; autoExpandDepth: number;
}) {
  // A filter forces open the branches that contain a hit, so matches are visible
  // without the user expanding by hand.
  const [open, setOpen] = useState(depth < autoExpandDepth);
  const expanded = filter ? true : open;

  if (!isContainer(value)) {
    return <div className="jv-row" style={{ paddingLeft: depth * 14 }}>
      {name !== undefined && <span className="jv-key">{name}:</span>}
      <Scalar value={value} />
    </div>;
  }

  const entries = Array.isArray(value)
    ? (value as unknown[]).map((v, i) => [String(i), v] as const)
    : (Object.entries(value as object) as (readonly [string, unknown])[]);
  const visible = filter
    ? entries.filter(([k, v]) => k.toLowerCase().includes(filter) || matches(v, filter))
    : entries;

  return <div>
    <div className="jv-row jv-branch" style={{ paddingLeft: depth * 14 }}
      onClick={() => !filter && setOpen(!open)} role="button" tabIndex={0}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (!filter) setOpen(!open); } }}
      aria-expanded={expanded}>
      <span className="jv-caret">{expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</span>
      {name !== undefined && <span className="jv-key">{name}:</span>}
      <span className="jv-summary">{summarise(value)}</span>
    </div>
    {expanded && visible.map(([k, v]) => (
      <Node key={k} name={k} value={v} depth={depth + 1} filter={filter} autoExpandDepth={autoExpandDepth} />
    ))}
    {expanded && filter && visible.length === 0 && (
      <div className="jv-row jv-empty" style={{ paddingLeft: (depth + 1) * 14 }}>No matching keys or values</div>
    )}
  </div>;
}

export default function ApiJsonView({ value, filter = '', autoExpandDepth = 2 }: Props) {
  const needle = filter.trim().toLowerCase();
  const anyMatch = useMemo(() => matches(value, needle), [value, needle]);
  if (needle && !anyMatch) return <p className="jv-nomatch">Nothing in this response matches “{filter}”.</p>;
  return <div className="jv-root">
    <Node value={value} depth={0} filter={needle} autoExpandDepth={autoExpandDepth} />
  </div>;
}
