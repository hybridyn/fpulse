/**
 * AgentCard — typed visual card the Copilot can emit inline with text.
 *
 * The agent's final text may contain blocks like:
 *
 *   [CARD]{"kind":"card","type":"kpi_strip","title":"Workspace","tiles":[...]}[/CARD]
 *
 * The chat renderer extracts these blocks via parseAgentCards(), renders
 * them with this component, and renders the surrounding text normally.
 *
 * Design rules (locked 2026-05-01):
 *   - NEVER crash on malformed input. A bad card → render as a code block
 *     so the user can still see what the agent tried to say.
 *   - NEVER include credentials / hostnames / SQL bodies in the card data.
 *     Sanitization already happens at the agent boundary; cards are pure
 *     presentation of what's already been cleaned.
 *   - Light mode only for now (matches the surrounding chat bubble style).
 *
 * OSS scope. Plus may add bar_chart / trend_line / metric_tile later;
 * those are intentionally not implemented here to keep the boundary clean.
 */

import { useMemo } from 'react';

// ──────────────────────────────────────────────────────────────────────
// Card schema
// ──────────────────────────────────────────────────────────────────────

export interface KpiTile {
  label: string;
  value: string | number;
  delta?: string;
  delta_dir?: 'up' | 'down' | 'flat';
  hint?: string;
}

export interface KpiStripCard {
  kind: 'card';
  type: 'kpi_strip';
  title?: string;
  tiles: KpiTile[];
}

export interface TableCard {
  kind: 'card';
  type: 'table';
  title?: string;
  columns: { key: string; label: string; align?: 'left' | 'right' }[];
  rows: Record<string, unknown>[];
  footer?: string;
}

/** Action payload emitted by every clickable chip / option / row.
 *  Travels verbatim to POST /api/ai/agent/action. */
export type AgentAction =
  | { kind: 'slot_fill'; intent_name: string; entity_kind: string; entity_id: string; entity_name: string }
  | { kind: 'fast_action'; verb: string; entity_kind: string; entity_id: string; entity_name: string }
  | { kind: 'execute'; endpoint: string; method?: string; query?: Record<string, unknown>; body?: Record<string, unknown> }
  | { kind: 'ask'; prompt: string }
  | { kind: 'navigate'; page: string; params?: Record<string, unknown>; then_ask?: string };

export interface ChoicesCard {
  kind: 'card';
  type: 'choices';
  title: string;
  subtitle?: string;
  choices: Array<{ label: string; emoji?: string; subtitle?: string; action: AgentAction }>;
  fallback?: string;
}

export interface NextActionsCard {
  kind: 'card';
  type: 'next_actions';
  chips: Array<{ label: string; icon?: string; style?: 'primary' | 'danger'; action: AgentAction }>;
}

export interface ConfirmCard {
  kind: 'card';
  type: 'confirm';
  title: string;
  summary: string;
  tier?: string;
  options: Array<{ label: string; style?: 'primary' | 'danger'; action: AgentAction }>;
  details?: Array<{ label: string; value: string }>;
}

export type AgentCardData = KpiStripCard | TableCard | ChoicesCard | NextActionsCard | ConfirmCard;

// ──────────────────────────────────────────────────────────────────────
// Parser — extract cards from the agent's text response
// ──────────────────────────────────────────────────────────────────────

const CARD_RE = /\[CARD\](.+?)\[\/CARD\]/gs;

export interface ParsedSegment {
  type: 'text' | 'card';
  text?: string;
  card?: AgentCardData;
  raw?: string; // raw block when card parse fails
}

export function parseAgentCards(text: string | undefined | null): ParsedSegment[] {
  if (!text) return [];
  const segments: ParsedSegment[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  CARD_RE.lastIndex = 0;
  while ((m = CARD_RE.exec(text)) !== null) {
    if (m.index > last) {
      const t = text.slice(last, m.index);
      if (t.trim()) segments.push({ type: 'text', text: t });
    }
    const raw = m[1].trim();
    try {
      const parsed = JSON.parse(raw) as AgentCardData;
      const KNOWN_TYPES = new Set(['kpi_strip', 'table', 'choices', 'next_actions', 'confirm']);
      if (parsed && parsed.kind === 'card' && KNOWN_TYPES.has(parsed.type)) {
        segments.push({ type: 'card', card: parsed });
      } else {
        // Unrecognized card type — fall back to raw block so the user can see
        segments.push({ type: 'text', text: '```json\n' + raw + '\n```' });
      }
    } catch {
      segments.push({ type: 'text', text: '```\n' + raw + '\n```', raw });
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) {
    const tail = text.slice(last);
    if (tail.trim()) segments.push({ type: 'text', text: tail });
  }
  return segments.length > 0 ? segments : [{ type: 'text', text }];
}

// ──────────────────────────────────────────────────────────────────────
// Renderers
// ──────────────────────────────────────────────────────────────────────

function KpiStrip({ data }: { data: KpiStripCard }) {
  const tiles = Array.isArray(data.tiles) ? data.tiles.slice(0, 6) : [];
  if (tiles.length === 0) return null;
  return (
    <div className="not-prose my-2">
      {data.title && (
        <div className="text-xs font-bold uppercase tracking-wider text-slate-500 mb-1.5">
          {data.title}
        </div>
      )}
      <div className={`grid gap-2 ${tiles.length <= 2 ? 'grid-cols-2' : tiles.length === 3 ? 'grid-cols-3' : 'grid-cols-2 sm:grid-cols-4'}`}>
        {tiles.map((t, i) => (
          <div key={i} className="rounded-lg bg-white border border-slate-200 px-2.5 py-2" title={t.hint}>
            <div className="text-[9px] font-bold uppercase tracking-wider text-slate-500 truncate">
              {t.label}
            </div>
            <div className="mt-0.5 text-base font-bold text-slate-800 truncate">
              {String(t.value)}
            </div>
            {t.delta && (
              <div
                className={`text-xs font-semibold ${
                  t.delta_dir === 'up' ? 'text-emerald-600'
                    : t.delta_dir === 'down' ? 'text-red-600'
                    : 'text-slate-500'
                }`}
              >
                {t.delta_dir === 'up' ? '▲ ' : t.delta_dir === 'down' ? '▼ ' : '· '}
                {t.delta}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function TableRenderer({ data }: { data: TableCard }) {
  const cols = Array.isArray(data.columns) ? data.columns : [];
  const rows = Array.isArray(data.rows) ? data.rows.slice(0, 50) : [];
  if (cols.length === 0) return null;
  return (
    <div className="not-prose my-2 rounded-lg border border-slate-200 overflow-hidden bg-white">
      {data.title && (
        <div className="px-2.5 py-1.5 text-xs font-bold uppercase tracking-wider text-slate-600 bg-slate-50 border-b border-slate-200">
          {data.title}
        </div>
      )}
      <div className="overflow-x-auto max-h-72 overflow-y-auto">
        <table className="w-full text-xs">
          <thead className="bg-slate-50 sticky top-0">
            <tr>
              {cols.map((c) => (
                <th
                  key={c.key}
                  className={`px-2 py-1.5 font-semibold text-slate-700 border-b border-slate-200 ${
                    c.align === 'right' ? 'text-right' : 'text-left'
                  }`}
                >
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri} className="hover:bg-slate-50">
                {cols.map((c) => (
                  <td
                    key={c.key}
                    className={`px-2 py-1.5 border-b border-slate-100 text-slate-700 ${
                      c.align === 'right' ? 'text-right font-mono' : ''
                    }`}
                  >
                    {formatCell(r[c.key])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.footer && (
        <div className="px-2.5 py-1 text-xs text-slate-500 bg-slate-50 border-t border-slate-200">
          {data.footer}
        </div>
      )}
    </div>
  );
}

function formatCell(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') return v.toLocaleString();
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  return String(v).slice(0, 200);
}

// ──────────────────────────────────────────────────────────────────────
// Interactive card renderers (May 5 2026 — click-driven Copilot UX)
// ──────────────────────────────────────────────────────────────────────

interface ActionableProps {
  onAction?: (action: AgentAction) => void;
  disabled?: boolean;
}

function ChoicesCardRenderer({ data, onAction, disabled }: { data: ChoicesCard } & ActionableProps) {
  const rows = Array.isArray(data.choices) ? data.choices.slice(0, 10) : [];
  return (
    <div className="not-prose my-2.5 rounded-xl border border-slate-200 overflow-hidden bg-white shadow-[0_1px_3px_rgba(0,0,0,0.04)] hover:shadow-[0_2px_8px_rgba(0,0,0,0.06)] transition-shadow">
      <div className="px-3.5 py-2.5 bg-gradient-to-r from-indigo-50 via-purple-50 to-pink-50 border-b border-slate-200">
        <div className="text-sm font-semibold text-slate-800 leading-tight">{data.title}</div>
        {data.subtitle && (
          <div className="text-xs text-slate-500 mt-0.5">{data.subtitle}</div>
        )}
      </div>
      <div className="divide-y divide-slate-100">
        {rows.map((c, i) => (
          <button
            key={i}
            type="button"
            disabled={disabled}
            onClick={() => onAction?.(c.action)}
            className="group w-full px-3.5 py-2.5 text-left flex items-center gap-2.5 hover:bg-indigo-50/60 active:bg-indigo-100/60 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {c.emoji && <span className="text-base shrink-0">{c.emoji}</span>}
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium text-slate-800 truncate group-hover:text-indigo-700 transition-colors">{c.label}</div>
              {c.subtitle && (
                <div className="text-xs text-slate-500 truncate mt-0.5">{c.subtitle}</div>
              )}
            </div>
            <span className="text-slate-300 group-hover:text-indigo-500 group-hover:translate-x-0.5 transition-all text-sm font-bold">→</span>
          </button>
        ))}
      </div>
      {data.fallback && (
        <div className="px-3.5 py-2 text-xs text-slate-500 bg-slate-50/60 border-t border-slate-100 italic">
          {data.fallback}
        </div>
      )}
    </div>
  );
}

function NextActionsRenderer({ data, onAction, disabled }: { data: NextActionsCard } & ActionableProps) {
  const chips = Array.isArray(data.chips) ? data.chips.slice(0, 6) : [];
  if (chips.length === 0) return null;
  return (
    <div className="not-prose my-2.5 flex flex-wrap gap-1.5">
      {chips.map((c, i) => {
        const styleClass = c.style === 'primary'
          ? 'bg-gradient-to-br from-indigo-600 to-indigo-700 text-white border-indigo-700 hover:from-indigo-700 hover:to-indigo-800 hover:shadow-md shadow-sm'
          : c.style === 'danger'
            ? 'bg-white text-red-700 border-red-300 hover:bg-red-50 hover:border-red-400'
            : 'bg-white text-slate-700 border-slate-200 hover:bg-indigo-50 hover:border-indigo-300 hover:text-indigo-700 shadow-sm';
        return (
          <button
            key={i}
            type="button"
            disabled={disabled}
            onClick={() => onAction?.(c.action)}
            className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium rounded-full border transition-all duration-150 active:scale-[0.97] disabled:opacity-50 disabled:cursor-not-allowed ${styleClass}`}
          >
            {c.icon && <span className="text-[12px]">{c.icon}</span>}
            <span>{c.label}</span>
          </button>
        );
      })}
    </div>
  );
}

function ConfirmRenderer({ data, onAction, disabled }: { data: ConfirmCard } & ActionableProps) {
  const opts = Array.isArray(data.options) ? data.options.slice(0, 4) : [];
  const tier = data.tier || 'safe_write';
  const tierStyle =
    tier === 'high_impact_write'
      ? { ring: 'ring-amber-300', badge: 'bg-amber-100 text-amber-800', label: 'High-impact write' }
      : { ring: 'ring-blue-300', badge: 'bg-blue-100 text-blue-800', label: 'Safe write' };
  return (
    <div className={`not-prose my-2 rounded-lg border border-slate-200 ring-1 ${tierStyle.ring} bg-white overflow-hidden`}>
      <div className="px-3 py-2 border-b border-slate-100">
        <div className="flex items-start justify-between gap-2">
          <div className="text-sm font-semibold text-slate-800">{data.title}</div>
          <span className={`shrink-0 text-xs font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${tierStyle.badge}`}>
            {tierStyle.label}
          </span>
        </div>
        <div className="text-[12px] text-slate-600 mt-1">{data.summary}</div>
      </div>
      {Array.isArray(data.details) && data.details.length > 0 && (
        <div className="px-3 py-2 bg-slate-50 border-b border-slate-100 grid grid-cols-2 gap-x-3 gap-y-1">
          {data.details.map((d, i) => (
            <div key={i} className="flex flex-col">
              <div className="text-[9px] font-bold uppercase tracking-wider text-slate-500">{d.label}</div>
              <div className="text-[12px] text-slate-800 truncate">{d.value}</div>
            </div>
          ))}
        </div>
      )}
      <div className="px-3 py-2 flex flex-wrap gap-2">
        {opts.map((o, i) => {
          const cls = o.style === 'primary'
            ? 'bg-indigo-600 text-white border-indigo-600 hover:bg-indigo-700'
            : o.style === 'danger'
              ? 'bg-red-600 text-white border-red-600 hover:bg-red-700'
              : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-50';
          return (
            <button
              key={i}
              type="button"
              disabled={disabled}
              onClick={() => onAction?.(o.action)}
              className={`inline-flex items-center gap-1 px-3 py-1.5 text-[12px] font-semibold rounded-md border transition disabled:opacity-50 disabled:cursor-not-allowed ${cls}`}
            >
              {o.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Public component — switches on type
// ──────────────────────────────────────────────────────────────────────

export default function AgentCard({ card, onAction, disabled }: { card: AgentCardData } & ActionableProps) {
  if (card.type === 'kpi_strip') return <KpiStrip data={card} />;
  if (card.type === 'table') return <TableRenderer data={card} />;
  if (card.type === 'choices') return <ChoicesCardRenderer data={card} onAction={onAction} disabled={disabled} />;
  if (card.type === 'next_actions') return <NextActionsRenderer data={card} onAction={onAction} disabled={disabled} />;
  if (card.type === 'confirm') return <ConfirmRenderer data={card} onAction={onAction} disabled={disabled} />;
  return null;
}

/**
 * Convenience: render an array of parsed segments (text + cards interleaved).
 * Use this from AgentChatPanel where you currently render `turn.text`.
 */
export function AgentSegments({ segments, onAction, disabled }: { segments: ParsedSegment[] } & ActionableProps) {
  const items = useMemo(() => segments, [segments]);
  return (
    <>
      {items.map((seg, i) => {
        if (seg.type === 'card' && seg.card) {
          return <AgentCard key={i} card={seg.card} onAction={onAction} disabled={disabled} />;
        }
        return (
          <div key={i} className="whitespace-pre-wrap leading-relaxed">
            {seg.text || ''}
          </div>
        );
      })}
    </>
  );
}
