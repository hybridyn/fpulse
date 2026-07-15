/**
 * TierChip — pair of pills shown in every page header so the user always
 * sees which environment they're in *and* which tier is active. Mirrors
 * the layout the Dashboard pioneered (Dev / Live + Free / Plus), brought
 * to every page so the page-level chrome stays consistent.
 *
 * Env pill   → Dev = emerald, Live = red. Sources of truth for the
 *              env toggle, useful at a glance when the toolbar's
 *              global DEV/PROD switch is offscreen.
 * Tier pill  → Free = slate, Plus = amber gradient.
 *
 * Adapts colors to a dark (PROD page-header) vs light (DEV page-header)
 * background so contrast stays correct in both environments.
 */
export default function TierChip({
  tier = 'free',
  environment = 'dev',
}: {
  tier?: string;
  environment?: string;
}) {
  const isPlus = tier === 'plus';
  const isProd = environment === 'prod';

  const envCls = isProd
    ? 'bg-red-500/20 text-red-300 border-red-500/30'
    : 'bg-emerald-100 text-emerald-700 border-emerald-200';

  const tierCls = isPlus
    ? isProd
      // PROD + Plus: amber gradient pops against dark slate background
      ? 'bg-gradient-to-r from-amber-400 to-orange-500 text-white border-amber-400/50 shadow-sm'
      // DEV + Plus: same amber gradient, slightly softer ring on light bg
      : 'bg-gradient-to-r from-amber-400 to-orange-500 text-white border-amber-500/40 shadow-sm'
    : isProd
      ? 'bg-slate-700 text-slate-200 border-slate-600'
      : 'bg-slate-100 text-slate-700 border-slate-300';

  // Free is the implicit default on OSS, so rendering a "FREE" pill on
  // every page is noise. We only show the tier chip for paid tiers
  // (Plus and up) — where it's a real differentiator the user wants to
  // see — and rely on the env chip alone for the Free case.
  return (
    <>
      <span
        className={`text-xs font-bold uppercase tracking-wider px-3 py-1 rounded-full border shrink-0 ${envCls}`}
        title={isProd ? 'Production environment' : 'Development environment'}
      >
        {isProd ? 'Live' : 'Dev'}
      </span>
      {isPlus && (
        <span
          className={`text-xs font-bold uppercase tracking-wider px-3 py-1 rounded-full border shrink-0 ${tierCls}`}
          title="F-Pulse+ (commercial extension)"
        >
          Plus
        </span>
      )}
    </>
  );
}
