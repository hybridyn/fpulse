/**
 * EmptyState — designed empty-state component used by every list page.
 *
 * Phase 1 foundation. Solves the "vast empty fluid container on a wide
 * screen" problem the user flagged: when a page has no data, this fills
 * the void with a centered illustration + clear next-step CTAs instead
 * of a lonely message floating in 1900px of whitespace.
 *
 * Usage pattern:
 *   {pipelines.length === 0 ? (
 *     <EmptyState
 *       icon={<PipelineIcon />}
 *       title="No pipelines yet"
 *       body="Build a pipeline to start moving data."
 *       primaryCta={{ label: '+ New Pipeline', onClick: handleNewPipeline }}
 *       secondaryCtas={[
 *         { label: 'Start from a template', onClick: openTemplates },
 *         { label: 'Open the tutorial', onClick: openTutorial },
 *       ]}
 *     />
 *   ) : (
 *     <Table />
 *   )}
 *
 * The illustration slot accepts any ReactNode — pages can pass a node-type
 * SVG, an emoji, a small Lottie, or just a colored circle.
 */

import { ReactNode } from 'react';

export interface CTAProps {
  label: string;
  onClick?: () => void;
  href?: string;
  variant?: 'primary' | 'secondary' | 'ghost';
}

export interface EmptyStateProps {
  /** Illustration / icon (centered above the title). Pass an SVG, emoji, or
   *  a colored circle. Sized to ~64-80px square via Tailwind classes on the
   *  parent. Not required — when omitted, just title + body + CTAs. */
  icon?: ReactNode;
  /** Headline (16-20px, semibold). */
  title: string;
  /** Body copy (12-14px, slate-600). Plain string or ReactNode. */
  body?: ReactNode;
  /** Primary action — large gradient button. */
  primaryCta?: CTAProps;
  /** Up to 3 secondary actions — outlined buttons. */
  secondaryCtas?: CTAProps[];
  /** Optional hint text below CTAs. */
  hint?: ReactNode;
}

function CTAButton({ cta }: { cta: CTAProps }) {
  const v = cta.variant ?? 'secondary';
  const cls = {
    primary:
      'inline-flex items-center justify-center gap-1.5 px-4 py-2 text-sm font-semibold rounded-lg bg-gradient-to-r from-indigo-500 to-purple-500 text-white shadow-sm hover:shadow-md hover:from-indigo-600 hover:to-purple-600 transition-all',
    secondary:
      'inline-flex items-center justify-center gap-1.5 px-4 py-2 text-sm font-semibold rounded-lg border border-slate-200 bg-white text-slate-700 hover:border-indigo-300 hover:text-indigo-700 hover:shadow-sm transition-all',
    ghost:
      'inline-flex items-center justify-center gap-1.5 px-4 py-2 text-sm font-medium text-slate-600 hover:text-indigo-700 transition-colors',
  }[v];
  if (cta.href) {
    return <a href={cta.href} className={cls}>{cta.label}</a>;
  }
  return (
    <button type="button" onClick={cta.onClick} className={cls}>
      {cta.label}
    </button>
  );
}

export function EmptyState(props: EmptyStateProps) {
  const { icon, title, body, primaryCta, secondaryCtas = [], hint } = props;
  return (
    <div className="flex items-center justify-center py-12 px-4">
      <div className="text-center max-w-md">
        {icon && (
          <div className="mx-auto mb-4 w-16 h-16 rounded-2xl bg-slate-100 flex items-center justify-center text-slate-400">
            {icon}
          </div>
        )}
        <h2 className="text-base font-semibold text-slate-800 mb-1">{title}</h2>
        {body && <p className="text-sm text-slate-600 mb-5">{body}</p>}
        {(primaryCta || secondaryCtas.length > 0) && (
          <div className="flex flex-wrap gap-2 justify-center">
            {primaryCta && <CTAButton cta={{ ...primaryCta, variant: 'primary' }} />}
            {secondaryCtas.map((cta, i) => (
              <CTAButton key={i} cta={{ ...cta, variant: cta.variant ?? 'secondary' }} />
            ))}
          </div>
        )}
        {hint && <div className="mt-4 text-xs text-slate-400">{hint}</div>}
      </div>
    </div>
  );
}

export default EmptyState;
