/**
 * CanvasCopyrightMark — Hybridyn brand mark pinned to the bottom-left
 * of the Editor's canvas column. The global CopyrightFooter is fixed
 * to the viewport's bottom-left and gets covered by the Assistant
 * panel on the Editor page, so we render a second copy *inside* the
 * canvas column where it's always visible.
 *
 * Same letter palette + typography as the global footer so the brand
 * mark reads as one consistent element wherever it appears.
 */

import { useDarkMode } from '../hooks/useDarkMode';

// 2026-06-15 — matches CopyrightFooter: palette sampled from the F-Pulse
// logo mark (gold "F", green + red refresh arrows). No blue.
const BRAND_COLORS = [
  '#E0A030',   // gold  — the logo "F"
  '#10A060',   // green — top refresh arrow
  '#B03030',   // red   — bottom refresh arrow
];

export default function CanvasCopyrightMark() {
  const dark = useDarkMode();
  const year = new Date().getFullYear();
  const brand = '@hybridyn'.split('');

  return (
    <a
      href="https://hybridyn.com"
      target="_blank"
      rel="noopener noreferrer"
      className="absolute bottom-3 left-3 z-10 inline-flex items-baseline gap-1.5 select-none"
      title="Built by Hybridyn"
    >
      <span
        className="text-base font-extrabold tracking-tight"
        style={{ color: dark ? '#F8FAFC' : '#000000' }}
      >
        © {year}
      </span>
      <span className="text-base font-extrabold tracking-tight">
        {brand.map((ch, i) => (
          <span key={i} style={{ color: BRAND_COLORS[i % BRAND_COLORS.length] }}>
            {ch}
          </span>
        ))}
      </span>
    </a>
  );
}
