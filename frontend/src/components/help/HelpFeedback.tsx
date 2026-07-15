/**
 * HelpFeedback — in-app contact point for the F-Pulse project (2026-06-18).
 *
 * Lets users reach the team without leaving the app: report an issue, request
 * a connector, and check for updates. Privacy-first for a local-first OSS tool:
 *   - Issue/connector links OPEN GitHub in the browser — the user reviews and
 *     submits; the app transmits nothing.
 *   - "Check for updates" is a user click that reads ONLY the public latest
 *     release; no usage data is sent, and it degrades cleanly when offline.
 * All URLs come from the backend (/api/app/info), so a GitHub-org move is a
 * one-line change in backend/fpulse/app_meta.py.
 */
import { useEffect, useState } from 'react';
import { api } from '../../api/client';

interface AppInfo {
  version: string;
  homepage: string;
  docs_url: string;
  repo_url: string;
  issues_url: string;
  new_issue_url: string;
  releases_url: string;
  discussions_url: string;
}

interface UpdateResult {
  checked: boolean;
  available?: boolean;
  current: string;
  latest?: string | null;
  url?: string;
  notes?: string;
  offline?: boolean;
  reason?: string;
  releases_url?: string;
}

export default function HelpFeedback({ dark = false }: { dark?: boolean }) {
  const [info, setInfo] = useState<AppInfo | null>(null);
  const [upd, setUpd] = useState<UpdateResult | null>(null);
  const [checking, setChecking] = useState(false);
  const [diagCopied, setDiagCopied] = useState(false);

  useEffect(() => {
    api.get<AppInfo>('/api/app/info').then(setInfo).catch(() => { /* hub still degrades */ });
  }, []);

  const open = (url?: string) => { if (url) window.open(url, '_blank', 'noopener,noreferrer'); };
  const reportIssue = () => open(info?.new_issue_url);
  const requestConnector = () =>
    open(info ? `${info.repo_url}/issues/new?template=connector-request.md` : undefined);

  const diagnostics = () =>
    `F-Pulse OSS ${info?.version ?? '?'}\nBrowser: ${navigator.userAgent}\nWhen: ${new Date().toISOString()}`;
  const copyDiagnostics = async () => {
    try {
      await navigator.clipboard.writeText(diagnostics());
      setDiagCopied(true);
      setTimeout(() => setDiagCopied(false), 1500);
    } catch { /* clipboard blocked — ignore */ }
  };

  const checkUpdates = async () => {
    setChecking(true);
    setUpd(null);
    try {
      setUpd(await api.get<UpdateResult>('/api/app/update-check'));
    } catch {
      setUpd({ checked: false, offline: true, current: info?.version ?? '?' });
    } finally {
      setChecking(false);
    }
  };

  const card = dark ? 'bg-[#111827] border-white/10' : 'bg-white border-slate-200';
  const btn = dark
    ? 'bg-slate-800 border-slate-700 text-slate-200 hover:bg-slate-700'
    : 'bg-white border-slate-200 text-slate-700 hover:bg-slate-50';
  const muted = dark ? 'text-slate-400' : 'text-slate-500';
  const link = `underline hover:no-underline ${dark ? 'text-slate-300' : 'text-slate-600'}`;

  return (
    <div className={`rounded-xl border shadow-sm p-5 mb-4 ${card}`}>
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h2 className={`text-base font-bold ${dark ? 'text-slate-100' : 'text-slate-800'}`}>Help &amp; Feedback</h2>
          <p className={`text-xs mt-0.5 ${muted}`}>
            Reach the F-Pulse team without leaving the app.{info ? ` You’re on version ${info.version}.` : ''}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <button type="button" onClick={reportIssue} disabled={!info}
            className={`px-3 py-1.5 text-xs font-semibold rounded-lg border disabled:opacity-50 ${btn}`}>
            Report an issue
          </button>
          <button type="button" onClick={requestConnector} disabled={!info}
            className={`px-3 py-1.5 text-xs font-semibold rounded-lg border disabled:opacity-50 ${btn}`}>
            Request a connector
          </button>
          <button type="button" onClick={checkUpdates} disabled={checking}
            className={`px-3 py-1.5 text-xs font-semibold rounded-lg border disabled:opacity-50 ${btn}`}>
            {checking ? 'Checking…' : 'Check for updates'}
          </button>
        </div>
      </div>

      {upd && (
        <div className={`mt-3 text-xs rounded-lg px-3 py-2 ${
          upd.available
            ? (dark ? 'bg-emerald-500/10 text-emerald-300' : 'bg-emerald-50 text-emerald-700')
            : upd.checked
              ? (dark ? 'bg-slate-800 text-slate-300' : 'bg-slate-50 text-slate-600')
              : (dark ? 'bg-amber-500/10 text-amber-300' : 'bg-amber-50 text-amber-700')
        }`}>
          {upd.available ? (
            <>Update available: <strong>v{upd.latest}</strong> (you have v{upd.current}).{' '}
              <a className="underline" href={upd.url} target="_blank" rel="noreferrer">Release notes →</a></>
          ) : upd.checked ? (
            <>You’re on the latest version (v{upd.current}).</>
          ) : (
            <>Couldn’t check for updates{upd.offline ? ' (offline)' : ''}.{' '}
              <a className="underline" href={upd.releases_url || info?.releases_url} target="_blank" rel="noreferrer">View releases →</a></>
          )}
        </div>
      )}

      <div className={`mt-3 flex items-center gap-3 flex-wrap text-xs ${muted}`}>
        <button type="button" onClick={copyDiagnostics} className={link}>
          {diagCopied ? 'Diagnostics copied ✓' : 'Copy diagnostics for an issue'}
        </button>
        {info?.docs_url && <a className={link} href={info.docs_url} target="_blank" rel="noreferrer">Docs</a>}
        {info?.repo_url && <a className={link} href={info.repo_url} target="_blank" rel="noreferrer">GitHub</a>}
        {info?.discussions_url && <a className={link} href={info.discussions_url} target="_blank" rel="noreferrer">Discussions</a>}
      </div>

      <p className={`mt-2 text-[11px] ${muted}`}>
        Issue links open GitHub in your browser — you review and submit. The update check reads only the
        public release list. F-Pulse sends no usage data.
      </p>
    </div>
  );
}
