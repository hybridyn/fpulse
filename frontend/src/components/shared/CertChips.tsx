/**
 * CertChips — connector capability + certification badges (#12 step 2).
 *
 * Renders the cert-matrix data for a single connector as a horizontal
 * strip of chips:
 *   [Production] [Source] [Pagination ✓] [Incremental ✓] [Test ✓]
 *
 * Inputs come from the new fields on /api/connectors/cert-matrix
 * (added in #12 step 1): `roles`, `capabilities`, plus the existing
 * `depth_label` + `validation_status` + `manifest_version`.
 *
 * Usage patterns:
 *   <CertChips certRow={row} />              // full strip
 *   <CertChips certRow={row} compact />      // just the cert-label chip
 *
 * If certRow is undefined (e.g. cert matrix hasn't loaded yet, or the
 * connector id is missing from the matrix), the component renders
 * nothing — silent rather than confusing-blank-chip.
 *
 * Data fetching: this component does NOT fetch on its own; callers
 * pass the row from a parent that fetched the whole cert matrix once
 * and looks rows up by id. See `useCertMatrix()` helper below.
 */

import { useEffect, useState } from 'react';
import { api } from '../../api/client';

// ── Module-level cert matrix cache ───────────────────────────────────
//
// Same pattern as the license cache in client.ts — one fetch per app
// session, shared across every component that wants a row. Cert
// matrix changes only on manifest edits, so a session-long cache is
// safe. Refresh on a hard reload.

interface CertRow {
  id: string;
  display_name: string;
  category: string;
  vendor: string;
  manifest_version: number;
  depth_score: number;
  depth_label: string;
  validation_status: 'pass' | 'fail' | 'unvalidated';
  issues_count: number;
  streams_count: number;
  last_error?: string;
  roles?: Array<'source' | 'sink' | 'action' | 'trigger'>;
  capabilities?: {
    source?: boolean;
    sink?: boolean;
    action?: boolean;
    trigger?: boolean;
    pagination?: boolean;
    incremental?: boolean;
    schema?: boolean;
    test?: boolean;
    // F5 (2026-05-30) — P3 cert matrix additions:
    oauth_refresh?: boolean;
    rate_limit?: boolean;
    schema_drift?: boolean;
    backfill_safety?: boolean;
  };
  // F5 — curated + auto-inferred connector gaps; rendered as a tooltip
  // on the cert label chip so the operator sees them before adopting.
  known_gaps?: string[];
}

let _certCache: Record<string, CertRow> | null = null;
let _certPromise: Promise<Record<string, CertRow>> | null = null;

function fetchCertMatrix(): Promise<Record<string, CertRow>> {
  if (_certCache) return Promise.resolve(_certCache);
  if (_certPromise) return _certPromise;
  _certPromise = api.getCertMatrix().then((data: { rows: CertRow[] }) => {
    const byId: Record<string, CertRow> = {};
    for (const row of data.rows || []) {
      byId[row.id] = row;
    }
    _certCache = byId;
    return byId;
  }).catch((err) => {
    // On failure, return an empty map so callers degrade gracefully —
    // chips just don't render. Reset the in-flight promise so a
    // retry on next mount is possible.
    _certPromise = null;
    console.warn('[CertChips] cert matrix fetch failed:', err);
    return {};
  });
  return _certPromise;
}

/**
 * React hook — returns the cert-matrix row for a connector id, or
 * undefined while loading / on error.
 */
export function useCertRow(connectorId: string | undefined): CertRow | undefined {
  const [row, setRow] = useState<CertRow | undefined>(() => {
    if (!connectorId || !_certCache) return undefined;
    return _certCache[connectorId];
  });

  useEffect(() => {
    if (!connectorId) return;
    let alive = true;
    fetchCertMatrix().then((byId) => {
      if (!alive) return;
      setRow(byId[connectorId]);
    });
    return () => {
      alive = false;
    };
  }, [connectorId]);

  return row;
}

// ── Chip styling helpers ─────────────────────────────────────────────
//
// Cert label has its own color scale — Production = green (you can
// rely on it), Beta = amber (it works, has known gaps), Alpha = orange
// (early), Stub = red (placeholder). v1-* labels render slate (the
// "uncertified but functional" tier).

function certLabelClasses(label: string): string {
  switch (label) {
    case 'production':
      return 'bg-emerald-100 text-emerald-700 border-emerald-300';
    case 'beta':
      return 'bg-amber-100 text-amber-700 border-amber-300';
    case 'alpha':
      return 'bg-orange-100 text-orange-700 border-orange-300';
    case 'stub':
      return 'bg-red-100 text-red-700 border-red-300';
    case 'v1-functional':
      return 'bg-sky-100 text-sky-700 border-sky-300';
    case 'v1-basic':
    case 'v1-stub':
    default:
      return 'bg-slate-100 text-slate-600 border-slate-300';
  }
}

function certLabelDisplay(label: string): string {
  switch (label) {
    case 'production':
      return 'Production';
    case 'beta':
      return 'Beta';
    case 'alpha':
      return 'Alpha';
    case 'stub':
      return 'Stub';
    case 'v1-functional':
      return 'v1 functional';
    case 'v1-basic':
      return 'v1 basic';
    case 'v1-stub':
      return 'v1 stub';
    default:
      return label;
  }
}

// Common chip class — keeps the visual rhythm consistent across the
// strip. Each chip is small and dense; the strip wraps on narrow
// containers so it never blows up the row height.
const CHIP_BASE =
  'inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded-md border whitespace-nowrap';

const ROLE_CHIP_CLS = 'bg-indigo-50 text-indigo-700 border-indigo-200';
const CAP_CHIP_CLS = 'bg-slate-50 text-slate-700 border-slate-200';

function TickIcon() {
  return (
    <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

// ── Main component ───────────────────────────────────────────────────

interface Props {
  certRow?: CertRow;
  /** When true, render only the cert label chip (no roles, no capabilities).
   * Useful for tight rows like the connection list. */
  compact?: boolean;
  /** Hover tooltip on the cert label — shows depth_score + streams_count
   * + the last validator error if any. */
  showHover?: boolean;
}

/**
 * Convenience wrapper — takes a connector type id, calls useCertRow
 * internally, renders CertChips. Lets callers slot the chips into a
 * row without managing the hook themselves. Renders nothing while
 * the matrix is loading or if the id is missing from it.
 */
export function CertChipsForType({
  type,
  compact = false,
  showHover = true,
}: {
  type: string | undefined;
  compact?: boolean;
  showHover?: boolean;
}) {
  const row = useCertRow(type);
  return <CertChips certRow={row} compact={compact} showHover={showHover} />;
}

export default function CertChips({ certRow, compact = false, showHover = true }: Props) {
  if (!certRow) return null;

  const labelCls = certLabelClasses(certRow.depth_label);
  const labelText = certLabelDisplay(certRow.depth_label);
  const hoverTitle = showHover
    ? [
        `Depth score: ${certRow.depth_score}/5`,
        `Streams: ${certRow.streams_count}`,
        certRow.validation_status !== 'pass' ? `Validation: ${certRow.validation_status}` : '',
        certRow.last_error ? `Last error: ${certRow.last_error}` : '',
      ]
        .filter(Boolean)
        .join('\n')
      : undefined;

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className={`${CHIP_BASE} ${labelCls}`} title={hoverTitle}>
        {labelText}
      </span>
      {!compact && (
        <>
          {certRow.roles?.map((role) => (
            <span key={role} className={`${CHIP_BASE} ${ROLE_CHIP_CLS}`} title={`Connector role: ${role}`}>
              {role}
            </span>
          ))}
          {certRow.capabilities?.test && (
            <span className={`${CHIP_BASE} ${CAP_CHIP_CLS}`} title="Test connection available">
              <TickIcon /> Test
            </span>
          )}
          {certRow.capabilities?.schema && (
            <span className={`${CHIP_BASE} ${CAP_CHIP_CLS}`} title="Schema discovery declared">
              <TickIcon /> Schema
            </span>
          )}
          {certRow.capabilities?.pagination && (
            <span className={`${CHIP_BASE} ${CAP_CHIP_CLS}`} title="Pagination wired in at least one stream">
              <TickIcon /> Pagination
            </span>
          )}
          {certRow.capabilities?.incremental && (
            <span className={`${CHIP_BASE} ${CAP_CHIP_CLS}`} title="Incremental cursor declared in at least one stream">
              <TickIcon /> Incremental
            </span>
          )}
          {/* F5 (2026-05-30) — P3 cert capability additions. */}
          {certRow.capabilities?.oauth_refresh && (
            <span className={`${CHIP_BASE} ${CAP_CHIP_CLS}`} title="OAuth refresh-token handling declared — pipelines survive access-token expiry">
              <TickIcon /> OAuth refresh
            </span>
          )}
          {certRow.capabilities?.rate_limit && (
            <span className={`${CHIP_BASE} ${CAP_CHIP_CLS}`} title="Rate-limit / backoff policy declared — connector won't slam the API on high-volume runs">
              <TickIcon /> Rate limit
            </span>
          )}
          {certRow.capabilities?.schema_drift && (
            <span className={`${CHIP_BASE} ${CAP_CHIP_CLS}`} title="Schema-drift policy declared — upstream column add/remove won't silently break downstream">
              <TickIcon /> Drift policy
            </span>
          )}
          {certRow.capabilities?.backfill_safety && (
            <span className={`${CHIP_BASE} ${CAP_CHIP_CLS}`} title="Backfill safety declared — re-running a window won't duplicate rows">
              <TickIcon /> Backfill safe
            </span>
          )}
          {certRow.known_gaps && certRow.known_gaps.length > 0 && (
            <span
              className={`${CHIP_BASE} bg-amber-50 text-amber-700 border-amber-300`}
              title={`Known gaps (${certRow.known_gaps.length}):\n` + certRow.known_gaps.map((g) => '• ' + g).join('\n')}
            >
              ⚠ {certRow.known_gaps.length} gap{certRow.known_gaps.length === 1 ? '' : 's'}
            </span>
          )}
        </>
      )}
    </div>
  );
}
