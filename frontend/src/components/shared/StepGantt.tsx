/**
 * StepGantt — N5 round 1.
 *
 * Horizontal Gantt visualization of a pipeline run's step timings.
 * One row per step, ordered top-to-bottom by start time. Bar width is
 * proportional to step duration over the full run window. Bars are
 * color-coded by step status; small "running" bars pulse.
 *
 * Data-pure — caller passes step_logs. Slot it into the run detail
 * view (ExecutionsPage step tab) to surface slow steps at a glance,
 * or onto the executions list for a compact view.
 *
 * Performance: SVG with one <rect> per step. Cheap up to ~500 steps;
 * pipelines this big are rare in OSS. Tooltip on hover via title.
 */

import { useMemo, type ReactNode } from 'react';

export interface GanttStep {
  step_id: string;
  step_name?: string;
  status: 'success' | 'error' | 'warning' | 'running' | 'skipped' | string;
  /** ISO-8601 start time. Required. */
  start_time: string;
  /** ISO-8601 end time. Optional — running steps haven't ended. */
  end_time?: string;
  /** Optional row count for the tooltip. */
  row_count?: number;
}

interface Props {
  steps: GanttStep[];
  /** Optional total run window. When omitted, derived from min/max
   *  across the steps. */
  runStart?: string;
  runEnd?: string;
  /** Pixel height of each row. Defaults to 22px (dense). */
  rowHeight?: number;
  /** Pixel width of the chart area. Defaults to 720 (resize if needed). */
  width?: number;
  /** Click handler — fires with the clicked step's step_id. */
  onSelect?: (stepId: string) => void;
  /** Visually highlight one step (e.g. the currently-selected row). */
  highlightedStepId?: string | null;
}

const COLOR_BY_STATUS: Record<string, string> = {
  success: '#10b981',  // emerald-500
  error: '#ef4444',    // red-500
  warning: '#f59e0b',  // amber-500
  running: '#3b82f6',  // blue-500
  skipped: '#cbd5e1',  // slate-300
};

const FALLBACK_COLOR = '#94a3b8'; // slate-400

function colorFor(status: string): string {
  return COLOR_BY_STATUS[status] || FALLBACK_COLOR;
}

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const rs = Math.round(s - m * 60);
  return `${m}m ${rs}s`;
}

function fmtClock(d: Date): string {
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

const LABEL_WIDTH = 160; // px reserved on the left for the step name
const AXIS_HEIGHT = 18;   // px for the bottom time axis

export default function StepGantt({
  steps,
  runStart,
  runEnd,
  rowHeight = 22,
  width = 720,
  onSelect,
  highlightedStepId = null,
}: Props): ReactNode {
  const { startMs, endMs, sorted } = useMemo(() => {
    const parsed = steps
      .map((s) => {
        const start = new Date(s.start_time).getTime();
        const end = s.end_time ? new Date(s.end_time).getTime() : start;
        return { ...s, _startMs: start, _endMs: end };
      })
      .filter((s) => Number.isFinite(s._startMs));

    parsed.sort((a, b) => a._startMs - b._startMs);

    const minStart = runStart
      ? new Date(runStart).getTime()
      : parsed.length > 0
        ? parsed[0]._startMs
        : Date.now();
    const maxEnd = runEnd
      ? new Date(runEnd).getTime()
      : parsed.length > 0
        ? Math.max(...parsed.map((s) => s._endMs))
        : minStart + 1;

    return {
      startMs: minStart,
      endMs: Math.max(maxEnd, minStart + 1),
      sorted: parsed,
    };
  }, [steps, runStart, runEnd]);

  if (sorted.length === 0) {
    return (
      <div className="text-xs text-slate-500 italic py-4 text-center">
        No step timing data yet.
      </div>
    );
  }

  const totalMs = endMs - startMs;
  const chartWidth = width - LABEL_WIDTH;
  const chartHeight = sorted.length * rowHeight + AXIS_HEIGHT;

  // Pick ~5 tick marks across the time axis.
  const tickCount = 5;
  const ticks = Array.from({ length: tickCount + 1 }).map((_, i) => {
    const t = startMs + (totalMs * i) / tickCount;
    const x = LABEL_WIDTH + (chartWidth * i) / tickCount;
    return { t, x };
  });

  return (
    <svg
      width={width}
      height={chartHeight}
      viewBox={`0 0 ${width} ${chartHeight}`}
      className="block"
      role="img"
      aria-label={`Gantt chart of ${sorted.length} step${sorted.length === 1 ? '' : 's'}`}
    >
      {/* Vertical grid lines aligned with ticks */}
      {ticks.map((tk, i) => (
        <line
          key={`grid-${i}`}
          x1={tk.x}
          x2={tk.x}
          y1={0}
          y2={sorted.length * rowHeight}
          stroke="#e2e8f0"
          strokeWidth={1}
        />
      ))}

      {/* Step rows */}
      {sorted.map((s, idx) => {
        const startX = LABEL_WIDTH + (chartWidth * (s._startMs - startMs)) / totalMs;
        const endX = LABEL_WIDTH + (chartWidth * (s._endMs - startMs)) / totalMs;
        const barW = Math.max(2, endX - startX);
        const y = idx * rowHeight + 4;
        const barH = rowHeight - 8;
        const dur = s._endMs - s._startMs;
        const highlighted = highlightedStepId === s.step_id;
        const tooltip =
          `${s.step_name || s.step_id} — ${s.status} · ${fmtDuration(dur)}` +
          (s.row_count != null ? ` · ${s.row_count} rows` : '');

        return (
          <g key={s.step_id}>
            {/* Left label */}
            <text
              x={LABEL_WIDTH - 8}
              y={y + barH / 2 + 4}
              textAnchor="end"
              fontSize={11}
              fontWeight={highlighted ? 700 : 500}
              fill={highlighted ? '#0f172a' : '#475569'}
            >
              {(s.step_name || s.step_id).slice(0, 22)}
            </text>

            {/* Bar */}
            <rect
              x={startX}
              y={y}
              width={barW}
              height={barH}
              rx={3}
              ry={3}
              fill={colorFor(s.status)}
              opacity={highlighted ? 1 : 0.92}
              stroke={highlighted ? '#0f172a' : 'none'}
              strokeWidth={highlighted ? 1.5 : 0}
              className={
                (onSelect ? 'cursor-pointer ' : '') +
                (s.status === 'running' ? 'animate-pulse' : '')
              }
              onClick={onSelect ? () => onSelect(s.step_id) : undefined}
            >
              <title>{tooltip}</title>
            </rect>

            {/* Inline duration label if there's room */}
            {barW > 50 && (
              <text
                x={startX + barW / 2}
                y={y + barH / 2 + 4}
                textAnchor="middle"
                fontSize={10}
                fontWeight={600}
                fill="#fff"
                pointerEvents="none"
              >
                {fmtDuration(dur)}
              </text>
            )}
          </g>
        );
      })}

      {/* Time axis at the bottom */}
      {ticks.map((tk, i) => (
        <g key={`tick-${i}`}>
          <line
            x1={tk.x}
            x2={tk.x}
            y1={sorted.length * rowHeight}
            y2={sorted.length * rowHeight + 4}
            stroke="#94a3b8"
            strokeWidth={1}
          />
          <text
            x={tk.x}
            y={sorted.length * rowHeight + AXIS_HEIGHT - 2}
            textAnchor={i === 0 ? 'start' : i === tickCount ? 'end' : 'middle'}
            fontSize={9}
            fill="#64748b"
          >
            {fmtClock(new Date(tk.t))}
          </text>
        </g>
      ))}
    </svg>
  );
}
