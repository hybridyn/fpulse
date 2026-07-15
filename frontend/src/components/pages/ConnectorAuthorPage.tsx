/**
 * ConnectorAuthorPage — Sprint C launch demo.
 *
 * Paste an OpenAPI spec URL (or sample API responses) → get a v2 manifest
 * skeleton + validation report. The deterministic generator runs server-side
 * via /api/connectors/author/{from-openapi,from-samples}.
 *
 * Embedded inside AIPage as the "Author" subtab. Standalone as well.
 */

import { useState, useEffect } from 'react';
import { useDarkMode } from '../../hooks/useDarkMode';
import { api } from '../../api/client';
import { usePageContext } from '../../hooks/usePageContext';

type Mode = 'openapi' | 'samples';

/**
 * Curated "common starting points" — vendors that publish a clean
 * OpenAPI 3.x spec at a stable URL, covering popular categories so a
 * first-time author sees an example that's close to whatever they need.
 *
 * Click → pre-fills connector_id + display_name + openapi_url + flips
 * mode to 'openapi' + advances the step indicator. From there the user
 * hits Continue → Generate, gets a working manifest in ~90 seconds
 * without ever having to think up an example to test the flow on.
 *
 * Selection criteria — these all need to be:
 *   1. Apache 2.0 / public-domain OpenAPI specs (vendor publishes openly)
 *   2. Stable URLs (GitHub raw / official CDN, not docs-site URLs that
 *      rewrite on every site refresh)
 *   3. Modern OpenAPI 3.x (not Swagger 2.0 — the generator handles 3.x
 *      strictly per /api/connectors/author/from-openapi)
 *   4. Categories that resonate: payments, dev tools, communication,
 *      e-commerce, infrastructure, financial-data, telecom.
 *
 * Adding more: append an entry. The grid below auto-flows.
 */
interface StartingPoint {
  id: string;             // becomes connector_id (lowercase + underscore only)
  name: string;           // becomes display_name
  category: string;       // shown as a small chip on the card
  url: string;            // OpenAPI URL the server fetches at generate time
  blurb: string;          // one-line description
}

const STARTING_POINTS: StartingPoint[] = [
  {
    id: 'stripe',
    name: 'Stripe',
    category: 'Payments',
    url: 'https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json',
    blurb: 'Payments, subscriptions, billing.',
  },
  {
    id: 'github',
    name: 'GitHub',
    category: 'Developer',
    url: 'https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json',
    blurb: 'Repos, issues, pull requests, Actions.',
  },
  {
    id: 'slack',
    name: 'Slack',
    category: 'Communication',
    url: 'https://raw.githubusercontent.com/slackapi/slack-api-specs/master/web-api/slack_web_openapi_v2.json',
    blurb: 'Channels, users, files, messages.',
  },
  {
    id: 'twilio',
    name: 'Twilio',
    category: 'Telecom',
    url: 'https://raw.githubusercontent.com/twilio/twilio-oai/main/spec/yaml/twilio_api_v2010.yaml',
    blurb: 'SMS, voice, phone-number management.',
  },
  {
    id: 'digitalocean',
    name: 'DigitalOcean',
    category: 'Infrastructure',
    url: 'https://api-engineering.nyc3.cdn.digitaloceanspaces.com/spec-ci/DigitalOcean-public.v2.yaml',
    blurb: 'Droplets, Kubernetes, databases.',
  },
  {
    id: 'plaid',
    name: 'Plaid',
    category: 'Financial data',
    url: 'https://raw.githubusercontent.com/plaid/plaid-openapi/master/2020-09-14.yml',
    blurb: 'Bank-account aggregation, transactions.',
  },
];

interface ValidationResult {
  connector_id: string;
  valid: boolean;
  declared_depth_score: number;
  computed_depth_score: number;
  effective_depth_score: number;
  errors: string[];
  warnings: string[];
  streams_evaluated: string[];
}

interface AuthorResponse {
  manifest: Record<string, any>;
  validation: ValidationResult;
  mode: Mode;
}

export default function ConnectorAuthorPage({ embedded = false }: { embedded?: boolean }) {
  const dark = useDarkMode();
  const [mode, setMode] = useState<Mode>('openapi');
  const [authorStep, setAuthorStep] = useState<0 | 1 | 2>(0);
  const [connectorId, setConnectorId] = useState('');

  // FOLLOW-3 (2026-05-19) — publish authoring context so the Copilot
  // knows the user is mid-author-flow if they ask "what should I name
  // this connector?" or "is this manifest depth-3?".
  usePageContext({
    page: 'author',
    filters: { mode, connector_id: connectorId || null },
  });
  const [displayName, setDisplayName] = useState('');
  const [openapiUrl, setOpenapiUrl] = useState('');
  const [samplesText, setSamplesText] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [streamName, setStreamName] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<AuthorResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reset = () => { setResult(null); setError(null); };

  // 2026-05-29: pre-fill from URL query params. Gallery + empty-state
  // links route here as `#author?prefill_id=stripe&prefill_url=...`
  // (the older `#ai?tab=author&...` shape was broken — AIPage ignores
  // `?tab=` and lands on the default tab. Direct `#author` route in
  // App.tsx renders AIPage with `initialTab="author"`. Fixed 2026-06-02.)
  // We read the params on mount, populate the form, switch to openapi
  // mode (since prefill carries a URL not pasted samples), and clean
  // the URL so a refresh doesn't fire the prefill twice.
  useEffect(() => {
    try {
      const hash = window.location.hash;
      const qIndex = hash.indexOf('?');
      if (qIndex === -1) return;
      const params = new URLSearchParams(hash.slice(qIndex + 1));
      const prefillId = params.get('prefill_id');
      const prefillUrl = params.get('prefill_url');
      const prefillName = params.get('prefill_name');
      if (!prefillUrl && !prefillId) return;
      if (prefillUrl) {
        setMode('openapi');
        setOpenapiUrl(prefillUrl);
      }
      if (prefillId) {
        // Same lowercase + underscore guard as the manual input.
        setConnectorId(prefillId.toLowerCase().replace(/[^a-z0-9_]/g, ''));
      }
      if (prefillName) {
        setDisplayName(prefillName);
      } else if (prefillId) {
        // Reasonable default — capitalise the id when no name is given.
        setDisplayName(prefillId.charAt(0).toUpperCase() + prefillId.slice(1));
      }
      // Strip the prefill params from the URL so a refresh doesn't
      // re-fire (and a copied URL doesn't carry the seed).
      params.delete('prefill_id');
      params.delete('prefill_url');
      params.delete('prefill_name');
      const remaining = params.toString();
      const newHash = hash.slice(0, qIndex) + (remaining ? `?${remaining}` : '');
      // history.replaceState avoids triggering a new entry that
      // back-button would land back on the prefilled state.
      window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}${newHash}`);
    } catch {
      // Best-effort: prefill failure must not break the page.
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const generate = async () => {
    if (!connectorId.trim()) {
      setError('connector_id is required');
      return;
    }
    setLoading(true); reset();
    try {
      // 2026-05-19 (P1 #10 of PAGE_BY_PAGE_AUDIT.md): migrated from raw
      // `fetch` to the shared api client so this surface inherits the
      // global auth refresh, workspace-id header, 401 interceptor, and
      // backend-reachable banner instead of running on its own pipe.
      let body: any;
      let endpoint: string;

      if (mode === 'openapi') {
        if (!openapiUrl.trim()) { setError('Provide an OpenAPI URL'); setLoading(false); return; }
        endpoint = '/connectors/author/from-openapi';
        body = {
          connector_id: connectorId.trim(),
          display_name: displayName.trim() || undefined,
          openapi_url: openapiUrl.trim(),
        };
      } else {
        let parsed: any;
        try { parsed = JSON.parse(samplesText); }
        catch { setError('Samples must be valid JSON (array of objects, or single object)'); setLoading(false); return; }
        const samples = Array.isArray(parsed) ? parsed : [parsed];
        endpoint = '/connectors/author/from-samples';
        body = {
          connector_id: connectorId.trim(),
          display_name: displayName.trim() || undefined,
          base_url: baseUrl.trim() || undefined,
          stream_name: streamName.trim() || undefined,
          samples,
        };
      }

      try {
        const res = await api.post<AuthorResponse>(endpoint, body);
        setResult(res);
      } catch (e: any) {
        setError(e?.message || 'Generation failed');
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const downloadManifest = () => {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result.manifest, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${connectorId || 'connector'}.v2.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // D2 — copy the generated connector definition to clipboard. Tied to
  // the next-step affordance below; many operators paste the manifest
  // straight into a deploy automation rather than downloading.
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const copyManifest = async () => {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(result.manifest, null, 2));
      setCopyState('copied');
      setTimeout(() => setCopyState('idle'), 1500);
    } catch {
      setCopyState('failed');
      setTimeout(() => setCopyState('idle'), 1500);
    }
  };

  // Save & use (Beta) — persist the connector to the writable store so it
  // loads immediately as a Beta tile (no filesystem, no restart). The page
  // above generates the v2 *cert* draft; the runtime engine loads v1, so we
  // fetch the v1 runtime manifest first, then POST it to the save endpoint.
  // The backend gates saving to admin/lead; everyone can then use it.
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [saveMsg, setSaveMsg] = useState<string>('');
  const saveAsBetaConnector = async () => {
    if (mode !== 'openapi') {
      setSaveState('error');
      setSaveMsg('Save & use currently supports the OpenAPI path; for samples, download the manifest for now.');
      return;
    }
    if (!connectorId.trim() || !openapiUrl.trim()) {
      setSaveState('error');
      setSaveMsg('Need a connector id and an OpenAPI URL.');
      return;
    }
    setSaveState('saving');
    setSaveMsg('');
    try {
      const rt = await api.post<{ manifest: any }>('/connectors/author/from-openapi-runtime', {
        connector_id: connectorId.trim(),
        display_name: displayName.trim() || undefined,
        openapi_url: openapiUrl.trim(),
      });
      const res = await api.post<{ name: string; streams: number }>(
        '/connectors/author/save', { manifest: rt.manifest },
      );
      setSaveState('saved');
      setSaveMsg(`Saved "${res.name}" as a Beta connector (${res.streams} stream${res.streams === 1 ? '' : 's'}). It is now in the SaaS Connector node — no restart needed.`);
    } catch (e: any) {
      setSaveState('error');
      const msg = e?.message || 'Save failed';
      setSaveMsg(/403|forbidden|role|requires/i.test(msg)
        ? 'Saving a connector requires an admin or lead role.'
        : msg);
    }
  };

  // ── Styles ────────────────────────────────────────────────────────
  // Sizing aims for comfortable readability: body text 14px (text-sm),
  // labels 13.5px (text-[13.5px] / text-sm-ish), helper captions 12px.
  // Earlier sizes were 10-12px throughout — fine for dense tables but
  // wrong for a data-entry surface.
  const cardCls = dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200';
  const inputCls = `w-full px-3 py-2 text-sm rounded-lg border ${
    dark ? 'bg-slate-800 border-slate-700 text-slate-100' : 'bg-white border-slate-200 text-slate-700'
  } focus:outline-none focus:ring-2 focus:ring-pipe-300`;
  const labelCls = `block text-sm font-semibold mb-1.5 ${dark ? 'text-slate-200' : 'text-slate-700'}`;
  const helperCls = `text-xs mt-1 ${dark ? 'text-slate-400' : 'text-slate-500'}`;
  const sourceReady = mode === 'openapi' ? !!openapiUrl.trim() : !!samplesText.trim();
  const basicsReady = !!connectorId.trim();
  const canGenerate = basicsReady && sourceReady && !loading;
  const steps: Array<{ key: 0 | 1 | 2; label: string; detail: string }> = [
    { key: 0, label: 'Basics', detail: 'Method and connector identity' },
    { key: 1, label: 'Source', detail: 'OpenAPI URL or sample JSON' },
    { key: 2, label: 'Generate', detail: 'Create and validate connector definition' },
  ];

  return (
    <div className={`${embedded ? '' : 'flex-1 overflow-auto p-6'} ${dark ? 'text-slate-100' : 'text-slate-800'}`}>
      <div className="max-w-[1300px] mx-auto space-y-4">
        {/* Header */}
        {!embedded && (
          <div>
            <h1 className="text-2xl font-bold">Connector Authoring</h1>
            <p className={`text-sm leading-relaxed mt-1 ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
              Generate a F-Pulse v2 connector definition from an OpenAPI spec or sample API responses.
              The generator is deterministic — the AI polish layer (better defaults, smarter
              pagination inference) is a follow-up. Output is a starter, not a finished connector.
            </p>
          </div>
        )}

        {/* Two-column layout: input | output */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Input pane */}
          <div className={`rounded-xl border shadow-sm p-5 space-y-4 ${cardCls}`}>
            <div>
              <span className="text-xs font-bold uppercase tracking-wider">Input</span>
              <p className={`mt-1 text-sm leading-relaxed ${dark ? 'text-slate-200' : 'text-slate-800'}`}>
                <strong>Generate a connector from an API spec.</strong> Paste an OpenAPI
                URL (or sample JSON responses) → get a connector definition with
                endpoints, schemas, primary keys, pagination, auth, and rate limits.
              </p>
              <p className={`mt-1 text-[12px] leading-relaxed ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
                Skips the hand-written extractor, pagination loop, and schema-drift
                handling. The output runs on the F-Pulse runtime directly — no Python
                source to maintain.
              </p>
              <p className={`mt-2 text-[12px] leading-relaxed ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
                Pick <strong>OpenAPI spec</strong> when the vendor publishes one —
                fastest and most accurate. Use <strong>Sample responses</strong> when
                they don't.
              </p>
            </div>

            {/* Step indicator — radio cards, not a hidden toggle.
                Z34 (2026-05-23): the previous logic derived `done`
                purely from `authorStep > step.key`, which lit a green ✓
                badge on a step the user had jumped past without
                actually filling its fields. Now `done` requires both
                forward progression AND the step's data being valid
                (basicsReady for step 0, sourceReady for step 1). The
                step 2 (Generate) chip is never "done" until the
                manifest is actually generated. Direct click on a
                forward step is gated on the previous step's readiness
                so the same trap can't be triggered from the indicator. */}
            <div className={`rounded-xl border p-3 ${dark ? 'border-slate-700 bg-slate-900/50' : 'border-slate-200 bg-slate-50'}`}>
              <div className="grid grid-cols-3 gap-2">
                {steps.map((step, index) => {
                  const active = authorStep === step.key;
                  // Step 0 is "done" iff we've moved past it AND Basics is valid.
                  // Step 1 is "done" iff we've moved past it AND Source is valid.
                  // Step 2 is never "done" by step traversal — the Generate
                  //   button at the bottom is the only completion signal,
                  //   and a successful `result` lights it up post-generate.
                  const stepReadyMap: Record<0 | 1 | 2, boolean> = {
                    0: basicsReady,
                    1: sourceReady,
                    2: !!result,
                  };
                  const done =
                    step.key === 2
                      ? !!result
                      : authorStep > step.key && stepReadyMap[step.key];
                  // Forward navigation via the chip is blocked when the
                  // previous step isn't ready. Backward navigation
                  // (jumping to step 0 from step 2) is always allowed.
                  const canJump =
                    step.key <= authorStep
                      ? true
                      : step.key === 1
                        ? basicsReady
                        : basicsReady && sourceReady;
                  return (
                    <button
                      key={step.key}
                      type="button"
                      onClick={() => { if (canJump) setAuthorStep(step.key); }}
                      disabled={!canJump}
                      title={
                        canJump
                          ? undefined
                          : step.key === 1
                            ? 'Add a Connector ID first.'
                            : `Fill ${mode === 'openapi' ? 'the OpenAPI URL' : 'sample JSON'} first.`
                      }
                      className={`text-left rounded-lg border px-3 py-2 transition-colors ${
                        active
                          ? (dark ? 'border-indigo-400 bg-indigo-500/15 text-indigo-100' : 'border-indigo-400 bg-indigo-50 text-indigo-800')
                          : done
                            ? (dark ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-100' : 'border-emerald-200 bg-emerald-50 text-emerald-800')
                            : (dark ? 'border-slate-700 bg-slate-900 text-slate-300' : 'border-slate-200 bg-white text-slate-600')
                      } ${!canJump ? 'opacity-50 cursor-not-allowed' : ''}`}
                    >
                      <div className="flex items-center gap-2">
                        <span className={`grid h-6 w-6 place-items-center rounded-full text-xs font-bold ${
                          active
                            ? 'bg-indigo-500 text-white'
                            : done
                              ? 'bg-emerald-500 text-white'
                              : (dark ? 'bg-slate-800 text-slate-300' : 'bg-slate-100 text-slate-500')
                        }`}>
                          {done ? '✓' : index + 1}
                        </span>
                        <span className="text-sm font-bold">{step.label}</span>
                      </div>
                      <div className={`mt-1 text-[11px] ${dark ? 'text-slate-400' : 'text-slate-500'}`}>{step.detail}</div>
                    </button>
                  );
                })}
              </div>
            </div>

            <div className={authorStep === 0 ? 'space-y-4' : 'hidden'}>
            <div className="grid grid-cols-2 gap-2">
              {([
                { id: 'openapi', title: 'OpenAPI spec', subtitle: 'Paste a spec URL' },
                { id: 'samples', title: 'Sample responses', subtitle: 'Paste raw JSON' },
              ] as { id: Mode; title: string; subtitle: string }[]).map(opt => (
                <button
                  key={opt.id}
                  type="button"
                  onClick={() => { setMode(opt.id); reset(); }}
                  className={`text-left p-3.5 rounded-lg border-2 transition-colors ${
                    mode === opt.id
                      ? (dark ? 'bg-indigo-500/15 border-indigo-400/60' : 'bg-indigo-50 border-indigo-400')
                      : (dark ? 'bg-slate-900 border-slate-700 hover:border-slate-500' : 'bg-white border-slate-200 hover:border-slate-300')
                  }`}
                >
                  <div className={`text-sm font-bold ${
                    mode === opt.id
                      ? (dark ? 'text-indigo-200' : 'text-indigo-700')
                      : (dark ? 'text-slate-200' : 'text-slate-700')
                  }`}>{opt.title}</div>
                  <div className={`text-xs mt-1 ${dark ? 'text-slate-400' : 'text-slate-500'}`}>{opt.subtitle}</div>
                </button>
              ))}
            </div>

            {/* 2026-05-29: Common starting points gallery — only shown in
                openapi mode (samples mode requires paste; nothing to
                pre-fill). Closes the "I want to try this but don't have
                a URL handy" friction: click any card → connector_id,
                display_name, OpenAPI URL pre-filled, ready to Continue
                → Generate. Source list is STARTING_POINTS (top of file).
                Counts as both an onboarding affordance AND a working
                demo of what the from-OpenAPI path can do. */}
            {mode === 'openapi' && (
              <div>
                <div className={`flex items-center justify-between mb-2`}>
                  <span className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
                    Common starting points
                  </span>
                  <span className={`text-[11px] ${dark ? 'text-slate-500' : 'text-slate-400'}`}>
                    Click to pre-fill ↓
                  </span>
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                  {STARTING_POINTS.map(sp => (
                    <button
                      key={sp.id}
                      type="button"
                      onClick={() => {
                        setConnectorId(sp.id);
                        setDisplayName(sp.name);
                        setOpenapiUrl(sp.url);
                        reset();
                      }}
                      className={`text-left p-2.5 rounded-lg border transition-colors group ${
                        connectorId === sp.id && openapiUrl === sp.url
                          ? (dark ? 'border-indigo-400/60 bg-indigo-500/10' : 'border-indigo-400 bg-indigo-50')
                          : (dark ? 'border-slate-700 bg-slate-900/40 hover:border-slate-500 hover:bg-slate-900' : 'border-slate-200 bg-white hover:border-indigo-300 hover:bg-slate-50')
                      }`}
                      title={`${sp.name} — ${sp.blurb}\nWill fill in: ${sp.url}`}
                    >
                      <div className="flex items-center justify-between gap-1">
                        <span className={`text-sm font-bold ${
                          connectorId === sp.id && openapiUrl === sp.url
                            ? (dark ? 'text-indigo-200' : 'text-indigo-800')
                            : (dark ? 'text-slate-200' : 'text-slate-800')
                        }`}>
                          {sp.name}
                        </span>
                        <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-semibold uppercase tracking-wider ${
                          dark ? 'bg-slate-800 text-slate-400' : 'bg-slate-100 text-slate-500'
                        }`}>
                          {sp.category}
                        </span>
                      </div>
                      <div className={`text-[11px] mt-1 leading-snug ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
                        {sp.blurb}
                      </div>
                    </button>
                  ))}
                </div>
                <div className={`text-[11px] mt-2 ${dark ? 'text-slate-500' : 'text-slate-400'}`}>
                  Or paste your own OpenAPI URL below — any modern vendor with a public spec works.
                </div>
              </div>
            )}

            <div className={`h-px ${dark ? 'bg-slate-800' : 'bg-slate-100'}`} />

            <div>
              <label className={labelCls}>Connector ID <span className="text-red-500">*</span></label>
              <input
                value={connectorId}
                onChange={e => setConnectorId(e.target.value.replace(/[^a-z0-9_]/g, ''))}
                placeholder="e.g. acme_api"
                className={inputCls}
              />
              <div className={helperCls}>
                Lowercase, underscores only. This becomes the connector definition filename.
              </div>
            </div>

            <div>
              <label className={labelCls}>Display name <span className={dark ? 'text-slate-500' : 'text-slate-400'}>(optional)</span></label>
              <input
                value={displayName}
                onChange={e => setDisplayName(e.target.value)}
                placeholder="Acme API"
                className={inputCls}
              />
            </div>
            </div>

            <div className={authorStep === 1 ? 'space-y-4' : 'hidden'}>
            {mode === 'openapi' ? (
              <div>
                <label className={labelCls}>OpenAPI spec URL <span className="text-red-500">*</span></label>
                <input
                  value={openapiUrl}
                  onChange={e => setOpenapiUrl(e.target.value)}
                  placeholder="https://api.example.com/openapi.json"
                  className={inputCls}
                />
                <div className={helperCls}>
                  Public URL to a JSON or YAML OpenAPI 3.x spec. We'll fetch it server-side.
                </div>
              </div>
            ) : (
              <>
                <div>
                  <label className={labelCls}>Sample response(s) <span className="text-red-500">*</span></label>
                  <textarea
                    value={samplesText}
                    onChange={e => setSamplesText(e.target.value)}
                    rows={10}
                    placeholder='[{"id": "1", "name": "Acme", "created_at": "2026-05-06T00:00:00Z"}]'
                    className={`${inputCls} font-mono`}
                  />
                  <div className={helperCls}>
                    Paste real JSON from a curl. A single object, an array, or a wrapped
                    response like {'{'} "data": [...] {'}'}.
                  </div>
                </div>
                <div>
                  <label className={labelCls}>Base URL <span className={dark ? 'text-slate-500' : 'text-slate-400'}>(optional)</span></label>
                  <input
                    value={baseUrl}
                    onChange={e => setBaseUrl(e.target.value)}
                    placeholder="https://api.example.com"
                    className={inputCls}
                  />
                  <div className={helperCls}>
                    Recorded in the connector definition only — used for documentation, not for fetching.
                  </div>
                </div>
                <div>
                  <label className={labelCls}>Stream name <span className={dark ? 'text-slate-500' : 'text-slate-400'}>(optional)</span></label>
                  <input
                    value={streamName}
                    onChange={e => setStreamName(e.target.value)}
                    placeholder="customers"
                    className={inputCls}
                  />
                  <div className={helperCls}>
                    Defaults to the wrapper key (e.g. <code>data</code>) or <code>items</code>.
                  </div>
                </div>
              </>
            )}
            </div>

            {authorStep === 2 && (
              <div className={`rounded-xl border p-4 space-y-3 ${dark ? 'border-slate-700 bg-slate-900' : 'border-slate-200 bg-slate-50'}`}>
                <div>
                  <div className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-slate-400' : 'text-slate-500'}`}>Ready to generate</div>
                  <div className={`mt-2 text-sm ${dark ? 'text-slate-200' : 'text-slate-700'}`}>
                    <span className="font-semibold">{connectorId || 'Unnamed connector'}</span> will be generated from{' '}
                    <span className="font-semibold">{mode === 'openapi' ? 'an OpenAPI spec' : 'sample JSON responses'}</span>.
                  </div>
                </div>
                {!basicsReady && (
                  <div className={`text-xs rounded-lg px-3 py-2 ${dark ? 'bg-amber-500/10 text-amber-200' : 'bg-amber-50 text-amber-700'}`}>
                    Add a Connector ID before generating.
                  </div>
                )}
                {!sourceReady && (
                  <div className={`text-xs rounded-lg px-3 py-2 ${dark ? 'bg-amber-500/10 text-amber-200' : 'bg-amber-50 text-amber-700'}`}>
                    Add {mode === 'openapi' ? 'an OpenAPI spec URL' : 'sample JSON'} before generating.
                  </div>
                )}
              </div>
            )}

            <button
              type="button"
              onClick={generate}
              disabled={!canGenerate}
              className={`${authorStep === 2 ? 'w-full' : 'hidden'} py-2.5 text-sm font-semibold rounded-lg transition-colors ${
                !canGenerate
                  ? (dark ? 'bg-slate-800 text-slate-500' : 'bg-slate-200 text-slate-400')
                  : 'bg-gradient-to-r from-indigo-500 to-purple-500 hover:from-indigo-600 hover:to-purple-600 text-white shadow-sm'
              }`}
            >
              {loading ? 'Generating…' : 'Generate connector definition'}
            </button>

            {error && (
              <div className={`px-3 py-2.5 rounded-lg text-sm ${dark ? 'bg-red-500/10 text-red-300' : 'bg-red-50 text-red-700'}`}>
                {error}
              </div>
            )}

            <div className="flex items-center justify-between gap-2 pt-2">
              <button
                type="button"
                onClick={() => setAuthorStep((Math.max(0, authorStep - 1) as 0 | 1 | 2))}
                disabled={authorStep === 0}
                className={`px-4 py-2 text-sm font-semibold rounded-lg border ${
                  authorStep === 0
                    ? (dark ? 'border-slate-800 text-slate-600' : 'border-slate-100 text-slate-300')
                    : (dark ? 'border-slate-700 text-slate-200 hover:bg-slate-800' : 'border-slate-200 text-slate-700 hover:bg-slate-50')
                }`}
              >
                Back
              </button>
              {authorStep < 2 ? (
                <button
                  type="button"
                  onClick={() => setAuthorStep((Math.min(2, authorStep + 1) as 0 | 1 | 2))}
                  disabled={authorStep === 0 ? !basicsReady : !sourceReady}
                  className={`px-4 py-2 text-sm font-semibold rounded-lg ${
                    (authorStep === 0 ? !basicsReady : !sourceReady)
                      ? (dark ? 'bg-slate-800 text-slate-500' : 'bg-slate-200 text-slate-400')
                      : 'bg-indigo-600 text-white hover:bg-indigo-700'
                  }`}
                >
                  Continue
                </button>
              ) : (
                <button
                  type="button"
                  onClick={generate}
                  disabled={!canGenerate}
                  className={`px-4 py-2 text-sm font-semibold rounded-lg ${
                    !canGenerate
                      ? (dark ? 'bg-slate-800 text-slate-500' : 'bg-slate-200 text-slate-400')
                      : 'bg-indigo-600 text-white hover:bg-indigo-700'
                  }`}
                >
                  {loading ? 'Generating...' : 'Generate'}
                </button>
              )}
            </div>
          </div>

          {/* Output pane */}
          <div className={`rounded-xl border shadow-sm p-5 space-y-4 ${cardCls}`}>
            <div className="flex items-center gap-2">
              <span className="text-xs font-bold uppercase tracking-wider">Output</span>
              {result && (
                <div className="ml-auto flex items-center gap-2">
                  <button
                    type="button"
                    onClick={copyManifest}
                    className={`px-3 py-1 text-xs font-semibold rounded ${
                      copyState === 'copied'
                        ? (dark ? 'bg-emerald-500/30 text-emerald-200' : 'bg-emerald-200 text-emerald-800')
                        : copyState === 'failed'
                          ? (dark ? 'bg-red-500/20 text-red-300' : 'bg-red-100 text-red-700')
                          : (dark ? 'bg-slate-700 text-slate-200 hover:bg-slate-600' : 'bg-slate-100 text-slate-700 hover:bg-slate-200')
                    }`}
                  >
                    {copyState === 'copied' ? 'Copied!' : copyState === 'failed' ? 'Copy failed' : 'Copy'}
                  </button>
                  <button
                    type="button"
                    onClick={downloadManifest}
                    className={`px-3 py-1 text-xs font-semibold rounded ${
                      dark ? 'bg-emerald-500/20 text-emerald-300 hover:bg-emerald-500/30' : 'bg-emerald-100 text-emerald-700 hover:bg-emerald-200'
                    }`}
                  >
                    Download .v2.json
                  </button>
                  <button
                    type="button"
                    onClick={saveAsBetaConnector}
                    disabled={saveState === 'saving'}
                    title="Save as a Beta connector and use it immediately (admin/lead)"
                    className={`px-3 py-1 text-xs font-semibold rounded disabled:opacity-50 ${
                      saveState === 'saved'
                        ? (dark ? 'bg-emerald-500/30 text-emerald-200' : 'bg-emerald-200 text-emerald-800')
                        : (dark ? 'bg-pipe-500/20 text-pipe-200 hover:bg-pipe-500/30' : 'bg-pipe-100 text-pipe-700 hover:bg-pipe-200')
                    }`}
                  >
                    {saveState === 'saving' ? 'Saving…' : saveState === 'saved' ? 'Saved ✓' : 'Save & use (Beta)'}
                  </button>
                </div>
              )}
            </div>

            {!result ? (
              <div className={`text-sm italic py-16 text-center ${dark ? 'text-slate-500' : 'text-slate-400'}`}>
                Generated connector definition will appear here.
              </div>
            ) : (
              <>
                {/* Validation badge */}
                <div className={`flex items-center gap-2 px-3 py-2.5 rounded-lg text-sm ${
                  result.validation.valid
                    ? (dark ? 'bg-emerald-500/10 text-emerald-300' : 'bg-emerald-50 text-emerald-700')
                    : (dark ? 'bg-amber-500/10 text-amber-300' : 'bg-amber-50 text-amber-700')
                }`}>
                  <span className="font-bold">
                    {result.validation.valid ? '✓ Valid' : '⚠ Has errors'}
                  </span>
                  <span className="opacity-70">
                    Depth {result.validation.effective_depth_score}/5 · {result.validation.streams_evaluated.length} stream{result.validation.streams_evaluated.length === 1 ? '' : 's'}
                  </span>
                </div>

                {result.validation.errors.length > 0 && (
                  <details className={`text-sm rounded-lg p-3 ${dark ? 'bg-red-500/10' : 'bg-red-50'}`}>
                    <summary className={`cursor-pointer font-semibold ${dark ? 'text-red-300' : 'text-red-700'}`}>
                      {result.validation.errors.length} error(s)
                    </summary>
                    <ul className="mt-2 space-y-1 ml-4 list-disc">
                      {result.validation.errors.map((e, i) => (
                        <li key={i} className={`font-mono text-xs ${dark ? 'text-red-300' : 'text-red-700'}`}>{e}</li>
                      ))}
                    </ul>
                  </details>
                )}

                {result.validation.warnings.length > 0 && (
                  <details className={`text-sm rounded-lg p-3 ${dark ? 'bg-amber-500/10' : 'bg-amber-50'}`}>
                    <summary className={`cursor-pointer font-semibold ${dark ? 'text-amber-300' : 'text-amber-700'}`}>
                      {result.validation.warnings.length} warning(s)
                    </summary>
                    <ul className="mt-2 space-y-1 ml-4 list-disc">
                      {result.validation.warnings.map((w, i) => (
                        <li key={i} className={`font-mono text-xs ${dark ? 'text-amber-300' : 'text-amber-700'}`}>{w}</li>
                      ))}
                    </ul>
                  </details>
                )}

                {/* Manifest JSON */}
                <pre className={`text-xs font-mono leading-relaxed p-4 rounded-lg overflow-auto max-h-[600px] ${
                  dark ? 'bg-slate-900 text-slate-200' : 'bg-slate-50 text-slate-700'
                }`}>
                  {JSON.stringify(result.manifest, null, 2)}
                </pre>

                {/* D2 — Next-steps affordance. Closes the V15 audit gap
                    on the Author Connector tab: after a successful
                    generation, the operator was left with a JSON blob
                    and no clear "now what". Three concrete steps with
                    the file path inline so it can be copied. */}
                <div className={`rounded-lg p-4 ${dark ? 'bg-indigo-500/10 border border-indigo-500/30' : 'bg-indigo-50 border border-indigo-200'}`}>
                  <div className={`text-sm font-bold mb-2 ${dark ? 'text-indigo-200' : 'text-indigo-800'}`}>
                    Use this connector
                  </div>
                  <ol className={`text-sm space-y-1.5 ml-4 list-decimal ${dark ? 'text-indigo-100' : 'text-indigo-900'}`}>
                    <li>
                      Click <strong>Save &amp; use (Beta)</strong> above — it persists and loads
                      instantly, no restart (requires an admin/lead role).
                    </li>
                    <li>
                      Open the <strong>SaaS Connector</strong> node (or Connections → New connection)
                      — your connector appears in the picker, tagged <strong>Beta</strong>.
                    </li>
                    <li>
                      Prefer files / version control? <strong>Download .v2.json</strong> instead and
                      commit it under <code className={`px-1 py-0.5 rounded text-xs font-mono ${dark ? 'bg-slate-900/60' : 'bg-white border border-indigo-200'}`}>connectors/manifests/</code>.
                    </li>
                  </ol>
                  {saveMsg && (
                    <div className={`text-xs mt-2 px-2 py-1.5 rounded ${
                      saveState === 'saved'
                        ? (dark ? 'bg-emerald-500/15 text-emerald-200' : 'bg-emerald-50 text-emerald-700')
                        : saveState === 'error'
                          ? (dark ? 'bg-red-500/15 text-red-300' : 'bg-red-50 text-red-700')
                          : (dark ? 'text-indigo-300/80' : 'text-indigo-700/80')
                    }`}>
                      {saveMsg}
                    </div>
                  )}
                  <div className={`text-xs mt-2 ${dark ? 'text-indigo-300/80' : 'text-indigo-700/80'}`}>
                    The generator output is a <strong>starter</strong>. Review auth + pagination and
                    test it; add fixtures + run the certify CLI to promote it from Beta toward Certified.
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
