import { useEffect, useMemo, useRef, useState } from 'react';
import { Loader2, Save, Download, Copy, Check, AlertTriangle, ArrowRight } from 'lucide-react';
import { api } from '../api/client';
import OpenApiSecurityReview from './OpenApiSecurityReview';

/**
 * Step two of the API Explorer: turn the endpoint you just tested into a
 * connector draft.
 *
 * Two generator inputs, both already in hand by the time this panel is
 * reachable — the imported OpenAPI spec, or the live response just sampled.
 * They are not equivalent:
 *
 *   spec    → /from-openapi returns a v2 cert manifest AND a v1 runtime
 *             manifest. Only the runtime one is loadable by the SaaS
 *             Connector node, so only this path can be saved as a connector.
 *   samples → /from-samples returns the v2 manifest alone. There is no
 *             runtime definition to save, so this path is review/export only.
 *
 * The UI says which one is in play rather than offering a Save button that
 * would fail.
 */

type Validation = {
  connector_id: string; valid: boolean;
  declared_depth_score: number; computed_depth_score: number; effective_depth_score: number;
  errors: string[]; warnings: string[]; streams_evaluated: string[];
};
type AuthorResponse = {
  manifest: Record<string, any>;
  validation: Validation;
  mode: string;
  runtime_manifest?: Record<string, any>;
};

type Source = 'openapi' | 'samples';

const CATEGORIES = ['saas', 'payments', 'developer', 'communication', 'infrastructure', 'analytics', 'database', 'other'];

/** `https://api.stripe.com/v1/customers` → `stripe`; good enough as a prefill. */
export function suggestConnectorId(url: string): string {
  let host = '';
  try { host = new URL(url).hostname; } catch { return ''; }
  const parts = host.split('.').filter(p => p && p !== 'www' && p !== 'api');
  const name = parts.length > 1 ? parts[parts.length - 2] : parts[0] || '';
  return name.toLowerCase().replace(/[^a-z0-9_]/g, '_').slice(0, 64);
}

/** The generator takes 1–5 objects; an array response contributes its first rows. */
export function samplesFrom(data: unknown): Record<string, any>[] {
  if (Array.isArray(data)) return data.filter(item => item && typeof item === 'object' && !Array.isArray(item)).slice(0, 5);
  if (data && typeof data === 'object') return [data as Record<string, any>];
  return [];
}

export default function ApiConnectorDraft({ specText, sourceUrl, responseData, requestUrl }: {
  specText: string;
  sourceUrl: string;
  responseData: unknown;
  requestUrl: string;
}) {
  const hasSpec = !!(specText.trim() || sourceUrl);
  const samples = useMemo(() => samplesFrom(responseData), [responseData]);
  const hasSamples = samples.length > 0;

  const [source, setSource] = useState<Source>(hasSpec ? 'openapi' : 'samples');
  const [connectorId, setConnectorId] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [category, setCategory] = useState('saas');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<AuthorResponse | null>(null);
  const [error, setError] = useState('');
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [saveMsg, setSaveMsg] = useState('');
  const [copied, setCopied] = useState(false);
  const generation = useRef(0);

  // Keep the available source honest as the user imports a spec or re-sends.
  useEffect(() => {
    if (source === 'openapi' && !hasSpec) setSource('samples');
    if (source === 'samples' && !hasSamples && hasSpec) setSource('openapi');
  }, [hasSpec, hasSamples, source]);

  useEffect(() => { if (!connectorId) setConnectorId(suggestConnectorId(requestUrl)); }, [requestUrl]);

  // A new generation invalidates any prior draft and its save state.
  const reset = () => { generation.current += 1; setResult(null); setError(''); setSaveState('idle'); setSaveMsg(''); };

  const generate = async () => {
    const id = connectorId.trim();
    if (!id) { setError('Enter a connector id.'); return; }
    if (!/^[a-z0-9_]+$/.test(id)) { setError('Connector id may use lowercase letters, digits and underscores only.'); return; }
    reset();
    setBusy(true);
    const current = ++generation.current;
    try {
      const body: Record<string, any> = { connector_id: id, display_name: displayName.trim() || undefined, category };
      let endpoint: string;
      if (source === 'openapi') {
        endpoint = '/connectors/author/from-openapi';
        if (specText.trim()) body.openapi_text = specText;
        else body.openapi_url = sourceUrl;
      } else {
        endpoint = '/connectors/author/from-samples';
        body.samples = samples;
        body.base_url = requestUrl;
      }
      const response = await api.post<AuthorResponse>(endpoint, body);
      if (generation.current === current) setResult(response);
    } catch (e: any) {
      if (generation.current === current) setError(e?.message || 'Generation failed');
    } finally { setBusy(false); }
  };

  const save = async () => {
    if (!result?.runtime_manifest) return;
    setSaveState('saving'); setSaveMsg('');
    const current = generation.current;
    try {
      const saved = await api.post<{ name: string; streams: number }>(
        '/connectors/author/save', { manifest: result.runtime_manifest });
      if (generation.current !== current) return;
      setSaveState('saved');
      setSaveMsg(`Saved “${saved.name}” as a Beta connector with ${saved.streams} stream(s). It is usable now — no restart.`);
    } catch (e: any) {
      if (generation.current !== current) return;
      setSaveState('error');
      setSaveMsg(e?.message || 'Save failed. Saving a connector requires an admin account.');
    }
  };

  const definition = result?.runtime_manifest || result?.manifest;
  const download = () => {
    if (!definition) return;
    const blob = new Blob([JSON.stringify(definition, null, 2)], { type: 'application/json' });
    const href = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = href;
    link.download = `${definition.id || connectorId || 'connector'}${result?.runtime_manifest ? '' : '.v2'}.json`;
    link.click();
    URL.revokeObjectURL(href);
  };

  if (!hasSpec && !hasSamples) {
    return <p className="api-draft-empty">
      Send a request, or import an OpenAPI specification, and this step can draft a connector from it.
    </p>;
  }

  return <div className="api-draft">
    <p className="api-draft-lead">
      Turn the endpoint you just tested into a connector definition. Generation is deterministic and runs
      server-side — nothing is saved until you choose to save it.
    </p>

    <div className="api-draft-source" role="radiogroup" aria-label="Generate from">
      <button role="radio" aria-checked={source === 'openapi'} disabled={!hasSpec}
        onClick={() => { setSource('openapi'); reset(); }} className={source === 'openapi' ? 'is-active' : ''}>
        <strong>OpenAPI specification</strong>
        <span>{hasSpec ? 'Full endpoint list and declared auth. Can be saved as a connector.' : 'Import a specification to enable this.'}</span>
      </button>
      <button role="radio" aria-checked={source === 'samples'} disabled={!hasSamples}
        onClick={() => { setSource('samples'); reset(); }} className={source === 'samples' ? 'is-active' : ''}>
        <strong>This response{hasSamples ? ` (${samples.length} sample${samples.length === 1 ? '' : 's'})` : ''}</strong>
        <span>{hasSamples ? 'Field types inferred from real data. Review/export only — not savable.' : 'Send a request that returns JSON objects.'}</span>
      </button>
    </div>

    <div className="api-draft-fields">
      <label>Connector id
        <input aria-label="Connector id" value={connectorId} placeholder="stripe"
          onChange={e => { setConnectorId(e.target.value); reset(); }} className="api-field-input" />
      </label>
      <label>Display name
        <input aria-label="Connector display name" value={displayName} placeholder="Stripe"
          onChange={e => { setDisplayName(e.target.value); reset(); }} className="api-field-input" />
      </label>
      <label>Category
        <select aria-label="Connector category" value={category}
          onChange={e => { setCategory(e.target.value); reset(); }} className="api-field-input">
          {CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
      </label>
    </div>

    <button onClick={generate} disabled={busy} className="api-draft-generate">
      {busy ? <Loader2 size={15} className="animate-spin" /> : <ArrowRight size={15} />}
      {busy ? 'Generating...' : 'Generate connector draft'}
    </button>

    {error && <p role="alert" className="api-draft-error">{error}</p>}

    {result && <div className="api-draft-result">
      <div className="api-draft-result-head">
        <span className={`api-draft-verdict ${result.validation?.valid ? 'is-ok' : 'is-warn'}`}>
          {result.validation?.valid ? 'Validation passed' : 'Validation flagged issues'}
        </span>
        {result.runtime_manifest
          ? <span className="api-draft-meta">{result.runtime_manifest.streams?.length || 0} runtime stream(s)</span>
          : <span className="api-draft-meta">Definition only — no runtime manifest from this source</span>}
      </div>

      {(result.validation?.errors?.length > 0 || result.validation?.warnings?.length > 0) && <ul className="api-draft-findings">
        {result.validation.errors.map(item => <li key={item} className="is-error"><AlertTriangle size={13} />{item}</li>)}
        {result.validation.warnings.map(item => <li key={item} className="is-warning"><AlertTriangle size={13} />{item}</li>)}
      </ul>}

      {result.runtime_manifest && <OpenApiSecurityReview streams={result.runtime_manifest.streams || []} />}

      <details className="api-draft-json">
        <summary>Connector definition</summary>
        <pre>{JSON.stringify(definition, null, 2)}</pre>
      </details>

      <div className="api-draft-actions">
        <button onClick={download} className="api-toolbar-button"><Download size={14} />Download</button>
        <button className="api-toolbar-button" onClick={async () => {
          try { await navigator.clipboard.writeText(JSON.stringify(definition, null, 2)); setCopied(true); }
          catch { setError('Clipboard unavailable'); }
        }}>{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? 'Copied' : 'Copy'}</button>
        {result.runtime_manifest
          ? <button onClick={save} disabled={saveState === 'saving' || saveState === 'saved'} className="api-draft-save">
              {saveState === 'saving' ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
              {saveState === 'saved' ? 'Saved' : saveState === 'saving' ? 'Saving...' : 'Save as Beta connector'}
            </button>
          : <span className="api-draft-note">Saving needs the OpenAPI source — a sample-derived draft has no runtime definition.</span>}
      </div>

      {saveMsg && <p role={saveState === 'error' ? 'alert' : 'status'}
        className={saveState === 'error' ? 'api-draft-error' : 'api-draft-saved'}>{saveMsg}</p>}
    </div>}
  </div>;
}
