/**
 * KPITile + KPIStrip — hero-strip components for list / dashboard pages.
 *
 * Shipped as part of Phase 1 (foundation) of the page-design audit. Reused
 * by every list view (Dashboard, Pipelines, Executions, Connections, Pool,
 * Insights, Notifications, Reports) so the "land with 3-5 hero numbers"
 * pattern stays consistent.
 *
 * Design decisions:
 *  - Tones: indigo (default / info), emerald (success), amber (warn / monitor),
 *    red (failure / diagnose), slate (neutral)
 *  - Delta direction: 'up' (green if isGood, red if isBad), 'down' (inverse), 'flat' (slate)
 *  - Sparklines are SVG, no chart library — keep bundle light
 *  - Click handler turns the tile into a button (whole tile clickable)
 *  - Loading state shown via the Skeleton component, not built into KPITile
 *    (caller decides whether to render <Skeleton> or <KPITile> based on state)
 */

import { ReactNode } from 'react';

export type KPITone = 'indigo' | 'emerald' | 'amber' | 'red' | 'slate';
export type DeltaDir = 'up' | 'down' | 'flat';

export interface KPITileProps {
  /** Top label (small uppercase-ish, but not all-caps). E.g. "Pipelines". */
  label: string;
  /** Headline value. Can be a number, a formatted string, or a node. */
  value: ReactNode;
  /** Optional sub-line shown below value. E.g. "12 active". */
  sublabel?: string;
  /** Optional delta vs previous period. */
  delta?: string;
  /** Direction the delta means. 'up' = increase, 'down' = decrease. */
  deltaDir?: DeltaDir;
  /** When true, an 'up' delta is good (green); when false, 'up' is bad (red). */
  upIsGood?: boolean;
  /** Visual tone — drives accent color. */
  tone?: KPITone;
  /** Inline 30-day or 24h sparkline. Numbers normalized 0-1 by the component. */
  sparkline?: number[];
  /** Optional tooltip on hover. */
  hint?: string;
  /** Click handler — when present the whole tile is clickable (button role). */
  onClick?: () => void;
}

const TONES: Record<KPITone, { ring: string; text: string; bg: string; chip: string }> = {
  indigo:  { ring: 'ring-indigo-100',  text: 'text-indigo-700',  bg: 'bg-indigo-50/50',  chip: 'bg-indigo-100 text-indigo-700' },
  emerald: { ring: 'ring-emerald-100', text: 'text-emerald-700', bg: 'bg-emerald-50/50', chip: 'bg-emerald-100 text-emerald-700' },
  amber:   { ring: 'ring-amber-100',   text: 'text-amber-800',   bg: 'bg-amber-50/50',   chip: 'bg-amber-100 text-amber-700' },
  red:     { ring: 'ring-red-100',     text: 'text-red-700',     bg: 'bg-red-50/50',     chip: 'bg-red-100 text-red-700' },
  slate:   { ring: 'ring-slate-200',   text: 'text-slate-700',   bg: 'bg-white',         chip: 'bg-slate-100 text-slate-700' },
};

function deltaColor(dir: DeltaDir | undefined, upIsGood: boolean): string {
  if (!dir || dir === 'flat') return 'text-slate-500';
  const isGood = (dir === 'up' && upIsGood) || (dir === 'down' && !upIsGood);
  return isGood ? 'text-emerald-600' : 'text-red-600';
}

function deltaArrow(dir: DeltaDir | undefined): string {
  if (dir === 'up') return '↑';
  if (dir === 'down') return '↓';
  return '·';
}

function Sparkline({ values, tone }: { values: number[]; tone: KPITone }) {
  if (values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const w = 80;
  const h = 24;
  const step = w / (values.length - 1);
  const points = values.map((v, i) => {
    const x = i * step;
    const y = h - ((v - min) / range) * h;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const stroke = {
    indigo: '#6366f1', emerald: '#10b981', amber: '#f59e0b',
    red: '#ef4444', slate: '#64748b',
  }[tone];
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="opacity-70" aria-hidden="true">
      <polyline
        points={points}
        fill="none"
        stroke={stroke}
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function KPITile(props: KPITileProps) {
  const {
    label, value, sublabel, delta, deltaDir, upIsGood = true,
    tone = 'slate', sparkline, hint, onClick,
  } = props;
  const t = TONES[tone];
  const dColor = deltaColor(deltaDir, upIsGood);
  const Tag = onClick ? 'button' : 'div';

  return (
    <Tag
      type={onClick ? 'button' : undefined as any}
      onClick={onClick}
      title={hint}
      className={[
        'relative rounded-xl border border-slate-200 bg-white px-4 py-3 ring-1 transition-all',
        t.ring,
        onClick ? 'text-left hover:shadow-md hover:border-slate-300 focus:outline-none focus:ring-2 focus:ring-indigo-300 cursor-pointer' : '',
      ].join(' ')}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-xs font-semibold text-slate-500 truncate">{label}</div>
          <div className={`mt-1 text-2xl font-bold tabular-nums truncate ${t.text}`}>{value}</div>
          {sublabel && <div className="text-xs text-slate-500 mt-0.5 truncate">{sublabel}</div>}
        </div>
        {sparkline && sparkline.length > 1 && (
          <div className="shrink-0 mt-1">
            <Sparkline values={sparkline} tone={tone} />
          </div>
        )}
      </div>
      {delta && (
        <div className={`mt-2 inline-flex items-center gap-1 text-xs font-semibold ${dColor}`}>
          <span aria-hidden="true">{deltaArrow(deltaDir)}</span>
          <span>{delta}</span>
        </div>
      )}
    </Tag>
  );
}

export interface KPIStripProps {
  /** Tiles to render. The strip auto-arranges them in a responsive grid. */
  tiles: KPITileProps[];
  /** Override the responsive grid. Default is 1 / 2 / 4 cols at sm / md / xl. */
  columnsClass?: string;
}

export function KPIStrip({ tiles, columnsClass }: KPIStripProps) {
  const cls =
    columnsClass ??
    'grid gap-3 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4';
  return (
    <div className={cls}>
      {tiles.map((t, i) => (
        <KPITile key={i} {...t} />
      ))}
    </div>
  );
}

export default KPIStrip;
