/**
 * Live {{ }} expression preview (C4, 2026-06-15).
 *
 * The component renders nothing for a plain value, and for a {{ }} expression
 * it debounces, calls the backend resolver with the selected node's upstream
 * sample row (+ every node's samples for $('Node') refs), and shows the result.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';

const STATE = {
  selectedNodeId: 's2',
  edges: [{ source: 's1', target: 's2' }],
  nodes: [{ id: 's1', data: { label: 'Source' } }],
  stepResults: { s1: { sample_data: [{ name: 'Ada' }] } },
};

vi.mock('../stores/workflowStore', () => {
  const useWorkflowStore: any = (sel: any) => sel(STATE);
  useWorkflowStore.getState = () => STATE;
  return { useWorkflowStore };
});

const { previewExpression } = vi.hoisted(() => ({ previewExpression: vi.fn() }));
vi.mock('../api/client', () => ({ api: { previewExpression } }));

import ExpressionPreview from '../components/ExpressionPreview';

describe('ExpressionPreview', () => {
  beforeEach(() => {
    previewExpression.mockReset();
    previewExpression.mockResolvedValue({ ok: true, result: 'Ada', value_type: 'str' });
  });

  it('renders nothing for a plain (non-expression) value', () => {
    const { container } = render(<ExpressionPreview value="plain text" />);
    expect(container.querySelector('[data-testid="expr-preview"]')).toBeNull();
    expect(previewExpression).not.toHaveBeenCalled();
  });

  it('resolves a {{ }} expression against the upstream sample row', async () => {
    render(<ExpressionPreview value="{{ $json.name }}" />);
    await waitFor(() => expect(screen.getByText('Ada')).toBeInTheDocument(), { timeout: 2000 });
    expect(previewExpression).toHaveBeenCalledWith(
      expect.objectContaining({
        expression: '{{ $json.name }}',
        sample_row: { name: 'Ada' },
        node_samples: { Source: [{ name: 'Ada' }] },
      }),
    );
  });

  it('shows an inline hint when the expression fails', async () => {
    previewExpression.mockResolvedValueOnce({ ok: false, error: "no field 'x' on item" } as any);
    render(<ExpressionPreview value="{{ $json.x }}" />);
    await waitFor(() => expect(screen.getByText(/no field 'x'/)).toBeInTheDocument(), { timeout: 2000 });
  });
});
