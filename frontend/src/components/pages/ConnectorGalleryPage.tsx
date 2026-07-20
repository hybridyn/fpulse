/**
 * ConnectorGalleryPage — community-built connectors directory.
 *
 * Pairs with ConnectorAuthorPage (the build path) as the browse path.
 * Together they make the OSS extensibility loop visible in-product:
 * "you can build one" + "here's what others have built" + "share yours."
 *
 * Today: this is a static curated listing pointing at GitHub
 * Discussions for the live community board. There's no backend
 * marketplace yet — when we ship one, this page swaps the static
 * STARTER_ENTRIES for a fetched list and the rest of the layout
 * stays the same.
 *
 * Honest about the current state: a "How this works today" callout
 * explains that the live community list lives on GitHub for now, and
 * "Share yours" routes to the contribution issue template. No fake
 * marketplace numbers, no placeholder "1,234 connectors built by the
 * community" claims.
 */

import { useDarkMode } from '../../hooks/useDarkMode';
import { usePageContext } from '../../hooks/usePageContext';

/**
 * Hand-curated starter entries — examples to show "this is what a
 * community-built connector looks like." These are placeholders / known
 * starting points; the live list lives on GitHub Discussions. Each
 * entry is shippable today (the URL points at a real OpenAPI spec a
 * user could click from the Author Connector "starting points"
 * gallery to build the same connector). When we wire a backend
 * marketplace this array gets replaced with a fetched response.
 */
interface GalleryEntry {
  id: string;
  name: string;
  category: string;
  blurb: string;
  /** What the user does with this entry. */
  cta:
    | { kind: 'build_from_url'; url: string; label?: string }
    | { kind: 'external'; url: string; label?: string };
  /** Optional credit line. */
  credit?: string;
}

const STARTER_ENTRIES: GalleryEntry[] = [
  {
    id: 'stripe',
    name: 'Stripe',
    category: 'Payments',
    blurb: 'Payments, subscriptions, billing — all stream endpoints from the public OpenAPI spec.',
    cta: { kind: 'build_from_url', url: 'https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json' },
    credit: 'Source: stripe/openapi (Apache 2.0)',
  },
  {
    id: 'github',
    name: 'GitHub',
    category: 'Developer',
    blurb: 'Repos, issues, pull requests, Actions, releases. The full REST surface.',
    cta: { kind: 'build_from_url', url: 'https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json' },
    credit: 'Source: github/rest-api-description (MIT)',
  },
  {
    id: 'slack',
    name: 'Slack',
    category: 'Communication',
    blurb: 'Channels, users, files, messages. Web API v2.',
    cta: { kind: 'build_from_url', url: 'https://raw.githubusercontent.com/slackapi/slack-api-specs/master/web-api/slack_web_openapi_v2.json' },
    credit: 'Source: slackapi/slack-api-specs (MIT)',
  },
  {
    id: 'twilio',
    name: 'Twilio',
    category: 'Telecom',
    blurb: 'SMS, voice, phone-number management. v2010 API.',
    cta: { kind: 'build_from_url', url: 'https://raw.githubusercontent.com/twilio/twilio-oai/main/spec/yaml/twilio_api_v2010.yaml' },
    credit: 'Source: twilio/twilio-oai (Apache 2.0)',
  },
  {
    id: 'plaid',
    name: 'Plaid',
    category: 'Financial data',
    blurb: 'Bank-account aggregation, transactions, identity verification.',
    cta: { kind: 'build_from_url', url: 'https://raw.githubusercontent.com/plaid/plaid-openapi/master/2020-09-14.yml' },
    credit: 'Source: plaid/plaid-openapi (MIT)',
  },
  {
    id: 'digitalocean',
    name: 'DigitalOcean',
    category: 'Infrastructure',
    blurb: 'Droplets, Kubernetes, databases, networking.',
    cta: { kind: 'build_from_url', url: 'https://api-engineering.nyc3.cdn.digitaloceanspaces.com/spec-ci/DigitalOcean-public.v2.yaml' },
    credit: 'Source: DigitalOcean public spec',
  },
];

/**
 * URL → Author Connector page with the OpenAPI URL pre-filled.
 * Mirrors the click-to-prefill behaviour from STARTING_POINTS in
 * ConnectorAuthorPage. The hash carries the URL so a page reload
 * preserves it; the Author page reads it on mount and populates
 * the input.
 *
 * 2026-06-02 bug-fix: was previously `#ai?tab=author&...` which lands
 * on the AI Provider tab because AIPage ignores the `?tab=` query
 * param (only `initialTab` prop drives the tab). The route table in
 * App.tsx already maps `#author` → AIPage with `initialTab="author"`,
 * so use that direct route. Query params survive through to
 * ConnectorAuthorPage's prefill reader.
 */
function buildAuthorHref(openapiUrl: string, suggestedId: string): string {
  return `#author?prefill_id=${encodeURIComponent(suggestedId)}&prefill_url=${encodeURIComponent(openapiUrl)}`;
}

export default function ConnectorGalleryPage({ embedded = false }: { embedded?: boolean }) {
  const dark = useDarkMode();
  usePageContext({ page: 'gallery', filters: {} });

  const cardCls = dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200';
  const sectionTitleCls = `text-xs font-bold uppercase tracking-wider ${dark ? 'text-slate-400' : 'text-slate-500'}`;
  const bodyTextCls = dark ? 'text-slate-300' : 'text-slate-600';

  return (
    <div className={`${embedded ? '' : 'flex-1 overflow-auto p-6'} ${dark ? 'text-slate-100' : 'text-slate-800'}`}>
      <div className="max-w-[1300px] mx-auto space-y-5">
        {!embedded && (
          <div>
            <h1 className="text-2xl font-bold">Community Gallery</h1>
            <p className={`text-sm leading-relaxed mt-1 ${bodyTextCls}`}>
              Public OpenAPI specs you can turn into a manifest <strong>draft</strong> in ~90 seconds.
              Drafts still need auth wiring + smoke-testing before they're production-ready.
            </p>
          </div>
        )}

        {/* "How this works today" — honest about the current state.
            No fake marketplace numbers; the live community list lives
            on GitHub for now, this page surfaces the entry points. */}
        <div className={`rounded-xl border p-5 ${dark ? 'bg-indigo-500/10 border-indigo-500/30' : 'bg-indigo-50 border-indigo-200'}`}>
          <div className={`text-sm font-bold mb-1.5 ${dark ? 'text-indigo-200' : 'text-indigo-900'}`}>
            How this works today
          </div>
          <p className={`text-sm leading-relaxed ${dark ? 'text-indigo-100' : 'text-indigo-900'}`}>
            The starter entries below are public OpenAPI specs. One click pre-fills the{' '}
            <strong>Author Connector</strong> form and generates a manifest <strong>draft</strong> in ~90 seconds —
            you still review the auth flow, run a smoke test, and certify the result before it's a verified connector.
            These are not first-party manifests; see <a href="https://github.com/hybridyn/fpulse/blob/main/docs/connectors.md" target="_blank" rel="noopener noreferrer" className={`underline font-semibold ${dark ? 'text-indigo-200 hover:text-white' : 'text-indigo-700 hover:text-indigo-900'}`}>docs/connectors.md</a> for the tier-rated shipped catalog.
            The live community-contributed list lives on{' '}
            <a
              href="https://github.com/hybridyn/fpulse/discussions/categories/connectors"
              target="_blank"
              rel="noopener noreferrer"
              className={`underline font-semibold ${dark ? 'text-indigo-200 hover:text-white' : 'text-indigo-700 hover:text-indigo-900'}`}
            >
              GitHub Discussions
            </a>{' '}
            until we ship a backend marketplace. Have one to share?{' '}
            <a
              href="https://github.com/hybridyn/fpulse/issues/new/choose"
              target="_blank"
              rel="noopener noreferrer"
              className={`underline font-semibold ${dark ? 'text-indigo-200 hover:text-white' : 'text-indigo-700 hover:text-indigo-900'}`}
            >
              Open a contribution PR
            </a>
            .
          </p>
        </div>

        {/* Starter / curated entries */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <span className={sectionTitleCls}>Curated starting points</span>
            <span className={`text-[11px] ${dark ? 'text-slate-500' : 'text-slate-400'}`}>
              {STARTER_ENTRIES.length} starter specs · click any to pre-fill the Author Connector form
            </span>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {STARTER_ENTRIES.map((entry) => (
              <a
                key={entry.id}
                href={entry.cta.kind === 'build_from_url' ? buildAuthorHref(entry.cta.url, entry.id) : entry.cta.url}
                target={entry.cta.kind === 'external' ? '_blank' : undefined}
                rel={entry.cta.kind === 'external' ? 'noopener noreferrer' : undefined}
                className={`block rounded-xl border p-4 transition-colors group ${cardCls} ${
                  dark ? 'hover:border-slate-500 hover:bg-slate-900' : 'hover:border-indigo-300 hover:bg-slate-50'
                }`}
              >
                <div className="flex items-center justify-between gap-2 mb-1.5">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className={`text-sm font-bold truncate ${dark ? 'text-slate-100' : 'text-slate-900'}`}>
                      {entry.name}
                    </span>
                    {/* "Starter draft" status pill — prevents users mistaking these
                        for shipped, tier-certified connectors. The shipped catalog
                        with Production/Verified/Beta tiers lives at docs/connectors.md
                        and is rendered in the Connections picker. */}
                    <span
                      title="Generates an OpenAPI-based manifest draft. Not a verified first-party connector."
                      className={`text-[10px] px-1.5 py-0.5 rounded-full font-semibold uppercase tracking-wider whitespace-nowrap ${
                        dark ? 'bg-amber-500/15 text-amber-300 border border-amber-500/30'
                             : 'bg-amber-50 text-amber-700 border border-amber-200'
                      }`}
                    >
                      Draft starter
                    </span>
                  </div>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-semibold uppercase tracking-wider whitespace-nowrap ${
                    dark ? 'bg-slate-800 text-slate-400' : 'bg-slate-100 text-slate-500'
                  }`}>
                    {entry.category}
                  </span>
                </div>
                <p className={`text-xs leading-snug ${bodyTextCls}`}>{entry.blurb}</p>
                {entry.credit && (
                  <div className={`text-[10px] mt-2 italic ${dark ? 'text-slate-500' : 'text-slate-400'}`}>
                    {entry.credit}
                  </div>
                )}
                <div className={`mt-3 text-xs font-semibold flex items-center gap-1 ${
                  dark ? 'text-indigo-300 group-hover:text-indigo-200' : 'text-indigo-600 group-hover:text-indigo-800'
                }`}>
                  Generate manifest draft
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="5" y1="12" x2="19" y2="12" /><polyline points="12 5 19 12 12 19" />
                  </svg>
                </div>
              </a>
            ))}
          </div>
        </div>

        {/* Contribution CTAs — three paths so the user always finds the
            right one. "Build your own" (Author Connector) is the primary,
            "Share yours" is the contribution path, "Request a connector"
            is the no-build-needed path. */}
        <div className={`rounded-xl border p-5 ${cardCls}`}>
          <div className={sectionTitleCls + ' mb-3'}>Add to the gallery</div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <a
              href="#author"
              className={`block rounded-lg border p-3.5 transition-colors group ${
                dark
                  ? 'border-slate-700 bg-slate-900/50 hover:border-indigo-400 hover:bg-indigo-500/10'
                  : 'border-slate-200 bg-slate-50 hover:border-indigo-400 hover:bg-indigo-50'
              }`}
            >
              <div className={`text-sm font-bold mb-1 ${dark ? 'text-slate-100' : 'text-slate-900'}`}>
                Build your own
              </div>
              <p className={`text-xs leading-snug ${bodyTextCls}`}>
                Paste an OpenAPI URL or sample responses → working manifest in 90s. Stays on your install, or share via PR.
              </p>
            </a>
            <a
              href="https://github.com/hybridyn/fpulse/issues/new?template=connector-contribution.md"
              target="_blank"
              rel="noopener noreferrer"
              className={`block rounded-lg border p-3.5 transition-colors group ${
                dark
                  ? 'border-slate-700 bg-slate-900/50 hover:border-indigo-400 hover:bg-indigo-500/10'
                  : 'border-slate-200 bg-slate-50 hover:border-indigo-400 hover:bg-indigo-50'
              }`}
            >
              <div className={`text-sm font-bold mb-1 ${dark ? 'text-slate-100' : 'text-slate-900'}`}>
                Share yours
              </div>
              <p className={`text-xs leading-snug ${bodyTextCls}`}>
                Already built one? Open a contribution PR. We'll review + ship it as a first-party manifest, credited to you.
              </p>
            </a>
            <a
              href="https://github.com/hybridyn/fpulse/issues/new?template=connector-request.md"
              target="_blank"
              rel="noopener noreferrer"
              className={`block rounded-lg border p-3.5 transition-colors group ${
                dark
                  ? 'border-slate-700 bg-slate-900/50 hover:border-indigo-400 hover:bg-indigo-500/10'
                  : 'border-slate-200 bg-slate-50 hover:border-indigo-400 hover:bg-indigo-50'
              }`}
            >
              <div className={`text-sm font-bold mb-1 ${dark ? 'text-slate-100' : 'text-slate-900'}`}>
                Request a connector
              </div>
              <p className={`text-xs leading-snug ${bodyTextCls}`}>
                Don't want to build it yourself? File a connector-request issue with the vendor docs URL — we'll prioritise.
              </p>
            </a>
          </div>
        </div>
      </div>
    </div>
  );
}
