/**
 * Regression net for the 2026-06-10 validation-audit fixes.
 *
 * Pins the three drift classes the audit caught:
 *   1. Param-shape drift — validator requirements must match what the
 *      backend node actually reads (lookup_key, columns).
 *   2. Cycle detection — neither the canvas guard nor the validator
 *      caught indirect cycles before; both share graphCycles now.
 *   3. Registry drift — palette types must be classified in the intent
 *      map; the AI fallback builder must not emit hidden legacy types.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { validateWorkflow } from '../utils/validateWorkflow';
import { wouldCreateCycle, findCycleNodeIds } from '../utils/graphCycles';
import { INTENT_FOR_STEP_TYPE } from '../components/modulesPanelData';
import { HIDDEN_TYPES } from '../components/hiddenNodeTypes';
import { parsePipelineIntent } from '../ai/pipelineBuilder';

const node = (id: string, stepType: string, params: Record<string, any> = {}) => ({
  id,
  type: 'fpulseNode',
  data: { stepType, params, label: id },
});
const edge = (source: string, target: string) => ({ id: `${source}->${target}`, source, target });

describe('graphCycles', () => {
  it('detects that closing an indirect loop creates a cycle', () => {
    const edges = [edge('A', 'B'), edge('B', 'C')];
    expect(wouldCreateCycle('C', 'A', edges)).toBe(true);   // C→A closes A→B→C→A
    expect(wouldCreateCycle('A', 'C', edges)).toBe(false);  // A→C is a plain fan-out
    expect(wouldCreateCycle('A', 'A', edges)).toBe(true);   // self-loop
  });

  it('finds every node on a cycle and nothing else', () => {
    const ids = ['A', 'B', 'C', 'D'];
    const edges = [edge('A', 'B'), edge('B', 'C'), edge('C', 'A'), edge('C', 'D')];
    const cyc = findCycleNodeIds(ids, edges);
    expect(cyc).toEqual(new Set(['A', 'B', 'C']));
  });
});

describe('validateWorkflow — cycle detection', () => {
  it('flags an indirect cycle as an error on every member node', () => {
    const nodes = [node('A', 'filter', { condition: 'x > 1' }), node('B', 'filter', { condition: 'x > 2' }), node('C', 'filter', { condition: 'x > 3' })];
    const edges = [edge('A', 'B'), edge('B', 'C'), edge('C', 'A')];
    const issues = validateWorkflow(nodes, edges);
    const cycleErrors = issues.filter((i) => i.level === 'error' && /cycle/i.test(i.message));
    expect(cycleErrors.map((i) => i.nodeId).sort()).toEqual(['A', 'B', 'C']);
  });

  it('does not flag an acyclic chain', () => {
    const nodes = [node('A', 'filter', { condition: 'x > 1' }), node('B', 'filter', { condition: 'x > 2' })];
    const issues = validateWorkflow(nodes, [edge('A', 'B')]);
    expect(issues.some((i) => /cycle/i.test(i.message))).toBe(false);
  });
});

describe('validateWorkflow — lookup param drift (backend reads lookup_key only)', () => {
  const sources = [
    node('main', 'source', { connector_type: 'csv', file_path: 'a.csv' }),
    node('ref', 'source', { connector_type: 'csv', file_path: 'b.csv' }),
  ];
  const wires = [edge('main', 'lk'), edge('ref', 'lk')];

  it('requires lookup_key (the field the backend raises on)', () => {
    const issues = validateWorkflow([...sources, node('lk', 'lookup', {})], wires);
    expect(issues.some((i) => i.nodeId === 'lk' && i.field === 'lookup_key')).toBe(true);
  });

  it('does NOT require the phantom lookup_source field', () => {
    const issues = validateWorkflow(
      [...sources, node('lk', 'lookup', { lookup_key: 'id' })],
      wires,
    );
    expect(issues.filter((i) => i.nodeId === 'lk' && i.level === 'error')).toEqual([]);
  });
});

describe('validateWorkflow — derived_column param shape (backend reads columns array)', () => {
  const src = node('s', 'source', { connector_type: 'csv', file_path: 'a.csv' });

  it('flags the legacy flat name/expression shape as missing columns', () => {
    const issues = validateWorkflow(
      [src, node('d', 'derived_column', { name: 'total', expression: 'a*b' })],
      [edge('s', 'd')],
    );
    expect(issues.some((i) => i.nodeId === 'd' && i.field === 'columns')).toBe(true);
  });

  it('accepts the canonical columns array', () => {
    const issues = validateWorkflow(
      [src, node('d', 'derived_column', { columns: [{ name: 'total', expression: 'a*b' }] })],
      [edge('s', 'd')],
    );
    expect(issues.filter((i) => i.nodeId === 'd' && i.level === 'error')).toEqual([]);
  });
});

describe('validateWorkflow — input contract ceilings', () => {
  it('rejects a 3rd input on join (contract max 2)', () => {
    const nodes = [
      node('a', 'source', { connector_type: 'csv', file_path: 'a.csv' }),
      node('b', 'source', { connector_type: 'csv', file_path: 'b.csv' }),
      node('c', 'source', { connector_type: 'csv', file_path: 'c.csv' }),
      node('j', 'join', {}),
    ];
    const issues = validateWorkflow(nodes, [edge('a', 'j'), edge('b', 'j'), edge('c', 'j')]);
    expect(issues.some((i) => i.nodeId === 'j' && /at most 2/.test(i.message))).toBe(true);
  });
});

describe('validateWorkflow — node-audit round 2 param sanity', () => {
  const src = node('s', 'source', { connector_type: 'csv', file_path: 'a.csv' });
  const wire = [edge('s', 'x')];

  it('deduplicate requires key columns and warns on non-deterministic order', () => {
    const noKeys = validateWorkflow([src, node('x', 'deduplicate', {})], wire);
    expect(noKeys.some((i) => i.nodeId === 'x' && i.level === 'error' && i.field === 'key')).toBe(true);

    const noOrder = validateWorkflow(
      [src, node('x', 'deduplicate', { key: ['id'], strategy: 'keep_first' })], wire,
    );
    expect(noOrder.some((i) => i.nodeId === 'x' && i.level === 'warning' && /non-deterministic/i.test(i.message))).toBe(true);

    const ordered = validateWorkflow(
      [src, node('x', 'deduplicate', { key: ['id'], order_by: 'created_at DESC' })], wire,
    );
    expect(ordered.filter((i) => i.nodeId === 'x')).toEqual([]);
  });

  it('sample validates count/percent per mode, honoring legacy fraction', () => {
    const badPct = validateWorkflow(
      [src, node('x', 'sample', { mode: 'percent', percent: 150 })], wire,
    );
    expect(badPct.some((i) => i.nodeId === 'x' && /between 0 and 100/.test(i.message))).toBe(true);

    const legacyFraction = validateWorkflow(
      [src, node('x', 'sample', { fraction: 0.5 })], wire,   // percent mode inferred
    );
    expect(legacyFraction.filter((i) => i.nodeId === 'x' && i.level === 'error')).toEqual([]);

    const badCount = validateWorkflow(
      [src, node('x', 'sample', { mode: 'rows', count: -5 })], wire,
    );
    expect(badCount.some((i) => i.nodeId === 'x' && /greater than 0/.test(i.message))).toBe(true);
  });

  it('sort flags duplicate sort columns from both token and dict shapes', () => {
    const dupTokens = validateWorkflow(
      [src, node('x', 'sort', { sort_by: ['amount DESC', 'amount ASC'] })], wire,
    );
    expect(dupTokens.some((i) => i.nodeId === 'x' && /Duplicate sort column/.test(i.message))).toBe(true);

    const ok = validateWorkflow(
      [src, node('x', 'sort', { sort_by: ['amount DESC', 'name ASC'] })], wire,
    );
    expect(ok.filter((i) => i.nodeId === 'x')).toEqual([]);
  });

  it('upsert (Keep Latest) requires keys and warns without order_by', () => {
    const noKeys = validateWorkflow([src, node('x', 'upsert', {})], wire);
    expect(noKeys.some((i) => i.nodeId === 'x' && i.level === 'error' && i.field === 'key')).toBe(true);

    const noOrder = validateWorkflow([src, node('x', 'upsert', { key: ['id'] })], wire);
    expect(noOrder.some((i) => i.nodeId === 'x' && /non-deterministic/i.test(i.message))).toBe(true);
  });

  it('scd2 requires business key + tracked columns and warns on overlap', () => {
    const ref = node('r', 'source', { connector_type: 'csv', file_path: 'b.csv' });
    const wires = [edge('s', 'x'), edge('r', 'x')];
    const empty = validateWorkflow([src, ref, node('x', 'scd2', {})], wires);
    expect(empty.some((i) => i.nodeId === 'x' && i.field === 'business_key')).toBe(true);
    expect(empty.some((i) => i.nodeId === 'x' && i.field === 'tracked_columns')).toBe(true);

    const overlapping = validateWorkflow(
      [src, ref, node('x', 'scd2', { business_key: ['customer_id'], tracked_columns: ['customer_id', 'name'] })],
      wires,
    );
    expect(overlapping.some((i) => i.nodeId === 'x' && i.level === 'warning' && /also listed as tracked/.test(i.message))).toBe(true);
  });

  it('delete_data blocks the unimplemented files mode and warns on no-op', () => {
    const filesMode = validateWorkflow(
      [src, node('x', 'delete_data', { target_kind: 'files', target_path: '/data/' })], wire,
    );
    expect(filesMode.some((i) => i.nodeId === 'x' && i.level === 'error' && /NO files would be deleted/.test(i.message))).toBe(true);

    const noCondition = validateWorkflow([src, node('x', 'delete_data', { target_kind: 'rows' })], wire);
    expect(noCondition.some((i) => i.nodeId === 'x' && i.level === 'warning' && /remove nothing/.test(i.message))).toBe(true);
  });

  it('switch_case flags empty + duplicate case values and missing default', () => {
    const issues = validateWorkflow(
      [src, node('x', 'switch_case', {
        column: 'status',
        expression: 'status',
        cases: [{ value: 'PROD' }, { value: 'PROD' }, { value: '' }],
      })],
      wire,
    );
    expect(issues.some((i) => i.nodeId === 'x' && /Duplicate case value/.test(i.message))).toBe(true);
    expect(issues.some((i) => i.nodeId === 'x' && /empty value/.test(i.message))).toBe(true);
    expect(issues.some((i) => i.nodeId === 'x' && i.level === 'warning' && /default branch/i.test(i.message))).toBe(true);
  });

  it('wait_delay validates the delay range against the engine cap', () => {
    const zero = validateWorkflow([src, node('x', 'wait_delay', { seconds: 0 })], wire);
    expect(zero.some((i) => i.nodeId === 'x' && /greater than 0/.test(i.message))).toBe(true);

    const capped = validateWorkflow([src, node('x', 'wait_delay', { seconds: 900 })], wire);
    expect(capped.some((i) => i.nodeId === 'x' && i.level === 'warning' && /capped at 300s/.test(i.message))).toBe(true);

    const legacyUnits = validateWorkflow([src, node('x', 'wait_delay', { duration: 2, unit: 'minutes' })], wire);
    expect(legacyUnits.filter((i) => i.nodeId === 'x' && i.level === 'error')).toEqual([]);
  });

  it('aggregate flags duplicate aliases, bad identifiers, and non-COUNT without a column', () => {
    const dup = validateWorkflow(
      [src, node('x', 'aggregate', { functions: [
        { function: 'SUM', column: 'amount', alias: 'total' },
        { function: 'AVG', column: 'amount', alias: 'total' },
      ] })], wire,
    );
    expect(dup.some((i) => i.nodeId === 'x' && /Duplicate aggregate alias/.test(i.message))).toBe(true);

    const noCol = validateWorkflow(
      [src, node('x', 'aggregate', { functions: [{ function: 'SUM', column: '*', alias: 's' }] })], wire,
    );
    expect(noCol.some((i) => i.nodeId === 'x' && /SUM needs a column/.test(i.message))).toBe(true);

    const countStar = validateWorkflow(
      [src, node('x', 'aggregate', { functions: [{ function: 'COUNT', column: '*', alias: 'n' }] })], wire,
    );
    expect(countStar.filter((i) => i.nodeId === 'x' && i.level === 'error')).toEqual([]);
  });

  it('window requires ORDER BY for ranking/navigation functions', () => {
    const noOrder = validateWorkflow(
      [src, node('x', 'window', { window_functions: [{ function: 'ROW_NUMBER', alias: 'rn' }] })], wire,
    );
    expect(noOrder.some((i) => i.nodeId === 'x' && i.field === 'order_by' && /need an Order By/.test(i.message))).toBe(true);

    const withOrder = validateWorkflow(
      [src, node('x', 'window', { window_functions: [{ function: 'ROW_NUMBER', alias: 'rn' }], order_by: ['amount DESC'] })], wire,
    );
    expect(withOrder.filter((i) => i.nodeId === 'x' && i.level === 'error')).toEqual([]);

    // running totals (SUM/AVG) don't strictly require ORDER BY
    const runningNoOrder = validateWorkflow(
      [src, node('x', 'window', { window_functions: [{ function: 'SUM', column: 'amount', alias: 'rt' }] })], wire,
    );
    expect(runningNoOrder.some((i) => i.field === 'order_by')).toBe(false);
  });

  it('conditional_split validates branch names and conditions', () => {
    const empty = validateWorkflow([src, node('x', 'conditional_split', { conditions: [] })], wire);
    expect(empty.some((i) => i.nodeId === 'x' && i.level === 'warning' && /No split conditions/.test(i.message))).toBe(true);

    const dup = validateWorkflow([src, node('x', 'conditional_split', {
      conditions: [{ name: 'hi', condition: 'a>1' }, { name: 'hi', condition: 'a>2' }],
    })], wire);
    expect(dup.some((i) => i.nodeId === 'x' && /Duplicate branch name/.test(i.message))).toBe(true);

    const noCond = validateWorkflow([src, node('x', 'conditional_split', {
      conditions: [{ name: 'hi', condition: '' }],
    })], wire);
    expect(noCond.some((i) => i.nodeId === 'x' && /no condition/.test(i.message))).toBe(true);

    const ok = validateWorkflow([src, node('x', 'conditional_split', {
      conditions: [{ name: 'hi', condition: 'a>1' }], default_output: 'rest',
    })], wire);
    expect(ok.filter((i) => i.nodeId === 'x' && i.level === 'error')).toEqual([]);
  });

  it('foreach_pipeline requires a sub-pipeline id', () => {
    const missing = validateWorkflow([src, node('x', 'foreach_pipeline', {})], wire);
    expect(missing.some((i) => i.nodeId === 'x' && i.field === 'pipeline_id')).toBe(true);
    const ok = validateWorkflow([src, node('x', 'foreach_pipeline', { pipeline_id: 'child' })], wire);
    expect(ok.filter((i) => i.nodeId === 'x' && i.level === 'error')).toEqual([]);
  });

  it('foreach_pipeline (control-flow guardrails): needs an input to iterate', () => {
    // No wire → nothing to loop over.
    const noInput = validateWorkflow([node('x', 'foreach_pipeline', { pipeline_id: 'child' })], []);
    expect(noInput.some((i) => i.nodeId === 'x' && i.field === 'input')).toBe(true);
    // With an upstream input → no input error.
    const withInput = validateWorkflow([src, node('x', 'foreach_pipeline', { pipeline_id: 'child' })], wire);
    expect(withInput.some((i) => i.nodeId === 'x' && i.field === 'input')).toBe(false);
  });

  it('foreach_pipeline (control-flow guardrails): blocks running ITSELF (recursion)', () => {
    const selfRef = validateWorkflow(
      [src, node('x', 'foreach_pipeline', { pipeline_id: 'wf-1' })], wire, [], 'wf-1',
    );
    expect(selfRef.some((i) => i.nodeId === 'x' && i.field === 'pipeline_id' && /recurse/i.test(i.message))).toBe(true);
    // A different sub-pipeline is fine.
    const other = validateWorkflow(
      [src, node('x', 'foreach_pipeline', { pipeline_id: 'wf-2' })], wire, [], 'wf-1',
    );
    expect(other.some((i) => i.nodeId === 'x' && i.field === 'pipeline_id')).toBe(false);
  });

  it('foreach_pipeline (control-flow guardrails): max_iterations sanity', () => {
    const zero = validateWorkflow([src, node('x', 'foreach_pipeline', { pipeline_id: 'c', max_iterations: 0 })], wire);
    expect(zero.some((i) => i.nodeId === 'x' && i.field === 'max_iterations' && i.level === 'error')).toBe(true);
    const huge = validateWorkflow([src, node('x', 'foreach_pipeline', { pipeline_id: 'c', max_iterations: 5000 })], wire);
    expect(huge.some((i) => i.nodeId === 'x' && i.field === 'max_iterations' && i.level === 'warning')).toBe(true);
  });

  it('warns on an undeclared ${param.x} reference (dynamic-input typo guard)', () => {
    const flt = node('x', 'filter', { condition: 'amount > ${param.min_amount}' });
    // No declared params → warning that names the undeclared parameter.
    const undeclared = validateWorkflow([src, flt], wire);
    expect(undeclared.some((i) => i.nodeId === 'x' && i.level === 'warning' && /min_amount/.test(i.message))).toBe(true);
    // Declared → no undeclared-parameter warning.
    const declared = validateWorkflow([src, flt], wire, [{ name: 'min_amount' }]);
    expect(declared.some((i) => i.nodeId === 'x' && /undeclared parameter/i.test(i.message))).toBe(false);
  });

  it('lookup_activity requires an output_var capture name', () => {
    const noVar = validateWorkflow([src, node('x', 'lookup_activity', {})], wire);
    expect(noVar.some((i) => i.nodeId === 'x' && i.field === 'output_var')).toBe(true);

    const ok = validateWorkflow(
      [src, node('x', 'lookup_activity', { output_var: 'watermark' })], wire,
    );
    expect(ok.filter((i) => i.nodeId === 'x' && i.level === 'error')).toEqual([]);
  });

  it('flatten_explode requires a column', () => {
    const issues = validateWorkflow([src, node('x', 'flatten_explode', { mode: 'explode' })], wire);
    expect(issues.some((i) => i.nodeId === 'x' && i.field === 'column')).toBe(true);
  });
});

describe('palette intent registry', () => {
  it('classifies the managed local-table nodes (regression: fell into Automate)', () => {
    expect(INTENT_FOR_STEP_TYPE.local_table_source).toBe('Import');
    expect(INTENT_FOR_STEP_TYPE.local_table_sink).toBe('Publish');
  });
});

describe('AI fallback builder emits only modern palette types', () => {
  it('normalizes legacy source/sink/validate types before returning', () => {
    const result = parsePipelineIntent(
      'load sales.csv then filter amount > 100 then clean data then save to parquet',
    );
    expect(result).not.toBeNull();
    const types = result!.steps.map((s) => s.type);
    for (const t of types) {
      expect(HIDDEN_TYPES.has(t), `step type "${t}" is a hidden legacy type`).toBe(false);
    }
    expect(types).toContain('source');
    expect(types).toContain('destination');
    expect(types).toContain('data_quality');   // was `validate`
    expect(types).not.toContain('validate');
  });

  it('maps file extensions to the right source connector_type', () => {
    const result = parsePipelineIntent('load events.json then filter x > 1 then save to output.csv');
    expect(result).not.toBeNull();
    const src = result!.steps.find((s) => s.type === 'source');
    expect(src?.params.connector_type).toBe('json');
    const dest = result!.steps.find((s) => s.type === 'destination');
    expect(dest?.params.connector_type).toBe('csv');
  });
});

// C3 / Phase 2 — validation derived from the backend param_schema registry.
// These set window.__fpulse_node_types (the registry the running app loads) and
// prove: schema-required fields are flagged; the schema OVERRIDES over-strict
// legacy maps (kills drift); one-of groups + dedicated blocks aren't doubled.
describe('validateWorkflow — schema-driven required fields (C3)', () => {
  const REG = [
    { step_type: 'derived_column', param_schema: [{ name: 'columns', required: true }] },
    // filter marks NOTHING required (condition OR rules) — the legacy map's
    // `filter:['condition']` was over-strict.
    { step_type: 'filter', param_schema: [{ name: 'condition' }, { name: 'rules' }] },
    { step_type: 'deduplicate', param_schema: [{ name: 'key', required: true }, { name: 'order_by' }] },
    { step_type: 'csv_source', param_schema: [{ name: 'file_path', required: true }] },
    { step_type: 'source', param_schema: [{ name: 'connector_type', required: true }] },
    { step_type: 'data_quality', param_schema: [{ name: 'rules', required: true }] },
  ];

  beforeEach(() => { (globalThis as any).__fpulse_node_types = REG; });
  afterEach(() => { delete (globalThis as any).__fpulse_node_types; });

  it('flags a schema-required field when missing', () => {
    const issues = validateWorkflow([node('d', 'derived_column', {})], []);
    expect(issues.some((i) => i.nodeId === 'd' && i.field === 'columns' && i.level === 'error')).toBe(true);
  });

  it('passes once the schema-required field is present', () => {
    const issues = validateWorkflow(
      [node('d', 'derived_column', { columns: [{ name: 'x', expression: '1' }] })], [],
    );
    expect(issues.some((i) => i.nodeId === 'd' && i.field === 'columns')).toBe(false);
  });

  it('lets the schema override an over-strict legacy map (filter requires nothing)', () => {
    // TRANSFORM_REQUIREMENTS still lists filter:['condition'], but the loaded
    // schema marks nothing required → no false positive on an advanced-mode
    // filter that uses rules instead of a condition.
    const issues = validateWorkflow([node('f', 'filter', { rules: [{ field: 'a' }] })], []);
    expect(issues.some((i) => i.nodeId === 'f' && i.field === 'condition')).toBe(false);
  });

  it('fills a coverage gap the hand-maps missed (data_quality needs rules)', () => {
    const issues = validateWorkflow([node('q', 'data_quality', {})], []);
    expect(issues.some((i) => i.nodeId === 'q' && i.field === 'rules' && i.level === 'error')).toBe(true);
  });

  it('does not double-report a field a dedicated block already covers', () => {
    const nodes = [node('s', 'csv_source', { file_path: 'a.csv' }), node('dd', 'deduplicate', {})];
    const issues = validateWorkflow(nodes, [edge('s', 'dd')]);
    const keyErrors = issues.filter((i) => i.nodeId === 'dd' && i.field === 'key' && i.level === 'error');
    expect(keyErrors.length).toBe(1);   // the dedicated rich message wins, not two
  });

  it('respects one-of groups (csv_source: file_path OR url)', () => {
    const issues = validateWorkflow([node('s', 'csv_source', { url: 'http://x/a.csv' })], []);
    expect(issues.some((i) => i.nodeId === 's' && i.field === 'file_path' && i.level === 'error')).toBe(false);
  });
});
