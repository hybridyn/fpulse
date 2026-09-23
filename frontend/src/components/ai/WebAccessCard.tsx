import { useEffect, useState } from 'react';
import { api } from '../../api/client';

/**
 * Admin control for the Copilot's web access (Settings → AI Provider).
 *
 * F-Pulse is local-first, so the Copilot can't browse by default. Flipping
 * this on registers two READ-tier tools live (no restart): web_fetch (fetch a
 * URL) and web_search (needs a search provider + key). Backed by
 * GET/PUT /api/ai/web-access; the raw key is never returned (has_key only).
 */

interface WebAccessState {
  enabled: boolean;
  setting_enabled?: boolean;
  provider: string;
  has_key: boolean;
  endpoint?: string;
  supported_providers: string[];
}

// Provider families. KEY providers take an API key the user brings; URL
// providers point at an endpoint the enterprise/Hybridyn controls (no per-user
// third-party signup).
const KEY_PROVIDERS = ['brave', 'tavily'];
const URL_PROVIDERS = ['searxng', 'hybridyn'];
const PROVIDER_LABELS: Record<string, string> = {
  brave: 'Brave (API key)',
  tavily: 'Tavily (API key)',
  searxng: 'SearXNG (self-hosted, keyless)',
  hybridyn: 'Hybridyn managed (Plus)',
};

export default function WebAccessCard({ dark = false }: { dark?: boolean }) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [state, setState] = useState<WebAccessState | null>(null);

  // Editable fields
  const [enabled, setEnabled] = useState(false);
  const [provider, setProvider] = useState('');
  const [apiKey, setApiKey] = useState('');      // blank = keep existing
  const [endpoint, setEndpoint] = useState('');  // URL for searxng / hybridyn
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const s = await api.get<WebAccessState>('/api/ai/web-access');
        if (!alive) return;
        setState(s);
        setEnabled(!!s.enabled);
        setProvider(s.provider || '');
        setEndpoint(s.endpoint || '');
      } catch (e: any) {
        if (/403|forbidden|admin/i.test(e?.message || '')) setForbidden(true);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const body: Record<string, unknown> = { enabled, provider, endpoint: endpoint.trim() };
      if (apiKey.trim()) body.api_key = apiKey.trim();   // only send when changed
      const s = await api.put<WebAccessState>('/api/ai/web-access', body);
      setState(s);
      setEnabled(!!s.enabled);
      setProvider(s.provider || '');
      setEndpoint(s.endpoint || '');
      setApiKey('');
      setMsg({ kind: 'ok', text: 'Saved. Takes effect on the Copilot\'s next message — no restart.' });
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || 'Save failed' });
    } finally {
      setSaving(false);
    }
  };

  if (forbidden) return null;   // non-admins don't see the control at all
  if (loading) return null;

  const cardCls = dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200';
  const labelCls = `block text-sm font-semibold ${dark ? 'text-slate-200' : 'text-slate-700'}`;
  const helpCls = `text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`;
  const inputCls = `w-full px-3 py-2 text-sm rounded-lg border ${
    dark ? 'bg-slate-800 border-slate-700 text-slate-100' : 'bg-white border-slate-200 text-slate-700'
  } focus:outline-none focus:ring-2 focus:ring-pipe-300`;

  return (
    <div className={`rounded-xl border shadow-sm p-5 mt-4 ${cardCls}`}>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className={`text-base font-bold ${dark ? 'text-white' : 'text-slate-800'}`}>
            Copilot web access
          </h3>
          <p className={`${helpCls} mt-1 max-w-xl`}>
            Off by default — F-Pulse is local-first. Turn it on to let the Copilot
            fetch public URLs and (with a search key) search the web to find API docs.
            Applies live on the next Copilot message.
          </p>
        </div>
        {/* Toggle */}
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          onClick={() => setEnabled((v) => !v)}
          className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
            enabled ? 'bg-amber-500' : (dark ? 'bg-slate-600' : 'bg-slate-300')
          }`}
        >
          <span className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${enabled ? 'translate-x-5' : 'translate-x-0.5'}`} />
        </button>
      </div>

      {enabled && (
        <div className="mt-4 space-y-4">
          <div className={`text-xs rounded-lg px-3 py-2 ${dark ? 'bg-emerald-500/10 text-emerald-200' : 'bg-emerald-50 text-emerald-700'}`}>
            <strong>web_fetch</strong> works with just the toggle — the Copilot can read a URL you give it.
            Web <strong>search</strong> (discovery) needs a provider + key below.
          </div>

          <div>
            <label className={labelCls}>Search provider</label>
            <select value={provider} onChange={(e) => setProvider(e.target.value)} className={`${inputCls} mt-1`}>
              <option value="">None (fetch-only)</option>
              {(state?.supported_providers || [...KEY_PROVIDERS, ...URL_PROVIDERS]).map((p) => (
                <option key={p} value={p}>{PROVIDER_LABELS[p] || p}</option>
              ))}
            </select>
            <div className={`${helpCls} mt-1`}>
              For enterprises: <strong>SearXNG</strong> runs in your own network — keyless, private, nothing
              leaves your perimeter. <strong>Hybridyn managed</strong> is a hosted gateway (no signup, Plus).
              Brave/Tavily need a personal key. Leave as None for URL-fetch only.
            </div>
          </div>

          {/* KEY providers → API key field */}
          {KEY_PROVIDERS.includes(provider) && (
            <div>
              <label className={labelCls}>API key</label>
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={state?.has_key ? '•••••••• (leave blank to keep current key)' : `Your ${provider} API key`}
                className={`${inputCls} mt-1 font-mono`}
                autoComplete="off"
              />
              <div className={`${helpCls} mt-1`}>
                Stored on this instance only. {state?.has_key ? 'A key is already saved.' : 'Required for web search.'}
              </div>
            </div>
          )}

          {/* URL providers → endpoint field (+ optional token for the gateway) */}
          {URL_PROVIDERS.includes(provider) && (
            <>
              <div>
                <label className={labelCls}>
                  {provider === 'searxng' ? 'SearXNG URL' : 'Gateway URL'}
                </label>
                <input
                  type="text"
                  value={endpoint}
                  onChange={(e) => setEndpoint(e.target.value)}
                  placeholder={provider === 'searxng' ? 'http://searxng.internal:8080' : 'https://search.hybridyn.com'}
                  className={`${inputCls} mt-1 font-mono`}
                  autoComplete="off"
                />
                <div className={`${helpCls} mt-1`}>
                  {provider === 'searxng'
                    ? 'Your self-hosted SearXNG instance (enable the JSON format in its settings.yml). Keyless — no third-party account.'
                    : 'The Hybridyn-hosted search gateway. Managed for you; no per-user signup.'}
                </div>
              </div>
              {provider === 'hybridyn' && (
                <div>
                  <label className={labelCls}>License / gateway token <span className={helpCls}>(optional)</span></label>
                  <input
                    type="password"
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder={state?.has_key ? '•••••••• (leave blank to keep current)' : 'Bearer token for the gateway'}
                    className={`${inputCls} mt-1 font-mono`}
                    autoComplete="off"
                  />
                </div>
              )}
            </>
          )}
        </div>
      )}

      <div className="mt-4 flex items-center gap-3">
        <button
          type="button"
          onClick={save}
          disabled={saving}
          className="px-4 py-2 text-sm font-semibold text-white bg-gradient-to-r from-amber-500 to-orange-500 hover:from-amber-600 hover:to-orange-600 rounded-lg shadow-sm disabled:opacity-60"
        >
          {saving ? 'Saving…' : 'Save web access'}
        </button>
        {msg && (
          <span className={`text-xs ${msg.kind === 'ok' ? (dark ? 'text-emerald-300' : 'text-emerald-600') : (dark ? 'text-red-300' : 'text-red-600')}`}>
            {msg.text}
          </span>
        )}
      </div>
    </div>
  );
}
