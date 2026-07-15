/**
 * CanvasDensityToggle — small floating control that lets the user
 * switch how much info the canvas paints — on BOTH nodes AND edges.
 *
 * Anchors to the bottom-right of the canvas (above the MiniMap when
 * the minimap is on; the minimap stays in its xyflow-controlled
 * position so we leave a gap). Three buttons mirror the
 * CanvasLabelDensity enum (clean / metrics / verbose).
 *
 * Scope expanded 2026-06-02: previously controlled per-edge pill
 * density only. Now also drives node-text density in FPulseNode.tsx:
 *   - clean   nodes show icon + title only (subtitle hidden)
 *   - metrics nodes show title + subtitle (type label)
 *   - verbose nodes additionally show a param-preview line
 * Edge behaviour from the original implementation is unchanged.
 *
 * Why a top-level canvas control instead of burying this in
 * Settings → General: the noise that motivates this preference is
 * visible IN the canvas, so the lever to fix it lives IN the
 * canvas — discoverability beats consistency here.
 */

import { useEditorPreferences, setGeneralPreference, type CanvasLabelDensity } from '../hooks/useEditorPreferences';

const MODES: { key: CanvasLabelDensity; label: string; hint: string; icon: React.ReactNode }[] = [
  {
    key: 'clean',
    label: 'Clean',
    hint: 'Compact. Nodes: icon + name only. Edges: labels hidden. Best for big pipelines you want to scan structurally.',
    icon: (
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <line x1="5" y1="12" x2="19" y2="12" />
      </svg>
    ),
  },
  {
    key: 'metrics',
    label: 'Metrics',
    hint: 'Balanced. Nodes add the type subtitle. Edges add row counts and schema deltas when interesting.',
    icon: (
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <line x1="4" y1="20" x2="4" y2="10" />
        <line x1="10" y1="20" x2="10" y2="4" />
        <line x1="16" y1="20" x2="16" y2="14" />
      </svg>
    ),
  },
  {
    key: 'verbose',
    label: 'Verbose',
    hint: 'Full detail. Nodes show a param-preview line. Every edge label shown. Useful for small pipelines or debugging.',
    icon: (
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <line x1="3" y1="6" x2="21" y2="6" />
        <line x1="3" y1="12" x2="21" y2="12" />
        <line x1="3" y1="18" x2="21" y2="18" />
      </svg>
    ),
  },
];

export default function CanvasDensityToggle() {
  const { labelDensity, showPipelineOutline, showMinimap } = useEditorPreferences();

  // Sit above the minimap when present; when off, hug the bottom-right.
  // Minimap default size in Canvas.tsx is 160×110 + ~12 px shadow.
  const bottomOffset = showMinimap ? 132 : 12;

  return (
    <div
      className="absolute right-3 z-20 flex flex-col items-end gap-2"
      style={{ bottom: bottomOffset }}
      onMouseDown={(e) => e.stopPropagation()}
    >
      {/* Outline drawer toggle — pairs naturally with density (both
          are "how much do I want to see?" controls). */}
      <button
        onClick={() => setGeneralPreference('showPipelineOutline', !showPipelineOutline)}
        className={`flex items-center gap-1.5 px-2 py-1 rounded-lg border shadow-sm text-[10px] font-semibold uppercase tracking-wider transition-colors ${
          showPipelineOutline
            ? 'bg-amber-50 border-amber-200 text-amber-700 hover:bg-amber-100'
            : 'bg-white/95 backdrop-blur-sm border-slate-200 text-slate-600 hover:bg-slate-50'
        }`}
        title={showPipelineOutline ? 'Hide pipeline outline' : 'Show pipeline outline — scannable list of all steps'}
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="8" y1="6" x2="21" y2="6" />
          <line x1="8" y1="12" x2="21" y2="12" />
          <line x1="8" y1="18" x2="21" y2="18" />
          <line x1="3" y1="6" x2="3.01" y2="6" />
          <line x1="3" y1="12" x2="3.01" y2="12" />
          <line x1="3" y1="18" x2="3.01" y2="18" />
        </svg>
        Outline
      </button>

      {/* Density button group. */}
      <div className="flex bg-white/95 backdrop-blur-sm border border-slate-200 rounded-lg shadow-sm overflow-hidden">
        {MODES.map((m) => {
          const active = labelDensity === m.key;
          return (
            <button
              key={m.key}
              onClick={() => setGeneralPreference('labelDensity', m.key)}
              className={`flex items-center gap-1 px-2 py-1 text-[10px] font-semibold uppercase tracking-wider transition-colors border-r border-slate-200 last:border-r-0 ${
                active
                  ? 'bg-amber-50 text-amber-700'
                  : 'text-slate-600 hover:bg-slate-50'
              }`}
              title={m.hint}
            >
              {m.icon}
              {m.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
