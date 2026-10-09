import { useState } from 'react';
import { KeyRound, ShieldCheck, ShieldAlert, Unlock } from 'lucide-react';

export interface OpenApiSecurity {
  type: 'openapi';
  alternatives: Array<{
    supported: boolean;
    schemes: Array<{ name: string; type: string; scopes: string[]; reason?: string; fields: Record<string, string> }>;
  }>;
}

export function securityLabel(option: OpenApiSecurity['alternatives'][number]): string {
  return option.schemes.length
    ? option.schemes.map(s => `${s.name}${s.scopes.length ? ` (${s.scopes.join(', ')})` : ''}`).join(' + ')
    : 'No authentication';
}

export default function OpenApiSecurityReview({ streams }: {
  streams: Array<{ name: string; path: string; auth?: OpenApiSecurity }>;
}) {
  const [selectedName, setSelectedName] = useState('');
  const selected = streams.find(s => s.name === selectedName) || streams[0];
  const supportedCount = streams.filter(s => s.auth?.alternatives.some(a => a.supported)).length;
  return (
    <section aria-label="Operation authentication" className="space-y-4 text-sm min-w-0">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
        <span>{streams.length} endpoints</span>
        <span>{supportedCount} with supported authentication</span>
      </div>
      {!selected ? <p className="py-6 text-slate-500">No GET endpoints in this definition.</p> : <>
        <label className="block text-xs font-semibold" htmlFor="openapi-review-endpoint">Endpoint</label>
        <select id="openapi-review-endpoint" value={selected.name} onChange={e => setSelectedName(e.target.value)}
          className="w-full min-w-0 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 focus:outline-none focus:ring-2 focus:ring-pipe-300">
          {streams.map(s => <option key={s.name} value={s.name}>GET {s.path}</option>)}
        </select>
        <div className="flex items-start gap-2 border-b border-slate-200 pb-3">
          <KeyRound size={16} className="shrink-0 mt-0.5 text-slate-500" aria-hidden="true" />
          <div className="min-w-0"><h3 className="font-semibold">Authentication requirements</h3>
            <div className="mt-1 text-xs text-slate-500 break-all">{selected.name}</div>
          </div>
        </div>
        {!selected.auth && <p className="text-slate-500">Authentication metadata unavailable.</p>}
        <ul className="divide-y divide-slate-200">
          {selected.auth?.alternatives.map((option, index) => {
            const Icon = !option.supported ? ShieldAlert : option.schemes.length ? ShieldCheck : Unlock;
            return <li key={index} className="py-3 first:pt-0 space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-semibold">{index === 0 ? 'Option 1' : `OR option ${index + 1}`}</span>
                <span className={`inline-flex items-center gap-1 text-xs ${option.supported ? 'text-slate-500' : 'text-amber-700'}`}>
                  <Icon size={14} aria-hidden="true" />{option.supported ? 'Method supported' : 'Unsupported'}
                </span>
              </div>
              {!option.schemes.length && <div>No authentication</div>}
              {option.schemes.map((scheme, schemeIndex) => <div key={scheme.name} className="min-w-0">
                <div className="break-words">{schemeIndex > 0 && <span className="text-xs font-semibold text-slate-500 mr-2">AND</span>}{scheme.name}</div>
                {!!scheme.scopes.length && <div className="text-xs text-slate-500 mt-1 break-words">Scopes: {scheme.scopes.join(', ')}</div>}
                {scheme.reason && <div className="text-xs text-slate-500 mt-1 break-words">{scheme.reason}</div>}
              </div>)}
            </li>;
          })}
        </ul>
        <div className="flex flex-wrap justify-between gap-2 border-t border-slate-200 pt-3 text-xs text-slate-500">
          <span>{selected.auth?.alternatives.every(option => !option.schemes.length) ? 'Credentials not required' : 'Credentials not configured'}</span><span>API access not tested</span>
        </div>
      </>}
    </section>
  );
}
