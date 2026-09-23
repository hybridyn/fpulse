import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Toggle } from '../components/pages/SettingsPage';

describe('settings switch accessibility', () => {
  it('exposes its name and checked state and retains toggle behavior', () => {
    const onChange = vi.fn();
    const { rerender } = render(<Toggle enabled={false} onChange={onChange} label="Show minimap" />);
    const control = screen.getByRole('switch', { name: 'Show minimap' });
    expect(control).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(control);
    expect(onChange).toHaveBeenCalledWith(true);
    rerender(<Toggle enabled onChange={onChange} label="Show minimap" />);
    expect(control).toHaveAttribute('aria-checked', 'true');
  });
});
