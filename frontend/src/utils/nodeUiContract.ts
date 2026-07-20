/**
 * NodeConfigFrame contract deriver (2026-06-16).
 *
 * Produces the standard "Data In" / "Data Out" descriptors that wrap a node's
 * Processing form so every node config reads the same way:
 *
 *     Data In  →  Processing  →  Data Out  →  Settings
 *
 * Everything here is derived from the SAME authoritative sources the executor
 * uses, so it can't drift from runtime behaviour:
 *   - input arity   ← contractFor()       (registry-driven)
 *   - side effects   ← sideEffectClassFor() (registry-driven)
 *   - output ports   ← branchPortsFor()
 * The runtime bits the deriver can't know on its own (which upstream nodes are
 * connected, their columns, the output columns) are passed in by the caller,
 * which has the live schema from useUpstreamSchema.
 */
import { contractFor, sideEffectClassFor, outputKindFor, schemaStabilityFor, sideEffectLabel, type SideEffectClass } from './nodeArity';
import { branchPortsFor } from './branchPorts';

export interface DataInPort {
  /** 'Input' for single-input nodes; 'Left'/'Right'/'New feed'/… for multi. */
  role: string;
  /** Upstream node label when connected; '' otherwise. */
  label: string;
  columns: string[];
  connected: boolean;
  required: boolean;
}

export interface DataInDescriptor {
  required: number;
  optional: number;
  variadic: boolean;
  ports: DataInPort[];
  /** One-line human summary, e.g. "Needs 2 inputs: Left and Right." */
  note: string;
}

export type OutDisposition =
  | 'rows'         // pure transform — produces rows for the next node
  | 'passthrough'  // sink — writes externally, input passes through
  | 'transformed'  // action — emits a new relation reflecting the result
  | 'terminal'     // nothing meaningful continues downstream
  | 'branches'     // routes rows to >1 named output port
  | 'variable'     // writes a runtime variable; input passes through
  | 'report'       // statistics / metadata, not a transformable dataset
  | 'control';     // orchestrates execution (loops, waits); no dataset out

export interface DataOutPort { id: string; label: string; color?: string }

export interface DataOutDescriptor {
  disposition: OutDisposition;
  summary: string;
  ports: DataOutPort[];
  columns: string[];
  sideEffect: SideEffectClass | null;
  /** Per-action consequence phrase for side-effect nodes (e.g. "Deletes rows
   *  or files", "Sends an email"). null for pure in-memory transforms. Drives
   *  the prominent ⚠ warning band so write/delete/send nodes don't read like
   *  ordinary row transforms. */
  sideEffectNote: string | null;
  /** True when the output schema is data-dependent (pivot, unpivot, SQL
   *  transform, flatten, …) so columns are only known after the first run. The
   *  band shows an honest "known after first run" contract instead of nothing. */
  schemaDynamic: boolean;
}

/** Positional role labels per multi-input node. Single-input → 'Input'. */
const INPUT_ROLES: Record<string, string[]> = {
  join: ['Left', 'Right'],
  lookup: ['Main', 'Lookup'],              // Lookup Join (enrichment)
  scd2: ['New feed', 'Existing snapshot'],
  union: ['Input'],                        // variadic → "Input 1..N"
};

export interface ResolvedInput { label: string; columns: string[] }

/**
 * Build the Data In descriptor. `inputs` are the connected upstreams in the
 * ROLE order the node expects — the caller orders them (e.g. Join puts the
 * Left side first based on `left_input_id`). Pass `[]` when nothing is
 * connected yet; the descriptor still renders the expected slots.
 */
export function buildDataIn(stepType: string, inputs: ResolvedInput[] = []): DataInDescriptor {
  const c = contractFor(stepType);
  const roles = INPUT_ROLES[stepType];
  const slots = Math.max(c.required + c.optional, inputs.length, roles ? roles.length : 0);

  const ports: DataInPort[] = [];
  for (let i = 0; i < slots; i++) {
    const role = roles
      ? (roles[i] ?? (c.variadic ? `${roles[roles.length - 1]} ${i + 1}` : `Input ${i + 1}`))
      : (slots > 1 ? `Input ${i + 1}` : 'Input');
    const inp = inputs[i];
    ports.push({
      role,
      label: inp?.label ?? '',
      columns: inp?.columns ?? [],
      connected: !!inp,
      required: i < c.required,
    });
  }

  let note: string;
  if (c.required === 0 && c.optional === 0 && !c.variadic) {
    note = 'No input — this node produces data.';
  } else if (c.variadic) {
    note = `Accepts ${c.required}+ inputs.`;
  } else if (c.required === 0) {
    note = 'Optional input.';
  } else {
    const roleHint = roles ? `: ${roles.slice(0, c.required).join(' and ')}` : '';
    note = `Needs ${c.required} input${c.required === 1 ? '' : 's'}${roleHint}.`;
  }

  return { required: c.required, optional: c.optional, variadic: c.variadic, ports, note };
}

/** Build the Data Out descriptor. `outputColumns` optional (when schema known). */
export function buildDataOut(
  stepType: string,
  params: Record<string, unknown>,
  outputColumns: string[] = [],
): DataOutDescriptor {
  const se = sideEffectClassFor(stepType);
  const kind = outputKindFor(stepType);
  const ports: DataOutPort[] = branchPortsFor(stepType, params).map((p) => ({
    id: p.id,
    label: p.label || 'Output',
    color: p.color,
  }));
  const isBranching = ports.length > 1;

  let disposition: OutDisposition;
  let summary: string;
  // Branch ports win (a node wired to multiple named outputs IS branching,
  // regardless of kind). Then defer to the authoritative output_kind so
  // variable / report / control nodes read truthfully instead of "rows".
  if (isBranching || kind === 'branch') {
    disposition = 'branches';
    summary = ports.length > 1
      ? `Routes rows to ${ports.length} outputs: ${ports.map((p) => p.label).join(', ')}.`
      : 'Routes rows / flow to multiple outputs.';
  } else if (kind === 'variable') {
    disposition = 'variable';
    summary = 'Sets a runtime variable; input rows pass through unchanged.';
  } else if (kind === 'report') {
    disposition = 'report';
    summary = 'Produces a statistics / metadata report, not a row dataset.';
  } else if (kind === 'control') {
    disposition = 'control';
    summary = 'Controls execution flow; no dataset output.';
  } else if (kind === 'terminal' || se === 'terminal') {
    disposition = 'terminal';
    summary = 'Terminal — nothing meaningful continues downstream.';
  } else if (kind === 'side_effect' || se === 'passthrough') {
    disposition = 'passthrough';
    summary = 'Writes externally; input rows pass through unchanged.';
  } else if (se === 'transforming') {
    disposition = 'transformed';
    summary = 'Emits a new relation reflecting the action result.';
  } else {
    disposition = 'rows';
    summary = 'Produces rows for the next node.';
  }

  return {
    disposition, summary, ports, columns: outputColumns,
    sideEffect: se, sideEffectNote: sideEffectLabel(stepType),
    schemaDynamic: disposition === 'rows' && schemaStabilityFor(stepType) === 'dynamic',
  };
}

/**
 * Best-effort OUTPUT column derivation for the Data Out band.
 *
 * Honest by construction: returns real column names ONLY for deterministic
 * row transforms whose output we can compute from the input columns + params.
 * Returns the best-effort column SET for merges (join/lookup/union) and the
 * group+aggregate set for aggregate. Returns [] for dynamic/inference schemas
 * (pivot, unpivot, SQL transform, flatten — known only after a run; the band
 * shows a "known after first run" contract instead), and for side-effect /
 * control / branch / report / variable nodes (columns aren't the story there).
 * When it returns [], the band shows the disposition summary instead of a
 * column list we couldn't stand behind.
 *
 * Mirrors useUpstreamSchema.applyTrivialTransform and the backend
 * pre_validator._estimate_output_schema. Kept deliberately conservative —
 * showing wrong columns is worse than showing none.
 */
export function deriveOutputColumns(
  stepType: string,
  inputs: ResolvedInput[],
  params: Record<string, unknown>,
): string[] {
  // Dynamic / inference schemas are only known after a run — never guess.
  if (schemaStabilityFor(stepType) !== 'static') return [];

  const first = inputs[0]?.columns ?? [];
  if (first.length === 0) return [];
  const p = params as Record<string, any>;

  switch (stepType) {
    // Pure pass-through — column NAMES unchanged (rows filtered/reordered/recast).
    case 'filter':
    case 'sort':
    case 'sample':
    case 'deduplicate':
    case 'typecast':
      return first;

    case 'rename': {
      const map = (p.mappings || p.rename_map || p.mapping || {}) as Record<string, string>;
      return first.map((c) => (typeof map[c] === 'string' && map[c] ? map[c] : c));
    }

    case 'select': {
      const keep = Array.isArray(p.columns) ? p.columns.map(String) : [];
      return keep.length ? first.filter((c) => keep.includes(c)) : first;
    }

    case 'derived_column': {
      const out = [...first];
      const list = Array.isArray(p.columns) ? p.columns : [];
      for (const d of list) {
        const n = String((d && d.name) || '').trim();
        if (n && !out.includes(n)) out.push(n);
      }
      const single = String(p.name || '').trim();   // single-derivation param shape
      if (single && !out.includes(single)) out.push(single);
      return out;
    }

    case 'embedder': {
      const out = [...first];
      const oc = String(p.output_column || p.output || 'embedding').trim();
      if (oc && !out.includes(oc)) out.push(oc);
      return out;
    }

    // Union stacks rows; the column set is the (aligned) first input's schema.
    case 'union':
      return first;

    // Merges — best-effort column SET across inputs so the shape is visible
    // before a run. (Runtime suffixes any duplicate non-key names; we show the
    // distinct names rather than nothing.)
    case 'join':
    case 'lookup': {
      const out: string[] = [];
      for (const inp of inputs) {
        for (const c of inp.columns || []) {
          if (!out.includes(c)) out.push(c);
        }
      }
      return out;
    }

    // Aggregate replaces the schema with group-by keys + one column per
    // aggregate (its alias, else "<fn>_<col>").
    case 'aggregate': {
      let gb = p.group_by ?? p.groupBy ?? [];
      if (typeof gb === 'string') gb = [gb];
      const keys = Array.isArray(gb) ? gb.map(String).filter(Boolean) : [];
      const out = [...keys];
      const fns = Array.isArray(p.functions) ? p.functions
                : Array.isArray(p.aggregations) ? p.aggregations
                : Array.isArray(p.aggs) ? p.aggs : [];
      let parsed = 0;
      for (const f of fns) {
        const fn = f && (f.function || f.op || f.fn);
        const col = f && (f.column || f.col);
        const alias = String((f && (f.alias || f.output || f.as)) ||
          (fn && col ? `${fn}_${col}` : '')).trim();
        if (alias && !out.includes(alias)) { out.push(alias); parsed++; }
      }
      // Functions present but none parsed → we'd undercount the metrics, which
      // misleads. Defer to the summary instead of showing group keys only.
      if (fns.length > 0 && parsed === 0) return [];
      return out;
    }

    default:
      // scd2 / window / schema_mapper / pivot-family — param-shape- or
      // data-dependent; defer to the summary / "known after run" hint rather
      // than risk a wrong column list.
      return [];
  }
}
