import { useState } from 'react';
import { fireEvent, render, screen, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import RowActionsPopover from '../components/shared/RowActionsPopover';

function Fixture() {
  const [open, setOpen] = useState(false);
  return <><RowActionsPopover open={open} onOpenChange={setOpen}>
    <button>Publish</button><button disabled>Unavailable</button><button>Copy</button>
  </RowActionsPopover><button>Outside</button></>;
}

describe('pipeline row actions', () => {
  beforeEach(() => vi.stubGlobal('ResizeObserver', class {
    observe() {} disconnect() {}
  }));
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it('opens a portal with a named trigger and focuses the first action', () => {
    const { container } = render(<Fixture />);
    const trigger = screen.getByRole('button', { name: 'More actions' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(screen.getByRole('button', { name: 'Publish' })).toHaveFocus();
  });

  it('navigates enabled actions and restores trigger focus on Escape', () => {
    render(<Fixture />);
    const trigger = screen.getByRole('button', { name: 'More actions' });
    fireEvent.click(trigger);
    const dialog = screen.getByRole('dialog', { name: 'Pipeline actions' });
    fireEvent.keyDown(dialog, { key: 'ArrowDown' });
    expect(screen.getByRole('button', { name: 'Copy' })).toHaveFocus();
    fireEvent.keyDown(dialog, { key: 'Home' });
    expect(screen.getByRole('button', { name: 'Publish' })).toHaveFocus();
    fireEvent.keyDown(dialog, { key: 'End' });
    expect(screen.getByRole('button', { name: 'Copy' })).toHaveFocus();
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it('dismisses on outside pointer and focus departure', () => {
    render(<Fixture />);
    const trigger = screen.getByRole('button', { name: 'More actions' });
    fireEvent.click(trigger);
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Outside' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(trigger);
    fireEvent.blur(screen.getByRole('dialog'), { relatedTarget: screen.getByRole('button', { name: 'Outside' }) });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('places the panel above a low trigger and within the viewport', () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      return (this.getAttribute('role') === 'dialog'
        ? { width: 224, height: 200, left: 0, right: 224, top: 0, bottom: 200 }
        : { width: 28, height: 28, left: 10, right: 38, top: 740, bottom: 768 }) as DOMRect;
    });
    render(<Fixture />);
    fireEvent.click(screen.getByRole('button', { name: 'More actions' }));
    expect(screen.getByRole('dialog')).toHaveStyle({ left: '8px', top: '536px' });
  });
});
