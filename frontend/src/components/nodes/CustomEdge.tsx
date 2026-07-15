import { type EdgeProps, getSmoothStepPath, BaseEdge, EdgeLabelRenderer, useReactFlow } from '@xyflow/react';
import { useWorkflowStore } from '../../stores/workflowStore';
import { useEditorPreferences } from '../../hooks/useEditorPreferences';

const EDGE_CONDITIONS = ['completion', 'success', 'failure'] as const;
type EdgeCondition = (typeof EDGE_CONDITIONS)[number];

const CONDITION_STYLES: Record<EdgeCondition, { color: string; bg: string; border: string; label: string }> = {
  completion: { color: '#6366f1', bg: 'bg-indigo-50', border: 'border-indigo-200', label: 'On Completion' },
  success: { color: '#22c55e', bg: 'bg-emerald-50', border: 'border-emerald-200', label: 'On Success' },
  failure: { color: '#ef4444', bg: 'bg-red-50', border: 'border-red-200', label: 'On Failure' },
};

export default function CustomEdge({
  id,
  source,
  target,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  style = {},
  markerEnd,
  selected,
  data,
}: EdgeProps) {
  const { setEdges } = useReactFlow();
  const stepResults = useWorkflowStore((s) => s.stepResults);
  // Subscribe to graph state so the edge re-renders when the upstream
  // chain's deactivated-ness changes.
  const isBlocked = useWorkflowStore((s) => {
    void s.nodes; void s.edges;
    if (!source || !target) return false;
    // Edge is "blocked" if the source itself is deactivated, or if
    // any ancestor of the source is deactivated — in either case the
    // executor will skip the target and nothing flows through.
    return s.isNodeDeactivated(source) || s.isNodeBlockedByUpstream(source);
  });
  const sourceResult = source ? stepResults[source] : null;
  const targetResult = target ? stepResults[target] : null;
  const rowCount = sourceResult?.status === 'success' ? sourceResult.row_count : null;
  // C2 — Schema-delta badge. When both endpoints have run successfully,
  // compute the column-set diff between source.columns and target.columns
  // so users see at-a-glance what changed across this edge ("+1 / −2 / ~1").
  const rowDelta = (sourceResult?.status === 'success' && targetResult?.status === 'success')
    ? targetResult.row_count - sourceResult.row_count
    : null;
  const schemaDelta = (() => {
    if (sourceResult?.status !== 'success' || targetResult?.status !== 'success') return null;
    const sourceCols = new Set(sourceResult.columns);
    const targetCols = new Set(targetResult.columns);
    const added: string[] = [];
    const removed: string[] = [];
    for (const c of targetResult.columns) if (!sourceCols.has(c)) added.push(c);
    for (const c of sourceResult.columns) if (!targetCols.has(c)) removed.push(c);
    // Retypes need schema_info comparison; cheap to compute
    const sourceTypes = new Map(sourceResult.schema_info.map((c) => [c.name, c.type]));
    const retyped: string[] = [];
    for (const c of targetResult.schema_info) {
      const st = sourceTypes.get(c.name);
      if (st && st !== c.type) retyped.push(c.name);
    }
    if (added.length === 0 && removed.length === 0 && retyped.length === 0) return null;
    return { added, removed, retyped };
  })();

  const condition = ((data as any)?.condition || 'completion') as EdgeCondition;
  const condStyle = CONDITION_STYLES[condition] || CONDITION_STYLES.completion;

  // 2026-06-02 Phase 2 — derive three live-state signals from BOTH
  // stepResults (post-completion result body) AND the source node's
  // transient data.status (live executor signal that lands BEFORE the
  // result body, so edges turn amber the moment the executor picks
  // the node up rather than waiting for it to finish).
  //
  //   isRunning   source is mid-flight → faster + brighter animation.
  //   isSkipped   either endpoint was skipped → dashed slate, no anim.
  //   isFailure   either endpoint errored → red-tinted accent.
  //
  // Combined with the existing isBlocked path, the edge reflects four
  // execution states instead of just two (blocked / not-blocked).
  const sourceNodeStatus = useWorkflowStore((s) =>
    (s.nodes.find((n) => n.id === source)?.data as any)?.status as string | undefined,
  );
  const targetNodeStatus = useWorkflowStore((s) =>
    (s.nodes.find((n) => n.id === target)?.data as any)?.status as string | undefined,
  );
  const isRunning = sourceNodeStatus === 'running' || sourceResult?.status === 'running';
  const isSkipped =
    sourceNodeStatus === 'skipped' || targetNodeStatus === 'skipped' ||
    sourceResult?.status === 'skipped' || targetResult?.status === 'skipped';
  const isFailure =
    sourceNodeStatus === 'error' || targetNodeStatus === 'error' ||
    sourceResult?.status === 'error' || targetResult?.status === 'error';

  // Edge thickness scales with row count on a log scale: tiny jobs stay
  // crisp (2px), 1M-row jobs get visibly thicker (~4px). Capped so a
  // billion-row outlier doesn't blow out the canvas. Renders only when
  // the source has succeeded — running/queued edges stay at base width.
  const flowWidth = (() => {
    if (rowCount === null || rowCount === 0) return 2;
    // log10(1) = 0, log10(1M) = 6, log10(1B) = 9. Multiply by 0.35 and add
    // base 2 → range 2.0 to ~5.15. Cap at 5.5 for sanity.
    return Math.min(5.5, 2 + Math.log10(Math.max(1, rowCount)) * 0.35);
  })();

  // 2026-05-26 — labelDensity controls per-edge pill noise. See the
  // CanvasLabelDensity comment in useEditorPreferences.ts for the
  // motivating UX problem (fan-in joins stacking pills).
  const { labelDensity } = useEditorPreferences();

  // Condition pill visibility:
  //   - Selected / blocked / failure  → always show (user intent or
  //     genuinely alerting state).
  //   - verbose                       → always show.
  //   - metrics                       → show whenever non-default.
  //   - clean (default)               → hide; stroke colour carries
  //     the condition and the source-node Result badge carries
  //     row counts. Pill returns on selection.
  const showConditionLabel = (() => {
    if (selected || isBlocked) return true;
    if (condition === 'failure') return true;
    if (labelDensity === 'verbose') return true;
    if (labelDensity === 'metrics') return condition !== 'completion';
    return false; // clean
  })();

  // Blocked edges override the condition colouring — there's no point
  // showing "On Success" in green when no run will ever reach this
  // edge. Use slate so it visually recedes into the canvas.
  const blockedStyle = { color: '#94a3b8', bg: 'bg-slate-100', border: 'border-slate-300', label: 'Blocked' };
  const effectiveStyle = isBlocked ? blockedStyle : condStyle;

  const [edgePath, labelX, labelY] = getSmoothStepPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    borderRadius: 16,
  });

  const edgeColor = selected ? '#F5A623' : effectiveStyle.color;
  // Row-count chip visibility on the EDGE (the same number lives on
  // the source node's Result badge — see FPulseNode "Result badge"
  // block — so duplicating on every edge is the main fan-in noise
  // source). In `clean`, only the selected edge renders its chip;
  // power modes light up under their own rules.
  const showRowCountLabel = rowCount !== null && (() => {
    if (selected) return true;
    if (labelDensity === 'verbose') return true;
    if (labelDensity === 'metrics') {
      return (rowDelta !== null && rowDelta !== 0) || Boolean(schemaDelta);
    }
    return false; // clean
  })();

  const onDelete = () => {
    setEdges((edges) => edges.filter((e) => e.id !== id));
  };

  const cycleCondition = () => {
    const idx = EDGE_CONDITIONS.indexOf(condition);
    const next = EDGE_CONDITIONS[(idx + 1) % EDGE_CONDITIONS.length];
    setEdges((edges) =>
      edges.map((e) =>
        e.id === id ? { ...e, data: { ...e.data, condition: next } } : e,
      ),
    );
  };

  return (
    <>
      {/* Shadow/glow path — bumped opacity + width when running so the
          live edge has a noticeable halo. */}
      <BaseEdge
        id={`${id}-shadow`}
        path={edgePath}
        style={{
          stroke: isFailure ? '#ef4444' : (isRunning ? '#F5A623' : effectiveStyle.color),
          strokeWidth: selected ? 6 : (isRunning ? 7 : Math.max(4, flowWidth + 2)),
          opacity: isBlocked || isSkipped ? 0.04 : (isRunning ? 0.35 : 0.12),
          // Pulse the halo while running — pairs with the marching
          // dashes on the main path for unmistakable "live" signal.
          animation: isRunning ? 'fpulse-edge-halo 1.2s ease-in-out infinite' : 'none',
        }}
      />
      {/* Main animated path.
          - Blocked → no flow (already in place).
          - Skipped → static dashed slate (no animation; user knows nothing
            ran here, doesn't need it dancing).
          - Running → faster animation + brand amber stroke for unmistakable
            "in-flight" cue.
          - Failure → red-tinted regardless of condition. */}
      <BaseEdge
        id={id}
        path={edgePath}
        markerEnd={markerEnd}
        style={{
          ...style,
          stroke: isFailure ? '#ef4444' : (isRunning ? '#F5A623' : edgeColor),
          // Stroke width scales with row count on successful edges; stays
          // at base for queued/running/blocked.
          strokeWidth: selected ? 2.5 : (isRunning ? 2.5 : (rowCount !== null ? flowWidth : 2)),
          strokeDasharray: isSkipped ? '3 5' : '8 4',
          animation: isBlocked || isSkipped
            ? 'none'
            : (isRunning
                ? 'fpulse-edge-flow 0.6s linear infinite'
                : 'fpulse-edge-flow 1.5s linear infinite'),
          opacity: isBlocked ? 0.55 : (isSkipped ? 0.5 : 1),
        }}
      />
      <EdgeLabelRenderer>
        {/* Condition label on the edge. Default "On Completion" stays quiet
            unless selected so dense fan-in joins do not stack repeated pills. */}
        {showConditionLabel && (
        <div
          style={{
            position: 'absolute',
            transform: `translate(-50%, -170%) translate(${labelX}px,${labelY - 14}px)`,
            pointerEvents: 'all',
          }}
          className="nodrag nopan"
        >
          <button
            onClick={cycleCondition}
            disabled={isBlocked}
            className={`text-[8px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded-md border ${effectiveStyle.bg} ${effectiveStyle.border} hover:shadow-sm transition-all ${isBlocked ? 'cursor-not-allowed' : 'cursor-pointer'}`}
            style={{ color: effectiveStyle.color }}
            title={isBlocked ? 'Upstream is deactivated — this edge will not execute' : 'Click to change: Completion → Success → Failure'}
          >
            {effectiveStyle.label}
          </button>
        </div>
        )}
        {/* Row count label + C2 schema delta. Renders as a compact pill:
            "1,234 rows · −245 · +1 col · ~1 type". The pill shows after
            BOTH endpoints have run. With only source executed, it falls
            back to just the row count. */}
        {rowCount !== null && showRowCountLabel && (
          <div
            style={{
              position: 'absolute',
              transform: `translate(-50%, -100%) translate(${labelX}px,${labelY - 4}px)`,
              pointerEvents: 'none',
            }}
            className="nodrag nopan"
          >
            <span className="text-[9px] font-semibold text-slate-500 bg-white/90 px-1.5 py-0.5 rounded-md border border-slate-200 shadow-sm whitespace-nowrap inline-flex items-center gap-1">
              <span>{rowCount.toLocaleString()} {rowCount === 1 ? 'row' : 'rows'}</span>
              {rowDelta !== null && rowDelta !== 0 && (
                <span className={rowDelta < 0 ? 'text-red-600' : 'text-emerald-600'}>
                  · {rowDelta > 0 ? '+' : ''}{rowDelta.toLocaleString()}
                </span>
              )}
              {schemaDelta && schemaDelta.added.length > 0 && (
                <span className="text-emerald-600" title={`Added: ${schemaDelta.added.join(', ')}`}>
                  · +{schemaDelta.added.length} col{schemaDelta.added.length === 1 ? '' : 's'}
                </span>
              )}
              {schemaDelta && schemaDelta.removed.length > 0 && (
                <span className="text-red-600" title={`Removed: ${schemaDelta.removed.join(', ')}`}>
                  · −{schemaDelta.removed.length} col{schemaDelta.removed.length === 1 ? '' : 's'}
                </span>
              )}
              {schemaDelta && schemaDelta.retyped.length > 0 && (
                <span className="text-amber-600" title={`Retyped: ${schemaDelta.retyped.join(', ')}`}>
                  · ~{schemaDelta.retyped.length} type{schemaDelta.retyped.length === 1 ? '' : 's'}
                </span>
              )}
            </span>
          </div>
        )}
        {/* Delete button — on edge midpoint */}
        <div
          style={{
            position: 'absolute',
            transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
            pointerEvents: 'all',
            display: selected ? undefined : 'none',
          }}
          className="nodrag nopan"
        >
          <button
            onClick={onDelete}
            className="w-5 h-5 rounded-full bg-white border border-slate-300 flex items-center justify-center hover:bg-red-50 hover:border-red-400 shadow-lg transition-all hover:scale-125"
            title="Delete connection"
          >
            <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
      </EdgeLabelRenderer>
    </>
  );
}
