import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import HeroPromptCard from '../components/editor/HeroPromptCard';

vi.mock('../hooks/useAgentChatStore', () => ({ askCopilot: vi.fn() }));
vi.mock('../stores/workflowStore', () => ({
  useWorkflowStore: (selector: (state: { useTemplate: () => void }) => unknown) => selector({ useTemplate: vi.fn() }),
}));

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('empty editor layout', () => {
  it('provides a named input in a scrollable region', () => {
    render(<HeroPromptCard />);
    expect(screen.getByRole('textbox', { name: 'Pipeline description' })).toBeVisible();
    expect(screen.getByTestId('empty-editor-prompt')).toHaveClass('overflow-y-auto');
  });

  it('constrains template width on mobile and dismisses with Escape', () => {
    vi.stubGlobal('innerWidth', 390);
    render(<HeroPromptCard />);
    const trigger = screen.getByRole('button', { name: 'Use template' });
    fireEvent.click(trigger);
    const popover = document.querySelector('[data-template-popover]');
    expect(popover).toHaveStyle({ width: '374px', left: '8px' });
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    fireEvent.keyDown(popover!, { key: 'Escape' });
    expect(document.querySelector('[data-template-popover]')).toBeNull();
    expect(trigger).toHaveFocus();
  });
});
