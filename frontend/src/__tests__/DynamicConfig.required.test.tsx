/**
 * DynamicConfig — required-field marker (A1, 2026-06-15).
 *
 * Fields with `required: true` in param_schema already render up-front with
 * a `*` on the label. This test pins the added behavior: when such a field
 * is EMPTY, an inline "Required" hint appears; once filled, it disappears.
 * Auto-derived from param_schema — no per-node wiring.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';

import DynamicConfig from '../components/DynamicConfig';

const TYPE = 'local_table_sink';

const NODE_META = [
  {
    type: TYPE,
    label: 'Managed Table Sink',
    category: 'destination',
    description: '',
    default_params: { table_name: '' },
    param_schema: [
      { name: 'table_name', type: 'text', label: 'Table', required: true },
    ],
  },
];

describe('DynamicConfig — required-field marker', () => {
  beforeEach(() => { (window as any).__fpulse_node_types = NODE_META; });
  afterEach(() => { delete (window as any).__fpulse_node_types; });

  function mount(table_name: string) {
    render(
      <DynamicConfig stepType={TYPE} params={{ table_name }} nodeId="s1" onChange={vi.fn()} />,
    );
  }

  it('shows an inline Required hint when a required field is empty', () => {
    mount('');
    expect(screen.getByText(/Required — please fill this in\./i)).toBeInTheDocument();
  });

  it('hides the hint once the required field is filled', () => {
    mount('orders');
    expect(screen.queryByText(/Required — please fill this in\./i)).not.toBeInTheDocument();
  });
});
