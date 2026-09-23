import { act, renderHook } from '@testing-library/react';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { useCompactEditorPanels } from '../hooks/useCompactEditorPanels';
import { useWorkflowStore } from '../stores/workflowStore';

describe('compact editor panels', () => {
  let media: MediaQueryList;
  let changed: () => void;
  beforeEach(() => {
    media = { matches: true, addEventListener: vi.fn((_event, callback) => { changed = callback; }), removeEventListener: vi.fn() } as unknown as MediaQueryList;
    vi.stubGlobal('matchMedia', () => media);
    useWorkflowStore.setState({ nodesPanelOpen: true, chatOpen: true });
  });
  afterEach(() => vi.unstubAllGlobals());
  it('starts with rails and opens only one overlay at a time', () => {
    const { unmount } = renderHook(() => useCompactEditorPanels(true));
    expect(useWorkflowStore.getState().nodesPanelOpen).toBe(false);
    expect(useWorkflowStore.getState().chatOpen).toBe(false);
    act(() => useWorkflowStore.getState().setChatOpen(true));
    act(() => useWorkflowStore.getState().setNodesPanelOpen(true));
    expect(useWorkflowStore.getState().chatOpen).toBe(false);
    act(() => useWorkflowStore.getState().setChatOpen(true));
    expect(useWorkflowStore.getState().nodesPanelOpen).toBe(false);
    act(() => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })));
    expect(useWorkflowStore.getState().chatOpen).toBe(false);
    unmount();
    expect(media.removeEventListener).toHaveBeenCalled();
  });
  it('preserves desktop panels and collapses when entering compact width', () => {
    Object.assign(media, { matches: false });
    const { unmount } = renderHook(() => useCompactEditorPanels(true));
    expect(useWorkflowStore.getState().nodesPanelOpen).toBe(true);
    expect(useWorkflowStore.getState().chatOpen).toBe(true);
    Object.assign(media, { matches: true });
    act(() => changed());
    expect(useWorkflowStore.getState().nodesPanelOpen).toBe(false);
    unmount();
  });
  it('leaves other routes alone', () => {
    renderHook(() => useCompactEditorPanels(false));
    expect(useWorkflowStore.getState().chatOpen).toBe(true);
    expect(media.addEventListener).not.toHaveBeenCalled();
  });
});
