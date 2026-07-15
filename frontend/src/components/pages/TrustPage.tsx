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
        title="Trust posture"
        subtitle="F-Pulse runs on YOUR infrastructure. Data, credentials, and pipeline IR stay on the machine unless you configure a cloud LLM provider — and even then only sanitized summaries leave."
        titleAccessory={(
          <span className={`text-xs font-bold px-2.5 py-0.5 rounded-full uppercase tracking-wider ${dark ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30' : 'bg-emerald-100 text-emerald-700 border border-emerald-300'}`}>
            Public
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
            Every claim above is backed by a file in the repo. Reviewers and auditors can read these directly.
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
            <h3 className={`text-sm font-bold ${sectionLabel}`}>Verify, don't trust — audit endpoints</h3>
          </div>
          <div className="p-5 space-y-3">
            <p className={`text-sm ${sublabel}`}>
              Every claim in this page is backed by a queryable endpoint. Reviewers can pull the raw audit log without reading source. The data is already on YOUR machine — these endpoints just expose it.
            </p>
            <AuditEndpointRow
              dark={dark}
              title="Agent run traces"
              endpoint="GET /api/ai/agent/traces"
              desc="Replay-safe: tool I/O hashes only, never raw values. Outcome class, latency, redactions count."
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
              desc="Each row carries metadata.peak_memory_mb, cpu_seconds, parameter_values, and the SHA-256 IR snapshot the run executed."
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

function LivePostureSection({
  dark, cardCls, sectionLabel, sublabel,
}: {
  dark: boolean;
  cardCls: string;
  sectionLabel: string;
  sublabel: string;
}) {
  const [posture, setPosture] = useState<LivePosture | null>(null);
  const [evalSummary, setEvalSummary] = useState<LiveEval | null>(null);
  const [loading, setLoading] = useState(true);
  // 2026-05-19 (P1 #4 of PAGE_BY_PAGE_AUDIT.md): the posture section used
  // to `return null` on fetch failure, which left compliance reviewers
  // staring at a half-rendered page with no explanation. We now surface
  // the failure via the shared <ErrorBanner> with a retry so the reviewer
  // at least knows what's missing.
  const [postureError, setPostureError] = useState<string | null>(null);

  const loadPosture = () => {
    setLoading(true);
    setPostureError(null);
    let cancelled = false;
    (async () => {
      try {
        const [p, e] = await Promise.all([
          api.getTrustPosture().catch((err) => { throw err; }),
          api.getTrustEvalSummary().catch(() => null), // eval is supplementary; OK to omit
        ]);
        if (cancelled) return;
        if (p) setPosture(p as LivePosture);
        if (e) setEvalSummary(e as LiveEval);
      } catch (err: any) {
        if (!cancelled) setPostureError(err?.message || 'Failed to load /api/trust/posture');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  };

  useEffect(() => {
    const cleanup = loadPosture();
    return cleanup;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (loading) return null;
  if (postureError) {
    return (
      <div className={`${cardCls} p-4`}>
        <ErrorBanner
          title="Live posture unavailable"
          message={`${postureError} — the live host evidence (egress, provider, telemetry, eval pass-rate) is missing from this view. Static artifacts below still apply.`}
          onRetry={loadPosture}
        />
      </div>
    );
  }
  if (!posture) return null;

  const sov = posture.sovereignty;
  const passRatePct = evalSummary?.ran && evalSummary.pass_rate !== undefined ? Math.round(evalSummary.pass_rate * 100) : 0;
  const passRateOk = !!(evalSummary?.ran && passRatePct >= 80);

  // Pick a gradient per posture cell — green for "ok", amber otherwise.
  // Mirrors the Dashboard / Pipelines / Pool HeroCard rows so the page
  // joins the same visual family.
  const grad = (ok: boolean) =>
    ok ? 'from-emerald-400 to-emerald-500' : 'from-amber-400 to-orange-500';

  return (
    <div className={`${cardCls} overflow-hidden`}>
      <div className="px-5 py-3 border-b border-slate-100 flex items-center gap-2 flex-wrap">
        <h3 className={`text-sm font-bold ${sectionLabel}`}>Live posture (this host)</h3>
        <span className={`text-xs ${dark ? 'text-slate-500' : 'text-slate-500'} ml-auto`}>
          v{posture.posture_version} · {new Date(posture.as_of).toLocaleString()}
        </span>
      </div>

      <div className="p-4">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <HeroCard
            gradient={grad(sov.data_stays_local_by_default)}
            icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></svg>}
            label="Default egress"
            value={sov.data_stays_local_by_default ? 'None' : 'Custom'}
          />
          <HeroCard
            gradient={grad(sov.active_provider_is_local)}
            icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="20" height="14" rx="2" /><line x1="8" y1="21" x2="16" y2="21" /><line x1="12" y1="17" x2="12" y2="21" /></svg>}
            label="AI provider"
            value={sov.active_provider_summary.provider || 'none'}
          />
          <HeroCard
            gradient={grad(!sov.telemetry_currently_enabled)}
            icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2" /></svg>}
            label="Telemetry"
            value={sov.telemetry_currently_enabled ? 'On' : 'Off'}
          />
          <HeroCard
            gradient={grad(passRateOk)}
            icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" /></svg>}
            label="Eval pass rate"
            value={
              evalSummary?.ran && evalSummary.total
                ? `${evalSummary.passed}/${evalSummary.total}`
                : 'Not run'
            }
            valueSuffix={evalSummary?.ran && evalSummary.total ? ` (${passRatePct}%)` : ''}
            bar={evalSummary?.ran && evalSummary.total ? passRatePct : undefined}
          />
        </div>

        {/* Per-cell context underneath the HeroCard row — kept as a
            compact key/value list rather than baking each hint into the
            cards (HeroCard's `value` is meant for one short label). */}
        <div className={`mt-3 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-2 text-xs ${sublabel}`}>
          <div><strong className={sectionLabel}>Egress</strong> · Default config sends nothing off-box.</div>
          <div>
            <strong className={sectionLabel}>Provider</strong> ·{' '}
            {sov.active_provider_is_local ? `Local: ${sov.active_provider_summary.model || '—'}` : 'Cloud opt-in: prompts leave the host'}
          </div>
          <div>
            <strong className={sectionLabel}>Telemetry</strong> ·{' '}
            {sov.telemetry_currently_enabled ? 'Admin opted in.' : 'Off by default.'}
          </div>
          <div>
            <strong className={sectionLabel}>Eval</strong> ·{' '}
            {evalSummary?.ran && evalSummary.ran_at
              ? `Last run ${new Date(evalSummary.ran_at).toLocaleDateString()}`
              : 'Run python -m fpulse.eval.run to populate.'}
          </div>
        </div>

        {/* 2026-06-03 — context line for the Eval Pass Rate tile. The
            raw "48/339 (14%)" number above is honest but optically
            alarming; this sentence frames it correctly: the eval
            denominator is the FULL future-coverage battery (339
            prompts across all node types + adversarial cases), not
            "tests that should pass at v1.0". Coverage growth is on
            the post-1.0 reliability sprint roadmap. */}
        {evalSummary?.ran && evalSummary.total ? (
          <div className={`mt-2 text-xs ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
            <strong className={sectionLabel}>About the eval denominator</strong> ·{' '}
            {evalSummary.total} is the full prompt battery (every node type, AI
            prompts, adversarial edge cases). The {evalSummary.passed} passing
            today are the v1.0-shipped surfaces; the rest are pending coverage
            tracked in <code className="font-mono">docs/roadmap/reliability-sprint.md</code>.
            Honest measurement &gt; flattering selection bias.
          </div>
        ) : null}

        <div className="mt-4 flex flex-wrap gap-2 text-xs">
          <a
            href="/api/trust/posture"
            target="_blank"
            rel="noreferrer"
            className={`px-2.5 py-1 rounded font-mono ${
              dark
                ? 'bg-slate-900 text-emerald-300 hover:bg-slate-800'
                : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
            }`}
          >
            /api/trust/posture
          </a>
          <a
            href="/api/connectors/cert-matrix"
            target="_blank"
            rel="noreferrer"
            className={`px-2.5 py-1 rounded font-mono ${
              dark
                ? 'bg-slate-900 text-emerald-300 hover:bg-slate-800'
                : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
            }`}
          >
            /api/connectors/cert-matrix
          </a>
          <a
            href="/docs/compliance.md"
            target="_blank"
            rel="noreferrer"
            className={`px-2.5 py-1 rounded font-mono ${
              dark
                ? 'bg-slate-900 text-emerald-300 hover:bg-slate-800'
                : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
            }`}
          >
            docs/compliance.md
          </a>
          <a
            href="/docs/supported-models.md"
            target="_blank"
            rel="noreferrer"
            className={`px-2.5 py-1 rounded font-mono ${
              dark
                ? 'bg-slate-900 text-emerald-300 hover:bg-slate-800'
                : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
            }`}
          >
            docs/supported-models.md
          </a>
        </div>

        <div className={`text-xs mt-3 ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
          Every claim above is sourced from a live endpoint a reviewer can verify
          independently. {sov.deployment_model}.
        </div>
      </div>
    </div>
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
          Loading connector certification matrix…
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
        <h3 className={`text-sm font-bold ${sectionLabel}`}>Connector certification</h3>
        <span className={`text-xs font-bold px-2 py-0.5 rounded-full uppercase tracking-wider ${dark ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30' : 'bg-emerald-100 text-emerald-700 border border-emerald-300'}`}>
          Public · No auth
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
          Every connector in F-Pulse runs through the F0.1 validator and gets a depth score 0–5.
          <strong> Depth ≥ 3 means production-grade</strong> (auth, schema, retry, pagination, incremental sync, fixtures).{' '}
          <code className={`font-mono text-xs ${dark ? 'text-emerald-300' : 'text-emerald-700'}`}>curl /api/connectors/cert-matrix</code> verifies it.
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
            label="Production"
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
          tier system. The About card and readme cite "33 visible default" — the
          user-facing subset surfaced in the palette by default (excludes
          hidden / SMB-CRM-only manifests). Both are accurate for their
          respective definitions; see <code className="font-mono">docs/connectors.md</code> for the full per-connector matrix.
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
            Audited {new Date(matrix.last_audited).toLocaleString()} · re-runs on every request
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
