/**
 * CopyrightFooter
 * ─────────────────────────────────────────────────────────────────────────
 * Hybridyn brand mark pinned to the bottom-LEFT corner. Each letter of
 * "@hybridyn" cycles through the brand palette (gold / green / red) —
 * the three colours of the F-Pulse logo's refresh-cycle motif. Apr 18
 * revision: removed the drop-shadow glow (looked muddy against the light
 * page backgrounds) and bumped the font one step for better legibility.
 */
import { useDarkMode } from '../hooks/useDarkMode';

// 2026-06-15 — palette re-sampled from the actual F-Pulse logo mark
// (public/fpulse-logo-mark.png): amber-gold "F", green top arrow, red
// bottom arrow. The old palette's blue (#3B82F6) was never a brand colour.
const BRAND_COLORS = [
  '#E0A030',   // gold  — the logo "F"
  '#10A060',   // green — top refresh arrow
  '#B03030',   // red   — bottom refresh arrow
];

export default function CopyrightFooter({ environment = 'dev' }: { environment?: 'dev' | 'prod' } = {}) {
  const dark = useDarkMode();
  const isProd = environment === 'prod';
  const year = new Date().getFullYear();

  const brand = '@hybridyn'.split('');

  return (
    <div
      className="fixed bottom-2.5 left-4 z-30 pointer-events-none select-none"
      aria-label="Copyright — Hybridyn"
    >
      <a
        href="https://hybridyn.com"
        target="_blank"
        rel="noopener noreferrer"
        className={`pointer-events-auto inline-flex items-baseline gap-1.5 transition-colors ${
          isProd || dark ? 'text-slate-300' : 'text-slate-500'
        }`}
        title="Built by Hybridyn"
      >
        <span
          className="text-base font-extrabold tracking-tight"
          style={{ color: dark || isProd ? '#F8FAFC' : '#000000' }}
        >
          © {year}
        </span>
        <span className="text-base font-extrabold tracking-tight">
          {brand.map((ch, i) => (
            <span
              key={i}
              style={{ color: BRAND_COLORS[i % BRAND_COLORS.length] }}
            >
              {ch}
            </span>
          ))}
        </span>
      </a>
    </div>
  );
}
