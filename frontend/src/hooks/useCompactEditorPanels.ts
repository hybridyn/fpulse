import { useEffect } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';

/** Narrow editors keep both rails reachable without shrinking the canvas. */
export function useCompactEditorPanels(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return;
    const media = window.matchMedia('(max-width: 1279px)');
    const collapse = () => {
      if (media.matches) {
        useWorkflowStore.setState({ nodesPanelOpen: false, chatOpen: false });
      }
    };
    collapse();
    media.addEventListener('change', collapse);
    const unsubscribe = useWorkflowStore.subscribe((state, previous) => {
      if (!media.matches) return;
      if (state.nodesPanelOpen && !previous.nodesPanelOpen && state.chatOpen) {
        state.setChatOpen(false);
      } else if (state.chatOpen && !previous.chatOpen && state.nodesPanelOpen) {
        state.setNodesPanelOpen(false);
      }
    });
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && media.matches) collapse();
    };
    window.addEventListener('keydown', escape);
    return () => {
      unsubscribe();
      media.removeEventListener('change', collapse);
      window.removeEventListener('keydown', escape);
    };
  }, [enabled]);
}
