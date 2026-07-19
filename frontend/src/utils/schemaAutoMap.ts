/**
 * Schema Mapper auto-map (B1, 2026-06-15).
 *
 * Two intents, picked automatically:
 *   - Empty grid  → bootstrap a straight-through mapping (one row per source
 *                   column, source = target).
 *   - Existing rows → the user has declared a target schema; only FILL the
 *                   empty `source` cells by fuzzy-matching each row's target
 *                   name against the upstream columns. Existing rows are never
 *                   removed and their targets are never overwritten.
 *
 * Matching is name-based and normalization-tolerant (case / underscores /
 * spaces / camelCase), so `CustomerID`, `customer_id`, and `customer id` all
 * match. Pure functions — unit-tested directly.
 */

export interface SchemaMapRow {
  source?: string;
  target?: string;
  type?: string;
  default?: any;
}

/** Lowercase + strip every non-alphanumeric char, so naming styles collapse. */
export function normalizeName(s: string): string {
  return (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

/**
 * Best available source column for a target name.
 * Ranked: exact normalized → prefix (either direction) → substring. Columns
 * already consumed (`used`) are skipped. Returns '' when nothing reasonable.
 */
export function bestSourceMatch(target: string, columns: string[], used: Set<string>): string {
  const t = normalizeName(target);
  if (!t) return '';
  const avail = columns.filter((c) => !used.has(c));

  const exact = avail.find((c) => normalizeName(c) === t);
  if (exact) return exact;

  const prefix = avail.find((c) => {
    const n = normalizeName(c);
    return n.startsWith(t) || t.startsWith(n);
  });
  if (prefix) return prefix;

  const sub = avail.find((c) => {
    const n = normalizeName(c);
    return n.includes(t) || t.includes(n);
  });
  return sub || '';
}

/** Compute the new mapping rows for an auto-map action. */
export function autoMapSchema(rows: SchemaMapRow[], columns: string[]): SchemaMapRow[] {
  const safeCols = Array.isArray(columns) ? columns : [];
  const existing = Array.isArray(rows) ? rows : [];

  const hasContent = existing.some(
    (r) => (r.target || '').trim() !== '' || (r.source || '').trim() !== '',
  );

  // Empty grid → straight-through bootstrap.
  if (!hasContent) {
    return safeCols.map((c) => ({ source: c, target: c, type: 'string', default: '' }));
  }

  // Existing target schema → fill blank sources only.
  const used = new Set<string>();
  for (const r of existing) if (r.source) used.add(r.source);

  return existing.map((r) => {
    if (r.source) return r;
    const tgt = (r.target || '').trim();
    if (!tgt) return r;
    const match = bestSourceMatch(tgt, safeCols, used);
    if (match) {
      used.add(match);
      return { ...r, source: match };
    }
    return r;
  });
}
