import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowRight, Send, Upload, Copy, Braces, Loader2, ShieldCheck, Search, History, ChevronDown, Check } from 'lucide-react';
import './ApiExplorerPage.css';
import ApiRequestFields, { blankFields, requestFields, type RequestField } from '../ApiRequestFields';
import ApiJsonView from '../ApiJsonView';
import ApiAssertions from '../ApiAssertions';
import ApiConnectorDraft from '../ApiConnectorDraft';
import { runAssertions, isIncomplete, type Assertion, type AssertionOutcome } from '../../utils/apiAssertions';
import { api } from '../../api/client';
import { stageApiConnection } from '../../utils/apiExplorerDraft';
import { API_REFERENCES, galleryReference } from '../../utils/apiGallery';

type Result = { status: number; elapsed_ms: number; bytes: number; truncated: boolean; headers: Record<string, string>; data: any; text: string; sample_limit: number };
type Operation = { method: string; path: string; summary: string; security: any[] };
type Discovery = { base_url: string; operations: Operation[]; limit_reached?: boolean; source_url?: string };
/** One past send, kept in memory only — the Explorer persists nothing. */
type HistoryEntry = { id: string; method: string; url: string; status: number; elapsed_ms: number; at: string };

const methods = ['GET', 'HEAD', 'OPTIONS', 'POST', 'PUT', 'PATCH', 'DELETE'];
const authMethods = [['none', 'No authentication'], ['bearer', 'Bearer token'], ['basic', 'Basic authentication'], ['api_key', 'API key (header)'], ['api_key_query', 'API key (query)']];
const input = 'w-full min-w-0 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800';

/** Colour by side-effect, not by rainbow: reads safe / writes warm / delete red. */
function methodTone(method: string): string {
  if (method === 'DELETE') return 'is-delete';
  if (method === 'POST') return 'is-create';
  if (method === 'PUT' || method === 'PATCH') return 'is-update';
  return 'is-read';
}

function statusTone(status: number): string {
  if (status >= 500) return 'is-server-error';
  if (status >= 400) return 'is-client-error';
  if (status >= 300) return 'is-redirect';
  return 'is-success';
}

/** Short reason phrases for the codes an API explorer actually meets. */
const STATUS_TEXT: Record<number, string> = {
  200: 'OK', 201: 'Created', 202: 'Accepted', 204: 'No Content',
  301: 'Moved Permanently', 302: 'Found', 304: 'Not Modified',
  400: 'Bad Request', 401: 'Unauthorized', 403: 'Forbidden', 404: 'Not Found',
  405: 'Method Not Allowed', 409: 'Conflict', 422: 'Unprocessable Entity', 429: 'Too Many Requests',
  500: 'Internal Server Error', 502: 'Bad Gateway', 503: 'Service Unavailable', 504: 'Gateway Timeout',
};

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function cellType(value: any): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

/** Table cells read better unquoted; the type row under the header carries what the quotes used to. */
function cellText(value: any): string {
  if (value === undefined) return '';
  if (value === null) return 'null';
  if (typeof value === 'string') return value;
  if (typeof value === 'object') return Array.isArray(value) ? `[${value.length}]` : `{${Object.keys(value).length}}`;
  return String(value);
}

function Tabs({ label, value, items, onChange }: { label: string; value: string; items: { id: string; label: string; disabled?: boolean; badge?: string; tone?: string }[]; onChange: (value: string) => void }) {
  return <div role="tablist" aria-label={label} className="api-workbench-tabs">
    {items.map(item => <button key={item.id} role="tab" disabled={item.disabled} aria-selected={value === item.id} tabIndex={value === item.id ? 0 : -1}
      onClick={() => onChange(item.id)} onKeyDown={event => {
        const enabled = items.filter(i => !i.disabled);
        const current = enabled.findIndex(i => i.id === item.id);
        const index = event.key === 'ArrowRight' ? (current + 1) % enabled.length : event.key === 'ArrowLeft' ? (current - 1 + enabled.length) % enabled.length : event.key === 'Home' ? 0 : event.key === 'End' ? enabled.length - 1 : -1;
        if (index < 0) return;
        event.preventDefault(); onChange(enabled[index].id);
        const buttons = event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)');
        buttons?.[index]?.focus();
      }}>{item.label}{item.badge && <span className={`api-tab-badge ${item.tone || ''}`}>{item.badge}</span>}</button>)}
  </div>;
}

export function structure(value: any, path = '$', depth = 0): { path: string; type: string }[] {
  if (depth > 6) return [];
  const type = value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value;
  const children = Array.isArray(value) ? (value.length ? structure(value[0], `${path}[0]`, depth + 1) : [])
    : value && typeof value === 'object' ? Object.entries(value).slice(0, 40).flatMap(([k, v]) => structure(v, `${path}[${JSON.stringify(k)}]`, depth + 1)) : [];
  return [{ path, type }, ...children].slice(0, 200);
}

export default function ApiExplorerPage({ embedded = false }: { embedded?: boolean }) {
  const [url, setUrl] = useState('');
  const [method, setMethod] = useState('GET');
  const [auth, setAuth] = useState('none');
  const [token, setToken] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [keyName, setKeyName] = useState('X-API-Key');
  const [headers, setHeaders] = useState<RequestField[]>(blankFields);
  const [query, setQuery] = useState<RequestField[]>(blankFields);
  const [body, setBody] = useState('');
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState('json');
  const [spec, setSpec] = useState('');
  const [operations, setOperations] = useState<Operation[]>([]);
  const [base, setBase] = useState('');
  const [selected, setSelected] = useState('');
  const [specError, setSpecError] = useState('');
  const [discovering, setDiscovering] = useState(false);
  const [importStatus, setImportStatus] = useState('No specification loaded yet');
  const [endpointSearch, setEndpointSearch] = useState('');
  const [limitReached, setLimitReached] = useState(false);
  const [bodyFilter, setBodyFilter] = useState('');
  /** Raw spec URL for a gallery import — the generator re-fetches it server-side. */
  const [specSourceUrl, setSpecSourceUrl] = useState('');
  const [assertions, setAssertions] = useState<Assertion[]>([]);
  const [outcomes, setOutcomes] = useState<AssertionOutcome[]>([]);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [reference, setReference] = useState(() => galleryReference(window.location.hash));
  const referenceId = useRef(reference?.id);
  useEffect(() => {
    const update = () => {
      const next = galleryReference(window.location.hash);
      if (next?.id === referenceId.current) return;
      referenceId.current = next?.id;
      specRevision.current += 1;
      setReference(next);
      setOperations([]); setSelected(''); setSpec(''); setSpecError(''); setImportStatus('No specification loaded yet'); setSpecSourceUrl('');
    };
    window.addEventListener('hashchange', update);
    return () => window.removeEventListener('hashchange', update);
  }, []);
  const [confirm, setConfirm] = useState(false);
  const [copied, setCopied] = useState(false);
  const revision = useRef(0);
  const specRevision = useRef(0);
  const urlInput = useRef<HTMLInputElement>(null);
  const write = !['GET', 'HEAD', 'OPTIONS'].includes(method);
  useEffect(() => {
    revision.current += 1;
    setResult(null); setError(''); setConfirm(false); setCopied(false); setOutcomes([]);
  }, [url, method, auth, token, username, password, keyName, headers, query, body]);
  useEffect(() => () => { revision.current += 1; specRevision.current += 1; }, []);

  const send = async (confirmed = false) => {
    if (write && !confirmed) { setConfirm(true); return; }
    setConfirm(false); setBusy(true); setError(''); setResult(null); setOutcomes([]);
    const current = ++revision.current;
    try {
      const response = await api.post<Result>('/connectors/author/explorer/request', {
        url, method, auth_type: auth, token, username, password, key_name: keyName,
        headers: requestFields(headers), query: requestFields(query), body: write ? body : '', confirm_write: confirmed,
      });
      if (revision.current === current) {
        setResult(response);
        setOutcomes(runAssertions(assertions.filter(a => !isIncomplete(a)), response));
        setHistory(entries => [{
          id: crypto.randomUUID(), method, url, status: response.status,
          elapsed_ms: response.elapsed_ms, at: new Date().toLocaleTimeString(),
        }, ...entries].slice(0, 20));
      }
    } catch (e: any) { if (revision.current === current) setError(e?.message || 'Request failed'); }
    finally { setBusy(false); }
  };
  const discover = async () => {
    setDiscovering(true); setSpecError(''); setOperations([]); setSelected(''); setImportStatus('Discovering endpoints...');
    const current = ++specRevision.current;
    try {
      const data = await api.post<Discovery>('/connectors/author/explorer/discover', { text: spec });
      if (current === specRevision.current) acceptDiscovery(data);
    } catch (e: any) { if (current === specRevision.current) { setSpecError(e?.message || 'Import failed'); setImportStatus('Import failed'); } }
    finally { setDiscovering(false); }
  };
  const acceptDiscovery = (data: Discovery) => {
    setOperations(data.operations); setBase(data.base_url); setSelected(''); setEndpointSearch('');
    setSpecSourceUrl(data.source_url || '');
    setLimitReached(!!data.limit_reached);
    setImportStatus(data.operations.length ? `Imported: ${data.operations.length} endpoints` : 'Imported: no supported endpoints');
  };
  const loadReference = async () => {
    if (!reference || discovering) return;
    setDiscovering(true); setSpecError(''); setOperations([]); setSelected('');
    setImportStatus('Downloading and discovering endpoints...');
    const current = ++specRevision.current;
    try {
      const data = await api.post<Discovery>('/connectors/author/explorer/reference', { reference: reference.id });
      if (current === specRevision.current) { setSpec(''); acceptDiscovery(data); }
    } catch (e: any) {
      if (current === specRevision.current) {
        setImportStatus('Import failed');
        setSpecError(e?.message === 'Not Found' ? 'Specification loader unavailable. Restart the F-Pulse backend to enable this update.' : e?.message || 'Import failed. Retry the download.');
      }
    } finally { setDiscovering(false); }
  };
  const updateSpec = (text: string) => { specRevision.current += 1; setSpec(text); setOperations([]); setSelected(''); setSpecError(''); setImportStatus('No specification loaded yet'); setSpecSourceUrl(''); };
  const chooseOperation = (index: string) => {
    setSelected(index);
    const op = operations[Number(index)];
    if (!op || index === '') return;
    setMethod(op.method); setUrl(base.replace(/\/$/, '') + op.path);
    setAuth('none'); setToken(''); setUsername(''); setPassword(''); setHeaders(blankFields()); setQuery(blankFields()); setBody('');
  };
  const paths = result?.data != null ? structure(result.data) : [];
  const [recordPath, setRecordPath] = useState('$');
  let rows: any = result?.data;
  const selectedPath = paths.find(p => p.path === recordPath)?.path || '$';
  // Paths are generated locally; parse only bracket tokens, never evaluate code.
  for (const part of selectedPath.slice(1).matchAll(/\[("(?:[^"\\]|\\.)*"|\d+)\]/g)) rows = rows?.[JSON.parse(part[1])];
  rows = Array.isArray(rows) ? rows.slice(0, 100) : rows && typeof rows === 'object' ? [rows] : [];
  const columns = Array.from(new Set<string>(rows.flatMap((r: any) => r && typeof r === 'object' ? Object.keys(r) : ['value']))).slice(0, 30);
  const columnTypes = useMemo(() => {
    const types: Record<string, string> = {};
    for (const column of columns) {
      const sample = rows.find((r: any) => r && typeof r === 'object' ? r[column] !== undefined : r !== undefined);
      types[column] = cellType(sample && typeof sample === 'object' ? sample[column] : sample);
    }
    return types;
  }, [rows, columns]);
  const operation = selected === '' ? null : operations[Number(selected)];
  const matchingOperations = operations.map((op, index) => ({ op, index })).filter(({ op }) => `${op.method} ${op.path} ${op.summary}`.toLowerCase().includes(endpointSearch.toLowerCase()));
  const activeChecks = assertions.filter(a => !isIncomplete(a));
  const failedChecks = outcomes.filter(o => !o.passed).length;
  // Step two needs a generator input: an imported spec, or a response to infer from.
  const canDraft = !!(spec.trim() || specSourceUrl || (result && result.data != null));
  const handoff = () => {
    try { stageApiConnection(url, auth, keyName); window.location.hash = 'connections'; }
    catch (e: any) { setError(e.message); }
  };
  const replay = (entry: HistoryEntry) => { setMethod(entry.method); setUrl(entry.url); setHistoryOpen(false); };

  // The page mirrors the three steps the Gallery advertises, so arriving here
  // from a reference card does not dump you into an unexplained form.
  const hasUrl = !!url.trim();
  const currentStep = !hasUrl ? 1 : !result ? 2 : 3;
  // Each step is a real destination: the rail navigates rather than decorates.
  const steps = [
    { n: 1, label: 'Choose an endpoint', detail: 'A known API, a specification, or any URL',
      done: hasUrl, enabled: true, go: () => urlInput.current?.focus() },
    { n: 2, label: 'Test it', detail: 'Send one request and inspect the response',
      done: !!result, enabled: hasUrl,
      go: () => { if (result) setTab('json'); else urlInput.current?.focus(); } },
    { n: 3, label: 'Generate a connector', detail: 'Draft it, review the auth, save as Beta',
      done: false, enabled: canDraft, go: () => setTab('connector') },
  ];

  // Page chrome follows the shell convention documented in TrustPage: stacked
  // `rounded-lg border-slate-200 shadow-sm bg-white` cards, each with a
  // `px-5 py-3 border-b` header bar, spaced by `space-y-4`.
  return <div className={`w-full mx-auto text-slate-800 space-y-4 ${embedded ? '' : 'px-6 py-5 max-w-[1500px]'}`}>
    <div className="api-explorer-toolbar">
      {/* Step cards follow the boxed-wizard treatment the Author page used:
          indigo for the current step, emerald once done, slate when idle. */}
      <ol className="grid grid-cols-1 sm:grid-cols-3 gap-2 flex-1 min-w-0" aria-label="API Explorer steps">
        {steps.map((step, index) => {
          const active = step.n === currentStep;
          return <li key={step.n} aria-current={active ? 'step' : undefined} className="min-w-0">
            <button type="button" disabled={!step.enabled} onClick={step.go}
              className={`w-full text-left rounded-lg border px-3 py-2 transition-colors ${
                active ? 'border-indigo-400 bg-indigo-50 text-indigo-800'
                  : step.done ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                    : 'border-slate-200 bg-white text-slate-600'
              } ${step.enabled ? '' : 'opacity-50 cursor-not-allowed'}`}>
              <div className="flex items-center gap-2">
                <span className={`grid h-6 w-6 place-items-center rounded-full text-xs font-bold shrink-0 ${
                  active ? 'bg-indigo-500 text-white'
                    : step.done ? 'bg-emerald-500 text-white'
                      : 'bg-slate-100 text-slate-500'
                }`}>
                  {step.done ? <Check size={13} /> : index + 1}
                </span>
                <span className="text-sm font-bold truncate">{step.label}</span>
              </div>
              <div className="mt-1 text-[11px] text-slate-500">{step.detail}</div>
            </button>
          </li>;
        })}
      </ol>
      <div className="flex items-center gap-2">
        {history.length > 0 && <div className="api-history">
          <button className="api-toolbar-button" aria-expanded={historyOpen} onClick={() => setHistoryOpen(!historyOpen)}>
            <History size={14} />History ({history.length})<ChevronDown size={13} />
          </button>
          {historyOpen && <ul className="api-history-menu" aria-label="Request history">
            {history.map(entry => <li key={entry.id}>
              <button onClick={() => replay(entry)} title={entry.url}>
                <span className={`api-history-status ${statusTone(entry.status)}`}>{entry.status}</span>
                <span className={`api-history-method ${methodTone(entry.method)}`}>{entry.method}</span>
                <span className="api-history-url">{entry.url}</span>
                <span className="api-history-time">{entry.elapsed_ms} ms · {entry.at}</span>
              </button>
            </li>)}
          </ul>}
        </div>}
      </div>
    </div>

    {/* Two tall cards side by side — the INPUT / OUTPUT pairing the Author page
        used. Everything you configure is on the left; everything the API
        returned is on the right. */}
    <div className={`api-panes${hasUrl ? '' : ' is-idle'}`}>
      <section aria-label="API request" className="api-card api-pane">
        <div className="api-card-head">
          <h3>Input</h3>
          <span className="api-card-meta">{authMethods.find(a => a[0] === auth)?.[1]}</span>
        </div>
        <div className="api-request-bar">
        <select aria-label="HTTP method" value={method} onChange={e => setMethod(e.target.value)}
          className={`api-method ${methodTone(method)}`}>{methods.map(m => <option key={m}>{m}</option>)}</select>
        <input ref={urlInput} aria-label="Request URL" placeholder="https://api.example.com/v1/records" value={url} onChange={e => setUrl(e.target.value)} className={`${input} font-mono`} />
        <button disabled={busy || !url.trim()} onClick={() => send()} className="inline-flex items-center gap-2 px-4 py-2 bg-pipe-600 text-white rounded-lg text-sm font-semibold disabled:opacity-50">
          {busy ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}{busy ? 'Sending...' : 'Send request'}
        </button>
      </div>
      {/* Say why Send is unavailable instead of leaving a greyed button unexplained. */}
      {!hasUrl && <p className="api-request-hint">
        {operations.length > 0
          ? 'Pick an endpoint from the imported specification below, or type any URL here.'
          : reference
            ? `Load the ${reference.name} specification below to browse its endpoints — or type any URL here to test it directly.`
            : 'Enter the URL you want to test, or import an OpenAPI specification to browse a vendor’s endpoints.'}
      </p>}
      {confirm && <div role="alertdialog" aria-label="Confirm API request" className="border-b border-amber-300 p-4 bg-amber-50 flex flex-wrap justify-between items-center gap-3"><p className="text-sm">{method} may modify remote data. Send this request?</p><div className="flex gap-4"><button onClick={() => send(true)} className="text-sm font-semibold text-red-700">Confirm and send</button><button onClick={() => setConfirm(false)} className="text-sm">Cancel</button></div></div>}
      {error && <p role="alert" className="text-sm text-red-700 bg-red-50 border-b border-red-200 px-4 py-3 break-words">{error}</p>}
          {/* One scrolling form rather than four tabs: each section held a
              handful of fields, so tabbing hid most of the request behind
              clicks and left the card mostly empty. */}
          <div aria-label="Request configuration" className="api-request-panel">
            <section className="api-form-section">
              <h4 className="api-form-label">Authentication</h4>
              <label className="block text-sm">Method<select value={auth} onChange={e => { setAuth(e.target.value); setToken(''); setPassword(''); setUsername(''); }} className={`${input} mt-1`} aria-label="Authentication">{authMethods.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              {auth === 'basic' && <div className="grid grid-cols-2 gap-2"><label className="text-sm">Username<input autoComplete="off" value={username} onChange={e => setUsername(e.target.value)} className={input} /></label><label className="text-sm">Password<input autoComplete="new-password" type="password" value={password} onChange={e => setPassword(e.target.value)} className={input} /></label></div>}
              {auth !== 'none' && auth !== 'basic' && <div className="space-y-2">{auth.startsWith('api_key') && <label className="block text-sm">Key name<input value={keyName} onChange={e => setKeyName(e.target.value)} className={input} /></label>}<label className="block text-sm">{auth === 'bearer' ? 'Bearer token' : 'API key'}<input type="password" autoComplete="new-password" value={token} onChange={e => setToken(e.target.value)} className={input} /></label></div>}
              {auth === 'none' && <div className="api-auth-hint"><ShieldCheck size={15} />No authorization credentials are attached to this request.</div>}
              {url.startsWith('http://') && auth !== 'none' && <p role="status" className="text-xs text-amber-700 font-medium">HTTP is unencrypted. Use HTTPS to protect credentials in transit.</p>}
            </section>

            <section className="api-form-section">
              <h4 className="api-form-label">Query parameters<span>{query.filter(r => r.enabled && r.key).length}</span></h4>
              <ApiRequestFields label="parameter" rows={query} onChange={setQuery} />
            </section>

            <section className="api-form-section">
              <h4 className="api-form-label">Headers<span>{headers.filter(r => r.enabled && r.key).length}</span></h4>
              <ApiRequestFields label="header" rows={headers} onChange={setHeaders} />
            </section>

            <section className="api-form-section">
              <h4 className="api-form-label">Request body</h4>
              {write
                ? <textarea aria-label="Request body" rows={8} value={body} onChange={e => setBody(e.target.value)} className={`${input} font-mono`} placeholder='{ "name": "value" }' />
                : <p className="api-form-note">{method} requests do not send a body.</p>}
            </section>

            <p className="api-panel-note">Credentials live in this page only — they are sent with the request and never saved.</p>
          </div>
      </section>

      <section aria-label="API response" className="api-card api-pane">
          <div className="api-card-head">
            <h3>Output</h3>
            {result ? <div className="api-status-group">
              <span className={`api-status-badge ${statusTone(result.status)}`}>HTTP {result.status}{STATUS_TEXT[result.status] ? ` ${STATUS_TEXT[result.status]}` : ''}</span>
              <span className="api-status-meta">{result.elapsed_ms} ms</span>
              <span className="api-status-meta">{formatBytes(result.bytes)}</span>
            </div> : <span className="text-xs text-slate-400">{busy ? 'In progress' : 'Not sent'}</span>}
          </div>
          <Tabs label="Response views" value={tab} onChange={setTab} items={[
            { id: 'json', label: 'JSON / Text', disabled: !result },
            { id: 'table', label: 'Table', disabled: !result },
            { id: 'structure', label: 'Structure', disabled: !result },
            { id: 'headers', label: 'Headers', disabled: !result },
            { id: 'tests', label: 'Tests',
              badge: activeChecks.length ? (result ? `${outcomes.length - failedChecks}/${outcomes.length}` : String(activeChecks.length)) : undefined,
              tone: result && failedChecks ? 'is-fail' : result && outcomes.length ? 'is-pass' : '' },
            { id: 'connector', label: 'Connector', disabled: !canDraft },
          ]} />
          <div className="api-response-content">
            {tab === 'tests' ? <div className="p-4">
              <ApiAssertions rows={assertions} onChange={setAssertions} outcomes={outcomes} hasResponse={!!result} />
            </div> : tab === 'connector' ? <div className="p-4 overflow-auto max-h-[560px]">
              <ApiConnectorDraft specText={spec} sourceUrl={specSourceUrl} responseData={result?.data} requestUrl={url} />
            </div> : !result ? <div className="api-response-empty" role="status">
              <span className="api-empty-mark">
                {busy ? <Loader2 size={26} className="animate-spin" /> : <Braces size={26} strokeWidth={1.5} />}
              </span>
              <span className="api-empty-title">{busy ? 'Waiting for response…' : error ? 'Request unsuccessful' : 'No response yet'}</span>
              {!busy && !error && <>
                <span className="api-response-hint">Send a request and everything the API returned lands here.</span>
                {/* Name what arrives, so the empty pane previews its own value. */}
                <ul className="api-empty-preview" aria-hidden="true">
                  <li>Status &amp; timing</li><li>JSON tree</li><li>Typed table</li>
                  <li>Structure</li><li>Your checks</li><li>Connector draft</li>
                </ul>
              </>}
            </div> : <div className="api-response-inner">
              {result.truncated && <p role="status" className="api-response-warning">Response exceeds 256 KB. Preview truncated; JSON/table parsing unavailable.</p>}
              {tab === 'json' && result.data != null && <div className="api-response-toolbar">
                <Search size={14} className="text-slate-400" />
                <input aria-label="Filter response" placeholder="Filter keys and values..." value={bodyFilter} onChange={e => setBodyFilter(e.target.value)} className="api-response-filter" />
              </div>}
              {tab === 'table' && <label className="block text-xs api-record-path">Record path<select className={`${input} mt-1`} value={selectedPath} onChange={e => setRecordPath(e.target.value)} aria-label="Record path">{paths.filter(p => ['array', 'object'].includes(p.type)).map(p => <option key={p.path}>{p.path}</option>)}</select></label>}
              <div role="tabpanel" aria-label="Response data" className="api-response-data">
                {tab === 'json' && (result.data != null
                  ? <ApiJsonView value={result.data} filter={bodyFilter} />
                  : <pre className="api-response-pre">{result.text || '(empty response)'}</pre>)}
                {tab === 'headers' && <pre className="api-response-pre">{JSON.stringify(result.headers, null, 2)}</pre>}
                {tab === 'structure' && <ul className="api-structure-list">{paths.map(p => <li key={p.path}><code>{p.path}</code><span className={`api-type-chip is-${p.type}`}>{p.type}</span></li>)}{!paths.length && <li>No JSON structure available</li>}</ul>}
                {tab === 'table' && (rows.length ? <table className="api-response-table"><thead>
                  <tr>{['#', ...columns].map(c => <th key={c}>{c}</th>)}</tr>
                  <tr className="api-table-types">{['', ...columns].map(c => <th key={c}>{c ? columnTypes[c] : ''}</th>)}</tr>
                </thead><tbody>{rows.map((r: any, i: number) => <tr key={i}>
                  <td className="api-table-index">{i + 1}</td>
                  {columns.map(c => {
                    const value = r && typeof r === 'object' ? r[c] : r;
                    return <td key={c} className={`api-cell is-${cellType(value)}`} title={typeof value === 'object' && value !== null ? JSON.stringify(value) : undefined}>{cellText(value)}</td>;
                  })}
                </tr>)}</tbody></table> : <p className="p-3 text-sm text-slate-500">No records at this path</p>)}
              </div>
            </div>}
          </div>
          <div className="api-response-footer">
            <div className="flex flex-wrap gap-3 items-center justify-between">
              <button disabled={!result} title="Copy redacted response" onClick={async () => { if (!result) return; try { await navigator.clipboard.writeText(result.data != null ? JSON.stringify(result.data, null, 2) : result.text); setCopied(true); } catch { setError('Clipboard unavailable'); } }} className="inline-flex items-center gap-1 text-xs disabled:opacity-40"><Copy size={14} />{copied ? 'Copied' : 'Copy response'}</button>
              {result && <button onClick={handoff} className="inline-flex items-center gap-2 text-xs text-pipe-700 font-semibold border border-pipe-200 bg-white rounded-lg px-3 py-2">Create connection <ArrowRight size={14} /></button>}
            </div>
            {result && <p className="text-[11px] leading-relaxed text-slate-500">Sampled response · sensitive fields redacted · connection credentials are not transferred</p>}
          </div>
      </section>
    </div>

    {/* Starting points sit below the request bar: typing a URL is the common
        case, and these are examples. Never collapsible — hiding them once left
        the page instructing you to use a panel that was not on screen. */}
    <section aria-label="OpenAPI import"
      className={`api-card${!hasUrl ? ' is-leading' : ''}`}>
      <div className="api-card-head">
        <h3>Start from a sample</h3>
        <span className="api-card-meta">Examples to start from — or type any URL in the request bar above</span>
      </div>
      <div className="api-card-body">
        <div className="flex items-center justify-between gap-2 mb-2">
          <span className="text-xs font-bold uppercase tracking-wider text-slate-500">Common starting points</span>
          <span className="text-[11px] text-slate-400">Click to load a specification</span>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
          {API_REFERENCES.map(entry => {
            const picked = reference?.id === entry.id;
            return <button key={entry.id} type="button" aria-pressed={picked}
              title={`${entry.name} — ${entry.description}`}
              className={`text-left p-2.5 rounded-lg border transition-colors ${
                picked ? 'border-indigo-400 bg-indigo-50' : 'border-slate-200 bg-white hover:border-indigo-300 hover:bg-slate-50'
              }`}
              onClick={() => { window.location.hash = `author?reference=${entry.id}`; }}>
              <div className="flex items-center justify-between gap-1">
                <span className="flex items-center gap-2 min-w-0">
                  <span className="api-ref-mark" style={{ backgroundColor: entry.tint }} aria-hidden="true">
                    {entry.name.slice(0, 2)}
                  </span>
                  <span className={`text-sm font-bold truncate ${picked ? 'text-indigo-800' : 'text-slate-800'}`}>{entry.name}</span>
                </span>
                <span className="text-[10px] px-1.5 py-0.5 rounded-full font-semibold uppercase tracking-wider bg-slate-100 text-slate-500 shrink-0">
                  {entry.category}
                </span>
              </div>
              <p className="mt-1.5 text-[11px] leading-relaxed text-slate-500">{entry.description}</p>
            </button>;
          })}
        </div>
        <p className="api-refs-note">
          Public specifications only — these are not installed connectors. To use a connector
          F-Pulse already ships, go to <a href="#connections">Connections</a>; their certification
          tiers are listed on the <a href="#trust">Trust page</a>.
        </p>

      {reference && <div className="flex flex-wrap items-center gap-3 text-sm">
        <span className="font-semibold">{reference.name} API reference</span>
        <a href={reference.source} target="_blank" rel="noopener noreferrer" className="text-pipe-700 underline">Specification source</a>
        <button disabled={discovering} onClick={loadReference} className="inline-flex items-center gap-2 rounded-lg bg-pipe-600 text-white px-3 py-2 text-sm font-semibold disabled:opacity-50">
          {discovering ? <Loader2 size={14} className="animate-spin" /> : <Upload size={14} />}{discovering ? 'Loading specification...' : 'Load specification'}
        </button>
      </div>}
      <p role="status" className="text-xs text-slate-600">{importStatus}</p>
      <p className="text-xs text-slate-500">Paste/upload: 2 MB. Gallery download: 32 MB JSON / 2 MB YAML. Credentials are not required to download these public specifications.</p>
      <details className="space-y-3">
      <summary className="cursor-pointer text-sm font-semibold">Paste or upload a specification</summary>
      <label className="block text-sm">OpenAPI JSON / YAML<textarea aria-label="OpenAPI specification" value={spec} onChange={e => updateSpec(e.target.value)} rows={4} className={`${input} mt-1 font-mono`} /></label>
      <div className="flex flex-wrap gap-3 items-center">
        <input type="file" aria-label="Upload specification" accept=".json,.yaml,.yml" onChange={async e => {
          const file = e.target.files?.[0]; if (!file) return;
          if (file.size > 2 * 1024 * 1024) { setSpecError('Maximum specification size is 2 MB.'); return; }
          try { updateSpec(await file.text()); } catch { setSpecError('Could not read file'); }
        }} />
        <button disabled={!spec.trim() || discovering} onClick={discover} className="px-3 py-2 border border-slate-200 rounded-lg text-sm disabled:opacity-50">{discovering ? 'Importing...' : 'Discover endpoints'}</button>
      </div>
      </details>
      {specError && <p role="alert" className="text-sm text-red-700">{specError}</p>}
      {operations.length > 0 && <div className="space-y-2">
        <input aria-label="Search endpoints" placeholder="Search method, path or operation..." value={endpointSearch} onChange={e => setEndpointSearch(e.target.value)} className={input} />
        <label className="block text-sm">Endpoint<select aria-label="Discovered endpoint" value={selected} onChange={e => chooseOperation(e.target.value)} className={input}><option value="">Select an endpoint</option>{matchingOperations.map(({ op, index }) => <option key={index} value={index}>{op.method} {op.path} {op.summary}</option>)}</select></label>
        <p className="text-xs text-slate-500">{matchingOperations.length} matching endpoints{limitReached ? ' (discovery limited to 5,000)' : ''}</p>
      </div>}
      {operation && <p className="text-xs text-slate-500">Declared authentication: {operation.security.length ? operation.security.map(o => Object.keys(o).join(' + ') || 'None').join(' OR ') : 'None'}. Not validated.</p>}
      </div>
    </section>
  </div>;
}
