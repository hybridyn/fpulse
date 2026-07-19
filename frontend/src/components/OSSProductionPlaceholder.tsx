/**
 * OSSProductionPlaceholder — shown to Free-tier users when they switch
 * the environment selector to PROD. Replaces the page content with a
 * single, calm CTA describing what F-Pulse+ adds on top of OSS.
 *
 * Design intent (per V10 of the F-Pulse product vision):
 *   - Production is the clean commercial line between OSS and Plus.
 *   - In OSS, Production is "enabled-but-empty" — the env switch
 *     still works, but landing on PROD shows this placeholder
 *     instead of greyed-out buttons on every page.
 *   - One primary CTA (Learn more), one secondary (back to DEV).
 *   - No nag plastered on DEV pages; this only appears when the user
 *     explicitly switches to PROD.
 */
export default function OSSProductionPlaceholder({
  onSwitchToDev,
}: {
  onSwitchToDev: () => void;
}) {
  return (
    <div className="flex-1 flex items-center justify-center px-6 py-10 overflow-auto bg-gradient-to-b from-slate-50 to-slate-100">
      <div className="w-full max-w-2xl">
        {/* Card */}
        <div className="rounded-2xl bg-white border border-slate-200 shadow-lg overflow-hidden">
          {/* Header band — amber accent ties the Production surface to
              the F-Pulse+ tier visually, without screaming "buy now". */}
          <div className="px-7 pt-7 pb-5 border-b border-slate-100">
            <div className="flex items-center gap-3 mb-3">
              <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-amber-400 to-orange-500 flex items-center justify-center shadow-sm">
                {/* Shield + check — production-grade signal */}
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
                  <path d="m9 12 2 2 4-4" />
                </svg>
              </div>
              <div>
                <h1 className="text-2xl font-bold text-slate-900 leading-tight">Production environment</h1>
                <p className="text-sm text-slate-500 mt-0.5">A F-Pulse+ feature</p>
              </div>
            </div>
            <p className="text-sm text-slate-700 leading-relaxed">
              F-Pulse OSS runs your pipelines in DEV. <strong className="text-slate-900">F-Pulse+</strong> adds
              the production layer — promotion, approvals, separate credentials, and a deploy
              history so PROD changes are governed, not improvised.
            </p>
          </div>

          {/* What Plus adds — six concise bullets so the value is
              concrete, not marketing fluff. */}
          <div className="px-7 py-5">
            <ul className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3 text-sm text-slate-700">
              {[
                ['Two-gate approvals', 'A second pair of eyes before PROD changes ship.'],
                ['Deploy history', 'Every promotion logged with diff and approver.'],
                ['PROD credentials', 'Separate vault from DEV — no accidental cross-env writes.'],
                ['PROD schedules', 'Cron only fires after a published deploy.'],
                ['Workspace RBAC', 'Roles, not shared logins.'],
                ['Audit log retention', 'Compliance-grade trail across runs and edits.'],
              ].map(([title, desc]) => (
                <li key={title} className="flex items-start gap-2.5">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="text-amber-500 mt-0.5 shrink-0" aria-hidden="true">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                  <span>
                    <strong className="text-slate-900">{title}</strong> — <span className="text-slate-600">{desc}</span>
                  </span>
                </li>
              ))}
            </ul>
          </div>

          {/* Actions — primary opens hybridyn.com/f-pulse in a new tab;
              secondary returns to DEV so the user is never stuck. */}
          <div className="px-7 py-5 bg-slate-50 border-t border-slate-200 flex flex-col sm:flex-row items-stretch sm:items-center gap-3 sm:gap-4">
            <a
              href="https://hybridyn.com/f-pulse"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center justify-center gap-2 px-5 py-2.5 rounded-lg bg-gradient-to-r from-amber-500 to-orange-500 hover:from-amber-600 hover:to-orange-600 text-white text-sm font-bold shadow-sm transition-colors"
            >
              Learn more about F-Pulse+
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M7 17 17 7" />
                <path d="M7 7h10v10" />
              </svg>
            </a>
            <button
              type="button"
              onClick={onSwitchToDev}
              className="inline-flex items-center justify-center gap-2 px-5 py-2.5 rounded-lg bg-white border border-slate-300 hover:bg-slate-100 text-slate-700 text-sm font-semibold transition-colors"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="m12 19-7-7 7-7" />
                <path d="M19 12H5" />
              </svg>
              Switch back to DEV
            </button>
            <span className="text-xs text-slate-500 sm:ml-auto leading-relaxed">
              You're on the Free edition.
            </span>
          </div>
        </div>

        {/* Tiny footnote — not a CTA. Just transparent context. */}
        <p className="text-center text-xs text-slate-500 mt-4">
          Pipelines, credentials, and runs created in DEV stay yours. F-Pulse OSS is Apache 2.0.
        </p>
      </div>
    </div>
  );
}
