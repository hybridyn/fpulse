/**
 * Branch output ports (2026-06-11 multi-output). Pins which nodes expose
 * named output handles and the legacy load-remap, so a node only advertises
 * branches its backend actually routes.
 */
import { describe, it, expect } from 'vitest';
import { branchPortsFor, isBranchNode, resolveSourceHandle } from '../utils/branchPorts';

describe('branchPortsFor', () => {
  it('ordinary nodes expose a single output port', () => {
    expect(branchPortsFor('filter', {})).toEqual([{ id: 'output', label: '' }]);
    expect(branchPortsFor('aggregate', {})).toEqual([{ id: 'output', label: '' }]);
    expect(isBranchNode('filter')).toBe(false);
  });

  it('conditional_split exposes a port per named condition plus the default', () => {
    const ports = branchPortsFor('conditional_split', {
      conditions: [{ name: 'hi' }, { name: 'lo' }],
      default_output: 'rest',
    });
    expect(ports.map((p) => p.id)).toEqual(['hi', 'lo', 'rest']);
    expect(isBranchNode('conditional_split')).toBe(true);
  });

  it('conditional_split falls back to a single default port when empty', () => {
    expect(branchPortsFor('conditional_split', {}).map((p) => p.id)).toEqual(['default']);
  });

  it('data_quality exposes Pass/Reject only in reject mode', () => {
    expect(branchPortsFor('data_quality', { mode: 'reject' }).map((p) => p.id)).toEqual(['pass', 'reject']);
    expect(branchPortsFor('data_quality', { mode: 'drop' }).map((p) => p.id)).toEqual(['output']);
    expect(branchPortsFor('data_quality', {}).map((p) => p.id)).toEqual(['output']);
  });

  it('legacy "output" edge on a reject-mode DQ node remaps to its first branch (pass)', () => {
    expect(resolveSourceHandle('data_quality', 'output', { mode: 'reject' })).toBe('pass');
    expect(resolveSourceHandle('data_quality', 'reject', { mode: 'reject' })).toBe('reject');
    expect(resolveSourceHandle('data_quality', 'output', { mode: 'drop' })).toBeUndefined();
  });

  it('deduplicate exposes Unique/Duplicate only when emit_duplicates is on', () => {
    expect(branchPortsFor('deduplicate', { emit_duplicates: true }).map((p) => p.id)).toEqual(['unique', 'duplicate']);
    expect(branchPortsFor('deduplicate', {}).map((p) => p.id)).toEqual(['output']);
  });

  it('semantic_router exposes per-label + default ports only when route_outputs is on', () => {
    const ports = branchPortsFor('semantic_router', {
      route_outputs: true,
      labels: [{ name: 'billing' }, { name: 'support' }],
      default_label: 'other',
    });
    expect(ports.map((p) => p.id)).toEqual(['billing', 'support', 'other']);
    // off → single output
    expect(branchPortsFor('semantic_router', { labels: [{ name: 'billing' }] }).map((p) => p.id)).toEqual(['output']);
  });

  it('if_condition exposes True/False branch ports (2026-06-15 control-flow alignment)', () => {
    expect(branchPortsFor('if_condition', { condition: 'x > 0' }).map((p) => p.id)).toEqual(['true', 'false']);
    expect(isBranchNode('if_condition')).toBe(true);
  });

  it('switch_case (retired filter) stays single-output', () => {
    // switch_case is hidden from the palette; "Switch" is conditional_split.
    expect(branchPortsFor('switch_case', { cases: [{ value: 'A' }] }).map((p) => p.id)).toEqual(['output']);
  });

  it('data_profile exposes Report+Data ports only when passthrough_data is on (C2)', () => {
    expect(branchPortsFor('data_profile', { passthrough_data: true }).map((p) => p.id)).toEqual(['output', 'data']);
    expect(branchPortsFor('data_profile', {}).map((p) => p.id)).toEqual(['output']);
  });
});

describe('resolveSourceHandle (load-time remap)', () => {
  it('maps a legacy "output" edge on a branch node onto its first branch', () => {
    expect(resolveSourceHandle('conditional_split', 'output',
      { conditions: [{ name: 'hi' }], default_output: 'rest' })).toBe('hi');
  });

  it('preserves an explicit branch port', () => {
    expect(resolveSourceHandle('conditional_split', 'lo',
      { conditions: [{ name: 'hi' }, { name: 'lo' }] })).toBe('lo');
  });

  it('legacy "output" edge on if_condition maps to the True branch', () => {
    expect(resolveSourceHandle('if_condition', 'output', { condition: 'x > 0' })).toBe('true');
    expect(resolveSourceHandle('if_condition', 'false', { condition: 'x > 0' })).toBe('false');
  });

  it('ordinary node "output" → undefined (attaches to the default handle)', () => {
    expect(resolveSourceHandle('filter', 'output', {})).toBeUndefined();
    expect(resolveSourceHandle('filter', undefined, {})).toBeUndefined();
  });

  it('data_profile keeps Report on output, routes the data port (C2)', () => {
    expect(resolveSourceHandle('data_profile', 'output', { passthrough_data: true })).toBe('output');
    expect(resolveSourceHandle('data_profile', 'data', { passthrough_data: true })).toBe('data');
    // passthrough off → single output, default handle
    expect(resolveSourceHandle('data_profile', 'output', {})).toBeUndefined();
  });
});
