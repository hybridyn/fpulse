/**
 * nodeArity contract tests (2026-06-16).
 *
 * Two things are pinned here:
 *   1. The offline FALLBACK maps are correct after closing the
 *      frontend/backend drift (http_request accepts an optional input;
 *      local_table_sink + fail carry their side-effect class).
 *   2. The LIVE-REGISTRY derivation wins over the hand-maps when
 *      `window.__fpulse_node_types` is loaded — so the file can't drift
 *      from the backend again.
 */
import { afterEach, describe, expect, it } from 'vitest';
import {
  arityFor,
  contractFor,
  hasSideEffect,
  sideEffectClassFor,
} from '../utils/nodeArity';

function setRegistry(entries: unknown[] | undefined) {
  (window as unknown as { __fpulse_node_types?: unknown }).__fpulse_node_types = entries;
}

afterEach(() => {
  // Always return to the offline path so tests don't leak the registry.
  delete (window as unknown as { __fpulse_node_types?: unknown }).__fpulse_node_types;
});

describe('nodeArity offline fallback (drift fixes)', () => {
  it('http_request accepts an optional input (not source-like)', () => {
    expect(arityFor('http_request')).toBe('one');
    const c = contractFor('http_request');
    expect(c.required).toBe(0);
    expect(c.optional).toBe(1);
  });

  it('local_table_sink is a passthrough side effect', () => {
    expect(hasSideEffect('local_table_sink')).toBe(true);
    expect(sideEffectClassFor('local_table_sink')).toBe('passthrough');
  });

  it('fail is terminal', () => {
    expect(hasSideEffect('fail')).toBe(true);
    expect(sideEffectClassFor('fail')).toBe('terminal');
  });

  it('lookup_activity does not require an upstream input', () => {
    expect(contractFor('lookup_activity').required).toBe(0);
  });

  it('core arities still hold', () => {
    expect(arityFor('join')).toBe('many');
    expect(arityFor('source')).toBe('none');
    expect(arityFor('filter')).toBe('one');
    expect(sideEffectClassFor('filter')).toBeNull();
  });
});

describe('nodeArity live-registry derivation wins over hand-maps', () => {
  it('derives arity + side-effects from the registry when loaded', () => {
    setRegistry([
      // Deliberately contradict the hand-map defaults to prove the registry wins.
      { type: 'filter', arity: { required: 0, optional: 0, variadic: false }, side_effects: 'terminal' },
      { type: 'http_request', arity: { required: 2, optional: 0, variadic: false }, side_effects: null },
    ]);
    // filter is normally a pure 1-in transform; registry now says source-like + terminal.
    expect(arityFor('filter')).toBe('none');
    expect(sideEffectClassFor('filter')).toBe('terminal');
    expect(hasSideEffect('filter')).toBe(true);
    // http_request registry override → many in, pure.
    expect(arityFor('http_request')).toBe('many');
    expect(hasSideEffect('http_request')).toBe(false);
  });

  it('falls back to hand-maps for types absent from the registry', () => {
    setRegistry([{ type: 'some_other_node', arity: { required: 1, optional: 0, variadic: false } }]);
    // join isn't in this registry → hand-map fallback still applies.
    expect(arityFor('join')).toBe('many');
    expect(sideEffectClassFor('fail')).toBe('terminal');
  });
});
