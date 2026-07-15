/**
 * ProjectContextBar — the "Projects > Default · Show All" breadcrumb.
 *
 * Rendered by each project-scoped page (Pipelines, Executions, Credentials,
 * Connections) directly below its silver page header. Sticky so the
 * breadcrumb stays visible as the user scrolls through long pipeline /
 * execution lists — without the sticky behaviour it scrolls away on the
 * first screenful, which defeats its purpose (confirming "yes, I'm
 * filtered to project X").
 *
 * Not rendered on global surfaces (Dashboard, Admin, Help, Runbook,
 * Settings, Account) — seeing "Projects > Default" on Help is noise
 * because Help isn't project-scoped.
 *
 * The bar is its own component (not inlined in each page) so the styling
 * stays in one place. If a future design change wants a different look
 * for this breadcrumb (e.g. a slim grey strip instead of the current
 * blue), all four pages pick it up from a single edit here.
 */

interface ProjectContextBarProps {
  /** The currently-focused project's id. When falsy, the bar renders nothing. */
  projectId: string | null | undefined;
  /** Display name shown after the chevron. */
  projectName: string;
  /** Called when the user clicks the folder icon or "Projects" text —
   *  should navigate to the Projects listing. */
  onGoToProjects: () => void;
  /** Called when the user clicks "Show All" — should clear the project
   *  filter without changing the current page. */
  onClear: () => void;
}

export default function ProjectContextBar({
  projectId,
  projectName,
  onGoToProjects,
  onClear,
}: ProjectContextBarProps) {
  if (!projectId) return null;

  return (
    <div className="sticky top-[78px] z-20 px-5 py-2 bg-blue-50 border-b border-blue-200/50 flex items-center gap-2.5 shrink-0 shadow-sm">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-blue-400 shrink-0">
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
      </svg>
      <button
        onClick={onGoToProjects}
        className="text-xs text-blue-500 hover:text-blue-700 font-semibold"
      >
        Projects
      </button>
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="text-blue-300">
        <polyline points="9 18 15 12 9 6" />
      </svg>
      <span className="text-sm font-bold text-blue-800">{projectName}</span>
      <div className="flex-1" />
      <button
        onClick={onClear}
        className="text-xs text-blue-400 hover:text-blue-600 font-semibold px-2.5 py-1 rounded-lg hover:bg-blue-100 transition-colors"
      >
        Show All
      </button>
    </div>
  );
}
