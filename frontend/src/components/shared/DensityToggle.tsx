/**
 * DensityToggle — Compact / Comfortable / Spacious switch.
 *
 * Phase 1 foundation. Per design decision D-003, default is Comfortable.
 * Stored per-page in localStorage under `fpulse_table_density:<scope>` so
 * different list pages can have independent preferences.
 *
 * Returns:
 *   - density value (string)
 *   - rowPaddingClass (Tailwind py-* string for table rows)
 *   - cellPaddingClass (px-* string)
 *
 * Pages render the toggle inline with their other view controls and use
 * the row padding class on their `<tr>` / row container.
 */

import { useCallback, useEffect, useState } from 'react';

export type Density = 'compact' | 'comfortable' | 'spacious';

const DENSITY_OPTIONS: { value: Density; label: string; rowPad: string; cellPad: string }[] = [
  { value: 'compact',     label: 'Compact',     rowPad: 'py-1.5', cellPad: 'px-3' },
  { value: 'comfortable', label: 'Comfortable', rowPad: 'py-3',   cellPad: 'px-3' },
  { value: 'spacious',    label: 'Spacious',    rowPad: 'py-4',   cellPad: 'px-4' },
];

const STORAGE_KEY = 'fpulse_table_density:';

export function useDensity(scope: string, defaultValue: Density = 'compact') {
  const [density, setDensityState] = useState<Density>(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY + scope);
      if (raw === 'compact' || raw === 'comfortable' || raw === 'spacious') return raw;
    } catch { /* ignore */ }
    return defaultValue;
  });

  const setDensity = useCallback((d: Density) => {
    setDensityState(d);
    try {
      localStorage.setItem(STORAGE_KEY + scope, d);
    } catch { /* ignore */ }
  }, [scope]);

  // Cross-tab sync — if the user changes density in another tab, pick it up.
  useEffect(() => {
    const handler = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY + scope && e.newValue) {
        if (e.newValue === 'compact' || e.newValue === 'comfortable' || e.newValue === 'spacious') {
          setDensityState(e.newValue);
        }
      }
    };
    window.addEventListener('storage', handler);
    return () => window.removeEventListener('storage', handler);
  }, [scope]);

  const opt = DENSITY_OPTIONS.find(o => o.value === density) ?? DENSITY_OPTIONS[1];

  return {
    density,
    setDensity,
    rowPaddingClass: opt.rowPad,
    cellPaddingClass: opt.cellPad,
  };
}

export interface DensityToggleProps {
  density: Density;
  onChange: (d: Density) => void;
  /** Compact button labels (icons only) when narrow. */
  iconOnly?: boolean;
}

// React 19 dropped the global JSX namespace shim; use React.JSX.Element
// or React.ReactElement instead. ReactElement is the most portable.
const ICONS: Record<Density, import('react').ReactElement> = {
  compact: (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <line x1="3" y1="6" x2="21" y2="6" />
      <line x1="3" y1="10" x2="21" y2="10" />
      <line x1="3" y1="14" x2="21" y2="14" />
      <line x1="3" y1="18" x2="21" y2="18" />
    </svg>
  ),
  comfortable: (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <line x1="3" y1="6" x2="21" y2="6" />
      <line x1="3" y1="12" x2="21" y2="12" />
      <line x1="3" y1="18" x2="21" y2="18" />
    </svg>
  ),
  spacious: (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <line x1="3" y1="7" x2="21" y2="7" />
      <line x1="3" y1="17" x2="21" y2="17" />
    </svg>
  ),
};

export function DensityToggle({ density, onChange, iconOnly = false }: DensityToggleProps) {
  return (
    <div
      className="inline-flex bg-slate-100 rounded-lg p-0.5"
      role="group"
      aria-label="Table density"
    >
      {DENSITY_OPTIONS.map(o => {
        const active = o.value === density;
        return (
          <button
            key={o.value}
            type="button"
            onClick={() => onChange(o.value)}
            title={o.label}
            aria-pressed={active}
            className={[
              'inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs font-semibold transition-colors',
              active ? 'bg-white shadow-sm text-slate-700' : 'text-slate-500 hover:text-slate-700',
            ].join(' ')}
          >
            {ICONS[o.value]}
            {!iconOnly && <span>{o.label}</span>}
          </button>
        );
      })}
    </div>
  );
}

export default DensityToggle;
