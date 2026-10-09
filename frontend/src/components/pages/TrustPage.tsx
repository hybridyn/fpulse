/**
 * TrustPage — UI surface for F-Pulse's trust posture.
 *
 * Promotes docs/trust.md from a static markdown file in the repo into
 * a live page reachable at #trust. Shown in:
 *   - Sidebar nav (under Help)
 *   - Linked from Insights → AI Provider (privacy badge)
 *   - Linked from the agent dock empty state
 *
 * Three pillars from project_fpulse_ai_operational_architecture.md:
 *   1. Deterministic core, probabilistic support
 *   2. Data sovereignty
 *   3. Full observability
 *
 * No live API calls — content is curated. Updates ship with releases.
 */

import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useDarkMode } from '../../hooks/useDarkMode';
import { api } from '../../api/client';
import HeroCard from '../shared/HeroCard';
import PageHeader from '../shared/PageHeader';
import ErrorBanner from '../shared/ErrorBanner';
import { usePageContext } from '../../hooks/usePageContext';
import { navigateTo } from '../../router';

// Marketing-style "three pillars" content moved to docs/trust.md as part
// of the May 10 2026 slim-down. The page now focuses on verifiable
// surfaces: live posture + cert matrix + artifact links + audit endpoints.

interface Artifact {
  name: string;
  desc: string;
  path: string;
}

const ARTIFACTS: Artifact[] = [
  { name: 'trust.md', desc: 'The full trust posture in markdown', path: 'docs/trust.md' },
  { name: 'ai-boundary-contract.md', desc: 'What the agent never sends to LLMs and how data is sanitized', path: 'docs/ai-boundary-contract.md' },
  { name: 'security.md', desc: 'Vulnerability disclosure + threat model', path: 'docs/security.md' },
  { name: 'performance.md', desc: 'Performance + memory budget targets per tier', path: 'docs/performance.md' },
  { name: 'customer-faq.md', desc: 'Privacy + data-handling FAQ in plain English', path: 'docs/customer-faq.md' },
  { name: 'tests/architecture/test_invariants.py', desc: '10 architecture invariants enforced by CI', path: 'backend/tests/architecture/test_invariants.py' },
];

export default function TrustPage({ embedded = false }: { embedded?: boolean } = {}) {
  const dark = useDarkMode();

  // FOLLOW-3 (2026-05-19) — publish a static handle for Trust posture
  // queries. No PII or live posture values are published; the Copilot
  // can still answer "where do I see the eval pass-rate?" by recognising
  // the page.
  usePageContext({ page: 'trust', filters: { embedded } });

  // Embedded mode: parent (AIPage) provides the page chrome; render only content.
  if (embedded) {
    return <TrustContent dark={dark} />;
  }

  return (
    <div className={`flex-1 flex flex-col overflow-hidden ${dark ? 'bg-[#0b1120]' : 'bg-canvas-bg'}`}>
      {/* 2026-05-19 (P1 #1 of PAGE_BY_PAGE_AUDIT.md): adopted the canonical
          sticky 78px <PageHeader> shell — the standalone Trust path used
          to render a non-sticky hero card that made the page look like a
          different product family. The "Public" pill moves to the title
          accessory slot so the header height matches all other pages. */}
      <PageHeader
        icon={(
          <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${dark ? 'bg-emerald-500/15 border border-emerald-500/20' : 'bg-emerald-50 border border-emerald-200'}`}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className={dark ? 'text-emerald-300' : 'text-emerald-700'}>
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
            </svg>
          </div>
        )}
        title="Security & Diagnostics"
        subtitle="Configuration, check results and recorded evidence."
        titleAccessory={(
          <span className={`text-xs font-bold px-2.5 py-0.5 rounded-full uppercase tracking-wider ${dark ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30' : 'bg-emerald-100 text-emerald-700 border border-emerald-300'}`}>
            Diagnostics
          </span>
        )}
      />
      <div className={`flex-1 overflow-auto ${dark ? 'bg-[#0b1120]' : 'bg-canvas-bg'}`}>
        <TrustContent dark={dark} />
      </div>
    </div>
  );
}

function TrustContent({ dark }: { dark: boolean }) {
  // Slimmed-down trust page (May 10 2026). Three sections only:
  //   1. Live posture + cert matrix — actually verifiable on this install
  //   2. Trust artifacts — links to docs/trust.md, security.md, etc.
  //   3. Audit endpoints — quick links so reviewers can pull raw data
  // Marketing-style sections (NEVER-do bullets, deployment modes, three
  // pillars, public catalog curl) moved to docs/trust.md where the
  // sales pitch belongs. The page is now a working tool for compliance
  // reviewers, not a brochure.
  // Standard card chrome used across the redesign — matches Pipelines,
  // Connections, Executions, etc. so the Trust page reads as part of
  // the same product family.
  const cardCls = dark
    ? 'rounded-lg border border-white/[0.08] shadow-sm bg-[#111827]'
    : 'rounded-lg border border-slate-200 shadow-sm bg-white';
  const sectionLabel = dark ? 'text-slate-300' : 'text-slate-700';
  const sublabel = dark ? 'text-slate-400' : 'text-slate-600';

  return (
    <div className="w-full px-6 py-5 space-y-4 max-w-[1500px] mx-auto">
        {/* Live posture — pulls from the host. Gate 4 evidence. */}
        <LivePostureSection dark={dark} cardCls={cardCls} sectionLabel={sectionLabel} sublabel={sublabel} />

        {/* Connector certification matrix — public, verifiable depth scores
            per connector. Pulls from /api/connectors/cert-matrix. */}
        <CertMatrixSection dark={dark} cardCls={cardCls} sectionLabel={sectionLabel} sublabel={sublabel} />

        {/* Trust artifacts — dark+amber header matches Pipelines /
            Connections / Pool tables. */}
        <div className={`${cardCls} overflow-hidden`}>
          <div className="px-5 py-3 border-b border-slate-100 flex items-center gap-2 flex-wrap">
            <h3 className={`text-sm font-bold ${sectionLabel}`}>Trust artifacts in the source tree</h3>
            <span className="text-xs text-slate-500">· {ARTIFACTS.length} files</span>
          </div>
          <p className={`text-xs px-5 pt-3 pb-2 ${sublabel}`}>
            Reference documents describe intended behavior. Their presence does not verify this installation.
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900 border-b-2 border-amber-400/40">
                  <th className="text-left px-5 py-2.5 text-xs font-bold text-amber-300 uppercase tracking-wider w-1/4">File</th>
                  <th className="text-left px-4 py-2.5 text-xs font-bold text-amber-300 uppercase tracking-wider">What it covers</th>
                  <th className="text-left px-4 py-2.5 text-xs font-bold text-amber-300 uppercase tracking-wider">Path</th>
                </tr>
              </thead>
              <tbody className={`divide-y ${dark ? 'divide-white/[0.06]' : 'divide-slate-100'}`}>
                {ARTIFACTS.map((a) => (
                  <tr key={a.path} className={`align-top ${dark ? 'hover:bg-slate-900/40' : 'hover:bg-slate-50'}`}>
                    <td className="px-5 py-2.5">
                      <code className={`font-mono text-xs font-bold ${dark ? 'text-emerald-300' : 'text-emerald-700'}`}>
                        {a.name}
                      </code>
                    </td>
                    <td className={`px-4 py-2.5 text-sm ${dark ? 'text-slate-300' : 'text-slate-700'}`}>{a.desc}</td>
                    <td className="px-4 py-2.5">
                      <code className={`font-mono text-xs ${dark ? 'text-slate-500' : 'text-slate-500'}`}>{a.path}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* Audit visibility — same card chrome as the rest of the page.
            CTA buttons live at the bottom so the section reads top-to-
            bottom: intro → endpoints → actions. */}
        <div className={`${cardCls} overflow-hidden`}>
          <div className="px-5 py-3 border-b border-slate-100 flex items-center gap-2 flex-wrap">
            <h3 className={`text-sm font-bold ${sectionLabel}`}>Evidence endpoints</h3>
          </div>
          <div className="p-5 space-y-3">
            <p className={`text-sm ${sublabel}`}>
              These endpoints expose recorded application data subject to access controls. Availability does not establish complete audit coverage or compliance certification.
            </p>
            <AuditEndpointRow
              dark={dark}
              title="Agent run traces"
              endpoint="GET /api/ai/agent/traces"
              desc="Recorded agent decisions and outcomes. Inspect retained evidence for the run being reviewed."
            />
            <AuditEndpointRow
              dark={dark}
              title="Token + cost wallet"
              endpoint="GET /api/ai/agent/budget"
              desc="Daily caps, current usage, request count, dollar cost — all per-user and per-workspace."
            />
            <AuditEndpointRow
              dark={dark}
              title="Pipeline executions + compute usage"
              endpoint="GET /api/monitor/executions"
              desc="Recorded execution metadata and available snapshots. Missing measurements are not evidence of zero usage."
            />
            <div className="flex flex-wrap gap-2 pt-1">
              <button
                type="button"
                onClick={() => navigateTo('executions')}
                className={`px-3 py-1.5 text-xs font-semibold rounded-md ${
                  dark
                    ? 'bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-200'
                    : 'bg-emerald-600 hover:bg-emerald-700 text-white'
                }`}
              >
                Open Executions →
              </button>
              <button
                type="button"
                onClick={() => navigateTo('account')}
                className={`px-3 py-1.5 text-xs font-semibold rounded-md ${
                  dark
                    ? 'bg-white/[0.06] hover:bg-white/[0.1] text-slate-200'
                    : 'bg-white hover:bg-slate-100 text-slate-700 ring-1 ring-slate-200'
                }`}
              >
                View token usage →
              </button>
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className={`text-xs text-center pt-4 ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
          Found a security issue? See <code className="font-mono">security.md</code> for responsible disclosure. We respond within 48 hours.
        </div>
    </div>
  );
}

function AuditEndpointRow({
  dark, title, endpoint, desc,
}: {
  dark: boolean;
  title: string;
  endpoint: string;
  desc: string;
}) {
  return (
    <div className={`rounded-lg p-3 ${dark ? 'bg-[#0b1120] border border-white/[0.08]' : 'bg-slate-50/60 border border-slate-200'}`}>
      <div className={`text-sm font-bold mb-1 ${dark ? 'text-slate-200' : 'text-slate-800'}`}>{title}</div>
      <code className={`font-mono text-xs block ${dark ? 'text-emerald-300' : 'text-emerald-700'}`}>
        {endpoint}
      </code>
      <div className={`text-xs mt-1 ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
        {desc}
      </div>
    </div>
  );
}


// ─────────────────────────────────────────────────────────────────────
// Live posture — Gate 4 evidence sourced from /api/trust/posture and
// /api/trust/eval-summary. Renders at the top of TrustContent so the
// curated story below is anchored by real numbers from the host.
// ─────────────────────────────────────────────────────────────────────

interface LivePosture {
  posture_version: string;
  as_of: string;
  sovereignty: {
    data_stays_local_by_default: boolean;
    telemetry_currently_enabled: boolean;
    active_provider_is_local: boolean;
    active_provider_summary: { provider: string; model: string; is_local: boolean };
    deployment_model: string;
  };
}

interface LiveEval {
  ran: boolean;
  passed?: number;
  total?: number;
  pass_rate?: number;
  ran_at?: string;
  message?: string;
}

function LivePostureSection({ dark, sectionLabel, sublabel }: {
  dark: boolean; cardCls: string; sectionLabel: string; sublabel: string;
}) {
  const [attempt, setAttempt] = useState(0);
  const [posture, setPosture] = useState<any>(null);
  const [evaluation, setEvaluation] = useState<any>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErrors([]);
    Promise.allSettled([api.getTrustPosture(), api.getTrustEvalSummary()]).then(([p, e]) => {
      if (cancelled) return;
      setPosture(p.status === 'fulfilled' ? p.value : null);
      setEvaluation(e.status === 'fulfilled' ? e.value : null);
      setErrors([
        ...(p.status === 'rejected' ? ['Installation diagnostics unavailable.'] : []),
        ...(e.status === 'rejected' ? ['Evaluation evidence unavailable.'] : []),
      ]);
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [attempt]);
  const provider = posture?.sovereignty?.active_provider_summary;
  const telemetry = posture?.sovereignty?.telemetry_currently_enabled;
  const labels: Record<string, string> = {
    verified: 'Verified', configured: 'Configured', not_checked: 'Not checked', failed: 'Failed',
  };
  const controls = posture?.security_baseline || [];
  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h2 className={`text-base font-semibold ${sectionLabel}`}>Installation diagnostics</h2>
        <button type="button" onClick={() => setAttempt(value => value + 1)} disabled={loading}
          className="inline-flex items-center gap-2 px-3 py-2 border rounded-md text-sm disabled:opacity-50"
          title="Refresh diagnostics"><RefreshCw size={16} />Refresh</button>
      </div>
      {loading && <p role="status" className={sublabel}>Checking configuration...</p>}
      {errors.map(error => <p key={error} role="alert" className="text-sm text-red-600">{error}</p>)}
      {posture && <>
        <p className={`text-xs ${sublabel}`}>Response generated: {new Date(posture.as_of).toLocaleString()}. This is not an audit timestamp.</p>
        <dl className="grid grid-cols-1 md:grid-cols-3 gap-4 text-sm">
          <div><dt className={sublabel}>AI provider (current account)</dt>
            <dd className={sectionLabel}>{provider?.provider || 'Unknown'} {provider?.model || ''}</dd>
            <dd className={sublabel}>{labels[provider?.status] || 'Not checked'}; connection not tested here.</dd></div>
          <div><dt className={sublabel}>AI location</dt>
            <dd className={sectionLabel}>{provider?.is_local === true ? 'Local provider configured' : provider?.is_local === false ? 'Cloud provider configured' : 'Not checked'}</dd></div>
          <div><dt className={sublabel}>Telemetry consent</dt>
            <dd className={sectionLabel}>{telemetry === true ? 'Enabled' : telemetry === false ? 'Disabled' : 'Unknown'}</dd></div>
        </dl>
        <p className={`text-xs ${sublabel}`}>Configured means a setting was found, not that enforcement passed. Network egress has not been measured.</p>
        <div className="overflow-x-auto">
          <table className="w-full text-sm text-left">
            <thead><tr className="border-b"><th className="py-2 pr-3">Control</th><th className="py-2 pr-3">Status</th><th className="py-2 pr-3">Evidence / scope</th><th className="py-2">Checked at</th></tr></thead>
            <tbody>{controls.map((control: any) => <tr key={control.key} className="border-b align-top">
              <td className="py-3 pr-3">{control.label}</td>
              <td className="py-3 pr-3 whitespace-nowrap">{labels[control.status] || 'Not checked'}</td>
              <td className={`py-3 pr-3 ${sublabel}`}>{control.detail}{control.evidence && <div>{control.evidence}</div>}</td>
              <td className="py-3">{control.checked_at ? new Date(control.checked_at).toLocaleString() : 'Not checked'}</td>
            </tr>)}</tbody>
          </table>
        </div>
      </>}
      <div className="border-t pt-4">
        <h3 className={`text-sm font-semibold ${sectionLabel}`}>AI evaluation evidence</h3>
        <p className={`text-sm ${sublabel}`}>{evaluation?.ran
          ? `${evaluation.passed ?? 'Unknown'} / ${evaluation.total ?? 'Unknown'} passed. Last run: ${evaluation.ran_at || 'Not recorded'}.`
          : evaluation ? 'No recorded evaluation run.' : 'Evidence unavailable.'}</p>
        <p className={`text-xs mt-1 ${sublabel}`}>Results cover only the recorded test run, not every model, connector or workload. Failed cases are not automatically explained by roadmap coverage.</p>
      </div>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Sprint D — Connector Certification Matrix section
//
// Pulls /api/connectors/cert-matrix (no auth) and renders:
//   1. Top-line summary: total connectors + count by depth label
//   2. Sortable table of every connector with its depth score 0-5
//
// The point: a skeptical buyer can compare F-Pulse to peers on quality
// per connector, not just count. "18 reliable connectors" beats "300
// mediocre ones" — but only if the reliability is publicly verifiable.
// ─────────────────────────────────────────────────────────────────────

interface CertRow {
  id: string;
  display_name: string;
  category?: string;
  vendor?: string;
  manifest_version?: number;
  depth_score: number;
  depth_label: string;
  validation_status: string;
  v1_capability_score?: number;
  issues_count?: number;
  streams_count?: number;
}

interface CertMatrix {
  rows?: CertRow[];
  by_label?: Record<string, number>;
  total?: number;
  last_audited?: string;
}

function CertMatrixSection({
  dark, cardCls, sectionLabel, sublabel,
}: {
  dark: boolean;
  cardCls: string;
  sectionLabel: string;
  sublabel: string;
}) {
  const [matrix, setMatrix] = useState<CertMatrix | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  // 2026-06-17 — the standalone #cert-matrix page was folded into this
  // section. The full per-connector table is now an expandable block here
  // (collapsed by default), with the label filter + search ported over
  // from that page so nothing was lost by removing it.
  const [expanded, setExpanded] = useState(false);
  const [labelFilter, setLabelFilter] = useState<string>('all');
  const [search, setSearch] = useState<string>('');

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // 2026-05-19 (P1 #10 of PAGE_BY_PAGE_AUDIT.md): migrated from raw
        // `fetch` to the shared api client so this surface inherits the
        // global 401 interceptor, X-Workspace-Id header, and the
        // backend-reachable signal that drives the global banner.
        const data = await api.getCertMatrix();
        if (!cancelled) setMatrix(data as CertMatrix);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (loading) {
    return (
      <div className={`${cardCls} px-5 py-4`}>
        <div className={`text-sm ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
          Loading connector validation inventory…
        </div>
      </div>
    );
  }
  if (error || !matrix || !matrix.rows) {
    return null;  // Silent failure — this is a nice-to-have on the trust page.
  }

  const rows = matrix.rows;
  const total = matrix.total || rows.length;
  const byLabel = matrix.by_label || {};

  // Sort rows: highest depth first, then production > beta > alpha > stub.
  const labelOrder: Record<string, number> = {
    production: 4, beta: 3, alpha: 2, stub: 1,
    'v1-functional': 3, 'v1-basic': 2, 'v1-stub': 1,
  };
  const sorted = [...rows].sort((a, b) => {
    if (a.depth_score !== b.depth_score) return b.depth_score - a.depth_score;
    const la = labelOrder[a.depth_label] ?? 0;
    const lb = labelOrder[b.depth_label] ?? 0;
    if (la !== lb) return lb - la;
    return a.display_name.localeCompare(b.display_name);
  });
  const filtered = sorted.filter((r) => {
    if (labelFilter !== 'all' && r.depth_label !== labelFilter) return false;
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      return (
        r.id.toLowerCase().includes(q) ||
        (r.display_name || '').toLowerCase().includes(q) ||
        (r.vendor || '').toLowerCase().includes(q) ||
        (r.category || '').toLowerCase().includes(q)
      );
    }
    return true;
  });
  const visible = showAll ? filtered : filtered.slice(0, 12);

  const productionCount = byLabel.production || 0;
  const betaCount = byLabel.beta || 0;
  const alphaCount = byLabel.alpha || 0;
  const stubCount = byLabel.stub || 0;
  const v1Count = (byLabel['v1-functional'] || 0) + (byLabel['v1-basic'] || 0) + (byLabel['v1-stub'] || 0);

  return (
    <div className={`${cardCls} overflow-hidden`}>
      <div className="px-5 py-3 border-b border-slate-100 flex items-center gap-2 flex-wrap">
        <h3 className={`text-sm font-bold ${sectionLabel}`}>Connector validation inventory</h3>
        <span className={`text-xs font-bold px-2 py-0.5 rounded-full uppercase tracking-wider ${dark ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30' : 'bg-emerald-100 text-emerald-700 border border-emerald-300'}`}>
          Declared capabilities
        </span>
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className={`ml-auto text-xs font-semibold ${dark ? 'text-indigo-300 hover:text-indigo-200' : 'text-indigo-600 hover:text-indigo-700'}`}
        >
          {expanded ? 'Hide full matrix ▾' : `Show full matrix (${total}) ▸`}
        </button>
      </div>

      <div className="p-4 space-y-4">
        <p className={`text-sm ${sublabel}`}>
          Catalog labels and manifest validation describe declared capabilities.
          They do not certify credentials, live connectivity, data correctness or production readiness.
        </p>

        {/* Top-line counters — kept on HeroCard for layout consistency
            but rendered with much softer 100-to-200 gradients so the
            Live Posture row above stays the primary attention sink.
            Two-tier hierarchy: bold gradients for live signals,
            muted pastels for inventory counts. */}
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
          <HeroCard
            gradient="from-indigo-100 to-indigo-200"
            icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2" /></svg>}
            // 2026-06-03 — renamed "Total" → "Catalog (all)" so users
            // who compare this number to the About card's "33
            // Connectors" or the readme's "33 visible default" don't
            // wonder which is right. Both are right, for different
            // definitions; the context line below makes the
            // relationship explicit.
            label="Catalog (all)"
            value={String(total)}
          />
          <HeroCard
            gradient="from-emerald-100 to-emerald-200"
            icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>}
            label="Declared production"
            value={String(productionCount)}
          />
          <HeroCard
            gradient="from-amber-100 to-amber-200"
            icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" /></svg>}
            label="Beta"
            value={String(betaCount)}
          />
          <HeroCard
            gradient="from-rose-100 to-rose-200"
            icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>}
            label="Alpha / Stub"
            value={String(alphaCount + stubCount)}
          />
          <HeroCard
            gradient="from-slate-100 to-slate-200"
            icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" /><line x1="3" y1="9" x2="21" y2="9" /></svg>}
            label="v1 (legacy)"
            value={String(v1Count)}
          />
        </div>

        {/* 2026-06-03 — count-definition explainer. Until this turn the
            Trust "Total" silently included v1 legacy entries, so users
            comparing it to the About card ("33 Connectors") or the
            readme ("33 first-party visible default") would see a
            mismatch. This sentence reconciles the numbers in one
            line, sourced from the live cert-matrix. */}
        <div className={`text-xs ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
          <strong className={sectionLabel}>How to read these counts</strong> ·{' '}
          <code className="font-mono">Catalog (all) = {total}</code> includes both v2 tier-rated
          connectors ({productionCount + betaCount + alphaCount + stubCount}) and
          v1 legacy entries ({v1Count}) that haven't been migrated to the new
          tier system. These counts describe the catalog, not successful live
          connector tests.
        </div>

        {/* Full per-connector matrix — folded in from the old standalone
            #cert-matrix page (2026-06-17) as an expandable section. Collapsed
            by default to keep the Trust page scannable; expand for the full
            filterable / searchable table. */}
        {expanded && (
          <>
            {/* Label filter + search — ported from the standalone page so
                the fold loses nothing. */}
            <div className="flex flex-wrap items-center gap-2">
              <span className={`text-xs uppercase tracking-wide mr-1 ${sublabel}`}>Filter</span>
              {([
                ['all', 'All', total],
                ['production', 'Production', productionCount],
                ['beta', 'Beta', betaCount],
                ['alpha', 'Alpha', alphaCount],
                ['stub', 'Stub', stubCount],
              ] as Array<[string, string, number]>).map(([key, lbl, count]) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setLabelFilter(key)}
                  className={`px-2.5 py-1 rounded text-xs border transition-colors ${
                    labelFilter === key
                      ? (dark ? 'bg-indigo-500/20 text-indigo-200 border-indigo-500/40' : 'bg-slate-900 text-white border-slate-900')
                      : (dark ? 'bg-transparent text-slate-400 border-white/[0.1] hover:bg-white/[0.05]' : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-50')
                  }`}
                >
                  {lbl} <span className="opacity-60">({count})</span>
                </button>
              ))}
              <input
                type="text"
                placeholder="Search id, name, vendor, category"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className={`ml-auto px-3 py-1.5 rounded border text-sm w-56 ${dark ? 'bg-[#0b1120] border-white/[0.1] text-slate-200 placeholder:text-slate-500' : 'border-slate-300'}`}
              />
            </div>

            {/* Per-connector table — dark+amber header, matches every
                other data table in the app. */}
            <div className={`rounded-lg overflow-hidden border ${dark ? 'border-white/[0.08]' : 'border-slate-200'}`}>
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900 border-b-2 border-amber-400/40">
                    <th className="text-left px-4 py-2.5 text-xs font-bold text-amber-300 uppercase tracking-wider">Connector</th>
                    <th className="text-left px-3 py-2.5 text-xs font-bold text-amber-300 uppercase tracking-wider hidden sm:table-cell">Category</th>
                    <th className="text-left px-3 py-2.5 text-xs font-bold text-amber-300 uppercase tracking-wider">Depth</th>
                    <th className="text-left px-3 py-2.5 text-xs font-bold text-amber-300 uppercase tracking-wider">Status</th>
                    <th className="text-right px-3 py-2.5 text-xs font-bold text-amber-300 uppercase tracking-wider hidden md:table-cell">Streams</th>
                  </tr>
                </thead>
                <tbody className={`divide-y ${dark ? 'divide-white/[0.06] bg-[#0b1120]' : 'divide-slate-100 bg-white'}`}>
                  {visible.map((r) => (
                    <tr key={r.id} className={dark ? 'hover:bg-slate-900/60' : 'hover:bg-slate-50'}>
                      <td className={`px-4 py-2 ${dark ? 'text-slate-200' : 'text-slate-800'}`}>
                        <div className="font-semibold text-sm">{r.display_name}</div>
                        <div className={`text-xs font-mono ${dark ? 'text-slate-500' : 'text-slate-400'}`}>{r.id}</div>
                      </td>
                      <td className={`px-3 py-2 hidden sm:table-cell text-sm ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
                        {r.category || '—'}
                      </td>
                      <td className="px-3 py-2">
                        <DepthBar dark={dark} score={r.depth_score} />
                      </td>
                      <td className="px-3 py-2">
                        <DepthBadge dark={dark} label={r.depth_label} status={r.validation_status} />
                      </td>
                      <td className={`px-3 py-2 text-right hidden md:table-cell text-sm tabular-nums ${dark ? 'text-slate-300' : 'text-slate-600'}`}>
                        {r.streams_count ?? '—'}
                      </td>
                    </tr>
                  ))}
                  {visible.length === 0 && (
                    <tr>
                      <td colSpan={5} className={`text-center py-6 text-sm ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
                        No connectors match the current filter.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            {filtered.length > 12 && (
              <div className="text-center">
                <button
                  type="button"
                  onClick={() => setShowAll(!showAll)}
                  className={`text-xs font-semibold ${dark ? 'text-indigo-300 hover:text-indigo-200' : 'text-indigo-600 hover:text-indigo-700'}`}
                >
                  {showAll ? `Show top 12 only` : `Show all ${filtered.length} connectors`}
                </button>
              </div>
            )}
          </>
        )}

        {matrix.last_audited && (
          <div className={`text-xs ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
            Inventory evaluated {new Date(matrix.last_audited).toLocaleString()} · not a live connector certification
          </div>
        )}
      </div>
    </div>
  );
}

function DepthBar({ dark, score }: { dark: boolean; score: number }) {
  const filled = Math.max(0, Math.min(5, score));
  return (
    <div className="flex items-center gap-1.5">
      <div className="flex gap-0.5">
        {[1, 2, 3, 4, 5].map((i) => (
          <div
            key={i}
            className={`h-1.5 w-3 rounded-full ${
              i <= filled
                ? (filled >= 3
                    ? (dark ? 'bg-emerald-400' : 'bg-emerald-500')
                    : (dark ? 'bg-amber-400' : 'bg-amber-500'))
                : (dark ? 'bg-slate-700' : 'bg-slate-200')
            }`}
          />
        ))}
      </div>
      <span className={`text-xs font-mono tabular-nums ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
        {filled}/5
      </span>
    </div>
  );
}

function DepthBadge({ dark, label, status }: { dark: boolean; label: string; status: string }) {
  const tones: Record<string, { light: string; darkVar: string }> = {
    production:       { light: 'bg-emerald-100 text-emerald-700', darkVar: 'bg-emerald-500/15 text-emerald-300' },
    beta:             { light: 'bg-amber-100 text-amber-700',     darkVar: 'bg-amber-500/15 text-amber-300' },
    alpha:            { light: 'bg-rose-100 text-rose-700',       darkVar: 'bg-rose-500/15 text-rose-300' },
    stub:             { light: 'bg-slate-100 text-slate-600',     darkVar: 'bg-slate-500/15 text-slate-400' },
    'v1-functional':  { light: 'bg-blue-100 text-blue-700',       darkVar: 'bg-blue-500/15 text-blue-300' },
    'v1-basic':       { light: 'bg-blue-100 text-blue-700',       darkVar: 'bg-blue-500/15 text-blue-300' },
    'v1-stub':        { light: 'bg-slate-100 text-slate-600',     darkVar: 'bg-slate-500/15 text-slate-400' },
  };
  const t = tones[label] || tones.stub;
  const failed = status === 'fail';
  return (
    <div className="flex items-center gap-1.5">
      <span className={`text-xs font-bold px-2 py-0.5 rounded uppercase tracking-wider ${dark ? t.darkVar : t.light}`}>
        {label}
      </span>
      {failed && (
        <span className={`text-xs ${dark ? 'text-red-400' : 'text-red-600'}`} title="Validator errors present">
          ⚠
        </span>
      )}
    </div>
  );
}
