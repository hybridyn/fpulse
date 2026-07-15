import { describe, it, expect } from 'vitest';
import { autoMapSchema, bestSourceMatch, normalizeName } from '../utils/schemaAutoMap';

describe('schemaAutoMap', () => {
  it('normalizes naming styles to a common form', () => {
    expect(normalizeName('Customer_ID')).toBe('customerid');
    expect(normalizeName('customer id')).toBe('customerid');
    expect(normalizeName('CustomerID')).toBe('customerid');
  });

  it('bootstraps an empty grid straight-through from source columns', () => {
    const out = autoMapSchema([], ['id', 'name', 'amount']);
    expect(out).toEqual([
      { source: 'id', target: 'id', type: 'string', default: '' },
      { source: 'name', target: 'name', type: 'string', default: '' },
      { source: 'amount', target: 'amount', type: 'string', default: '' },
    ]);
  });

  it('fills blank sources on an existing target schema by fuzzy match', () => {
    const rows = [
      { source: '', target: 'customer_id', type: 'int' },
      { source: '', target: 'full_name', type: 'string' },
    ];
    const out = autoMapSchema(rows, ['CustomerID', 'FullName', 'extra']);
    expect(out[0].source).toBe('CustomerID');
    expect(out[1].source).toBe('FullName');
    // existing targets/types are preserved; no straight-through append
    expect(out).toHaveLength(2);
    expect(out[0].target).toBe('customer_id');
    expect(out[0].type).toBe('int');
  });

  it('never overwrites a source the user already set, and avoids double-using a column', () => {
    const rows = [
      { source: 'name', target: 'full_name' },
      { source: '', target: 'name' },        // would also want "name", but it's taken
    ];
    const out = autoMapSchema(rows, ['name']);
    expect(out[0].source).toBe('name');       // untouched
    expect(out[1].source).toBe('');           // no column left to claim
  });

  it('bestSourceMatch ranks exact over prefix over substring', () => {
    const used = new Set<string>();
    expect(bestSourceMatch('id', ['user_id', 'id'], used)).toBe('id');           // exact
    expect(bestSourceMatch('cust', ['customer'], used)).toBe('customer');         // prefix
    expect(bestSourceMatch('order_total', ['total'], used)).toBe('total');        // substring
    expect(bestSourceMatch('zzz', ['aaa', 'bbb'], used)).toBe('');                // none
  });
});
