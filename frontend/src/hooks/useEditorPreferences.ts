/**
 * useEditorPreferences — single source of truth for the General-tab
 * settings that the editor canvas consumes.
 *
 * Reads from `localStorage['fpulse-settings']` on mount, then listens
 * for the `fpulse-settings-changed` window event so the editor reacts
 * live when the user saves Settings without needing a page reload.
 *
 * Settings page dispatches the event from handleSave():
 *     window.dispatchEvent(new CustomEvent('fpulse-settings-changed'));
 *
 * Defaults match what SettingsPage initializes its useState calls with,
 * so a fresh install with no saved preferences gets sensible behavior.
 */

import { useEffect, useState, useCallback } from 'react';

export type RunSafetyMode = 'live' | 'sample' | 'dry_run' | 'validate_only';

// 2026-05-26 — Canvas label density. Controls how much per-edge
// info the canvas paints by default. The motivating UX problem: at
// fan-in joins, every incoming edge stamps its own "On Success" +
// row-count pill at the edge midpoint, and the midpoints all
// cluster in the same vertical channel — a 4-source join already
// stacks 8 pills before reaching the node. A 30-node pipeline with
// joins / switches becomes a wall of badges.
//
// Three modes, matching the same info-density progression most
// flow tools land on (other flow-based / data-prep tools):
//   - clean    Edge stroke colour carries the condition; failure
//              edges keep their pill because failure deserves
//              weight. Selected edges always show full detail.
//              Row counts live on the node Result badge, not on
//              edges (the same info, anchored to one chip per node
//              instead of N chips per edge).
//   - metrics  Row counts surface on edges WHEN they're interesting
//              (delta vs upstream, or schema changed). Condition
//              pill shows for non-default conditions.
//   - verbose  Original behaviour — everything always visible.
//              Power-user mode for inspecting a small pipeline.
//
// Default `clean` because most 30-40 step pipelines look catastrophic
// in verbose; users who want the metrics can flip it back from the
// canvas density toggle (bottom-right of the editor).
export type CanvasLabelDensity = 'clean' | 'metrics' | 'verbose';

export interface EditorPreferences {
  autoSave: boolean;          // Auto-save pipeline canvas changes
  autoFitView: boolean;       // Fit-view after adding new nodes
  confirmDelete: boolean;     // Show confirm dialog before deleting
  showMinimap: boolean;       // Show React Flow MiniMap
  snapToGrid: boolean;        // Align nodes to a 20px grid when dragging
  defaultRunSafetyMode: RunSafetyMode; // Initial Run-toolbar mode
  // AI safety mode (May 17 2026 — Review #2 tweak): when true, the
  // Copilot disables write tools (apply_pipeline_draft, draft_alert_rule)
  // and always renders raw SQL/diffs alongside answers. Read-only tools
  // and the chat itself still work. Send as ``X-FPulse-AI-Safety: 1``
  // header on every /api/ai/agent call so the backend can echo it back
  // to the user as a banner ("Safety mode is on — writes will be blocked").
  aiSafetyMode: boolean;
  // C3 — schema-delta overlay on canvas nodes (2026-05-18). When ON,
  // each node renders a small "+2/~1/−1" chip showing how it changed
  // its input's schema. Default OFF because the chips add visual
  // density that power users love and casual users find noisy. Lives
  // alongside other display prefs (showMinimap).
  showSchemaDeltas: boolean;
  // 2026-05-26 — canvas label density (see CanvasLabelDensity above).
  labelDensity: CanvasLabelDensity;
  // 2026-05-26 — Pipeline Outline drawer (collapsible right rail).
  // Lists every node in topological order with status + row count, so
  // 30-40 step pipelines stay scannable without panning the canvas.
  showPipelineOutline: boolean;
}

const DEFAULTS: EditorPreferences = {
  autoSave: true,
  autoFitView: true,
  confirmDelete: true,
  showMinimap: true,
  snapToGrid: false,
  defaultRunSafetyMode: 'sample',
  aiSafetyMode: false,
  showSchemaDeltas: false,
  labelDensity: 'clean',
  showPipelineOutline: false,
};

function isCanvasLabelDensity(v: unknown): v is CanvasLabelDensity {
  return v === 'clean' || v === 'metrics' || v === 'verbose';
}

const STORAGE_KEY = 'fpulse-settings';
export const SETTINGS_CHANGED_EVENT = 'fpulse-settings-changed';

function readPreferences(): EditorPreferences {
  if (typeof window === 'undefined') return DEFAULTS;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULTS;
    const parsed = JSON.parse(raw) as {
      general?: Partial<EditorPreferences>;
      editor?: { defaultRunSafetyMode?: RunSafetyMode };
      ai?: { safetyMode?: boolean };
    };
    const general = parsed.general || {};
    const editor = parsed.editor || {};
    const ai = parsed.ai || {};
    const mode = editor.defaultRunSafetyMode;
    return {
      autoSave: typeof general.autoSave === 'boolean' ? general.autoSave : DEFAULTS.autoSave,
      autoFitView: typeof general.autoFitView === 'boolean' ? general.autoFitView : DEFAULTS.autoFitView,
      confirmDelete: typeof general.confirmDelete === 'boolean' ? general.confirmDelete : DEFAULTS.confirmDelete,
      showMinimap: typeof general.showMinimap === 'boolean' ? general.showMinimap : DEFAULTS.showMinimap,
      snapToGrid: typeof general.snapToGrid === 'boolean' ? general.snapToGrid : DEFAULTS.snapToGrid,
      defaultRunSafetyMode:
        mode === 'live' || mode === 'sample' || mode === 'dry_run' || mode === 'validate_only'
          ? mode
          : DEFAULTS.defaultRunSafetyMode,
      aiSafetyMode: typeof ai.safetyMode === 'boolean' ? ai.safetyMode : DEFAULTS.aiSafetyMode,
      showSchemaDeltas:
        typeof general.showSchemaDeltas === 'boolean'
          ? general.showSchemaDeltas
          : DEFAULTS.showSchemaDeltas,
      labelDensity: isCanvasLabelDensity((general as any).labelDensity)
        ? (general as any).labelDensity
        : DEFAULTS.labelDensity,
      showPipelineOutline:
        typeof (general as any).showPipelineOutline === 'boolean'
          ? (general as any).showPipelineOutline
          : DEFAULTS.showPipelineOutline,
    };
  } catch {
    return DEFAULTS;
  }
}

/**
 * Hook returning live editor preferences. Re-reads when SettingsPage
 * dispatches `fpulse-settings-changed`, so the canvas reacts without
 * a page reload.
 */
export function useEditorPreferences(): EditorPreferences {
  const [prefs, setPrefs] = useState<EditorPreferences>(() => readPreferences());

  useEffect(() => {
    const handler = () => setPrefs(readPreferences());
    window.addEventListener(SETTINGS_CHANGED_EVENT, handler);
    // Also listen to the cross-tab `storage` event so preferences sync
    // between two open windows of F-Pulse.
    const storageHandler = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) setPrefs(readPreferences());
    };
    window.addEventListener('storage', storageHandler);
    return () => {
      window.removeEventListener(SETTINGS_CHANGED_EVENT, handler);
      window.removeEventListener('storage', storageHandler);
    };
  }, []);

  return prefs;
}

/**
 * One-shot accessor for non-React contexts (e.g. inside an event handler
 * created outside the React lifecycle). Always returns the latest value
 * straight from localStorage; doesn't subscribe.
 */
export function getEditorPreferences(): EditorPreferences {
  return readPreferences();
}

/**
 * Helper for SettingsPage to call after saving — fires the event
 * other components are listening for.
 */
export function broadcastEditorPreferencesChanged(): void {
  if (typeof window === 'undefined') return;
  try {
    window.dispatchEvent(new CustomEvent(SETTINGS_CHANGED_EVENT));
  } catch {
    /* ignore */
  }
}

/**
 * Mutate one general-tab preference and broadcast the change.
 * Used by inline canvas controls (density toggle, outline button)
 * so they don't have to round-trip through SettingsPage. Keeps the
 * `fpulse-settings.general.*` shape SettingsPage owns.
 */
export function setGeneralPreference<K extends keyof EditorPreferences>(
  key: K,
  value: EditorPreferences[K],
): void {
  if (typeof window === 'undefined') return;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    const general = (parsed.general || {}) as Record<string, unknown>;
    general[key as string] = value;
    parsed.general = general;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(parsed));
    broadcastEditorPreferencesChanged();
  } catch {
    /* ignore — preference is a UX nicety, never block on persistence */
  }
}

/**
 * Convenience: returns the latest preferences AND a refresh callback.
 * Components that only need to re-read on mount (no live subscription)
 * can use this without the useEffect.
 */
export function useEditorPreferencesOnce(): [EditorPreferences, () => void] {
  const [prefs, setPrefs] = useState<EditorPreferences>(() => readPreferences());
  const refresh = useCallback(() => setPrefs(readPreferences()), []);
  return [prefs, refresh];
}
