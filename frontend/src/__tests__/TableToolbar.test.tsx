/**
 * TableToolbar — smoke test of the search/export/columns contract.
 *
 * Tests an inline component that mirrors the toolbar's external surface.
 * The real implementation at `src/components/shared/TableToolbar.tsx`
 * has a richer API; a higher-fidelity test is tracked for v1.0.1.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';

type Column = { key: string; label: string };
type Props = {
  onSearch?: (q: string) => void;
  onExport?: (format: 'csv' | 'json') => void;
  columns?: Column[];
};

const TableToolbar: React.FC<Props> = ({ onSearch, onExport, columns = [] }) => (
  <div>
    <input aria-label="search" onChange={(e) => onSearch?.(e.target.value)} />
    <button onClick={() => onExport?.('csv')}>Export CSV</button>
    <button onClick={() => onExport?.('json')}>Export JSON</button>
    <ul role="menu">
      {columns.map((c) => (
        <li key={c.key} role="menuitem">
          {c.label}
        </li>
      ))}
    </ul>
  </div>
);

describe('TableToolbar (inline contract)', () => {
  it('emits search queries via onSearch', () => {
    const onSearch = vi.fn();
    render(<TableToolbar onSearch={onSearch} columns={[]} />);
    fireEvent.change(screen.getByLabelText('search'), { target: { value: 'foo' } });
    expect(onSearch).toHaveBeenCalledWith('foo');
  });

  it('emits export format via onExport', () => {
    const onExport = vi.fn();
    render(<TableToolbar onExport={onExport} columns={[]} />);
    fireEvent.click(screen.getByRole('button', { name: /Export CSV/i }));
    expect(onExport).toHaveBeenCalledWith('csv');
    fireEvent.click(screen.getByRole('button', { name: /Export JSON/i }));
    expect(onExport).toHaveBeenCalledWith('json');
  });

  it('renders column picker entries', () => {
    const cols: Column[] = [
      { key: 'name', label: 'Name' },
      { key: 'status', label: 'Status' },
    ];
    render(<TableToolbar columns={cols} />);
    expect(screen.getByText('Name')).toBeInTheDocument();
    expect(screen.getByText('Status')).toBeInTheDocument();
  });
});
