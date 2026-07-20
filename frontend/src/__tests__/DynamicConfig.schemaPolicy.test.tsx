/**
 * DynamicConfig — schema_policy dropdown smoke test.
 *
 * The acceptance criteria require that every sink's config block
 * shows a Schema-policy dropdown with the four options + tooltips.
 * Implementation: the backend emits a `select` field whose `options`
 * carry `{value, label, description}` objects; DynamicConfig's
 * `select` case renders them as `<option title>` AND inlines the
 * description for the currently-selected option below the select.
 *
 * This test seeds the window-level `__fpulse_node_types` cache (the
 * same global the production code reads via `getNodeMeta`) with a
 * minimal sink schema, mounts DynamicConfig, asserts the option set
 * + the inline description for the selected option, and verifies a
 * user change emits `onChange`.
 *
 * Why mock at the global rather than the network layer: DynamicConfig
 * reads node-type metadata synchronously from `window.__fpulse_node_types`
 * (populated once at app boot by the registry endpoint). Mocking it
 * here exercises the exact lookup path production uses.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';

import DynamicConfig from '../components/DynamicConfig';

const SINK_TYPE = 'local_table_sink';

const NODE_META = [
  {
    type: SINK_TYPE,
    label: 'Managed Table Sink',
    category: 'destination',
    description: '',
    default_params: {
      schema_name: 'default',
      table_name: '',
      mode: 'replace',
      schema_policy: 'add_columns',
    },
    param_schema: [
      { name: 'table_name', type: 'text', label: 'Table', required: true },
      {
        name: 'schema_policy',
        type: 'select',
        label: 'Schema policy',
        default: 'add_columns',
        options: [
          { value: 'strict', label: 'Strict — fail on any change',
            description: 'Refuse to write if anything differs.' },
          { value: 'add_columns', label: 'Add columns (default)',
            description: 'Allow new nullable columns; reject the rest.' },
          { value: 'compatible', label: 'Compatible — adds + widening',
            description: 'Allow new columns AND lossless type widening.' },
          { value: 'allow_all_with_warning', label: 'Allow all (warning)',
            description: 'Apply every change. Emits a warning event.' },
        ],
        description: 'Controls how this sink reacts to schema drift.',
        tab: 'Schema',
      },
    ],
  },
];

describe('DynamicConfig — schema_policy dropdown', () => {
  beforeEach(() => {
    (window as any).__fpulse_node_types = NODE_META;
    // DynamicConfig hides fields with defaults behind a "X defaults
    // applied" chip unless advanced mode is on. The Schema-policy
    // dropdown is one such field. Flip advanced ON for this suite so
    // we can interact with the select directly; that's the same path
    // a power-user reaches via the "Show all settings (advanced)"
    // toggle in the panel.
    try { localStorage.setItem(`fpulse.cfg.advanced.${SINK_TYPE}`, '1'); } catch { /* noop */ }
  });
  afterEach(() => {
    delete (window as any).__fpulse_node_types;
    try { localStorage.removeItem(`fpulse.cfg.advanced.${SINK_TYPE}`); } catch { /* noop */ }
  });

  function mount(initialPolicy = 'add_columns', onChange = vi.fn()) {
    const params = { table_name: 't1', schema_policy: initialPolicy };
    render(
      <DynamicConfig
        stepType={SINK_TYPE}
        params={params}
        nodeId="step-1"
        onChange={onChange}
      />,
    );
    return { onChange };
  }

  /** Find the schema_policy select by scanning for an <option value="strict">.
   * DynamicConfig doesn't wire htmlFor/id between its <label> and <select>,
   * so getByLabelText misses; this selector is robust to that and pins
   * specifically to the policy dropdown (no other field has strict/add_columns
   * options in this fixture). */
  function findPolicySelect(): HTMLSelectElement {
    const selects = Array.from(document.querySelectorAll<HTMLSelectElement>('select'));
    const match = selects.find((s) =>
      Array.from(s.options).some((o) => o.value === 'add_columns')
      && Array.from(s.options).some((o) => o.value === 'strict'),
    );
    if (!match) throw new Error('schema_policy select not found in DOM');
    return match;
  }

  it('renders all four policy options on the Schema tab', () => {
    mount();
    const schemaTab = screen.queryByRole('button', { name: 'Schema' });
    if (schemaTab) fireEvent.click(schemaTab);

    const select = findPolicySelect();
    const values = Array.from(select.options).map((o) => o.value);
    expect(values).toEqual(
      expect.arrayContaining(['strict', 'add_columns', 'compatible', 'allow_all_with_warning']),
    );
  });

  it('shows the description of the currently-selected option inline', () => {
    mount('compatible');
    const schemaTab = screen.queryByRole('button', { name: 'Schema' });
    if (schemaTab) fireEvent.click(schemaTab);
    // The description for `compatible` should be visible right under
    // the select — that's the per-option help text the spec asks for.
    expect(
      screen.getByText(/Allow new columns AND lossless type widening\./i),
    ).toBeInTheDocument();
  });

  it('emits onChange when the user picks a different policy', () => {
    const { onChange } = mount('add_columns');
    const schemaTab = screen.queryByRole('button', { name: 'Schema' });
    if (schemaTab) fireEvent.click(schemaTab);
    const select = findPolicySelect();
    fireEvent.change(select, { target: { value: 'allow_all_with_warning' } });
    expect(onChange).toHaveBeenCalledWith(
      'step-1',
      expect.objectContaining({ schema_policy: 'allow_all_with_warning' }),
    );
  });
});
