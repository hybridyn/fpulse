/**
 * Branch output ports for canvas nodes (2026-06-11 multi-output).
 *
 * Single source of truth for "which named output handles does this node
 * expose?" Consumed by:
 *   - FPulseNode      — renders one source Handle per port (id = port name).
 *   - workflowStore   — maps edge.sourceHandle ↔ connection.from_port on
 *                       save/load, and remaps legacy `output` edges onto
 *                       the first branch so old pipelines don't dangle.
 *
 * IMPORTANT — honesty rule: a node only appears here if its BACKEND actually
 * routes those ports. Two mechanisms qualify:
 *   1. `_split_output` row-tagging (subsets of ONE schema) — `conditional_split`
 *      ("Switch"), `if_condition` (True/False), `data_quality` (reject),
 *      `deduplicate` (emit_duplicates), `semantic_router` (route_outputs).
 *   2. Heterogeneous named outputs (distinct schemas per port, C1 2026-06-15) —
 *      `data_profile` (passthrough_data) emits the Report on `output` and the
 *      original rows on `data` via ctx.set_named_output.
 * Add a node here ONLY when its execute() actually emits those ports.
 */

export interface BranchPort {
  id: string;
  label: string;
  color?: string;
}

const DEFAULT_PORT: BranchPort = { id: 'output', label: '' };
const DEFAULT_COLOR = '#94a3b8';

/** Ordered list of output ports for a node. Single `output` for ordinary nodes. */
export function branchPortsFor(stepType: string, params: any): BranchPort[] {
  // Data Quality in `reject` mode tags rows pass/reject (_split_output) and
  // exposes two streams. Other DQ modes keep a single output.
  if (stepType === 'data_quality' && String(params?.mode) === 'reject') {
    return [
      { id: 'pass', label: 'Pass', color: '#22c55e' },
      { id: 'reject', label: 'Reject', color: '#ef4444' },
    ];
  }
  // Deduplicate can expose Unique + Duplicate streams (opt-in).
  if (stepType === 'deduplicate' && params?.emit_duplicates) {
    return [
      { id: 'unique', label: 'Unique', color: '#22c55e' },
      { id: 'duplicate', label: 'Duplicate', color: '#f59e0b' },
    ];
  }
  // If Condition is a true two-way brancher (2026-06-15 control-flow alignment): the
  // backend tags each row 'true'/'false' via _split_output.
  if (stepType === 'if_condition') {
    return [
      { id: 'true', label: 'True', color: '#22c55e' },
      { id: 'false', label: 'False', color: '#ef4444' },
    ];
  }
  // Semantic Router branches to one output per label when route_outputs is on.
  if (stepType === 'semantic_router' && params?.route_outputs) {
    const labels = Array.isArray(params?.labels) ? params.labels : [];
    const ports: BranchPort[] = labels
      .map((l: any) => String(l?.name ?? '').trim())
      .filter(Boolean)
      .map((n: string) => ({ id: n, label: n }));
    const def = (String(params?.default_label ?? 'other').trim()) || 'other';
    if (!ports.some((p) => p.id === def)) {
      ports.push({ id: def, label: def, color: DEFAULT_COLOR });
    }
    return ports.length ? ports : [{ id: def, label: def, color: DEFAULT_COLOR }];
  }
  // Data Profile dual-output (C2, 2026-06-15): the Report stays the primary
  // 'output' port; when passthrough_data is on, a second 'data' port carries
  // the original rows (heterogeneous named output, not _split_output).
  if (stepType === 'data_profile' && params?.passthrough_data) {
    return [
      { id: 'output', label: 'Report', color: '#8b5cf6' },
      { id: 'data', label: 'Data', color: '#3b82f6' },
    ];
  }
  if (stepType === 'conditional_split') {
    const conds = Array.isArray(params?.conditions) ? params.conditions : [];
    const ports: BranchPort[] = conds
      .map((c: any) => String(c?.name ?? '').trim())
      .filter(Boolean)
      .map((n: string) => ({ id: n, label: n }));
    const def = (String(params?.default_output ?? 'default').trim()) || 'default';
    if (!ports.some((p) => p.id === def)) {
      ports.push({ id: def, label: def, color: DEFAULT_COLOR });
    }
    return ports.length ? ports : [{ id: def, label: def, color: DEFAULT_COLOR }];
  }
  return [DEFAULT_PORT];
}

/** True when the node exposes more than one real output port. */
export function isBranchNode(stepType: string): boolean {
  return stepType === 'conditional_split' || stepType === 'if_condition';
}

/**
 * Resolve the handle an edge should attach to when loading a saved pipeline.
 * Legacy connections carry `from_port = "output"` (the schema default) even
 * for branch nodes. Map those onto the node's FIRST branch port so the edge
 * doesn't dangle on a handle that no longer exists — preserving the old
 * "first branch wins" behaviour.
 */
export function resolveSourceHandle(
  fromStepType: string | undefined,
  fromPort: string | undefined,
  fromParams: any,
): string | undefined {
  // Ports-driven (not a static type list) so it works for any node that
  // branches based on its params — conditional_split AND data_quality in
  // reject mode, etc.
  const ports = fromStepType ? branchPortsFor(fromStepType, fromParams) : [];
  const isBranch = ports.length > 1;
  const port = fromPort || 'output';
  if (!isBranch) {
    return port === 'output' ? undefined : port;
  }
  if (port && port !== 'output') return port;
  // Legacy 'output' edge on a branch node → its first branch (no dangle).
  return ports[0]?.id;
}
