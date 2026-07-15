/**
 * nodeUiContract deriver tests (2026-06-16) — the Data In / Data Out
 * descriptors that back the NodeConfigFrame shell.
 */
import { describe, expect, it } from 'vitest';
import { buildDataIn, buildDataOut, deriveOutputColumns } from '../utils/nodeUiContract';

const IN = (columns: string[]) => [{ label: 'Source', columns }];

describe('buildDataIn', () => {
  it('join shows Left + Right slots even with nothing connected', () => {
    const d = buildDataIn('join', []);
    expect(d.required).toBe(2);
    expect(d.ports.map((p) => p.role)).toEqual(['Left', 'Right']);
    expect(d.ports.every((p) => p.required && !p.connected)).toBe(true);
    expect(d.note).toContain('Left and Right');
  });

  it('join maps the first resolved input to Left, second to Right', () => {
    const d = buildDataIn('join', [
      { label: 'Orders', columns: ['id', 'amount'] },
      { label: 'Customers', columns: ['id', 'name'] },
    ]);
    expect(d.ports[0].role).toBe('Left');
    expect(d.ports[0].label).toBe('Orders');
    expect(d.ports[0].columns).toContain('amount');
    expect(d.ports[1].role).toBe('Right');
    expect(d.ports[1].label).toBe('Customers');
    expect(d.ports.every((p) => p.connected)).toBe(true);
  });

  it('join with one connected input leaves the Right side unconnected', () => {
    const d = buildDataIn('join', [{ label: 'Orders', columns: ['id'] }]);
    expect(d.ports[0].connected).toBe(true);
    expect(d.ports[1].connected).toBe(false);
    expect(d.ports[1].role).toBe('Right');
  });

  it('single-input node has one "Input" port', () => {
    const d = buildDataIn('filter', [{ label: 'Source', columns: ['a'] }]);
    expect(d.ports).toHaveLength(1);
    expect(d.ports[0].role).toBe('Input');
    expect(d.required).toBe(1);
  });
});

describe('buildDataOut', () => {
  it('join produces rows with the union of input columns', () => {
    const d = buildDataOut('join', {}, ['id', 'amount', 'name']);
    expect(d.disposition).toBe('rows');
    expect(d.ports).toHaveLength(1);
    expect(d.columns).toEqual(['id', 'amount', 'name']);
  });

  it('if_condition branches to True/False', () => {
    const d = buildDataOut('if_condition', {});
    expect(d.disposition).toBe('branches');
    expect(d.ports.map((p) => p.label)).toEqual(['True', 'False']);
  });

  it('a sink is passthrough', () => {
    expect(buildDataOut('db_sink', {}).disposition).toBe('passthrough');
  });

  it('an action is transformed; fail is terminal', () => {
    expect(buildDataOut('http_request', {}).disposition).toBe('transformed');
    expect(buildDataOut('fail', {}).disposition).toBe('terminal');
  });

  it('side-effect nodes carry a consequence note; pure transforms do not', () => {
    const sink = buildDataOut('db_sink', {});
    expect(sink.sideEffect).not.toBeNull();
    expect(typeof sink.sideEffectNote).toBe('string');
    const pure = buildDataOut('filter', {});
    expect(pure.sideEffect).toBeNull();
    expect(pure.sideEffectNote).toBeNull();
  });

  it('flags dynamic-schema row nodes so the band can say "known after first run"', () => {
    expect(buildDataOut('pivot', {}).schemaDynamic).toBe(true);
    expect(buildDataOut('transform', {}).schemaDynamic).toBe(true);
    expect(buildDataOut('filter', {}).schemaDynamic).toBe(false);
    expect(buildDataOut('db_sink', {}).schemaDynamic).toBe(false);
  });
});

describe('deriveOutputColumns', () => {
  it('pass-through nodes keep the input column names', () => {
    expect(deriveOutputColumns('filter', IN(['a', 'b']), {})).toEqual(['a', 'b']);
    expect(deriveOutputColumns('sort', IN(['a', 'b']), {})).toEqual(['a', 'b']);
    expect(deriveOutputColumns('deduplicate', IN(['a', 'b']), {})).toEqual(['a', 'b']);
    expect(deriveOutputColumns('typecast', IN(['a', 'b']), {})).toEqual(['a', 'b']);
  });

  it('derived_column appends the new column(s)', () => {
    expect(deriveOutputColumns('derived_column', IN(['a']), { columns: [{ name: 'c' }] }))
      .toEqual(['a', 'c']);
    expect(deriveOutputColumns('derived_column', IN(['a']), { name: 'c' })).toEqual(['a', 'c']);
  });

  it('select keeps only the chosen columns; rename applies the mapping', () => {
    expect(deriveOutputColumns('select', IN(['a', 'b', 'c']), { columns: ['a', 'c'] }))
      .toEqual(['a', 'c']);
    expect(deriveOutputColumns('rename', IN(['a', 'b']), { mappings: { a: 'x' } }))
      .toEqual(['x', 'b']);
  });

  it('returns [] for dynamic schemas — only known after a run', () => {
    expect(deriveOutputColumns('pivot', IN(['a', 'b']), {})).toEqual([]);
    expect(deriveOutputColumns('transform', IN(['a', 'b']), {})).toEqual([]);
    expect(deriveOutputColumns('unpivot', IN(['a', 'b']), {})).toEqual([]);
  });

  it('derives merges (join/lookup) as the combined column set', () => {
    const inputs = [
      { label: 'Orders', columns: ['id', 'amount'] },
      { label: 'Customers', columns: ['id', 'name'] },
    ];
    expect(deriveOutputColumns('join', inputs, {})).toEqual(['id', 'amount', 'name']);
    expect(deriveOutputColumns('lookup', inputs, {})).toEqual(['id', 'amount', 'name']);
  });

  it('derives aggregate as group-by keys + aggregate aliases', () => {
    const cols = deriveOutputColumns('aggregate', IN(['region', 'amount']), {
      group_by: ['region'],
      functions: [{ function: 'sum', column: 'amount', alias: 'total' }],
    });
    expect(cols).toEqual(['region', 'total']);
  });

  it('returns [] for side-effect / control nodes', () => {
    expect(deriveOutputColumns('db_sink', IN(['a', 'b']), {})).toEqual([]);
    expect(deriveOutputColumns('if_condition', IN(['a', 'b']), {})).toEqual([]);
  });

  it('returns [] when no input columns are available', () => {
    expect(deriveOutputColumns('filter', [], {})).toEqual([]);
  });
});
