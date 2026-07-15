/**
 * TemplatesPage — gallery of curated pipeline templates.
 *
 * Each template is a one-click starting point that drops a working set
 * of nodes onto the Editor canvas. Source of truth is
 * src/templates/catalog.ts; this page is purely the picker UI.
 *
 * Layout follows the canonical Workflows-family page template:
 *   • 78px sticky banner with title cluster + HubTabs in the center +
 *     actions on the right
 *   • hero brand statement
 *   • filter chips (complexity / category)
 *   • responsive card grid
 *
 * Theme reads as "F-Pulse: clear and futuristic" — bold gradient hero,
 * each card carries a distinct gradient header that matches its template
 * id, generous typography per the readability rule.
 */

import { useEffect, useMemo, useState } from 'react';
import { useWorkflowStore } from '../../stores/workflowStore';
import { TEMPLATE_CATALOG, TemplateDefinition } from '../../templates/catalog';
import { useDarkMode } from '../../hooks/useDarkMode';
import HubTabs, { WORKFLOWS_TABS } from '../HubTabs';
import TierChip from '../shared/TierChip';
import { toast } from '../Toast';
import { api } from '../../api/client';
import { uiConfirm } from '../../ui/dialog';
import { usePageContext } from '../../hooks/usePageContext';
import PageHeader from '../shared/PageHeader';
import { navigateTo } from '../../router';
import type { Page } from '../../types';

interface Props {
  onOpenEditor: () => void;
  environment?: 'dev' | 'prod';
  tier?: string;
}

// Display shape used by the gallery card. User templates are normalized
// into this shape (see `normalizeUserTemplate`) so both kinds render
// through the same component.
interface DisplayTemplate extends TemplateDefinition {
  source: 'builtin' | 'user';
}

// Canonical "Yours" theme — one gradient for every user template so
// user-saved cards are visually distinct from the built-in rainbow.
const USER_GRADIENT = 'from-slate-700 via-indigo-700 to-violet-700';
const USER_ACCENT = 'text-indigo-700';
const USER_ICON =
  'M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z';

function normalizeUserTemplate(raw: any): DisplayTemplate {
  const stepCount = Array.isArray(raw.steps) ? raw.steps.length : 0;
  return {
    id: raw.id,
    name: raw.name,
    tagline: raw.tagline || 'Saved by you',
    description: raw.description || 'A template you saved from the Pipelines page.',
    complexity: stepCount >= 4 ? 'complex' : 'simple',
    category: raw.category || 'Custom',
    gradient: USER_GRADIENT,
    accent: USER_ACCENT,
    icon: USER_ICON,
    tags: ['Yours', `${stepCount} nodes`],
    steps: raw.steps || [],
    connections: raw.connections || [],
    source: 'user',
  };
}

export default function TemplatesPage({ onOpenEditor, environment = 'dev', tier = 'free' }: Props) {
  const dark = useDarkMode();
  const useTemplate = useWorkflowStore((s) => s.useTemplate);
  // Single 4-way filter — replaces the old Complexity + Source + Category
  // rows. The user thinks in "simple / complex / mine / everything", not
  // in three orthogonal dimensions.
  const [view, setView] = useState<'all' | 'simple' | 'complex' | 'user'>('all');
  const [userTemplates, setUserTemplates] = useState<DisplayTemplate[]>([]);
  const [, setLoadingUser] = useState(true);
  const isProd = environment === 'prod';

  // Fetch user templates once on mount + refresh after save/delete via
  // the small `refreshTick` state below.
  const [refreshTick, setRefreshTick] = useState(0);
  useEffect(() => {
    let cancel = false;
    setLoadingUser(true);
    api.listUserTemplates()
      .then((data) => {
        if (cancel) return;
        const list = (data?.templates || []).map(normalizeUserTemplate);
        setUserTemplates(list);
      })
      .catch(() => { /* silent — gallery still shows built-ins */ })
      .finally(() => { if (!cancel) setLoadingUser(false); });
    return () => { cancel = true; };
  }, [refreshTick]);

  const allTemplates: DisplayTemplate[] = useMemo(() => {
    const builtins = TEMPLATE_CATALOG.map<DisplayTemplate>((t) => ({ ...t, source: 'builtin' }));
    return [...userTemplates, ...builtins];
  }, [userTemplates]);

  const visible = useMemo(() => {
    return allTemplates.filter((t) => {
      if (view === 'all') return true;
      if (view === 'user') return t.source === 'user';
      // 'simple' / 'complex' filter built-ins by complexity; user
      // templates are excluded since they live under their own filter.
      return t.source === 'builtin' && t.complexity === view;
    });
  }, [allTemplates, view]);

  // 2026-05-19 (P1 #8 of PAGE_BY_PAGE_AUDIT.md): publish context so the
  // Copilot can answer "show me a complex template that loads CSV" or
  // "do I have any user templates yet?" without re-fetching.
  usePageContext({
    page: 'templates',
    visible_ids: visible.map((t) => t.id),
    filters: { view },
  });

  const counts = useMemo(() => {
    let simple = 0, complex = 0;
    for (const t of TEMPLATE_CATALOG) (t.complexity === 'simple' ? simple++ : complex++);
    const userN = userTemplates.length;
    return {
      simple,
      complex,
      total: allTemplates.length,
      builtin: TEMPLATE_CATALOG.length,
      user: userN,
    };
  }, [allTemplates, userTemplates]);

  // Preview-before-load: clicking "Use this template" opens a preview
  // dialog with the node list + the warning that the current canvas
  // will be replaced. The user has to explicitly confirm before
  // anything happens — no more silent canvas takeover.
  const [previewTpl, setPreviewTpl] = useState<DisplayTemplate | null>(null);

  const handleUse = (tpl: DisplayTemplate) => {
    setPreviewTpl(tpl);
  };

  const confirmLoad = async () => {
    if (!previewTpl) return;
    const tpl = previewTpl;
    setPreviewTpl(null);
    try {
      await useTemplate(tpl.id);
      toast.success('Template loaded', `${tpl.name} dropped onto the canvas`);
      onOpenEditor();
    } catch {
      toast.error('Could not load template', 'Please try again or check the console');
    }
  };

  const handleDeleteUser = async (tpl: DisplayTemplate) => {
    const ok = await uiConfirm({
      title: 'Delete this template?',
      message: `"${tpl.name}" will be removed from your library. The pipeline it was saved from is unaffected.`,
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteUserTemplate(tpl.id);
      toast.success('Template deleted');
      setRefreshTick((n) => n + 1);
    } catch (e: any) {
      toast.error('Delete failed', e?.message || 'Try again');
    }
  };

  return (
    <div className={`flex-1 flex flex-col overflow-hidden ${dark ? 'bg-[#0B1220]' : 'bg-canvas-bg'}`}>
      {/* FOLLOW-1 (2026-05-19) — migrated from bespoke sticky header
          to the shared <PageHeader>. Title accessory carries TierChip;
          tabs slot holds HubTabs; actions slot holds the Blank canvas
          button. */}
      <PageHeader
        environment={environment}
        icon={(
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-violet-500">
            <rect x="3" y="3" width="7" height="7" rx="1" />
            <rect x="14" y="3" width="7" height="7" rx="1" />
            <rect x="3" y="14" width="7" height="7" rx="1" />
            <path d="M14 17h7" />
            <path d="M17.5 14v7" />
          </svg>
        )}
        title="Templates"
        subtitle={`${counts.builtin} built-in · ${counts.user} yours · ${counts.simple} simple · ${counts.complex} complex`}
        titleAccessory={<TierChip tier={tier} environment={environment} />}
        tabs={(
          <HubTabs
            tabs={WORKFLOWS_TABS}
            active="templates"
            onNavigate={(p) => navigateTo(p as Page)}
            environment={environment}
            dark={dark}
          />
        )}
        actions={(
          <button
            type="button"
            onClick={onOpenEditor}
            className="px-4 py-2 text-sm font-semibold rounded-lg bg-white text-slate-700 border border-slate-300 hover:bg-slate-50 transition-colors"
            title="Open the Editor on a blank canvas"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="inline mr-1.5 -mt-0.5">
              <path d="M12 5v14" /><path d="M5 12h14" />
            </svg>
            Blank canvas
          </button>
        )}
      />

      {/* ── Scrolling body ───────────────────────────────────────────── */}
      <div className="flex-1 overflow-auto">
        <div className="w-full max-w-[1500px] mx-auto px-8 py-6">
          {/* Page intro — full-width gradient-bordered banner with the
              heading and tagline on a single line. The 4px violet →
              fuchsia → emerald wrapper matches the Active Provider card
              and the Save-as-template dialog. */}
          <div className="rounded-xl p-[4px] bg-gradient-to-r from-violet-500 via-fuchsia-500 to-emerald-500 shadow-sm">
            <div className={`rounded-[8px] px-5 py-3 flex items-center gap-4 flex-wrap ${dark ? 'bg-[#111827]' : 'bg-white'}`}>
              <h2 className={`text-lg font-bold shrink-0 ${dark ? 'text-slate-100' : 'text-slate-800'}`}>
                Start from a working pipeline
              </h2>
              <p className={`text-sm leading-relaxed flex-1 min-w-0 ${dark ? 'text-slate-300' : 'text-slate-600'}`}>
                Pick a starting shape, click <strong className={dark ? 'text-slate-100' : 'text-slate-800'}>Use this template</strong>,
                and edit the nodes for your data — built-in patterns plus anything you've saved from the Pipelines page.
              </p>
            </div>
          </div>

          {/* ── Filter row — single 4-button picker ────────────────────
              Combines the previous Complexity + Source + Category rows
              into one set the user actually thinks in: how complex is it,
              and is it mine or built-in? */}
          <div className="mt-5 flex flex-wrap items-center gap-2">
            <FilterChip label={`All · ${counts.total}`} active={view === 'all'} onClick={() => setView('all')} dark={dark} />
            <FilterChip label={`Simple · ${counts.simple}`} active={view === 'simple'} onClick={() => setView('simple')} dark={dark} />
            <FilterChip label={`Complex · ${counts.complex}`} active={view === 'complex'} onClick={() => setView('complex')} dark={dark} />
            <FilterChip label={`User defined · ${counts.user}`} active={view === 'user'} onClick={() => setView('user')} dark={dark} />
          </div>

          {/* ── Card grid ───────────────────────────────────────────── */}
          <div className="mt-6 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5">
            {visible.map((tpl) => (
              <TemplateCard
                key={tpl.id}
                tpl={tpl}
                dark={dark}
                onUse={() => handleUse(tpl)}
                onDelete={tpl.source === 'user' ? () => handleDeleteUser(tpl) : undefined}
              />
            ))}
          </div>

          {visible.length === 0 && view === 'user' && (
            <div className={`mt-8 rounded-xl border ${dark ? 'border-white/[0.08] bg-white/[0.02]' : 'border-slate-200 bg-white'} p-6 text-center`}>
              <div className={`mx-auto mb-3 w-10 h-10 rounded-lg flex items-center justify-center ${dark ? 'bg-violet-500/10' : 'bg-violet-50'}`}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={dark ? 'text-violet-300' : 'text-violet-600'}>
                  <rect x="3" y="3" width="7" height="7" rx="1" />
                  <rect x="14" y="3" width="7" height="7" rx="1" />
                  <rect x="3" y="14" width="7" height="7" rx="1" />
                  <path d="M14 17h7" />
                  <path d="M17.5 14v7" />
                </svg>
              </div>
              <h3 className={`text-sm font-bold ${dark ? 'text-slate-100' : 'text-slate-800'}`}>You haven't saved any templates yet</h3>
              <p className={`mt-1 text-sm ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
                Turn any working pipeline into a reusable template. Two ways:
              </p>
              <ul className={`mt-3 inline-block text-left text-sm space-y-1.5 ${dark ? 'text-slate-300' : 'text-slate-700'}`}>
                <li>
                  <span className="font-semibold">Editor</span> — open a pipeline, click the <strong>⋮</strong> kebab menu in the toolbar, pick <strong>Save as template…</strong>
                </li>
                <li>
                  <span className="font-semibold">Pipelines</span> — find any row's <strong>Save as template</strong> action (the violet grid icon)
                </li>
              </ul>
              <p className={`mt-4 text-xs ${dark ? 'text-slate-500' : 'text-slate-400'}`}>
                Saved templates show up here and in the Editor's "Use template" picker.
              </p>
            </div>
          )}
          {visible.length === 0 && view !== 'user' && (
            <div className={`mt-12 text-center text-sm ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
              No templates match this filter combination.
            </div>
          )}
        </div>
      </div>

      {/* Preview-before-load modal — shown when a card's "Use this
          template" button is clicked. Replaces the silent canvas
          takeover with an explicit confirmation step. */}
      {previewTpl && (
        <PreviewLoadDialog
          tpl={previewTpl}
          dark={dark}
          onCancel={() => setPreviewTpl(null)}
          onConfirm={confirmLoad}
        />
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────────
   Preview-load dialog — explicit confirmation step before a template
   replaces the canvas. Shows the template name + tagline + node list +
   a warning that the current canvas state will be overwritten.
   ───────────────────────────────────────────────────────────────────── */
function PreviewLoadDialog({
  tpl, dark, onCancel, onConfirm,
}: {
  tpl: DisplayTemplate;
  dark: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  // Esc closes the dialog without loading.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  const cardBg = dark ? 'bg-[#111827] text-slate-100' : 'bg-white text-slate-800';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-slate-900/60 backdrop-blur-sm" onClick={onCancel} />
      <div className={`relative w-full max-w-xl rounded-xl shadow-2xl overflow-hidden border ${cardBg} ${dark ? 'border-white/10' : 'border-slate-200'}`}>
        <div className={`px-6 py-4 border-b ${dark ? 'border-white/10' : 'border-slate-200'}`}>
          <div className={`text-xs font-semibold uppercase tracking-wider ${dark ? 'text-violet-300' : 'text-violet-600'}`}>
            Load template
          </div>
          <h2 className="mt-1 text-lg font-bold">{tpl.name}</h2>
          <p className={`mt-1 text-sm ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
            {tpl.tagline}
          </p>
        </div>

        <div className="p-6 space-y-4">
          <p className={`text-sm leading-relaxed ${dark ? 'text-slate-300' : 'text-slate-600'}`}>
            {tpl.description}
          </p>

          <div>
            <div className={`text-xs font-semibold uppercase tracking-wider mb-2 ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
              Nodes ({tpl.steps.length})
            </div>
            <div className="flex flex-wrap gap-1.5">
              {tpl.steps.map((s) => (
                <span
                  key={s.id}
                  className={`text-xs px-2 py-1 rounded-md ${
                    dark ? 'bg-slate-800 text-slate-300 ring-1 ring-white/10' : 'bg-slate-100 text-slate-700 ring-1 ring-slate-200'
                  }`}
                >
                  {s.label || s.type}
                </span>
              ))}
            </div>
            <div className={`mt-2 text-xs ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
              {tpl.connections.length} connection{tpl.connections.length === 1 ? '' : 's'}
            </div>
          </div>

          <div className={`rounded-lg px-3 py-2.5 text-sm flex items-start gap-2 ${
            dark ? 'bg-amber-500/10 text-amber-200 border border-amber-500/20' : 'bg-amber-50 text-amber-800 border border-amber-200'
          }`}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 mt-0.5">
              <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
              <path d="M12 9v4" /><path d="M12 17h.01" />
            </svg>
            <span>
              Loading this template <strong>replaces</strong> the current canvas.
              Save your existing pipeline first if you want to keep it.
            </span>
          </div>
        </div>

        <div className={`flex items-center justify-end gap-2 px-6 py-4 border-t ${dark ? 'bg-white/[0.02] border-white/10' : 'bg-slate-50 border-slate-200'}`}>
          <button
            type="button"
            onClick={onCancel}
            className={`px-4 py-2 text-sm font-semibold rounded-lg border ${
              dark ? 'text-slate-300 bg-white/5 border-white/10 hover:bg-white/10' : 'text-slate-700 bg-white border-slate-300 hover:bg-slate-100'
            }`}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="px-4 py-2 text-sm font-semibold rounded-lg text-white bg-emerald-600 hover:bg-emerald-700"
          >
            Load template
          </button>
        </div>
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────────
   Filter chip
   ───────────────────────────────────────────────────────────────────── */
function FilterChip({
  label, active, onClick, dark, tone = 'slate',
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  dark: boolean;
  tone?: 'slate' | 'emerald' | 'violet';
}) {
  const activeCls =
    tone === 'emerald'
      ? 'bg-emerald-500 text-white border-emerald-500'
      : tone === 'violet'
        ? 'bg-violet-500 text-white border-violet-500'
        : dark
          ? 'bg-slate-200 text-slate-900 border-slate-200'
          : 'bg-slate-800 text-white border-slate-800';
  const idleCls = dark
    ? 'bg-white/5 text-slate-300 border-white/10 hover:bg-white/10'
    : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-50';
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-3.5 py-1.5 text-sm font-semibold rounded-full border transition-colors ${active ? activeCls : idleCls}`}
    >
      {label}
    </button>
  );
}

/* ─────────────────────────────────────────────────────────────────────
   Template card — one entry in the gallery
   ───────────────────────────────────────────────────────────────────── */
function TemplateCard({
  tpl, dark, onUse, onDelete,
}: {
  tpl: TemplateDefinition & { source?: 'builtin' | 'user' };
  dark: boolean;
  onUse: () => void;
  onDelete?: () => void;
}) {
  const cardBg = dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200';
  const isUser = tpl.source === 'user';
  // Compact card layout — icon sits inline next to the title so the
  // top-left dead space is reclaimed. Badges + delete float right on
  // the same row as the icon.
  return (
    <div className={`relative rounded-xl border shadow-sm overflow-hidden transition-all hover:shadow-md ${cardBg}`}>
      <div className="p-5 space-y-3">
        {/* Header row — icon | title + tagline | badges + delete */}
        <div className="flex items-start gap-3">
          <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 ${
            dark ? 'bg-violet-500/15 border border-violet-500/25' : 'bg-violet-50 border border-violet-200'
          }`}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={dark ? 'text-violet-300' : 'text-violet-600'}>
              {tpl.icon.split(' M').map((seg, i) => (
                <path key={i} d={i === 0 ? seg : 'M' + seg} />
              ))}
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <h3 className={`text-base font-bold leading-tight ${dark ? 'text-slate-100' : 'text-slate-900'}`}>
              {tpl.name}
            </h3>
            <p className={`mt-0.5 text-sm ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
              {tpl.tagline}
            </p>
          </div>
          <div className="flex items-center gap-1 flex-wrap justify-end shrink-0">
            {isUser && (
              <span className={`text-xs font-semibold px-2 py-0.5 rounded-md ${
                dark ? 'bg-emerald-500/15 text-emerald-300 ring-1 ring-emerald-500/25' : 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200'
              }`}>
                Yours
              </span>
            )}
            <span className={`text-xs font-semibold uppercase tracking-wider px-2 py-0.5 rounded-md ${
              tpl.complexity === 'simple'
                ? (dark ? 'bg-slate-800 text-slate-300' : 'bg-slate-100 text-slate-700')
                : (dark ? 'bg-violet-500/15 text-violet-300' : 'bg-violet-50 text-violet-700')
            }`}>
              {tpl.complexity}
            </span>
            {onDelete && (
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); onDelete(); }}
                title="Delete this template"
                className={`w-7 h-7 rounded-md flex items-center justify-center transition-colors ${
                  dark ? 'text-slate-500 hover:text-red-400 hover:bg-red-500/10' : 'text-slate-400 hover:text-red-600 hover:bg-red-50'
                }`}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="3 6 5 6 21 6" />
                  <path d="M19 6l-2 14H7L5 6" />
                  <path d="M9 6V4h6v2" />
                </svg>
              </button>
            )}
          </div>
        </div>

        {/* Description */}
        <p className={`text-sm leading-relaxed ${dark ? 'text-slate-300' : 'text-slate-600'}`}>
          {tpl.description}
        </p>

        {/* Stats + tags */}
        <div className="pt-1 flex items-center gap-1.5 flex-wrap">
          <span className={`text-xs font-semibold px-2 py-0.5 rounded-md ${
            dark ? 'bg-slate-800 text-slate-300' : 'bg-slate-100 text-slate-700'
          }`}>
            {tpl.steps.length} nodes
          </span>
          <span className={`text-xs font-semibold px-2 py-0.5 rounded-md ${
            dark ? 'bg-slate-800 text-slate-300' : 'bg-slate-100 text-slate-700'
          }`}>
            {tpl.category}
          </span>
          {tpl.tags.map((tag) => (
            <span
              key={tag}
              className={`text-xs px-2 py-0.5 rounded-md ${
                dark ? 'bg-white/[0.05] text-slate-400 ring-1 ring-white/10' : 'bg-slate-50 text-slate-600 ring-1 ring-slate-200'
              }`}
            >
              {tag}
            </span>
          ))}
        </div>

        {/* CTA — solid emerald. Deliberately NOT violet/purple because
            that's the F-Pulse Copilot's color and the two buttons would
            visually compete on the same screen. */}
        <button
          type="button"
          onClick={onUse}
          className="mt-2 w-full px-4 py-2 text-sm font-semibold rounded-lg text-white transition-colors bg-emerald-600 hover:bg-emerald-700"
        >
          Use this template →
        </button>
      </div>
    </div>
  );
}
