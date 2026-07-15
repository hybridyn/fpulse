/**
 * RunStatusGrid — N4 round 2.
 *
 * Calendar-style health grid: one row per pipeline, recent runs as
 * color-coded cells (oldest on the left, newest on the right). Lets
 * an operator spot patterns at a glance — "this pipeline keeps
 * failing on Wednesdays", "everything's green since the deploy."
 *
 * Data shape: caller passes an array of {id, name, statuses[]}
 * already padded to the same length (pad with 'unknown' on the LEFT
 * if some pipelines have fewer recent runs). The component renders
 * what it's given — no padding logic here.
 *
 * Color coding mirrors RunStatusSparkline (data-pure sibling for
 * inline use on table rows). The Grid is the multi-pipeline view;
 * the Sparkline is the single-pipeline row.
 */

import { useMemo, type ReactNode } from 'react';
import type { RunStatusKind } from './RunStatusSparkline';

interface PipelineRow {
  id: string;
  name: string;
  statuses: RunStatusKind[];
  /** Optional: highlight this row (e.g. user has it filtered/selected). */
  highlighted?: boolean;
}

interface Props {
  pipelines: PipelineRow[];
  /** Pixel side of each cell — defaults to 18px (default scale). */
  cellSize?: number;
  /** Pixel gap between cells — defaults to 2px. */
  cellGap?: number;
  /** When provided, called with (pipelineId, runIndex) on cell click —
   *  lets the host page deep-link into the run detail. */
  onCellClick?: (pipelineId: string, runIndex: number) => void;
  /** When provided, called with the pipelineId when the row label is
   *  clicked — for "open this pipeline." */
  onPipelineClick?: (pipelineId: string) => void;
  /** Pixel width reserved for the pipeline-name column. Default 200. */
  labelWidth?: number;
  /** Maximum cells to render per pipeline. Excess statuses get clipped
   *  off the LEFT (oldest first) so the right edge stays anchored to
   *  the most recent run. */
  maxCells?: number;
  /** When set, render a small "no data" placeholder centred in the
   *  scroll area. */
  emptyMessage?: string;
}

const COLOR_BY_STATUS: Record<RunStatusKind, string> = {
  success: '#10b981',  // emerald-500
  error: '#ef4444',    // red-500
  warning: '#f59e0b',  // amber-500
  running: '#3b82f6',  // blue-500
  skipped: '#cbd5e1',  // slate-300
  unknown: '#e2e8f0',  // slate-200 (lighter — "no data" rather than "ran but unclear")
};

const STATUS_LABEL: Record<RunStatusKind, string> = {
  success: 'succeeded',
  error: 'failed',
  warning: 'warning',
  running: 'running',
  skipped: 'skipped',
  unknown: 'no data',
};

function colorFor(status: string): string {
  return (COLOR_BY_STATUS as Record<string, string>)[status] || COLOR_BY_STATUS.unknown;
}

function statusLabelFor(status: string): string {
  return (STATUS_LABEL as Record<string, string>)[status] || 'unknown';
}

export default function RunStatusGrid({
  pipelines,
  cellSize = 18,
  cellGap = 2,
  onCellClick,
  onPipelineClick,
  labelWidth = 200,
  maxCells = 24,
  emptyMessage = 'No pipelines to show.',
}: Props): ReactNode {
  const { rows, columnCount, summary } = useMemo(() => {
    const trimmed = pipelines.map((p) => ({
      ...p,
      // Right-anchor: keep the LAST maxCells statuses (newest on
      // the right). If we have fewer than maxCells, pad with
      // 'unknown' on the left so every row has the same column count.
      statuses: (() => {
        const sliced = p.statuses.slice(-maxCells);
        const padding = Math.max(0, maxCells - sliced.length);
        return [...Array<RunStatusKind>(padding).fill('unknown'), ...sliced];
      })(),
    }));
    let total = 0;
    let failed = 0;
    let succeeded = 0;
    for (const p of trimmed) {
      for (const s of p.statuses) {
        if (s === 'unknown') continue;
        total += 1;
        if (s === 'error') failed += 1;
        if (s === 'success') succeeded += 1;
      }
    }
    return {
      rows: trimmed,
      columnCount: maxCells,
      summary:
        total === 0
          ? `${trimmed.length} pipeline${trimmed.length === 1 ? '' : 's'}, no recent runs`
          : `${trimmed.length} pipeline${trimmed.length === 1 ? '' : 's'} · ${succeeded} succeeded · ${failed} failed across ${total} runs`,
    };
  }, [pipelines, maxCells]);

  if (rows.length === 0) {
    return (
      <div className="text-xs text-slate-500 italic py-6 text-center">{emptyMessage}</div>
    );
  }

  const gridWidth = labelWidth + columnCount * (cellSize + cellGap);

  return (
    <div className="block" role="img" aria-label={summary}>
      {/* Header summary — gives the strip a one-line interpretation
          without forcing the user to count cells. */}
      <div className="flex items-center justify-between text-xs text-slate-500 mb-2 pr-1">
        <span className="font-medium">{summary}</span>
        <span className="text-slate-400">
          oldest <span className="text-slate-300">→</span> newest
        </span>
      </div>

      {/* Body — scrolls horizontally on narrow viewports so the grid
          stays one row per pipeline instead of wrapping into
          confusing multi-row blocks. */}
      <div className="overflow-x-auto">
        <div style={{ minWidth: gridWidth }} className="flex flex-col gap-1">
          {rows.map((p) => (
            <div key={p.id} className="flex items-center gap-1">
              {/* Row label — clickable when onPipelineClick provided. */}
              <button
                type="button"
                onClick={onPipelineClick ? () => onPipelineClick(p.id) : undefined}
                disabled={!onPipelineClick}
                style={{ width: labelWidth }}
                className={`text-xs font-medium truncate text-left pr-3 ${
                  onPipelineClick ? 'hover:text-blue-600 cursor-pointer' : 'cursor-default'
                } ${p.highlighted ? 'text-blue-700 font-bold' : 'text-slate-700'}`}
                title={p.name}
              >
                {p.name}
              </button>

              {/* Cells */}
              <div className="flex items-center" style={{ gap: cellGap }}>
                {p.statuses.map((s, i) => {
                  const realIdx = i;
                  const tooltip = `${p.name} — run ${realIdx + 1}/${p.statuses.length} · ${statusLabelFor(s)}`;
                  const cls = onCellClick && s !== 'unknown'
                    ? 'cursor-pointer hover:scale-110 transition-transform'
                    : 'cursor-default';
                  return (
                    <button
                      key={i}
                      type="button"
                      onClick={
                        onCellClick && s !== 'unknown'
                          ? () => onCellClick(p.id, realIdx)
                          : undefined
                      }
                      disabled={!onCellClick || s === 'unknown'}
                      title={tooltip}
                      aria-label={tooltip}
                      className={`rounded-sm ${cls} focus:outline-none focus:ring-1 focus:ring-amber-400`}
                      style={{
                        width: cellSize,
                        height: cellSize,
                        backgroundColor: colorFor(s),
                      }}
                    />
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Legend — bottom-left, small. */}
      <div className="flex flex-wrap items-center gap-3 mt-3 text-[10px] text-slate-500">
        {(['success', 'error', 'warning', 'running', 'skipped', 'unknown'] as RunStatusKind[]).map((s) => (
          <span key={s} className="inline-flex items-center gap-1.5">
            <span
              className="rounded-sm inline-block"
              style={{ width: 10, height: 10, backgroundColor: colorFor(s) }}
            />
            {statusLabelFor(s)}
          </span>
        ))}
      </div>
    </div>
  );
}
