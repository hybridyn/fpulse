import { useState, useEffect, useRef, useCallback } from 'react';
import { usePageContext } from '../../hooks/usePageContext';
import { navigateTo, navigateToSubRoute } from '../../router';
import { broadcastEditorPreferencesChanged } from '../../hooks/useEditorPreferences';
import { useDarkMode } from '../../hooks/useDarkMode';
import { uiAlert } from '../../ui/dialog';
import { api } from '../../api/client';
import TierChip from '../shared/TierChip';
import PageHeader from '../shared/PageHeader';
import AIProviderForm from '../ai/AIProviderForm';
import ProviderComparison from '../agent/ProviderComparison';
import AiPricingSection from '../settings/AiPricingSection';
import PublishPolicyCard from '../settings/PublishPolicyCard';

const APP_VERSION = '1.0.0';

function formatBytes(n: number): string {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(u.length - 1, Math.floor(Math.log10(n) / 3));
  return (n / Math.pow(1000, i)).toFixed(i === 0 ? 0 : 2) + ' ' + u[i];
}

// 'ai' added Apr 2026 — AI provider is a workspace-wide setting
// (not env-scoped), so its natural home is Settings, not Admin.
// Previously lived as a tab on both DEV and PROD Admin, which implied
// it was env-scoped — it isn't.
// 'ai' tab removed May 2 2026 — moved to Insights → AI Provider subtab
// (Insights was briefly called "AI-Hub" but renamed May 17 2026 PR 4).
// Settings is now strictly app-preference (general/security/notifications/about).
type SettingsTab = 'general' | 'security' | 'notifications' | 'about';

interface ToggleProps {
  enabled: boolean;
  onChange: (v: boolean) => void;
  label: string;
  description?: string;
  dark?: boolean;
}

function Toggle({ enabled, onChange, label, description, dark }: ToggleProps) {
  return (
    <div className="flex items-center justify-between py-3">
      <div>
        <div className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>{label}</div>
        {description && <div className="text-xs text-slate-400 mt-0.5">{description}</div>}
      </div>
      <button
        onClick={() => onChange(!enabled)}
        className={`relative w-10 h-5 rounded-full transition-colors ${enabled ? 'bg-pipe-500' : 'bg-slate-300'}`}
      >
        <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform ${enabled ? 'left-5' : 'left-0.5'}`} />
      </button>
    </div>
  );
}

function SectionHeader({ title, icon, dark }: { title: string; icon: React.ReactNode; dark?: boolean }) {
  return (
    <div className={`flex items-center gap-2 mb-4 pb-2 border-b ${dark ? 'border-white/[0.06]' : 'border-slate-200'}`}>
      <span className={dark ? 'text-slate-400' : 'text-slate-500'}>{icon}</span>
      <h3 className={`text-sm font-bold uppercase tracking-wider ${dark ? 'text-slate-300' : 'text-slate-700'}`}>{title}</h3>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Z26 (2026-05-23) — Backup settings panel.
//
// Wires the OSS Settings page to the existing /api/backup/* endpoints
// (settings GET/PUT, status, list, create, delete). OSS users get
// the local backup destination + schedule + retention + manual run;
// cloud destinations (S3 / Azure Blob / GCS / MinIO) ship as a
// Plus-gated preview card, matching the connectors-are-open / Plus-owns-
// the-operational-layer principle from MEMORY.md.
//
// Settings persist to <data_dir>/backup_settings.json (see
// backend/fpulse/storage/backup_scheduler.py). Status surface walks
// the local backups/ directory and surfaces the latest snapshot.
// ─────────────────────────────────────────────────────────────────────

interface BackupSettings {
  enabled: boolean;
  frequency: 'hourly' | 'daily' | 'weekly';
  daily_time: string;
  weekly_day: number;
  retention_count: number;
  provider: {
    provider: string;
    backup_dir?: string;
    [k: string]: any;
  };
}

interface BackupStatus {
  settings: BackupSettings;
  backups_dir: string;
  latest_backup: { name: string; size_bytes: number; created_at: string } | null;
  backup_count: number;
  next_backup_at: string | null;
}

interface BackupListEntry {
  key: string;
  size_bytes?: number;
  created_at?: string;
}

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function fmtBytesShort(n: number | undefined): string {
  if (!n || n < 0) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2)} ${u[i]}`;
}

function fmtIsoShort(iso: string | null | undefined): string {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  } catch { return iso; }
}

function BackupSettingsPanel({ dark, isPlus }: { dark: boolean; isPlus: boolean }) {
  const [settings, setSettings] = useState<BackupSettings | null>(null);
  const [status, setStatus] = useState<BackupStatus | null>(null);
  const [backups, setBackups] = useState<BackupListEntry[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [runningBackup, setRunningBackup] = useState(false);
  // Local form mirror so the user can edit fields without round-tripping
  // every keystroke. Synced from server on first load; flushed to server
  // when the user clicks Save.
  const [draft, setDraft] = useState<BackupSettings | null>(null);

  // ── Load on mount ─────────────────────────────────────────────────
  const refresh = useCallback(async () => {
    setLoadError(null);
    try {
      const [s, st, list] = await Promise.all([
        api.get<BackupSettings>('/api/backup/settings').catch(() => null),
        api.get<BackupStatus>('/api/backup/status').catch(() => null),
        api.get<BackupListEntry[]>('/api/backup/list?provider=local').catch(() => []),
      ]);
      if (s) {
        setSettings(s);
        setDraft(s);
      }
      if (st) setStatus(st);
      setBackups(Array.isArray(list) ? list : []);
    } catch (e: any) {
      setLoadError(e?.message || 'Failed to load backup settings');
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const handleSave = async () => {
    if (!draft) return;
    setSaving(true);
    try {
      const res = await api.put<{ status: string; settings: BackupSettings }>('/api/backup/settings', draft);
      if (res?.settings) {
        setSettings(res.settings);
        setDraft(res.settings);
      }
      await refresh();
    } catch (e: any) {
      uiAlert({ title: 'Save failed', message: e?.message || 'Could not save backup settings' });
    }
    setSaving(false);
  };

  const handleRunNow = async () => {
    setRunningBackup(true);
    try {
      await api.post<any>('/api/backup/create', draft?.provider || { provider: 'local' });
      await refresh();
    } catch (e: any) {
      uiAlert({ title: 'Backup failed', message: e?.message || 'Could not create backup' });
    }
    setRunningBackup(false);
  };

  const handleDeleteBackup = async (key: string) => {
    if (!window.confirm(`Delete backup "${key}"? This cannot be undone.`)) return;
    try {
      await api.delete(`/api/backup/${encodeURIComponent(key)}?provider=local`);
      await refresh();
    } catch (e: any) {
      uiAlert({ title: 'Delete failed', message: e?.message || 'Could not delete backup' });
    }
  };

  if (loadError) {
    return (
      <div className={`rounded-lg border p-4 ${dark ? 'bg-amber-500/10 border-amber-500/30 text-amber-200' : 'bg-amber-50 border-amber-200 text-amber-800'}`}>
        <div className="text-sm font-semibold">Backup settings unavailable</div>
        <div className="text-xs mt-0.5">{loadError}</div>
      </div>
    );
  }
  if (!settings || !draft) {
    return (
      <div className={`rounded-lg border p-4 text-sm ${dark ? 'bg-[#111827] border-white/[0.08] text-slate-400' : 'bg-white border-slate-200 text-slate-500'}`}>
        Loading backup settings…
      </div>
    );
  }

  // Plus-only cloud destinations preview. Each entry maps to the
  // backend provider id in /api/backup/test-provider; flipping
  // `isPlus` would allow these in the picker. Until then they render
  // as Plus chips so the upgrade path is visible without misleading
  // the user that they can pick the option today.
  const CLOUD_DESTINATIONS = [
    { id: 's3',         label: 'AWS S3',         hint: 'Bucket + region + IAM access keys' },
    { id: 'azure_blob', label: 'Azure Blob',     hint: 'Connection string or account key' },
    { id: 'gcs',        label: 'Google Cloud Storage', hint: 'Service-account JSON' },
    { id: 'minio',      label: 'MinIO',          hint: 'S3-compatible endpoint + access keys' },
  ];

  const dirty = JSON.stringify(draft) !== JSON.stringify(settings);

  return (
    <div className="space-y-4">
      {/* Status strip */}
      <div className={`rounded-lg border p-3 ${dark ? 'bg-[#0f1726] border-white/[0.06]' : 'bg-slate-50 border-slate-200'}`}>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
          <div>
            <div className={`uppercase tracking-wider text-[10px] font-bold ${dark ? 'text-slate-500' : 'text-slate-500'}`}>Last backup</div>
            <div className={`mt-1 font-semibold ${dark ? 'text-slate-200' : 'text-slate-800'}`}>{fmtIsoShort(status?.latest_backup?.created_at || null)}</div>
            {status?.latest_backup && (
              <div className={`mt-0.5 text-[11px] ${dark ? 'text-slate-500' : 'text-slate-500'}`}>{fmtBytesShort(status.latest_backup.size_bytes)} · {status.latest_backup.name}</div>
            )}
          </div>
          <div>
            <div className={`uppercase tracking-wider text-[10px] font-bold ${dark ? 'text-slate-500' : 'text-slate-500'}`}>Next scheduled</div>
            <div className={`mt-1 font-semibold ${dark ? 'text-slate-200' : 'text-slate-800'}`}>{status?.next_backup_at ? fmtIsoShort(status.next_backup_at) : 'Manual only'}</div>
          </div>
          <div>
            <div className={`uppercase tracking-wider text-[10px] font-bold ${dark ? 'text-slate-500' : 'text-slate-500'}`}>Retained</div>
            <div className={`mt-1 font-semibold ${dark ? 'text-slate-200' : 'text-slate-800'}`}>{status?.backup_count ?? 0} snapshots</div>
          </div>
          <div>
            <div className={`uppercase tracking-wider text-[10px] font-bold ${dark ? 'text-slate-500' : 'text-slate-500'}`}>Destination</div>
            <div className={`mt-1 font-semibold ${dark ? 'text-slate-200' : 'text-slate-800'}`}>Local</div>
            <div className={`mt-0.5 text-[11px] truncate ${dark ? 'text-slate-500' : 'text-slate-500'}`} title={status?.backups_dir || ''}>{status?.backups_dir || '—'}</div>
          </div>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={handleRunNow}
            disabled={runningBackup}
            className={`px-3 py-1.5 text-xs font-semibold rounded-lg border transition-colors ${runningBackup
              ? (dark ? 'bg-slate-800 border-slate-700 text-slate-500' : 'bg-slate-100 border-slate-200 text-slate-400')
              : (dark ? 'bg-emerald-600/20 border-emerald-500/40 text-emerald-200 hover:bg-emerald-600/30' : 'bg-emerald-50 border-emerald-200 text-emerald-800 hover:bg-emerald-100')
            }`}
          >
            {runningBackup ? 'Running…' : 'Backup now'}
          </button>
          <button
            type="button"
            onClick={refresh}
            className={`px-3 py-1.5 text-xs font-semibold rounded-lg border transition-colors ${dark ? 'bg-slate-800 border-slate-700 text-slate-300 hover:bg-slate-700' : 'bg-white border-slate-200 text-slate-700 hover:bg-slate-50'}`}
          >
            Refresh
          </button>
        </div>
      </div>

      {/* Schedule form */}
      <div className={`rounded-lg border p-4 space-y-4 ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
        <Toggle
          dark={dark}
          enabled={draft.enabled}
          onChange={(v) => setDraft({ ...draft, enabled: v })}
          label="Scheduled backups"
          description="Periodically snapshot the F-Pulse database. Manual backups via the button above always work regardless of this toggle."
        />

        <div className={`pt-3 border-t ${dark ? 'border-white/[0.06]' : 'border-slate-100'} grid grid-cols-1 md:grid-cols-2 gap-4`}>
          {/* Frequency */}
          <div>
            <label className={`text-xs font-semibold uppercase tracking-wider ${dark ? 'text-slate-400' : 'text-slate-600'}`}>Frequency</label>
            <select
              value={draft.frequency}
              onChange={(e) => setDraft({ ...draft, frequency: e.target.value as BackupSettings['frequency'] })}
              disabled={!draft.enabled}
              className={`mt-1 w-full px-3 py-2 text-sm rounded-lg border focus:outline-none focus:ring-2 ${dark ? 'bg-slate-900 border-white/[0.06] text-slate-200 focus:ring-violet-500/40' : 'bg-white border-slate-200 text-slate-700 focus:ring-violet-400'} disabled:opacity-50`}
            >
              <option value="hourly">Hourly</option>
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
            </select>
          </div>
          {/* Daily time (also used by weekly) */}
          {(draft.frequency === 'daily' || draft.frequency === 'weekly') && (
            <div>
              <label className={`text-xs font-semibold uppercase tracking-wider ${dark ? 'text-slate-400' : 'text-slate-600'}`}>Time (UTC)</label>
              <input
                type="time"
                value={draft.daily_time}
                onChange={(e) => setDraft({ ...draft, daily_time: e.target.value })}
                disabled={!draft.enabled}
                className={`mt-1 w-full px-3 py-2 text-sm rounded-lg border focus:outline-none focus:ring-2 ${dark ? 'bg-slate-900 border-white/[0.06] text-slate-200 focus:ring-violet-500/40' : 'bg-white border-slate-200 text-slate-700 focus:ring-violet-400'} disabled:opacity-50`}
              />
            </div>
          )}
          {/* Day of week (weekly only) */}
          {draft.frequency === 'weekly' && (
            <div>
              <label className={`text-xs font-semibold uppercase tracking-wider ${dark ? 'text-slate-400' : 'text-slate-600'}`}>Day of week</label>
              <div className="mt-1 flex flex-wrap gap-1">
                {WEEKDAYS.map((d, idx) => (
                  <button
                    key={d}
                    type="button"
                    onClick={() => setDraft({ ...draft, weekly_day: idx })}
                    disabled={!draft.enabled}
                    className={`px-2.5 py-1 text-xs font-semibold rounded-md border transition-colors ${draft.weekly_day === idx
                      ? (dark ? 'bg-violet-500/30 border-violet-400/60 text-white' : 'bg-violet-100 border-violet-300 text-violet-800')
                      : (dark ? 'bg-slate-800 border-slate-700 text-slate-400 hover:bg-slate-700' : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50')
                    } disabled:opacity-40 disabled:cursor-not-allowed`}
                  >
                    {d}
                  </button>
                ))}
              </div>
            </div>
          )}
          {/* Retention */}
          <div>
            <label className={`text-xs font-semibold uppercase tracking-wider ${dark ? 'text-slate-400' : 'text-slate-600'}`}>Retain last</label>
            <div className="mt-1 flex items-center gap-2">
              <input
                type="number"
                min={1}
                max={365}
                value={draft.retention_count}
                onChange={(e) => setDraft({ ...draft, retention_count: Math.max(1, Math.min(365, parseInt(e.target.value || '1', 10) || 1)) })}
                disabled={!draft.enabled}
                className={`w-24 px-3 py-2 text-sm rounded-lg border focus:outline-none focus:ring-2 ${dark ? 'bg-slate-900 border-white/[0.06] text-slate-200 focus:ring-violet-500/40' : 'bg-white border-slate-200 text-slate-700 focus:ring-violet-400'} disabled:opacity-50`}
              />
              <span className={`text-sm ${dark ? 'text-slate-400' : 'text-slate-600'}`}>snapshots</span>
            </div>
            <p className={`mt-1 text-[11px] ${dark ? 'text-slate-500' : 'text-slate-500'}`}>Older snapshots are pruned automatically.</p>
          </div>
        </div>

        <div className={`pt-3 border-t ${dark ? 'border-white/[0.06]' : 'border-slate-100'}`}>
          <label className={`text-xs font-semibold uppercase tracking-wider ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
            Local backup directory <span className="font-normal text-[10px]">(absolute path; blank = default)</span>
          </label>
          <input
            type="text"
            placeholder="(default) <FPULSE_DATA_DIR>/backups"
            value={draft.provider?.backup_dir || ''}
            onChange={(e) => setDraft({ ...draft, provider: { ...(draft.provider || { provider: 'local' }), provider: 'local', backup_dir: e.target.value } })}
            className={`mt-1 w-full px-3 py-2 text-sm font-mono rounded-lg border focus:outline-none focus:ring-2 ${dark ? 'bg-slate-900 border-white/[0.06] text-slate-200 focus:ring-violet-500/40' : 'bg-white border-slate-200 text-slate-700 focus:ring-violet-400'}`}
          />
        </div>

        <div className="flex items-center justify-end gap-2">
          {dirty && (
            <span className={`text-xs ${dark ? 'text-amber-300' : 'text-amber-700'}`}>Unsaved changes</span>
          )}
          <button
            type="button"
            disabled={!dirty || saving}
            onClick={() => setDraft(settings)}
            className={`px-3 py-1.5 text-xs font-semibold rounded-lg border transition-colors ${dark ? 'bg-slate-800 border-slate-700 text-slate-300 hover:bg-slate-700' : 'bg-white border-slate-200 text-slate-700 hover:bg-slate-50'} disabled:opacity-40 disabled:cursor-not-allowed`}
          >
            Revert
          </button>
          <button
            type="button"
            disabled={!dirty || saving}
            onClick={handleSave}
            className={`px-3 py-1.5 text-xs font-semibold rounded-lg border transition-colors ${saving
              ? (dark ? 'bg-slate-800 border-slate-700 text-slate-500' : 'bg-slate-100 border-slate-200 text-slate-400')
              : (dark ? 'bg-violet-600/30 border-violet-500/50 text-violet-100 hover:bg-violet-600/40' : 'bg-violet-50 border-violet-200 text-violet-800 hover:bg-violet-100')
            } disabled:opacity-40 disabled:cursor-not-allowed`}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>

      {/* Cloud destinations preview — Plus-gated */}
      <div className={`rounded-lg border p-4 ${dark ? 'bg-[#0f1726] border-white/[0.06]' : 'bg-white border-slate-200'}`}>
        <div className="flex items-center justify-between gap-2 mb-2">
          <div className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-slate-400' : 'text-slate-600'}`}>Backup destinations</div>
          {false && !isPlus && (
            <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-violet-100 text-violet-700">F-Pulse+</span>
          )}
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {/* Local (always active) */}
          <div className={`px-3 py-2 rounded-lg border ${dark ? 'bg-emerald-500/10 border-emerald-500/30' : 'bg-emerald-50 border-emerald-200'}`}>
            <div className="flex items-center justify-between gap-2">
              <span className={`text-sm font-semibold ${dark ? 'text-slate-100' : 'text-slate-900'}`}>Local filesystem</span>
              <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-emerald-500 text-white">Active</span>
            </div>
            <p className={`text-[11px] mt-1 leading-relaxed ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
              Snapshots written to the configured directory above. Default ships with OSS.
            </p>
          </div>
          {/* Cloud destinations — Plus only; hidden in single-operator OSS */}
          {isPlus && CLOUD_DESTINATIONS.map((c) => (
            <div
              key={c.id}
              className={`px-3 py-2 rounded-lg border ${isPlus
                ? (dark ? 'bg-[#0f1726] border-white/[0.06]' : 'bg-white border-slate-200')
                : (dark ? 'bg-[#0f1726] border-white/[0.06] opacity-80' : 'bg-white border-slate-200 opacity-90')
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className={`text-sm font-semibold ${dark ? 'text-slate-100' : 'text-slate-900'}`}>{c.label}</span>
                {isPlus ? (
                  <span className={`text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${dark ? 'bg-slate-700 text-slate-400' : 'bg-slate-200 text-slate-600'}`}>Off</span>
                ) : (
                  <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-violet-100 text-violet-700">F-Pulse+</span>
                )}
              </div>
              <p className={`text-[11px] mt-1 leading-relaxed ${dark ? 'text-slate-400' : 'text-slate-600'}`}>{c.hint}</p>
            </div>
          ))}
        </div>
        <p className={`text-[11px] mt-2 ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
          Snapshots are written to the <b>Local</b> destination.
        </p>
      </div>

      {/* Recent backups list */}
      {backups.length > 0 && (
        <div className={`rounded-lg border ${dark ? 'bg-[#0f1726] border-white/[0.06]' : 'bg-white border-slate-200'}`}>
          <div className={`px-3 py-2 border-b ${dark ? 'border-white/[0.06] text-slate-400' : 'border-slate-100 text-slate-600'} text-xs font-bold uppercase tracking-wider flex items-center justify-between`}>
            <span>Recent backups</span>
            <span className={dark ? 'text-slate-500' : 'text-slate-400'}>{backups.length} total</span>
          </div>
          <ul className={`divide-y ${dark ? 'divide-white/[0.04]' : 'divide-slate-100'}`}>
            {backups.slice(0, 8).map((b) => (
              <li key={b.key} className="flex items-center justify-between gap-3 px-3 py-2">
                <div className="min-w-0">
                  <div className={`text-xs font-mono truncate ${dark ? 'text-slate-200' : 'text-slate-800'}`}>{b.key}</div>
                  <div className={`text-[11px] ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
                    {b.created_at ? fmtIsoShort(b.created_at) : '—'}
                    {b.size_bytes != null && <> · {fmtBytesShort(b.size_bytes)}</>}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => handleDeleteBackup(b.key)}
                  className={`px-2 py-1 text-[11px] font-semibold rounded-md border transition-colors ${dark ? 'border-red-500/40 text-red-300 hover:bg-red-500/10' : 'border-red-200 text-red-700 hover:bg-red-50'}`}
                >
                  Delete
                </button>
              </li>
            ))}
          </ul>
          {backups.length > 8 && (
            <div className={`px-3 py-2 text-[11px] text-center border-t ${dark ? 'border-white/[0.06] text-slate-500' : 'border-slate-100 text-slate-500'}`}>
              … +{backups.length - 8} older snapshots
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Settings is mostly user-preference (theme, autosave, timezone) so it
 * reads the same in DEV and PROD, but per the canonical DEV/PROD header
 * standard the banner MUST still flip to the dark slate-900 / red accent
 * in PROD — otherwise the header alone is the only thing in the app
 * that doesn't repaint on env switch, which the user explicitly flagged
 * as inconsistent. We accept the prop and branch the banner chrome only;
 * the content below stays env-agnostic.
 */
export default function SettingsPage({ environment = 'dev', tier = 'free' }: { environment?: 'dev' | 'prod'; tier?: string } = {}) {
  const dark = useDarkMode();
  const isProd = environment === 'prod';
  const [tab, setTab] = useState<SettingsTab>('general');

  // OSS-4 (2026-05-19) — publish context so the Copilot can answer
  // "where do I change the SMTP host?" without traversing the tab strip
  // itself. Only the active tab id is published; concrete values are
  // not — Settings is a configuration surface and the values are not
  // safe to put in agent context.
  usePageContext({ page: 'settings', filters: { tab } });

  // Storage info — populated from /api/health/memory. The Dashboard's
  // "Storage" KPI deep-links here in DEV; on first paint we scroll the
  // Storage card into view so the user lands on relevant content rather
  // than the top of General.
  const [dbFiles, setDbFiles] = useState<Array<{ path: string; size_bytes: number }>>([]);
  const [storageError, setStorageError] = useState<string | null>(null);
  const [refreshingStorage, setRefreshingStorage] = useState(false);
  const storageRef = useRef<HTMLDivElement | null>(null);
  const smtpRef = useRef<HTMLDivElement | null>(null);

  // Approval policy — enforce two-person rule.
  // Hidden on Free (no PROD approval flow); shown to all on Plus, but
  // only Admin can flip the toggle (PUT is gated server-side too).
  const [enforceTwoPerson, setEnforceTwoPerson] = useState(false);
  const [approvalPolicySaving, setApprovalPolicySaving] = useState(false);
  const isPlus = tier === 'plus';

  const refreshStorage = async () => {
    setRefreshingStorage(true);
    setStorageError(null);
    try {
      const r = await (api as any).get('/api/health/memory');
      setDbFiles(Array.isArray(r?.db_files) ? r.db_files : []);
    } catch {
      setStorageError('Could not load storage info');
    } finally {
      setRefreshingStorage(false);
    }
  };

  useEffect(() => { refreshStorage(); }, []);

  // Load workspace settings once on mount; only Plus uses them.
  useEffect(() => {
    if (!isPlus) return;
    api.getWorkspaceSettings()
      .then((r) => setEnforceTwoPerson(!!r?.settings?.enforce_two_person_approval))
      .catch(() => { /* tolerant — keeps default false */ });
  }, [isPlus]);

  const saveApprovalPolicy = async (next: boolean) => {
    setApprovalPolicySaving(true);
    try {
      const r = await api.updateWorkspaceSettings({ enforce_two_person_approval: next });
      setEnforceTwoPerson(!!r?.settings?.enforce_two_person_approval);
      const { toast } = await import('../Toast');
      toast.success(
        next ? 'Two-person approval enforced' : 'Two-person approval disabled',
        next
          ? 'Gate 2 deploy approval must come from a different admin than Gate 1.'
          : 'Same admin may approve both gates.',
      );
    } catch (e: any) {
      // Revert optimistic toggle on failure.
      setEnforceTwoPerson(!next);
      const { toast } = await import('../Toast');
      toast.error('Setting save failed', e?.message || 'See logs.');
    } finally {
      setApprovalPolicySaving(false);
    }
  };

  // Hash subroute → tab switch. `#settings/notifications` lands on the
  // Notifications tab; `#settings/security` on Security; etc. Without
  // this, deep-links like the Quick Alert dialog's "Configure SMTP in
  // Settings →" land on the General tab and the user can't find SMTP.
  useEffect(() => {
    const onHash = () => {
      const raw = window.location.hash.replace('#', '');
      const seg = raw.split('/')[1];
      if (seg === 'notifications' || seg === 'security' || seg === 'about' || seg === 'general') {
        setTab(seg);
      }
    };
    onHash();
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  // Auto-scroll to Storage section when arriving via the Dashboard KPI
  // (Dashboard sets a sessionStorage breadcrumb before navigating).
  // Same pattern handles the Quick Alert dialog's "jump to SMTP" hint.
  useEffect(() => {
    try {
      const target = sessionStorage.getItem('fpulse_settings_jump_to');
      if (!target) return;
      if (tab === 'general' && target === 'storage') {
        sessionStorage.removeItem('fpulse_settings_jump_to');
        setTimeout(() => storageRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 80);
      } else if (tab === 'notifications' && target === 'smtp') {
        sessionStorage.removeItem('fpulse_settings_jump_to');
        setTimeout(() => smtpRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 80);
      }
    } catch {}
  }, [tab]);

  // Load persisted settings on mount (May 3 2026):
  //   - Notification config from /api/notifications/config (admin-only;
  //     non-admins get the default response without an error)
  //   - Other settings from localStorage (per-browser preferences that
  //     don't have a backend store yet)
  useEffect(() => {
    let cancelled = false;
    (async () => {
      // Local preferences
      try {
        const raw = localStorage.getItem('fpulse-settings');
        if (raw) {
          const s = JSON.parse(raw);
          if (s.general && !cancelled) {
            if (typeof s.general.autoSave === 'boolean') setAutoSave(s.general.autoSave);
            if (typeof s.general.autoFitView === 'boolean') setAutoFitView(s.general.autoFitView);
            if (typeof s.general.confirmDelete === 'boolean') setConfirmDelete(s.general.confirmDelete);
            if (typeof s.general.showMinimap === 'boolean') setShowMinimap(s.general.showMinimap);
            if (typeof s.general.snapToGrid === 'boolean') setSnapToGrid(s.general.snapToGrid);
            if (typeof s.general.showSchemaDeltas === 'boolean') setShowSchemaDeltas(s.general.showSchemaDeltas);
            if (typeof s.general.timezone === 'string') setTimezone(s.general.timezone);
          }
          if (s.security && !cancelled) {
            // encryptCredentials / dataEncryption / sanitizeInputs /
            // auditLogging stripped 2026-05-19 (P2 #5) — see comment on
            // the state declarations below.
            if (typeof s.security.sessionTimeout === 'string') setSessionTimeout(s.security.sessionTimeout);
            if (typeof s.security.ipWhitelist === 'string') setIpWhitelist(s.security.ipWhitelist);
            if (typeof s.security.twoFactor === 'boolean') setTwoFactor(s.security.twoFactor);
            if (typeof s.security.corsOrigins === 'string') setCorsOrigins(s.security.corsOrigins);
          }
          if (s.privacy && !cancelled) {
            if (typeof s.privacy.telemetryEnabled === 'boolean') setTelemetryEnabled(s.privacy.telemetryEnabled);
          }
          if (s.editor && !cancelled) {
            const m = s.editor.defaultRunSafetyMode;
            if (m === 'live' || m === 'sample' || m === 'dry_run' || m === 'validate_only') {
              setDefaultRunSafetyMode(m);
            }
          }
          if (s.ai && !cancelled && typeof s.ai.safetyMode === 'boolean') {
            setAiSafetyMode(s.ai.safetyMode);
          }
        }
      } catch {
        // Corrupt localStorage — ignore, keep defaults
      }

      // DuckDB tuning + pool runtime config (read-only display).
      try {
        const cfg = await api.getPoolConfig();
        if (!cancelled) setPoolRuntime(cfg);
      } catch {
        // /api/pool/config requires auth; fall back to null.
      }

      // Z4: Storage location + backend posture for the Operator Config
      // → Storage block. Returns active data_dir + free-disk numbers +
      // the backends list (local active, S3/Azure/GCS Plus-only).
      try {
        const loc = await api.get<any>('/api/storage/location');
        if (!cancelled) setStorageLocation(loc);
      } catch {
        // Unauthenticated or pre-Z4 backend — leave block in legacy mode.
      }

      // AI agent status — surfaces operator-set FPULSE_TOOL_ONLY_MODE.
      // Read-only badge on the AI section so the user knows the
      // chat will only accept fast-lane phrasings until an admin
      // unsets the env var and restarts.
      try {
        const status = await fetch('/api/ai/agent/status').then(r => r.ok ? r.json() : null);
        if (!cancelled && status && typeof status.tool_only_mode === 'boolean') {
          setAiToolOnlyMode(status.tool_only_mode);
        }
      } catch {
        // 404 (older backend) or network error — leave default false.
      }

      // Telemetry consent — backend-of-truth (admin_settings.telemetry_enabled).
      // localStorage value (saved earlier in this useEffect) is overridden by
      // the backend response so all admins see the same workspace-wide state.
      try {
        const consent = await api.getTelemetryConsent();
        if (!cancelled && typeof consent.enabled === 'boolean') {
          setTelemetryEnabled(consent.enabled);
        }
      } catch {
        // 403 (non-admin) or 503 (settings store unavailable) — silently
        // fall back to the localStorage value loaded above.
      }

      // Backend notification config (admin-only). Non-admins see 403;
      // we silently keep the OSS defaults so the UI still works.
      try {
        const cfg = await api.getNotificationConfig();
        if (cancelled || !cfg) return;
        if (typeof cfg.notify_on_success === 'boolean') setNotifyOnSuccess(cfg.notify_on_success);
        if (typeof cfg.notify_on_error === 'boolean') setNotifyOnError(cfg.notify_on_error);
        if (typeof cfg.notify_on_warning === 'boolean') setNotifyOnWarning(cfg.notify_on_warning);
        if (typeof cfg.notify_on_long_running === 'boolean') setNotifyOnLongRunning(cfg.notify_on_long_running);
        if (typeof cfg.long_running_threshold_min === 'number') setLongRunningThresholdMin(cfg.long_running_threshold_min);
        if (typeof cfg.notify_on_schedule_miss === 'boolean') setNotifyOnScheduleMiss(cfg.notify_on_schedule_miss);
        if (typeof cfg.email_enabled === 'boolean') setEmailNotifications(cfg.email_enabled);
        if (typeof cfg.browser_enabled === 'boolean') setBrowserNotifications(cfg.browser_enabled);
        // SMTP nested object — backend stores as notifications.smtp = { host, port, user, password, from_email, tls }
        const smtp = (cfg.smtp || {}) as Record<string, any>;
        if (typeof smtp.host === 'string') setSmtpHost(smtp.host);
        if (smtp.port != null) setSmtpPort(String(smtp.port));
        if (typeof smtp.user === 'string') setSmtpUser(smtp.user);
        if (typeof smtp.password === 'string') setSmtpPass(smtp.password);
        if (typeof smtp.from_email === 'string') setSmtpFrom(smtp.from_email);
        if (typeof smtp.tls === 'boolean') setSmtpTls(smtp.tls);
        if (typeof cfg.slack_webhook === 'string') setSlackWebhookUrl(cfg.slack_webhook);
        if (typeof cfg.discord_webhook === 'string') setDiscordWebhookUrl(cfg.discord_webhook);
        if (typeof cfg.teams_webhook === 'string') setTeamsWebhookUrl(cfg.teams_webhook);
        if (typeof cfg.webhook_url === 'string') setGenericWebhookUrl(cfg.webhook_url);
        if (typeof cfg.quiet_hours_enabled === 'boolean') setQuietHoursEnabled(cfg.quiet_hours_enabled);
        if (typeof cfg.quiet_hours_start === 'string') setQuietHoursStart(cfg.quiet_hours_start);
        if (typeof cfg.quiet_hours_end === 'string') setQuietHoursEnd(cfg.quiet_hours_end);
        if (typeof cfg.debounce_seconds === 'number') setDebounceSeconds(cfg.debounce_seconds);
        if (typeof cfg.daily_digest === 'boolean') setDailyDigest(cfg.daily_digest);
        if (typeof cfg.daily_digest_time === 'string') setDailyDigestTime(cfg.daily_digest_time);
      } catch {
        // 403 (non-admin) or 503 (settings store unavailable) — silent fallback
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // General settings
  const [autoSave, setAutoSave] = useState(true);
  const [autoFitView, setAutoFitView] = useState(true);
  const [confirmDelete, setConfirmDelete] = useState(true);
  const [showMinimap, setShowMinimap] = useState(true);
  const [snapToGrid, setSnapToGrid] = useState(false);
  // C3 — Opt-in schema-delta chip on each canvas node (default OFF;
  // adds visual density that power users love and casual users find
  // noisy). Consumed by FPulseNode via getEditorPreferences().
  const [showSchemaDeltas, setShowSchemaDeltas] = useState(false);
  const [timezone, setTimezone] = useState('UTC');

  // Security settings.
  // 2026-05-19 (P2 #5 of PAGE_BY_PAGE_AUDIT.md): `encryptCredentials`,
  // `dataEncryption`, `sanitizeInputs`, `auditLogging` were retained as
  // state after the UI was replaced with the read-only Security Posture
  // card (batches 11-12). They survive every Save round-trip but no UI
  // ever reads them — pure localStorage bloat. Removed; the persisted
  // values become orphans on the next save and are GC'd.
  const [sessionTimeout, setSessionTimeout] = useState('30');
  const [ipWhitelist, setIpWhitelist] = useState('');
  const [twoFactor, setTwoFactor] = useState(false);
  const [corsOrigins, setCorsOrigins] = useState('*');

  // Privacy / telemetry — opt-in, default OFF (May 3 2026).
  const [telemetryEnabled, setTelemetryEnabled] = useState(false);

  // Doc deep-link helper. Used by the "scaling guide", "SECURITY_DEPLOYMENT",
  // and "TRUST" links scattered through this page. Sets a sessionStorage
  // breadcrumb that DocsReference picks up + navigates to Help → Documentation
  // tab. Beats <a href="/docs/X.md"> which 404s (docs are served via
  // /api/reports/docs/content, not as static files).
  const openDoc = (path: string) => {
    try {
      sessionStorage.setItem('fpulse_help_initial_tab', 'reference');
      sessionStorage.setItem('fpulse_docs_jump_to', path);
    } catch {
      // sessionStorage disabled — navigation still works, just lands
      // on the default doc instead of the requested one.
    }
    navigateTo('help');
  };

  // Default run-safety mode (May 3 2026). Stored in localStorage; the
  // PreRunBanner reads it on mount as the initial value of the toggle.
  const [defaultRunSafetyMode, setDefaultRunSafetyMode] = useState<'live' | 'sample' | 'dry_run' | 'validate_only'>('sample');

  // AI safety mode (May 17 2026, Review #2). When on, the Copilot blocks
  // write tools and surfaces raw SQL/diff alongside every suggestion.
  // Sent on the AgentChatPanel request as ``X-FPulse-AI-Safety: 1``.
  const [aiSafetyMode, setAiSafetyMode] = useState(false);
  // Surfaced from /api/ai/agent/status — read-only flag for the
  // operator-set FPULSE_TOOL_ONLY_MODE env var. The UI shows a
  // banner when it's on so users know LLM lanes are blocked.
  const [aiToolOnlyMode, setAiToolOnlyMode] = useState(false);

  // DuckDB tuning — read-only display of the env-var-driven runtime
  // config. Populated on mount by /api/pool/config.
  const [poolRuntime, setPoolRuntime] = useState<{
    duckdb_memory_limit?: string;
    duckdb_threads?: number;
    duckdb_temp_dir?: string;
    max_workers?: number;
    cpu_cores?: number;
    ram?: { total_gb?: number };
    spill?: { disk_type?: string; io_wait_status?: string };
  } | null>(null);

  // Z4 (2026-05-23) — Storage location + backend posture. Powers the
  // Storage block in Operator Config: actual data_dir + free-disk
  // numbers + list of supported storage backends (local active in OSS,
  // S3 / Azure / GCS are Plus-only with their own descriptions).
  // Z27 extension: pending_data_dir / pending_restart surface a saved
  // override that the user hasn't restarted into yet.
  const [storageLocation, setStorageLocation] = useState<{
    data_dir?: string;
    env_var?: string;
    is_default?: boolean;
    active_backend?: string;
    pending_data_dir?: string | null;
    pending_restart?: boolean;
    override_set_at?: string | null;
    subtree?: Array<{ name: string; purpose: string }>;
    backends?: Array<{
      id: string;
      label: string;
      enabled: boolean;
      requires?: string | null;
      description: string;
    }>;
    disk?: { total_bytes: number; free_bytes: number; used_bytes: number } | null;
  } | null>(null);
  // Z27 inline-edit state for the data_dir field. Kept out of localSettings
  // because this is server state, not browser preferences.
  const [storageLocationEditing, setStorageLocationEditing] = useState(false);
  const [storageLocationDraft, setStorageLocationDraft] = useState('');
  const [storageLocationProbe, setStorageLocationProbe] = useState<{ ok?: boolean; path?: string; issues?: string[]; writable?: boolean; free_bytes?: number | null } | null>(null);
  const [storageLocationBusy, setStorageLocationBusy] = useState(false);
  const refreshStorageLocation = useCallback(async () => {
    try {
      const loc = await api.get<any>('/api/storage/location');
      setStorageLocation(loc);
    } catch { /* silent — banner handles the loading state */ }
  }, []);
  const validateStorageLocation = async () => {
    if (!storageLocationDraft.trim()) return;
    setStorageLocationBusy(true);
    setStorageLocationProbe(null);
    try {
      const probe = await api.post<any>('/api/storage/location/test', { data_dir: storageLocationDraft.trim() });
      setStorageLocationProbe(probe);
    } catch (e: any) {
      setStorageLocationProbe({ ok: false, issues: [e?.message || 'Validation request failed'] });
    }
    setStorageLocationBusy(false);
  };
  const saveStorageLocation = async () => {
    if (!storageLocationDraft.trim()) return;
    setStorageLocationBusy(true);
    try {
      await api.put<any>('/api/storage/location', { data_dir: storageLocationDraft.trim(), active_backend: 'local' });
      setStorageLocationEditing(false);
      setStorageLocationDraft('');
      setStorageLocationProbe(null);
      await refreshStorageLocation();
    } catch (e: any) {
      const detail = e?.message || 'Save failed';
      await uiAlert({ title: 'Save failed', message: detail });
    }
    setStorageLocationBusy(false);
  };
  const discardStorageLocation = async () => {
    if (!window.confirm('Discard the pending storage location change? F-Pulse will keep using the current location on next restart.')) return;
    setStorageLocationBusy(true);
    try {
      await api.delete<any>('/api/storage/location');
      await refreshStorageLocation();
    } catch (e: any) {
      await uiAlert({ title: 'Discard failed', message: e?.message || 'Could not clear the pending change' });
    }
    setStorageLocationBusy(false);
  };

  // Notification settings
  const [notifyOnSuccess, setNotifyOnSuccess] = useState(true);
  const [notifyOnError, setNotifyOnError] = useState(true);
  const [notifyOnWarning, setNotifyOnWarning] = useState(false);
  const [notifyOnLongRunning, setNotifyOnLongRunning] = useState(true);
  const [longRunningThresholdMin, setLongRunningThresholdMin] = useState(30);
  const [notifyOnScheduleMiss, setNotifyOnScheduleMiss] = useState(true);
  const [emailNotifications, setEmailNotifications] = useState(false);
  const [slackWebhookUrl, setSlackWebhookUrl] = useState('');
  const [discordWebhookUrl, setDiscordWebhookUrl] = useState('');
  const [teamsWebhookUrl, setTeamsWebhookUrl] = useState('');
  const [genericWebhookUrl, setGenericWebhookUrl] = useState('');
  const [browserNotifications, setBrowserNotifications] = useState(false);
  // SMTP — without these the alerts notifier dry-runs and emails
  // never reach the recipient. Persisted under
  // notifications.smtp.* in admin_settings; the alerts notifier
  // re-reads on every send so the form takes effect immediately.
  const [smtpHost, setSmtpHost] = useState('');
  const [smtpPort, setSmtpPort] = useState<string>('587');
  const [smtpUser, setSmtpUser] = useState('');
  const [smtpPass, setSmtpPass] = useState('');
  const [smtpFrom, setSmtpFrom] = useState('');
  const [smtpTls, setSmtpTls] = useState(true);
  const [quietHoursEnabled, setQuietHoursEnabled] = useState(false);
  const [quietHoursStart, setQuietHoursStart] = useState('22:00');
  const [quietHoursEnd, setQuietHoursEnd] = useState('07:00');
  const [debounceSeconds, setDebounceSeconds] = useState(60);
  const [dailyDigest, setDailyDigest] = useState(false);
  const [dailyDigestTime, setDailyDigestTime] = useState('08:00');

  const TABS: { id: SettingsTab; label: string; icon: React.ReactNode }[] = [
    {
      id: 'general', label: 'General',
      icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>,
    },
    {
      id: 'security', label: 'Security',
      icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></svg>,
    },
    {
      id: 'notifications', label: 'Notifications',
      icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" /></svg>,
    },
    // AI Provider tab removed May 2 2026 — lives in Insights → AI Provider.
    {
      id: 'about', label: 'About',
      icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><line x1="12" y1="16" x2="12" y2="12" /><line x1="12" y1="8" x2="12.01" y2="8" /></svg>,
    },
  ];

  const handleSave = async () => {
    // Local-only state (canvas preferences, security UI mirrors, telemetry consent
    // — these don't have a backend store and just persist per-browser).
    const localSettings = {
      general: { autoSave, autoFitView, confirmDelete, showMinimap, snapToGrid, showSchemaDeltas, timezone },
      security: { sessionTimeout, ipWhitelist, twoFactor, corsOrigins },
      privacy: { telemetryEnabled },
      editor: { defaultRunSafetyMode },
      ai: { safetyMode: aiSafetyMode },
    };
    localStorage.setItem('fpulse-settings', JSON.stringify(localSettings));

    // Notify other components (Canvas, PreRunBanner, etc.) that
    // localStorage editor preferences changed so they re-read without
    // needing a page reload.
    broadcastEditorPreferencesChanged();

    // Notification config goes to the backend (admin-only PUT) so the
    // worker_pool watchdog + scheduler actually see operator choices.
    // Falls back gracefully if the operator isn't admin or backend
    // store is unavailable — the local toggle still reflects in the UI.
    try {
      await api.putNotificationConfig({
        notify_on_success: notifyOnSuccess,
        notify_on_error: notifyOnError,
        notify_on_warning: notifyOnWarning,
        notify_on_long_running: notifyOnLongRunning,
        long_running_threshold_min: longRunningThresholdMin,
        notify_on_schedule_miss: notifyOnScheduleMiss,
        email_enabled: emailNotifications,
        browser_enabled: browserNotifications,
        webhook_url: genericWebhookUrl,
        slack_webhook: slackWebhookUrl,
        discord_webhook: discordWebhookUrl,
        teams_webhook: teamsWebhookUrl,
        quiet_hours_enabled: quietHoursEnabled,
        quiet_hours_start: quietHoursStart,
        quiet_hours_end: quietHoursEnd,
        debounce_seconds: debounceSeconds,
        daily_digest: dailyDigest,
        daily_digest_time: dailyDigestTime,
        // SMTP nested object — backend whitelists this whole-key.
        // Saving an empty host clears SMTP back to env-var-only mode.
        smtp: {
          host: smtpHost.trim(),
          port: parseInt(smtpPort, 10) || 587,
          user: smtpUser.trim(),
          password: smtpPass,
          from_email: smtpFrom.trim(),
          tls: smtpTls,
        },
      });
    } catch (err: any) {
      const msg = err?.message || String(err);
      // Non-admins can't write the workspace config — surface clearly
      // rather than swallowing.
      if (msg.includes('403') || msg.includes('forbid')) {
        await uiAlert('Local preferences saved. Notification config requires an admin to update.');
        return;
      }
      await uiAlert(`Local preferences saved. Notification sync failed: ${msg.slice(0, 200)}`);
      return;
    }

    // Telemetry consent → backend admin_settings (admin-only).
    // Best-effort: a sync failure shouldn't block the rest of save.
    try {
      await api.putTelemetryConsent(telemetryEnabled);
    } catch {
      // ignore — non-admins can't update workspace consent and that's
      // fine; the local localStorage save already happened above.
    }

    await uiAlert('Settings saved successfully');
  };

  // 2026-05-22 — H1 reflects the ACTIVE sub-tab (icon + label + subtitle),
  // matching the Insights page pattern. The TABS array already carries
  // per-sub-tab icons (cog / shield / bell / info-circle).
  const TAB_SUBTITLE: Record<SettingsTab, string> = {
    general: 'Editor preferences and per-user defaults.',
    security: 'Posture, authentication, and operator config.',
    notifications: 'Email, Slack, Discord, and webhook delivery.',
    about: 'Version, license, and runtime details.',
  };
  const activeTab = TABS.find((t) => t.id === tab) ?? TABS[0];

  return (
    <div className={`flex-1 flex flex-col overflow-hidden ${dark ? 'bg-[#0B1220]' : 'bg-canvas-bg'}`}>
      {/* Header + Tabs combined — canonical shared PageHeader shell */}
      <PageHeader
        environment={environment}
        icon={<span className={isProd ? 'text-red-400' : 'text-blue-500'}>{activeTab.icon}</span>}
        title={activeTab.label}
        titleAccessory={<TierChip tier={tier} environment={environment} />}
        subtitle={TAB_SUBTITLE[tab]}
        tabs={
          <div className="flex justify-center items-center gap-0.5">
          {TABS.map((t) => (
            <button
              key={t.id}
              // 2026-06-01: route through navigateToSubRoute so each tab
              // click is its own browser history entry. Before, all tab
              // clicks left URL at `#settings` and Back jumped straight
              // to Dashboard instead of stepping through visited tabs.
              // The hashchange listener (line ~563) syncs React state
              // when Back/Forward fires.
              onClick={() => { navigateToSubRoute('settings', t.id); setTab(t.id); }}
              className={`flex items-center gap-2 px-4 py-2.5 text-sm font-semibold rounded-lg transition-all capitalize ${
                tab === t.id
                  ? dark
                    ? 'border-violet-400 text-violet-200 font-bold bg-gradient-to-b from-violet-400/30 to-violet-600/20 shadow-[inset_0_0_0_1.5px_rgba(167,139,250,0.55),inset_0_0_10px_rgba(139,92,246,0.30),inset_0_1px_0_rgba(255,255,255,0.22)]'
                    : 'text-white font-bold bg-gradient-to-b from-slate-600 to-slate-800 shadow-[inset_0_0_0_1.5px_rgba(148,163,184,0.65),inset_0_0_10px_rgba(100,116,139,0.35),inset_0_1px_0_rgba(255,255,255,0.22)]'
                  : dark
                    ? 'border-transparent text-slate-500 hover:text-slate-300 hover:bg-white/[0.03]'
                    : 'border-transparent text-slate-900 font-bold hover:text-violet-700 hover:bg-violet-50/50'
              }`}
            >
              {t.icon}
              {t.label}
            </button>
          ))}
          </div>
        }
      />

      {/* Content */}
      <div className="flex-1 overflow-auto p-6">
        {/* Cap content at a comfortable reading width so cards don't stretch
            across 1900px monitors and feel sparse. Inner cards already span
            this width fully — outer container is just the gutter. */}
        <div className="w-full max-w-[1100px] mx-auto">
            {tab === 'general' && (
              <>
                {/* 2026-05-22 — icon swap: was the lightning-bolt
                    polygon (AI/sparks semantics). The section
                    represents Editor preferences, so the canonical
                    Editor pencil is the correct glyph. The lightning
                    bolt is reserved for AI / brand mark per the
                    icon-consistency memory rule. */}
                <SectionHeader dark={dark}
                  title="Editor Preferences"
                  icon={
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M12 20h9" />
                      <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
                    </svg>
                  }
                />
                <div className={`rounded-lg border shadow-sm px-4 divide-y ${dark ? 'bg-[#111827] border-white/[0.08] divide-white/[0.06]' : 'bg-white border-slate-200 divide-slate-100'}`}>
                  <Toggle dark={dark} enabled={autoSave} onChange={setAutoSave} label="Auto-save pipelines" description="Auto-save canvas changes to the backend ~2s after the last edit" />
                  <Toggle dark={dark} enabled={autoFitView} onChange={setAutoFitView} label="Auto-fit canvas view" description="Fit all nodes in view after adding a new node" />
                  <Toggle dark={dark} enabled={confirmDelete} onChange={setConfirmDelete} label="Confirm before delete" description="Show a confirmation dialog when deleting nodes from the canvas" />
                  <Toggle dark={dark} enabled={showMinimap} onChange={setShowMinimap} label="Show minimap" description="Display the React Flow minimap in the bottom-right of the canvas" />
                  <Toggle dark={dark} enabled={snapToGrid} onChange={setSnapToGrid} label="Snap to grid" description="Align nodes to a 20px grid when dragging" />
                  <Toggle dark={dark} enabled={showSchemaDeltas} onChange={setShowSchemaDeltas} label="Show schema deltas on nodes" description="Render a small +N/~N/−N chip on each node showing how it changed its input's schema (off by default — adds visual density)" />
                </div>

                {/* ── Default Run Safety Mode (May 3 2026) ────────────
                    Sets the initial value of the safety toggle on the
                    PreRunBanner for new pipelines. PreRunBanner reads
                    `localStorage['fpulse-settings'].editor.defaultRunSafetyMode`
                    on mount before falling back to its built-in default. */}
                <div className="mt-6">
                  <SectionHeader dark={dark}
                    title="Default Run Behavior"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="5 3 19 12 5 21 5 3" /></svg>}
                  />
                  <div className={`rounded-lg border shadow-sm px-4 py-3 ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
                    <label className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>Default safety mode for new pipeline runs</label>
                    <p className="text-xs text-slate-400 mt-0.5 mb-3">Pre-selects this mode in the Run toolbar. Override per-run.</p>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                      {([
                        { v: 'live' as const, label: 'Live', tip: 'Run on full upstream data and write to configured destinations.' },
                        { v: 'sample' as const, label: 'Sample', tip: 'Run on the first 100 rows only. No effect on destinations.' },
                        { v: 'dry_run' as const, label: 'Dry-run', tip: 'Plan only — validate the IR and produce previews without writing.' },
                        { v: 'validate_only' as const, label: 'Validate-only', tip: 'Schema + connection sanity check. No execution.' },
                      ]).map((m) => {
                        const active = defaultRunSafetyMode === m.v;
                        return (
                          <button
                            key={m.v}
                            type="button"
                            onClick={() => setDefaultRunSafetyMode(m.v)}
                            title={m.tip}
                            className={`px-3 py-2 text-sm font-semibold rounded-lg border transition-colors text-left ${
                              active
                                ? m.v === 'live'
                                  ? (dark ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40' : 'bg-emerald-50 text-emerald-700 border-emerald-300')
                                  : (dark ? 'bg-amber-500/20 text-amber-300 border-amber-500/40' : 'bg-amber-50 text-amber-700 border-amber-300')
                                : (dark ? 'bg-slate-800/50 text-slate-300 border-white/[0.08] hover:bg-slate-700/50' : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50')
                            }`}
                          >
                            {m.label}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </div>

                {/* ── AI Assistant ─────────────────────────────────────
                    Safety mode blocks every AI write tool. Sent as
                    X-FPulse-AI-Safety: 1 header on each /api/ai/agent
                    request. Tool-only mode banner surfaces the operator
                    env var so users know LLM lanes are blocked. */}
                <div className="mt-6">
                  <SectionHeader dark={dark}
                    title="AI Assistant"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></svg>}
                  />
                  <div className={`rounded-lg border shadow-sm px-4 divide-y ${dark ? 'bg-[#111827] border-white/[0.08] divide-white/[0.06]' : 'bg-white border-slate-200 divide-slate-100'}`}>
                    <Toggle
                      dark={dark}
                      enabled={aiSafetyMode}
                      onChange={setAiSafetyMode}
                      label="Safety mode"
                      description="Block AI write tools (create / modify pipelines, draft alerts). Read-only tools and chat still work."
                    />
                  </div>
                  {aiToolOnlyMode && (
                    <div className={`mt-2 px-3 py-2 rounded-lg text-xs border ${dark ? 'bg-amber-500/10 text-amber-300 border-amber-500/30' : 'bg-amber-50 text-amber-800 border-amber-200'}`}>
                      <strong>Tool-only mode is active.</strong> Copilot is running on deterministic tools only (no LLM) because <code>FPULSE_TOOL_ONLY_MODE=1</code> is set. Unset the env var and restart to enable open-ended questions.
                    </div>
                  )}
                </div>

                <div className="mt-6">
                  <SectionHeader dark={dark}
                    title="Regional"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10" /><line x1="2" y1="12" x2="22" y2="12" /></svg>}
                  />
                  <div className={`rounded-lg border shadow-sm px-4 py-3 ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
                    <label className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>Timezone</label>
                    <select
                      value={timezone}
                      onChange={(e) => setTimezone(e.target.value)}
                      className={`mt-1 block w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'bg-white border-slate-200 text-slate-700'}`}
                    >
                      <option value="UTC">UTC</option>
                      <option value="America/New_York">Eastern (ET)</option>
                      <option value="America/Chicago">Central (CT)</option>
                      <option value="America/Denver">Mountain (MT)</option>
                      <option value="America/Los_Angeles">Pacific (PT)</option>
                      <option value="Europe/London">London (GMT)</option>
                      <option value="Europe/Berlin">Berlin (CET)</option>
                      <option value="Asia/Kolkata">India (IST)</option>
                      <option value="Asia/Tokyo">Tokyo (JST)</option>
                      <option value="Australia/Sydney">Sydney (AEST)</option>
                    </select>
                  </div>
                </div>

                <div className="mt-6" ref={storageRef}>
                  <SectionHeader dark={dark}
                    title="Storage"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14a9 3 0 0 0 18 0V5" /><path d="M3 12a9 3 0 0 0 18 0" /></svg>}
                  />
                  <div className={`rounded-lg border shadow-sm px-4 py-3 ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
                    <div className="flex items-center justify-between mb-3">
                      <div>
                        <div className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>
                          Total: {formatBytes(dbFiles.reduce((a, f) => a + (f.size_bytes || 0), 0))}
                        </div>
                        <p className="text-xs text-slate-400 mt-0.5">SQLite database files used by F-Pulse</p>
                      </div>
                      <button
                        onClick={refreshStorage}
                        disabled={refreshingStorage}
                        className={`text-xs px-2.5 py-1 rounded-md border transition-colors disabled:opacity-50 ${dark ? 'border-white/[0.1] text-slate-300 hover:bg-white/[0.05]' : 'border-slate-200 text-slate-600 hover:bg-slate-50'}`}
                      >
                        {refreshingStorage ? 'Refreshing…' : 'Refresh'}
                      </button>
                    </div>
                    {storageError && (
                      <div className="text-xs text-red-500 mb-2">{storageError}</div>
                    )}
                    {dbFiles.length === 0 && !storageError && !refreshingStorage && (
                      <div className="text-xs text-slate-400">No files reported.</div>
                    )}
                    {dbFiles.length > 0 && (
                      <div className={`text-xs font-mono divide-y ${dark ? 'divide-white/[0.06] text-slate-300' : 'divide-slate-100 text-slate-600'}`}>
                        {dbFiles.map((f, i) => (
                          <div key={i} className="flex items-center justify-between py-1.5 gap-3">
                            <span className="truncate" title={f.path}>{f.path}</span>
                            <span className="shrink-0 tabular-nums">{formatBytes(f.size_bytes || 0)}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>

                {/* ── DuckDB tuning (read-only, May 3 2026) ──────────
                    Surfaces the env-var-driven runtime knobs that
                    docs/scaling.md walks through. Operators set these
                    via env at startup; the UI just reflects what's
                    active so users know what their installation is
                    doing without grepping docker logs. */}
                <div className="mt-6">
                  <SectionHeader dark={dark}
                    title="Execution Tuning"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>}
                  />
                  <div className={`rounded-lg border shadow-sm px-4 py-3 space-y-3 ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
                    <p className="text-xs text-slate-500 leading-relaxed">
                      DuckDB tuning is set via environment variables at startup. To change values, edit <code className={`text-xs font-mono px-1 rounded ${dark ? 'bg-slate-800 text-slate-300' : 'bg-slate-100 text-slate-700'}`}>.env</code> and restart the backend. See {' '}
                      <button
                        type="button"
                        onClick={() => openDoc('scaling.md')}
                        className={`${dark ? 'text-violet-300' : 'text-violet-600'} font-semibold hover:underline`}
                      >
                        scaling guide
                      </button>
                      {' '}for reference configurations.
                    </p>
                    {poolRuntime ? (
                      <>
                        <div className="grid grid-cols-2 gap-3">
                          <div>
                            <div className="flex items-center justify-between">
                              <label className={`text-xs font-medium ${dark ? 'text-slate-300' : 'text-slate-600'}`}>Memory limit per worker</label>
                              <code className={`text-[9px] font-mono px-1 rounded ${dark ? 'bg-slate-800 text-slate-400' : 'bg-slate-100 text-slate-600'}`}>FPULSE_DUCKDB_MEMORY_LIMIT</code>
                            </div>
                            <div className={`mt-1 px-3 py-2 text-sm font-mono rounded-lg border ${dark ? 'bg-[#0f1726] border-white/[0.06] text-slate-300' : 'bg-slate-50 border-slate-200 text-slate-700'}`}>
                              {poolRuntime.duckdb_memory_limit || '—'}
                            </div>
                          </div>
                          <div>
                            <div className="flex items-center justify-between">
                              <label className={`text-xs font-medium ${dark ? 'text-slate-300' : 'text-slate-600'}`}>DuckDB threads</label>
                              <code className={`text-[9px] font-mono px-1 rounded ${dark ? 'bg-slate-800 text-slate-400' : 'bg-slate-100 text-slate-600'}`}>FPULSE_DUCKDB_THREADS</code>
                            </div>
                            <div className={`mt-1 px-3 py-2 text-sm font-mono rounded-lg border ${dark ? 'bg-[#0f1726] border-white/[0.06] text-slate-300' : 'bg-slate-50 border-slate-200 text-slate-700'}`}>
                              {poolRuntime.duckdb_threads ?? '—'} <span className="text-slate-400 text-xs">/ {poolRuntime.cpu_cores ?? '?'} cores</span>
                            </div>
                          </div>
                          <div>
                            <div className="flex items-center justify-between">
                              <label className={`text-xs font-medium ${dark ? 'text-slate-300' : 'text-slate-600'}`}>Concurrent runs</label>
                              <code className={`text-[9px] font-mono px-1 rounded ${dark ? 'bg-slate-800 text-slate-400' : 'bg-slate-100 text-slate-600'}`}>FPULSE_MAX_CONCURRENT_RUNS</code>
                            </div>
                            <div className={`mt-1 px-3 py-2 text-sm font-mono rounded-lg border ${dark ? 'bg-[#0f1726] border-white/[0.06] text-slate-300' : 'bg-slate-50 border-slate-200 text-slate-700'}`}>
                              {poolRuntime.max_workers ?? '—'}
                            </div>
                          </div>
                          <div>
                            <div className="flex items-center justify-between">
                              <label className={`text-xs font-medium ${dark ? 'text-slate-300' : 'text-slate-600'}`}>Total RAM</label>
                              <span className="text-[9px] uppercase tracking-wider text-slate-400">detected</span>
                            </div>
                            <div className={`mt-1 px-3 py-2 text-sm font-mono rounded-lg border ${dark ? 'bg-[#0f1726] border-white/[0.06] text-slate-300' : 'bg-slate-50 border-slate-200 text-slate-700'}`}>
                              {poolRuntime.ram?.total_gb ? `${poolRuntime.ram.total_gb} GB` : '—'}
                            </div>
                          </div>
                        </div>
                        <div>
                          <div className="flex items-center justify-between">
                            <label className={`text-xs font-medium ${dark ? 'text-slate-300' : 'text-slate-600'}`}>DuckDB spill directory</label>
                            <code className={`text-[9px] font-mono px-1 rounded ${dark ? 'bg-slate-800 text-slate-400' : 'bg-slate-100 text-slate-600'}`}>FPULSE_DUCKDB_TEMP_DIR</code>
                          </div>
                          <div className={`mt-1 px-3 py-2 text-xs font-mono rounded-lg border break-all flex items-center justify-between gap-3 ${dark ? 'bg-[#0f1726] border-white/[0.06] text-slate-300' : 'bg-slate-50 border-slate-200 text-slate-700'}`}>
                            <span className="min-w-0 truncate">{poolRuntime.duckdb_temp_dir || '—'}</span>
                            {poolRuntime.spill?.disk_type && (
                              <span className={`shrink-0 text-xs font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${
                                poolRuntime.spill.disk_type === 'ssd'
                                  ? 'bg-emerald-100 text-emerald-700'
                                  : poolRuntime.spill.disk_type === 'hdd'
                                    ? 'bg-red-100 text-red-700'
                                    : 'bg-slate-200 text-slate-600'
                              }`}>
                                {poolRuntime.spill.disk_type === 'ssd' ? 'SSD ✓' : poolRuntime.spill.disk_type === 'hdd' ? 'HDD ⚠' : 'unknown'}
                              </span>
                            )}
                          </div>
                          {poolRuntime.spill?.disk_type === 'hdd' && (
                            <p className="text-xs text-red-600 mt-1">⚠ Move spill to SSD/NVMe — DuckDB performance drops 10-100× on HDD spill.</p>
                          )}
                        </div>
                      </>
                    ) : (
                      <p className="text-xs text-slate-400 italic">Loading pool config…</p>
                    )}
                  </div>
                </div>

                {/* ── AI Pricing — per-workspace rate table that feeds the
                    Insights → Activity Est. Cost tile. Saves broadcast
                    `fpulse-settings-changed` so the tile updates without
                    a reload. */}
                <div className="mt-6">
                  <SectionHeader dark={dark}
                    title="AI Pricing"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="1" x2="12" y2="23" /><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" /></svg>}
                  />
                  <AiPricingSection dark={dark} />
                </div>
              </>
            )}

            {tab === 'security' && (
              <>
                {/* ── Security Posture (read-only, May 3 2026) ──────────
                    Replaces the prior 4-toggle "Data Protection" group
                    that misled users — all four toggles were either
                    always-on baseline behavior (Fernet AES-128-CBC +
                    HMAC-SHA256 credential encryption, SQL input
                    sanitization) or Plus-only features (data-at-rest
                    encryption beyond the credentials store, audit
                    logging) that did nothing in OSS regardless of
                    toggle state. Shows the actual posture with honest
                    copy.
                    2026-06-03 — corrected "PBKDF2" → "Fernet" per the
                    pre-launch audit (docs/security/audit-2026-06-03.md
                    finding L1). The encryptor implementation has
                    always been Fernet; the comment + the user-visible
                    label below mislabelled it. */}
                <SectionHeader dark={dark}
                  title="Security Posture"
                  icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></svg>}
                />
                <div className={`rounded-lg border shadow-sm px-4 divide-y ${dark ? 'bg-[#111827] border-white/[0.08] divide-white/[0.06]' : 'bg-white border-slate-200 divide-slate-100'}`}>
                  {[
                    {
                      label: 'Stored credentials',
                      status: 'Encrypted at rest',
                      ok: true,
                      tier: 'baseline',
                      hint: 'Always on. Master key in ~/.fpulse/secret.key (POSIX 0600).',
                    },
                    {
                      label: 'Master key file permissions',
                      status: 'Verified at startup (fail-closed on POSIX)',
                      ok: true,
                      tier: 'baseline',
                      hint: 'F-Pulse refuses to start if the master key file is not 0600 unless FPULSE_ALLOW_INSECURE_KEY_PERMS=1 is explicitly set (dev only).',
                    },
                    {
                      label: 'SQL input sanitization',
                      status: 'Always on',
                      ok: true,
                      tier: 'baseline',
                      hint: 'All user-supplied inputs go through a sanitization pass before query execution. Cannot be disabled.',
                    },
                    {
                      label: 'HTTP rate limiting',
                      status: 'Per-IP sliding window',
                      ok: true,
                      tier: 'baseline',
                      hint: 'Default + auth + execute route classes. Tunable via FPULSE_RATE_LIMIT_* env vars; disable with FPULSE_RATE_LIMIT_DISABLE=1.',
                    },
                    {
                      label: 'Security headers',
                      status: 'X-Frame-Options · CSP · Referrer-Policy · HSTS-on-https',
                      ok: true,
                      tier: 'baseline',
                      hint: 'Applied to every HTTP response. CSP frame-ancestors configurable via FPULSE_FRAME_ANCESTORS.',
                    },
                    ...(isPlus ? [
                      {
                        label: 'Data at rest (intermediate)',
                        status: 'Vault-encrypted',
                        ok: true,
                        tier: 'plus',
                        hint: 'Intermediate pipeline data is encrypted on disk.',
                      },
                      {
                        label: 'Audit log',
                        status: 'Active with retention',
                        ok: true,
                        tier: 'plus',
                        hint: 'Every authenticated action, credential access, and admin action is recorded with configurable retention.',
                      },
                    ] : []),
                  ].map((item) => (
                    <div key={item.label} className="py-3 flex items-start gap-3">
                      <div className={`w-5 h-5 rounded-full flex items-center justify-center shrink-0 mt-0.5 ${
                        item.ok
                          ? (dark ? 'bg-emerald-500/20' : 'bg-emerald-100')
                          : (dark ? 'bg-violet-500/20' : 'bg-violet-100')
                      }`}>
                        {item.ok ? (
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={dark ? '#34d399' : '#16a34a'} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                        ) : (
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={dark ? '#a78bfa' : '#7c3aed'} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5z" /><path d="M2 17l10 5 10-5" /><path d="M2 12l10 5 10-5" /></svg>
                        )}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>{item.label}</span>
                          {false && item.tier === 'plus' && !isPlus && (
                            <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-violet-100 text-violet-700">F-Pulse+</span>
                          )}
                          <span className={`text-xs ${item.ok ? (dark ? 'text-emerald-400' : 'text-emerald-700') : (dark ? 'text-violet-400' : 'text-violet-700')}`}>
                            {item.status}
                          </span>
                        </div>
                        <p className="text-xs text-slate-400 mt-1 leading-relaxed">{item.hint}</p>
                      </div>
                    </div>
                  ))}
                </div>

                {/* Authentication — Plus only. 2FA + session-timeout
                    enforcement need the auth middleware that ships in
                    F-Pulse+. The UI was previously visible in OSS Free
                    with toggles that did nothing — Plus content leak. */}
                {isPlus && (
                  <div className="mt-6">
                    <SectionHeader dark={dark}
                      title="Authentication"
                      icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="11" width="18" height="11" rx="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></svg>}
                    />
                    <div className={`rounded-lg border shadow-sm px-4 divide-y ${dark ? 'bg-[#111827] border-white/[0.08] divide-white/[0.06]' : 'bg-white border-slate-200 divide-slate-100'}`}>
                      <Toggle dark={dark} enabled={twoFactor} onChange={setTwoFactor} label="Two-factor authentication" description="Require 2FA for all user logins (TOTP-based)" />
                      <div className="py-3">
                        <label className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>Session timeout (minutes)</label>
                        <p className="text-xs text-slate-400 mt-0.5">Auto-logout after inactivity</p>
                        <input
                          type="number"
                          value={sessionTimeout}
                          onChange={(e) => setSessionTimeout(e.target.value)}
                          min="5"
                          max="1440"
                          className={`mt-2 w-32 rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                        />
                      </div>
                    </div>
                  </div>
                )}

                {/* Approval Policy — gated to the commercial extension. */}
                {isPlus && (
                  <div className="mt-6">
                    <SectionHeader dark={dark}
                      title="Approval Policy"
                      icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="20 6 9 17 4 12" /></svg>}
                    />
                    <div className={`rounded-lg border shadow-sm px-4 ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
                      <Toggle
                        dark={dark}
                        enabled={enforceTwoPerson}
                        onChange={(v) => { setEnforceTwoPerson(v); saveApprovalPolicy(v); }}
                        label={`Require two-person approval${approvalPolicySaving ? ' (saving…)' : ''}`}
                        description="When on, deploy approval must come from a different reviewer than the prior approval. Use for compliance-heavy environments."
                      />
                    </div>
                  </div>
                )}

                {/* ── Operator config (read-only) ─────────────────────
                    These are env-var driven and read at startup. The
                    previous editable inputs saved to localStorage but
                    the backend never read them, so edits did nothing.
                    Now: read-only display with the env var name and
                    a deployment-guide link. */}
                <div className="mt-6">
                  <SectionHeader dark={dark}
                    title="Operator Config"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10" /><line x1="2" y1="12" x2="22" y2="12" /></svg>}
                  />
                  <div className={`rounded-lg border shadow-sm px-4 py-3 space-y-3 ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
                    <p className="text-xs text-slate-500 leading-relaxed">
                      These values are set via environment variables at startup. Editing them in the UI doesn't apply — change the env var and restart the backend. See {' '}
                      <button
                        type="button"
                        onClick={() => openDoc('security-deployment.md')}
                        className={`${dark ? 'text-violet-300' : 'text-violet-600'} font-semibold hover:underline`}
                      >
                        Security &amp; deployment guide
                      </button>
                      {' '}for the full operator guide.
                    </p>
                    <div>
                      <div className="flex items-center justify-between">
                        <label className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>CORS allowed origins</label>
                        <code className={`text-xs font-mono px-1.5 py-0.5 rounded ${dark ? 'bg-slate-800 text-slate-400' : 'bg-slate-100 text-slate-600'}`}>FPULSE_CORS_ORIGINS</code>
                      </div>
                      <div className={`mt-1 px-3 py-2 text-sm font-mono rounded-lg border ${dark ? 'bg-[#0f1726] border-white/[0.06] text-slate-300' : 'bg-slate-50 border-slate-200 text-slate-700'}`}>
                        {corsOrigins || '(unset — allows all origins; credentials-mode disabled for safety)'}
                      </div>
                    </div>
                    {isPlus && (
                    <div>
                      <div className="flex items-center justify-between">
                        <label className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>
                          IP allowlist
                        </label>
                        <code className={`text-xs font-mono px-1.5 py-0.5 rounded ${dark ? 'bg-slate-800 text-slate-400' : 'bg-slate-100 text-slate-600'}`}>FPULSE_PLUS_IP_ALLOWLIST</code>
                      </div>
                      <div className={`mt-1 px-3 py-2 text-sm font-mono rounded-lg border ${dark ? 'bg-[#0f1726] border-white/[0.06] text-slate-300' : 'bg-slate-50 border-slate-200 text-slate-700'}`}>
                        {ipWhitelist || '(unset — allow all IPs)'}
                      </div>
                    </div>
                    )}
                    <div>
                      <div className="flex items-center justify-between">
                        <label className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>Trust X-Forwarded-For from proxy</label>
                        <code className={`text-xs font-mono px-1.5 py-0.5 rounded ${dark ? 'bg-slate-800 text-slate-400' : 'bg-slate-100 text-slate-600'}`}>FPULSE_TRUSTED_PROXIES</code>
                      </div>
                      <div className={`mt-1 px-3 py-2 text-sm font-mono rounded-lg border ${dark ? 'bg-[#0f1726] border-white/[0.06] text-slate-300' : 'bg-slate-50 border-slate-200 text-slate-700'}`}>
                        {(typeof window !== 'undefined' && (window as any).__fpulseTrustedProxies) || '(unset — direct exposure mode; rate limiter uses peer IP)'}
                      </div>
                      <p className="text-xs text-slate-400 mt-1">Set to <code>1</code> when running behind nginx/Caddy/Cloudflare so the rate limiter sees the real client IP.</p>
                    </div>

                    {/* Z4 (2026-05-23): Storage location + backend posture.
                        Renders live data from /api/storage/location:
                          - actual data_dir (absolute path, copyable)
                          - free / used / total disk on that mount
                          - sub-tree layout from the backend (single source of truth)
                          - storage-backends list (local active in OSS;
                            S3 / Azure / GCS shown as Plus-only with chip
                            + description so the user can see the roadmap). */}
                    <div>
                      <div className="flex items-center justify-between">
                        <label className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>Workspace storage location</label>
                        <code className={`text-xs font-mono px-1.5 py-0.5 rounded ${dark ? 'bg-slate-800 text-slate-400' : 'bg-slate-100 text-slate-600'}`}>FPULSE_DATA_DIR</code>
                      </div>

                      {/* Z27 (2026-05-23) — pending-restart banner. Shows
                          when the user saved a new location through the
                          UI but hasn't restarted F-Pulse yet. The user
                          can either restart to apply, or discard the
                          pending change. */}
                      {storageLocation?.pending_restart && (
                        <div className={`mt-1 px-3 py-2.5 rounded-lg border ${dark ? 'bg-amber-500/10 border-amber-500/30' : 'bg-amber-50 border-amber-200'}`}>
                          <div className="flex items-start gap-2">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={dark ? 'text-amber-300 shrink-0 mt-0.5' : 'text-amber-700 shrink-0 mt-0.5'}>
                              <path d="M21 12a9 9 0 1 1-3-6.7" /><polyline points="21 4 21 10 15 10" />
                            </svg>
                            <div className="flex-1 min-w-0">
                              <div className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-amber-300' : 'text-amber-800'}`}>Restart required</div>
                              <p className={`mt-0.5 text-xs ${dark ? 'text-amber-200' : 'text-amber-700'}`}>
                                F-Pulse will switch to <code className="font-mono">{storageLocation.pending_data_dir}</code> on next restart. Move or copy the data tree to the new path before restarting.
                              </p>
                              <div className="mt-2 flex gap-2">
                                <button
                                  type="button"
                                  onClick={discardStorageLocation}
                                  disabled={storageLocationBusy}
                                  className={`px-2.5 py-1 text-[11px] font-semibold rounded-md border transition-colors ${dark ? 'bg-slate-800 border-slate-700 text-slate-300 hover:bg-slate-700' : 'bg-white border-amber-200 text-amber-800 hover:bg-amber-100'} disabled:opacity-50`}
                                >
                                  Discard pending change
                                </button>
                              </div>
                            </div>
                          </div>
                        </div>
                      )}

                      {/* Active data_dir + free disk + Change button */}
                      <div className={`mt-1 px-3 py-2.5 rounded-lg border ${dark ? 'bg-[#0f1726] border-white/[0.06]' : 'bg-slate-50 border-slate-200'}`}>
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className={`text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${storageLocation?.active_backend === 'local'
                            ? (dark ? 'bg-emerald-500/20 text-emerald-300' : 'bg-emerald-100 text-emerald-800')
                            : (dark ? 'bg-slate-700 text-slate-300' : 'bg-slate-200 text-slate-700')
                          }`}>
                            {storageLocation?.active_backend === 'local' ? 'Local · Active' : (storageLocation?.active_backend || 'local')}
                          </span>
                          {storageLocation?.is_default && (
                            <span className={`text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded ${dark ? 'bg-slate-700 text-slate-400' : 'bg-slate-100 text-slate-600'}`}>Default</span>
                          )}
                          <div className="ml-auto">
                            {!storageLocationEditing ? (
                              <button
                                type="button"
                                onClick={() => {
                                  setStorageLocationDraft(storageLocation?.pending_data_dir || storageLocation?.data_dir || '');
                                  setStorageLocationProbe(null);
                                  setStorageLocationEditing(true);
                                }}
                                className={`text-[11px] font-semibold px-2 py-0.5 rounded-md border transition-colors ${dark ? 'bg-slate-800 border-slate-700 text-slate-300 hover:bg-slate-700' : 'bg-white border-slate-200 text-slate-700 hover:bg-slate-50'}`}
                              >
                                Change…
                              </button>
                            ) : (
                              <button
                                type="button"
                                onClick={() => { setStorageLocationEditing(false); setStorageLocationDraft(''); setStorageLocationProbe(null); }}
                                className={`text-[11px] font-semibold px-2 py-0.5 rounded-md border transition-colors ${dark ? 'bg-slate-800 border-slate-700 text-slate-300 hover:bg-slate-700' : 'bg-white border-slate-200 text-slate-700 hover:bg-slate-50'}`}
                              >
                                Cancel
                              </button>
                            )}
                          </div>
                        </div>
                        <code className={`mt-2 block text-xs font-mono break-all ${dark ? 'text-slate-200' : 'text-slate-800'}`}>
                          {storageLocation?.data_dir || '(loading…)'}
                        </code>
                        {storageLocation?.disk && (
                          <div className="mt-2 flex items-center gap-3 text-[11px]">
                            <span className={dark ? 'text-slate-400' : 'text-slate-500'}>
                              <b className={dark ? 'text-slate-200' : 'text-slate-800'}>{formatBytes(storageLocation.disk.free_bytes)}</b> free of {formatBytes(storageLocation.disk.total_bytes)}
                            </span>
                            <span className={dark ? 'text-slate-500' : 'text-slate-400'}>
                              · {formatBytes(storageLocation.disk.used_bytes)} used
                            </span>
                            <div className={`flex-1 max-w-[200px] h-1.5 rounded-full overflow-hidden ${dark ? 'bg-slate-800' : 'bg-slate-200'}`}>
                              <div
                                className={`h-full ${
                                  storageLocation.disk.used_bytes / storageLocation.disk.total_bytes > 0.9
                                    ? 'bg-red-500'
                                    : storageLocation.disk.used_bytes / storageLocation.disk.total_bytes > 0.75
                                      ? 'bg-amber-500'
                                      : 'bg-emerald-500'
                                }`}
                                style={{ width: `${Math.min(100, (storageLocation.disk.used_bytes / storageLocation.disk.total_bytes) * 100)}%` }}
                              />
                            </div>
                          </div>
                        )}
                      </div>

                      {/* Sub-tree layout */}
                      <div className={`mt-2 px-3 py-2 rounded-lg border ${dark ? 'bg-[#0f1726] border-white/[0.06]' : 'bg-white border-slate-200'}`}>
                        <div className={`text-[10px] font-bold uppercase tracking-wider mb-1.5 ${dark ? 'text-slate-400' : 'text-slate-500'}`}>Sub-tree layout</div>
                        <div className="space-y-1">
                          {(storageLocation?.subtree || [
                            { name: 'uploads/', purpose: 'raw uploaded files' },
                            { name: 'outputs/', purpose: 'pipeline-generated artifacts' },
                            { name: 'tables/', purpose: 'managed Parquet tables (schema.name)' },
                            { name: 'trash/', purpose: 'soft-deleted files' },
                            { name: 'checkpoints/', purpose: 'execution artifacts (system)' },
                            { name: 'step_io/', purpose: 'execution artifacts (system)' },
                          ]).map((sub) => (
                            <div key={sub.name} className="flex items-baseline gap-3 text-xs">
                              <code className={`font-mono shrink-0 w-32 ${dark ? 'text-blue-300' : 'text-blue-700'}`}>{sub.name}</code>
                              <span className={dark ? 'text-slate-400' : 'text-slate-600'}>{sub.purpose}</span>
                            </div>
                          ))}
                        </div>
                      </div>

                      {/* Z27 — inline edit form. Shown when the user
                          clicks "Change…" above. Validate → Save flow:
                          POST /location/test probes the candidate (write
                          permission + free space); PUT /location persists
                          the override; the change takes effect on next
                          F-Pulse restart. */}
                      {storageLocationEditing ? (
                        <div className={`mt-2 px-3 py-3 rounded-lg border ${dark ? 'bg-[#0f1726] border-white/[0.06]' : 'bg-white border-slate-200'} space-y-3`}>
                          <div>
                            <label className={`text-[11px] font-bold uppercase tracking-wider ${dark ? 'text-slate-400' : 'text-slate-600'}`}>New data directory (absolute path)</label>
                            <input
                              type="text"
                              value={storageLocationDraft}
                              onChange={(e) => { setStorageLocationDraft(e.target.value); setStorageLocationProbe(null); }}
                              placeholder="/srv/fpulse/data"
                              className={`mt-1 w-full px-3 py-2 text-sm font-mono rounded-lg border focus:outline-none focus:ring-2 ${dark ? 'bg-slate-900 border-white/[0.06] text-slate-200 focus:ring-violet-500/40' : 'bg-white border-slate-200 text-slate-700 focus:ring-violet-400'}`}
                            />
                            <p className={`mt-1 text-[11px] ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
                              Move or copy the existing tree to this path before restarting. F-Pulse will use the new location on next boot.
                            </p>
                          </div>
                          {/* Probe result */}
                          {storageLocationProbe && (
                            <div className={`px-3 py-2 rounded-md border text-xs ${storageLocationProbe.ok
                              ? (dark ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-200' : 'bg-emerald-50 border-emerald-200 text-emerald-800')
                              : (dark ? 'bg-red-500/10 border-red-500/30 text-red-200' : 'bg-red-50 border-red-200 text-red-800')
                            }`}>
                              <div className="font-bold">{storageLocationProbe.ok ? '✓ Path is usable' : '✗ Path is not usable'}</div>
                              {storageLocationProbe.path && (
                                <div className="mt-0.5 font-mono break-all">{storageLocationProbe.path}</div>
                              )}
                              {!!(storageLocationProbe.issues && storageLocationProbe.issues.length > 0) && (
                                <ul className="mt-1 list-disc list-inside space-y-0.5">
                                  {storageLocationProbe.issues.map((it, i) => <li key={i}>{it}</li>)}
                                </ul>
                              )}
                              {storageLocationProbe.ok && storageLocationProbe.free_bytes != null && (
                                <div className="mt-1">{formatBytes(storageLocationProbe.free_bytes)} free at target</div>
                              )}
                            </div>
                          )}
                          <div className="flex flex-wrap gap-2">
                            <button
                              type="button"
                              onClick={validateStorageLocation}
                              disabled={storageLocationBusy || !storageLocationDraft.trim()}
                              className={`px-3 py-1.5 text-xs font-semibold rounded-lg border transition-colors ${dark ? 'bg-slate-800 border-slate-700 text-slate-200 hover:bg-slate-700' : 'bg-white border-slate-200 text-slate-700 hover:bg-slate-50'} disabled:opacity-40 disabled:cursor-not-allowed`}
                            >
                              {storageLocationBusy ? 'Working…' : 'Validate'}
                            </button>
                            <button
                              type="button"
                              onClick={saveStorageLocation}
                              disabled={storageLocationBusy || !storageLocationProbe?.ok}
                              className={`px-3 py-1.5 text-xs font-semibold rounded-lg border transition-colors ${dark ? 'bg-violet-600/30 border-violet-500/50 text-violet-100 hover:bg-violet-600/40' : 'bg-violet-50 border-violet-200 text-violet-800 hover:bg-violet-100'} disabled:opacity-40 disabled:cursor-not-allowed`}
                              title={storageLocationProbe?.ok ? 'Save the override; restart F-Pulse to apply' : 'Run Validate first'}
                            >
                              Save (apply on restart)
                            </button>
                          </div>
                        </div>
                      ) : (
                        <p className="text-xs text-slate-400 mt-2">
                          <b>Relocate to a different local path:</b> click <b>Change…</b> above to pick a new directory, or set <code>FPULSE_DATA_DIR=/your/path</code> + move the existing tree + restart. The reconciler back-fills the metadata index on first boot.
                        </p>
                      )}

                      {/* Storage backends — local active in OSS, others Plus */}
                      <div className="mt-4">
                        <div className={`text-[11px] font-bold uppercase tracking-wider mb-2 ${dark ? 'text-slate-400' : 'text-slate-600'}`}>Storage backends</div>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                          {(storageLocation?.backends || []).map((b) => {
                            const isActive = b.enabled;
                            const isPlusGated = !b.enabled && b.requires === 'plus';
                            return (
                              <div
                                key={b.id}
                                className={`px-3 py-2 rounded-lg border ${
                                  isActive
                                    ? (dark ? 'bg-emerald-500/10 border-emerald-500/30' : 'bg-emerald-50 border-emerald-200')
                                    : (dark ? 'bg-[#0f1726] border-white/[0.06] opacity-80' : 'bg-white border-slate-200 opacity-90')
                                }`}
                              >
                                <div className="flex items-center justify-between gap-2">
                                  <span className={`text-sm font-semibold ${dark ? 'text-slate-100' : 'text-slate-900'}`}>
                                    {b.label}
                                  </span>
                                  {isActive ? (
                                    <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-emerald-500 text-white">Active</span>
                                  ) : isPlusGated ? (
                                    null
                                  ) : (
                                    <span className={`text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${dark ? 'bg-slate-700 text-slate-400' : 'bg-slate-200 text-slate-600'}`}>Off</span>
                                  )}
                                </div>
                                <p className={`text-[11px] mt-1 leading-relaxed ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
                                  {b.description}
                                </p>
                              </div>
                            );
                          })}
                        </div>
                        <p className="text-xs text-slate-400 mt-2">
                          Metadata is written to the <b>Local</b> backend.
                        </p>
                      </div>
                    </div>
                  </div>
                </div>

                {/* Z26 (2026-05-23) — Backup & restore. Lives right
                    under Storage so the two concerns are co-located:
                    where the data lives, and how it's protected.
                    OSS: local destination + manual / scheduled snapshots.
                    Plus: cloud destinations (S3 / Azure / GCS / MinIO). */}
                <div className="mt-6">
                  <SectionHeader dark={dark}
                    title="Backup & Restore"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7" /><polyline points="21 4 21 10 15 10" /></svg>}
                  />
                  <BackupSettingsPanel dark={dark} isPlus={isPlus} />
                </div>

                {/* Security checklist — May 3 2026 rewrite. Previous
                    list reported truthy state from no-op localStorage
                    toggles, producing false-positive checkmarks for
                    Plus-only features. Now reflects actual posture:
                    baseline items are always-true; Plus items show as
                    "Plus required" when not Plus. */}
                <div className="mt-6">
                  <SectionHeader dark={dark}
                    title="Security Checklist"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="9 11 12 14 22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" /></svg>}
                  />
                  <div className={`rounded-lg border shadow-sm p-4 space-y-2 ${dark ? 'bg-[#111827] border-white/[0.08]' : 'border-slate-200'}`} style={dark ? undefined : { background: 'linear-gradient(135deg, #FFFFFF 0%, #FAFBFF 100%)' }}>
                    {[
                      { label: 'Credentials encrypted at rest (Fernet — AES-128-CBC + HMAC-SHA256)', ok: true, plus: false },
                      { label: 'Master key file permissions enforced (0600)', ok: true, plus: false },
                      { label: 'SQL input sanitization', ok: true, plus: false },
                      { label: 'Security headers (X-Frame-Options, CSP, HSTS)', ok: true, plus: false },
                      { label: 'HTTP rate limiter active', ok: true, plus: false },
                      { label: 'CORS restricted (not wildcard)', ok: corsOrigins !== '*' && corsOrigins.trim() !== '', plus: false },
                      { label: 'Behind reverse proxy (X-Forwarded-For trusted)', ok: false, plus: false, hint: 'Set FPULSE_TRUSTED_PROXIES=1 when behind nginx/Caddy.' },
                      { label: 'Data at rest encrypted', ok: isPlus, plus: true },
                      { label: 'Audit logging enabled', ok: isPlus, plus: true },
                      { label: '2FA enforced', ok: isPlus && twoFactor, plus: true },
                      { label: 'Session timeout configured', ok: isPlus && parseInt(sessionTimeout) <= 60, plus: true },
                      { label: 'IP allowlist configured', ok: isPlus && ipWhitelist.trim().length > 0, plus: true },
                    ].map((item) => {
                      const showsPlusChip = item.plus && !isPlus;
                      const tone = item.ok
                        ? (dark ? 'text-green-400' : 'text-green-700')
                        : showsPlusChip
                          ? (dark ? 'text-violet-400' : 'text-violet-700')
                          : (dark ? 'text-amber-400' : 'text-amber-700');
                      const iconBg = item.ok
                        ? (dark ? 'bg-green-500/20' : 'bg-green-100')
                        : showsPlusChip
                          ? (dark ? 'bg-violet-500/20' : 'bg-violet-100')
                          : (dark ? 'bg-amber-500/20' : 'bg-amber-100');
                      return (
                        <div key={item.label} className="flex items-start gap-2">
                          <div className={`w-5 h-5 rounded-full flex items-center justify-center shrink-0 mt-0.5 ${iconBg}`}>
                            {item.ok ? (
                              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#16a34a" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                            ) : showsPlusChip ? (
                              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#7c3aed" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5z" /><path d="M2 17l10 5 10-5" /><path d="M2 12l10 5 10-5" /></svg>
                            ) : (
                              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#d97706" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" /></svg>
                            )}
                          </div>
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className={`text-sm ${tone}`}>{item.label}</span>
                              {false && showsPlusChip && (
                                <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-violet-100 text-violet-700">F-Pulse+</span>
                              )}
                            </div>
                            {item.hint && !item.ok && (
                              <p className="text-xs text-slate-400 mt-0.5">{item.hint}</p>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>

                {/* ── Privacy / telemetry consent (May 3 2026) ──────── */}
                <div className="mt-6">
                  <SectionHeader dark={dark}
                    title="Privacy"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /><path d="M9 12l2 2 4-4" /></svg>}
                  />
                  <div className={`rounded-lg border shadow-sm px-4 py-3 ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex-1 min-w-0">
                        <div className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>
                          Send anonymous error reports
                        </div>
                        <div className={`text-xs mt-1 leading-relaxed ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
                          F-Pulse ships with <strong>zero telemetry by default</strong>. If you opt in,
                          we receive crash reports — exception type + sanitized stack trace,
                          F-Pulse + Python + OS version, and feature-flag state.
                        </div>
                        <div className={`text-xs mt-2 ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
                          We <strong>never</strong> send: pipeline data, query results, SQL text,
                          configuration values, env vars, credentials, API keys, file paths,
                          user IDs, workspace names, agent prompts, or LLM responses.
                        </div>
                        <div className="text-xs mt-2">
                          <button
                            type="button"
                            onClick={() => openDoc('trust.md')}
                            className={`${dark ? 'text-violet-300 hover:text-violet-200' : 'text-violet-600 hover:text-violet-700'} font-semibold`}
                          >
                            Read the full payload schema →
                          </button>
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={() => setTelemetryEnabled(!telemetryEnabled)}
                        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors shrink-0 ${
                          telemetryEnabled ? 'bg-pipe-500' : dark ? 'bg-slate-600' : 'bg-slate-300'
                        }`}
                      >
                        <span className={`inline-block h-4 w-4 rounded-full bg-white transition-transform ${
                          telemetryEnabled ? 'translate-x-6' : 'translate-x-1'
                        }`} />
                      </button>
                    </div>
                    <div className={`mt-3 pt-3 border-t text-xs ${dark ? 'border-white/[0.06] text-slate-500' : 'border-slate-100 text-slate-400'}`}>
                      Status: <strong className={telemetryEnabled
                        ? (dark ? 'text-emerald-400' : 'text-emerald-700')
                        : (dark ? 'text-slate-300' : 'text-slate-600')}>
                        {telemetryEnabled ? 'Opted in — anonymous usage stats only' : 'Off — nothing leaves this machine'}
                      </strong>
                    </div>
                  </div>
                </div>

                {/* ── Product knowledge reindex (admin) ─────────── */}
                {/* Lets admins re-index docs/product_facts/*.md
                    without restarting the backend. Shows the in-process
                    indexer state from /api/ai/product-knowledge/status.
                    Wrapped in `mt-6` to match the spacing every other
                    section header on this tab uses (Privacy, Audit,
                    Authentication, etc.) — without it the header sits
                    flush against the section above and looks misaligned. */}
                <div className="mt-6">
                  <ProductKnowledgeReindexCard dark={dark} />
                </div>

                {/* ── Publishing: require a business purpose (admin) ─── */}
                <PublishPolicyCard dark={dark} />
              </>
            )}

            {tab === 'notifications' && (
              <>
                {/* ── Pipeline event toggles ─────────────────────── */}
                <SectionHeader dark={dark}
                  title="Pipeline Notifications"
                  icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" /></svg>}
                />
                <div className={`rounded-lg border shadow-sm px-4 divide-y ${dark ? 'bg-[#111827] border-white/[0.08] divide-white/[0.06]' : 'bg-white border-slate-200 divide-slate-100'}`}>
                  <Toggle dark={dark} enabled={notifyOnSuccess} onChange={setNotifyOnSuccess} label="Notify on success" description="Show notification when pipeline completes successfully" />
                  <Toggle dark={dark} enabled={notifyOnError} onChange={setNotifyOnError} label="Notify on error" description="Show notification when pipeline fails with an error" />
                  <Toggle dark={dark} enabled={notifyOnWarning} onChange={setNotifyOnWarning} label="Notify on warnings" description="Show notification for data quality warnings" />
                </div>

                {/* ── Execution alerts ────────────────────────────── */}
                <div className="mt-6">
                  <SectionHeader dark={dark}
                    title="Execution Alerts"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>}
                  />
                  <div className={`rounded-lg border shadow-sm px-4 divide-y ${dark ? 'bg-[#111827] border-white/[0.08] divide-white/[0.06]' : 'bg-white border-slate-200 divide-slate-100'}`}>
                    <div className="py-3">
                      <div className="flex items-center justify-between">
                        <div>
                          <div className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>Long-running pipeline alert</div>
                          <div className="text-xs text-slate-400 mt-0.5">Alert when a pipeline execution exceeds the threshold</div>
                        </div>
                        <button
                          onClick={() => setNotifyOnLongRunning(!notifyOnLongRunning)}
                          className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors shrink-0 ${notifyOnLongRunning ? 'bg-pipe-500' : dark ? 'bg-slate-600' : 'bg-slate-300'}`}
                        >
                          <span className={`inline-block h-4 w-4 rounded-full bg-white transition-transform ${notifyOnLongRunning ? 'translate-x-6' : 'translate-x-1'}`} />
                        </button>
                      </div>
                      {notifyOnLongRunning && (
                        <div className="mt-2 flex items-center gap-2">
                          <label className={`text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`}>Threshold</label>
                          <input
                            type="number"
                            min={1}
                            max={1440}
                            value={longRunningThresholdMin}
                            onChange={(e) => setLongRunningThresholdMin(Math.max(1, parseInt(e.target.value) || 1))}
                            className={`w-20 rounded-lg border px-2 py-1 text-sm text-center focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                          />
                          <span className={`text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`}>minutes</span>
                        </div>
                      )}
                    </div>
                    <Toggle dark={dark} enabled={notifyOnScheduleMiss} onChange={setNotifyOnScheduleMiss} label="Schedule miss detection" description="Alert when a scheduled pipeline does not start within its expected window" />
                  </div>
                </div>

                {/* ── External channels ───────────────────────────── */}
                <div className="mt-6">
                  <SectionHeader dark={dark}
                    title="Notification Channels"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="2" y="4" width="20" height="16" rx="2" /><polyline points="22 7 12 13 2 7" /></svg>}
                  />
                  <div className={`rounded-lg border shadow-sm px-4 divide-y ${dark ? 'bg-[#111827] border-white/[0.08] divide-white/[0.06]' : 'bg-white border-slate-200 divide-slate-100'}`}>
                    <Toggle dark={dark} enabled={emailNotifications} onChange={setEmailNotifications} label="Email notifications" description="Send email alerts for pipeline events (configure SMTP below)" />
                    {/* SMTP config — without these the alerts notifier
                        dry-runs and emails never reach the recipient.
                        Saved into notifications.smtp on the backend;
                        the alerts notifier re-reads on every send so
                        the form takes effect immediately, no restart. */}
                    <div ref={smtpRef} className="py-3 space-y-2 scroll-mt-4">
                      <div>
                        <label className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>SMTP</label>
                        <p className="text-xs text-slate-400 mt-0.5">
                          Outgoing mail server for email alerts. For Gmail use <code className="font-mono">smtp.gmail.com</code> / <code className="font-mono">587</code> with a Google <em>App Password</em> (Account → Security → 2-Step Verification → App passwords). Leave host empty to disable.
                        </p>
                      </div>
                      <div className="grid grid-cols-2 gap-2">
                        <input
                          type="text"
                          value={smtpHost}
                          onChange={(e) => setSmtpHost(e.target.value)}
                          placeholder="smtp.gmail.com"
                          className={`rounded-lg border px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                        />
                        <input
                          type="number"
                          value={smtpPort}
                          onChange={(e) => setSmtpPort(e.target.value)}
                          placeholder="587"
                          className={`rounded-lg border px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                        />
                      </div>
                      <input
                        type="text"
                        value={smtpUser}
                        onChange={(e) => setSmtpUser(e.target.value)}
                        placeholder="your-account@gmail.com"
                        autoComplete="off"
                        className={`w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                      />
                      <input
                        type="password"
                        value={smtpPass}
                        onChange={(e) => setSmtpPass(e.target.value)}
                        placeholder="App password (NOT your regular Gmail password)"
                        autoComplete="new-password"
                        className={`w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                      />
                      <input
                        type="email"
                        value={smtpFrom}
                        onChange={(e) => setSmtpFrom(e.target.value)}
                        placeholder="From: address (defaults to user)"
                        className={`w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                      />
                      <label className={`flex items-center gap-2 text-xs ${dark ? 'text-slate-300' : 'text-slate-600'}`}>
                        <input
                          type="checkbox"
                          checked={smtpTls}
                          onChange={(e) => setSmtpTls(e.target.checked)}
                          className="rounded border-slate-300 text-pipe-600 focus:ring-pipe-300"
                        />
                        Use STARTTLS (recommended — required for Gmail and most providers)
                      </label>
                    </div>
                    <Toggle dark={dark} enabled={browserNotifications} onChange={(v) => {
                      if (v && 'Notification' in window && Notification.permission === 'default') {
                        Notification.requestPermission();
                      }
                      setBrowserNotifications(v);
                    }} label="Browser notifications" description="Desktop push notifications via the browser Notification API" />
                    <div className="py-3">
                      <label className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>Slack Webhook</label>
                      <p className="text-xs text-slate-400 mt-0.5">Incoming webhook URL for Slack channel notifications</p>
                      <input
                        type="url"
                        value={slackWebhookUrl}
                        onChange={(e) => setSlackWebhookUrl(e.target.value)}
                        placeholder="https://hooks.slack.com/services/..."
                        className={`mt-2 w-full rounded-lg border px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                      />
                    </div>
                    <div className="py-3">
                      <label className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>Microsoft Teams Webhook</label>
                      <p className="text-xs text-slate-400 mt-0.5">Incoming webhook URL for a Teams channel (Connector → Incoming Webhook)</p>
                      <input
                        type="url"
                        value={teamsWebhookUrl}
                        onChange={(e) => setTeamsWebhookUrl(e.target.value)}
                        placeholder="https://outlook.office.com/webhook/..."
                        className={`mt-2 w-full rounded-lg border px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                      />
                    </div>
                    <div className="py-3">
                      <label className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>Discord Webhook</label>
                      <p className="text-xs text-slate-400 mt-0.5">Discord channel webhook URL for pipeline event notifications</p>
                      <input
                        type="url"
                        value={discordWebhookUrl}
                        onChange={(e) => setDiscordWebhookUrl(e.target.value)}
                        placeholder="https://discord.com/api/webhooks/..."
                        className={`mt-2 w-full rounded-lg border px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                      />
                    </div>
                    <div className="py-3">
                      <label className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>Generic Webhook</label>
                      <p className="text-xs text-slate-400 mt-0.5">Custom webhook URL for any service that accepts JSON POST notifications</p>
                      <input
                        type="url"
                        value={genericWebhookUrl}
                        onChange={(e) => setGenericWebhookUrl(e.target.value)}
                        placeholder="https://your-service.example.com/webhook"
                        className={`mt-2 w-full rounded-lg border px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                      />
                    </div>
                  </div>
                </div>

                {/* Delivery controls — Plus-only; hidden entirely on OSS. */}
                {isPlus && (
                <div className="mt-6">
                  <SectionHeader dark={dark}
                    title="Delivery Controls"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" /></svg>}
                  />
                  <div className={`rounded-lg border shadow-sm px-4 divide-y ${dark ? 'bg-[#111827] border-white/[0.08] divide-white/[0.06]' : 'bg-white border-slate-200 divide-slate-100'}`}>
                    {/* Quiet hours */}
                    <div className="py-3">
                      <div className="flex items-center justify-between">
                        <div>
                          <div className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>
                            Quiet hours
                          </div>
                          <div className="text-xs text-slate-400 mt-0.5">Suppress non-critical notifications during off hours</div>
                        </div>
                        <button
                          onClick={() => setQuietHoursEnabled(!quietHoursEnabled)}
                          className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors shrink-0 ${quietHoursEnabled ? 'bg-pipe-500' : dark ? 'bg-slate-600' : 'bg-slate-300'}`}
                        >
                          <span className={`inline-block h-4 w-4 rounded-full bg-white transition-transform ${quietHoursEnabled ? 'translate-x-6' : 'translate-x-1'}`} />
                        </button>
                      </div>
                      {quietHoursEnabled && (
                        <div className="mt-2 flex items-center gap-3">
                          <div className="flex items-center gap-1.5">
                            <label className={`text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`}>From</label>
                            <input type="time" value={quietHoursStart} onChange={(e) => setQuietHoursStart(e.target.value)}
                              className={`rounded-lg border px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                            />
                          </div>
                          <div className="flex items-center gap-1.5">
                            <label className={`text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`}>To</label>
                            <input type="time" value={quietHoursEnd} onChange={(e) => setQuietHoursEnd(e.target.value)}
                              className={`rounded-lg border px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                            />
                          </div>
                          <span className={`text-xs ${dark ? 'text-slate-500' : 'text-slate-400'}`}>Errors still delivered immediately</span>
                        </div>
                      )}
                    </div>

                    {/* Debounce */}
                    <div className="py-3">
                      <div className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>
                        Notification debounce
                      </div>
                      <div className="text-xs text-slate-400 mt-0.5">Collapse repeated notifications from the same pipeline within this window</div>
                      <div className="mt-2 flex items-center gap-2">
                        <input
                          type="number"
                          min={0}
                          max={3600}
                          value={debounceSeconds}
                          onChange={(e) => setDebounceSeconds(Math.max(0, parseInt(e.target.value) || 0))}
                          className={`w-20 rounded-lg border px-2 py-1 text-sm text-center focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                        />
                        <span className={`text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`}>seconds (0 = no debounce)</span>
                      </div>
                    </div>

                    {/* Daily digest */}
                    <div className="py-3">
                      <div className="flex items-center justify-between">
                        <div>
                          <div className={`text-sm font-medium ${dark ? 'text-slate-200' : 'text-slate-700'}`}>
                            Daily digest
                          </div>
                          <div className="text-xs text-slate-400 mt-0.5">Receive a summary email with all pipeline events from the past 24 hours</div>
                        </div>
                        <button
                          onClick={() => setDailyDigest(!dailyDigest)}
                          className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors shrink-0 ${dailyDigest ? 'bg-pipe-500' : dark ? 'bg-slate-600' : 'bg-slate-300'}`}
                        >
                          <span className={`inline-block h-4 w-4 rounded-full bg-white transition-transform ${dailyDigest ? 'translate-x-6' : 'translate-x-1'}`} />
                        </button>
                      </div>
                      {dailyDigest && (
                        <div className="mt-2 flex items-center gap-2">
                          <label className={`text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`}>Send at</label>
                          <input type="time" value={dailyDigestTime} onChange={(e) => setDailyDigestTime(e.target.value)}
                            className={`rounded-lg border px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-pipe-300 ${dark ? 'bg-[#0f1726] border-white/[0.1] text-slate-200' : 'border-slate-200 text-slate-700'}`}
                          />
                          <span className={`text-xs ${dark ? 'text-slate-500' : 'text-slate-400'}`}>Workspace timezone</span>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
                )}
              </>
            )}

            {/* AI Provider tab REMOVED May 2 2026.
                It now lives in Insights → AI Provider subtab. Settings is
                strictly app-preference (general / security / notifications /
                about); LLM provider is an AI surface, not a preference. */}

            {tab === 'about' && (
              <>
                {/* Signed-in-as card — lifted straight from localStorage so
                    this page stays decoupled from the parent auth state.
                    Hidden entirely when no session is present (e.g. dev
                    autologin bypass mode), since fabricating identity would
                    be misleading. */}
                {(() => {
                  try {
                    const raw = localStorage.getItem('fpulse_user');
                    if (!raw) return null;
                    const u = JSON.parse(raw) as { name?: string; email?: string; role?: string; environments?: string[] };
                    if (!u?.email) return null;
                    const initial = (u.name || u.email || '?')[0].toUpperCase();
                    return (
                      <div className={`rounded-lg border shadow-sm p-5 mb-4 flex items-center gap-4 ${dark ? 'bg-[#111827] border-white/[0.08]' : 'border-slate-200'}`} style={dark ? undefined : { background: 'linear-gradient(135deg, #FFFFFF 0%, #FAFBFF 100%)' }}>
                        <div
                          className="w-12 h-12 rounded-lg flex items-center justify-center text-base font-bold text-white shadow-sm shrink-0"
                          style={{ background: 'linear-gradient(135deg, #F5A623, #D4880A)' }}
                        >
                          {initial}
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="text-xs font-semibold uppercase tracking-wide text-slate-400">Signed in as</div>
                          <div className={`text-sm font-bold truncate ${dark ? 'text-slate-100' : 'text-slate-800'}`}>{u.name || u.email}</div>
                          <div className="text-xs text-slate-500 truncate">{u.email}</div>
                          <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
                            {u.role && (
                              <span className="text-[9px] font-bold uppercase tracking-wide bg-amber-50 text-amber-700 px-1.5 py-0.5 rounded border border-amber-200">
                                {u.role.replace(/_/g, ' ')}
                              </span>
                            )}
                            {(u.environments || []).map((e) => (
                              <span key={e} className={`text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded border ${
                                e === 'prod'
                                  ? 'bg-red-50 text-red-700 border-red-200'
                                  : 'bg-emerald-50 text-emerald-700 border-emerald-200'
                              }`}>
                                {e}
                              </span>
                            ))}
                          </div>
                        </div>
                      </div>
                    );
                  } catch {
                    return null;
                  }
                })()}

                <div className={`rounded-lg border shadow-sm p-6 text-center ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
                  {/* 2026-06-02 — use the real brand mark (the same PNG
                      the sidebar uses) so the About card and the header
                      stay visually aligned. The old generic lightning
                      bolt SVG read as placeholder art and didn't match
                      anything else in the product. White plate matches
                      the sidebar pattern so the logo PNG stays crisp
                      regardless of light/dark mode. */}
                  <div className="w-16 h-16 rounded-2xl overflow-hidden bg-white shadow-lg mx-auto mb-4 ring-1 ring-slate-200">
                    <img src="/fpulse-logo-mark.png" alt="F-Pulse OSS" className="w-full h-full object-cover" />
                  </div>
                  <h2 className={`text-xl font-bold ${dark ? 'text-slate-100' : 'text-slate-800'}`}>{isPlus ? 'F-Pulse+' : 'F-Pulse OSS'}</h2>
                  {/* 2026-06-02 — tagline rewritten to match readme.md
                      lead ("Single-binary, local-first data pipeline
                      engine"). Previous "AI-Native Data Pipeline Builder"
                      contradicted the reliability-first v1.0 positioning
                      and led with a moat (AI) that competitors will close
                      within 12 months; lead with the durable one. */}
                  <p className="text-sm text-slate-500 mt-1">Single-binary, local-first data pipeline engine</p>
                  <p className="text-xs text-slate-400 mt-0.5">Version {APP_VERSION}</p>

                  {/* 2026-06-02 — stat tiles replaced with HONEST,
                      verifiable numbers:
                        - 40 node types: readme.md line 9 + ModulesPanel count
                        - 33 connectors: readme.md line 21 (4 db + 2 bulk + 27 SaaS REST)
                        - 27 templates: frontend/src/templates/catalog.ts header
                        - Apache 2.0: pyproject.toml + LICENSE
                      Previous 80+ / 15+ / 15+ / 20+ were inflated PR
                      numbers that don't match any source-of-truth. */}
                  <div className="mt-6 grid grid-cols-4 gap-3 text-center">
                    <div className={`rounded-lg p-3 ${dark ? 'bg-white/[0.04]' : 'bg-slate-50'}`}>
                      <div className="text-lg font-bold text-pipe-600">40</div>
                      <div className="text-xs text-slate-400 uppercase font-medium">Node Types</div>
                    </div>
                    <div className={`rounded-lg p-3 ${dark ? 'bg-white/[0.04]' : 'bg-slate-50'}`}>
                      <div className="text-lg font-bold text-pipe-600">33</div>
                      <div className="text-xs text-slate-400 uppercase font-medium">Connectors</div>
                    </div>
                    <div className={`rounded-lg p-3 ${dark ? 'bg-white/[0.04]' : 'bg-slate-50'}`}>
                      <div className="text-lg font-bold text-pipe-600">27</div>
                      <div className="text-xs text-slate-400 uppercase font-medium">Templates</div>
                    </div>
                    <div className={`rounded-lg p-3 ${dark ? 'bg-white/[0.04]' : 'bg-slate-50'}`}>
                      <div className="text-lg font-bold text-pipe-600">Apache</div>
                      <div className="text-xs text-slate-400 uppercase font-medium">License 2.0</div>
                    </div>
                  </div>

                  <div className="mt-6 text-xs text-slate-400">
                    <p>DuckDB-powered. Scheduler, alerts, run history built in.</p>
                    <p className="mt-1">Built with React, React Flow, DuckDB, and FastAPI.</p>
                    <p className="mt-3 text-slate-300">by Hybridyn Data Labs</p>
                  </div>
                </div>

                {/* 2026-06-18 — Running-as-an-app pointer. Surfaces the
                    always-on service one-liner + where the full guide lives,
                    so an operator on the About page can make F-Pulse start on
                    its own without hunting through docs. */}
                <div className={`mt-6 rounded-lg border shadow-sm p-5 ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
                  <SectionHeader dark={dark}
                    title="Running as an app"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" /><polyline points="3.27 6.96 12 12.01 20.73 6.96" /><line x1="12" y1="22.08" x2="12" y2="12" /></svg>}
                  />
                  <p className="text-xs text-slate-500 leading-relaxed">
                    F-Pulse runs as a local server you reach at <code className="text-slate-400">http://localhost:8001</code>.
                    Install it as a background service so it starts on its own and keeps running — and keeps firing
                    schedules — even after you sign out or close the window:
                  </p>
                  <pre className={`mt-2 text-[11px] rounded-md p-2 overflow-x-auto whitespace-pre-wrap ${dark ? 'bg-black/30 text-slate-300' : 'bg-slate-50 text-slate-600'}`}>{`python -m fpulse install-service --at-boot   # always-on, starts at boot
python -m fpulse uninstall-service           # remove (data preserved)`}</pre>
                  <p className="text-[11px] text-slate-400 mt-2">
                    Full walkthrough + the double-click installer: <b>Help &rarr; How-To &rarr; Install &amp; Run F-Pulse as an App</b>.
                    Check for a newer version from <b>Help &rarr; Help &amp; Feedback &rarr; Check for updates</b>.
                  </p>
                </div>

                {/* System info */}
                <div className="mt-6">
                  <SectionHeader dark={dark}
                    title="System Information"
                    icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="2" y="3" width="20" height="14" rx="2" /><line x1="8" y1="21" x2="16" y2="21" /><line x1="12" y1="17" x2="12" y2="21" /></svg>}
                  />
                  <div className={`rounded-lg border shadow-sm divide-y ${dark ? 'bg-[#111827] border-white/[0.08] divide-white/[0.06]' : 'bg-white border-slate-200 divide-slate-100'}`}>
                    {[
                      ['Product', isPlus ? 'F-Pulse+ (Subscription)' : 'F-Pulse (Open Source)'],
                      ['Version', APP_VERSION],
                      ['Frontend', 'React 18 · Vite · Tailwind CSS · React Flow'],
                      ['Backend', 'Python · FastAPI · SQLite · DuckDB'],
                      ['License', isPlus ? 'HMAC-signed, locally hosted' : 'Apache 2.0'],
                      ['Secrets', 'Encrypted at rest'],
                    ].map(([label, value]) => (
                      <div key={label} className="flex items-center justify-between px-4 py-2.5">
                        <span className="text-xs font-medium text-slate-500">{label}</span>
                        <span className={`text-xs ${dark ? 'text-slate-300' : 'text-slate-700'}`}>{value}</span>
                      </div>
                    ))}
                  </div>
                </div>

                {/* Resources block removed May 2 2026 — was a redundant
                    pointer to the Help page (one nav click away). */}
              </>
            )}

            {/* Save button */}
            {tab !== 'about' && (
              <div className="mt-8 flex justify-end">
                <button
                  onClick={handleSave}
                  className="px-6 py-2.5 text-white text-sm font-bold rounded-lg shadow-sm hover:shadow-md transition-all"
                  style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
                >
                  Save Settings
                </button>
              </div>
            )}
        </div>
      </div>
    </div>
  );
}


// ─────────────────────────────────────────────────────────────────────
// ProductKnowledgeReindexCard — Layer 2 admin op
// ─────────────────────────────────────────────────────────────────────

function ProductKnowledgeReindexCard({ dark }: { dark: boolean }) {
  const [status, setStatus] = useState<{
    ran_at: string | null;
    files: number;
    chunks: number;
    duration_ms: number;
    trigger: 'startup' | 'admin' | null;
    error: string | null;
    facts_dir_exists: boolean;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const out = await api.getProductKnowledgeStatus();
      setStatus(out as any);
    } catch (e: any) {
      setError(e?.message || 'Failed to load status');
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const handleReindex = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const out = await api.reindexProductKnowledge();
      setStatus(out as any);
    } catch (e: any) {
      setError(e?.message || 'Reindex failed (admin role required)');
    } finally {
      setBusy(false);
    }
  }, []);

  const lastRun = status?.ran_at ? new Date(status.ran_at).toLocaleString() : 'never';
  const haveData = status && status.chunks > 0;

  return (
    <>
      <SectionHeader dark={dark}
        title="AI product knowledge"
        icon={<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" /><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" /></svg>}
      />
      <div className={`rounded-lg border shadow-sm p-4 ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
        <div className={`text-[12px] mb-3 ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
          The Copilot retrieves curated facts from <code className="font-mono text-xs">docs/product_facts/*.md</code> on every chat turn (Layer 2 of the chat knowledge architecture). Edit a fact file, then click <strong>Reindex</strong> to apply without restarting the backend. Idempotent.
        </div>

        <div className={`grid grid-cols-2 md:grid-cols-4 gap-3 mb-4 text-xs ${dark ? 'text-slate-300' : 'text-slate-700'}`}>
          <div className={`rounded p-2 ${dark ? 'bg-[#0b1120] border border-white/[0.06]' : 'bg-slate-50 border border-slate-100'}`}>
            <div className={`uppercase tracking-wide ${dark ? 'text-slate-500' : 'text-slate-500'}`}>Chunks indexed</div>
            <div className="text-base font-semibold mt-0.5">{status?.chunks ?? '—'}</div>
          </div>
          <div className={`rounded p-2 ${dark ? 'bg-[#0b1120] border border-white/[0.06]' : 'bg-slate-50 border border-slate-100'}`}>
            <div className={`uppercase tracking-wide ${dark ? 'text-slate-500' : 'text-slate-500'}`}>Files</div>
            <div className="text-base font-semibold mt-0.5">{status?.files ?? '—'}</div>
          </div>
          <div className={`rounded p-2 ${dark ? 'bg-[#0b1120] border border-white/[0.06]' : 'bg-slate-50 border border-slate-100'}`}>
            <div className={`uppercase tracking-wide ${dark ? 'text-slate-500' : 'text-slate-500'}`}>Last run</div>
            <div className="text-xs font-medium mt-0.5">{lastRun}</div>
          </div>
          <div className={`rounded p-2 ${dark ? 'bg-[#0b1120] border border-white/[0.06]' : 'bg-slate-50 border border-slate-100'}`}>
            <div className={`uppercase tracking-wide ${dark ? 'text-slate-500' : 'text-slate-500'}`}>Trigger</div>
            <div className="text-xs font-medium mt-0.5">{status?.trigger ?? '—'}</div>
          </div>
        </div>

        {status && !status.facts_dir_exists && (
          <div className={`mb-3 rounded p-2 text-xs ${dark ? 'bg-amber-500/10 text-amber-300 border border-amber-500/20' : 'bg-amber-50 text-amber-800 border border-amber-200'}`}>
            <strong>docs/product_facts/ directory not found.</strong> Layer 2 will return zero chunks until the directory is created.
          </div>
        )}

        {(error || status?.error) && (
          <div className={`mb-3 rounded p-2 text-xs ${dark ? 'bg-red-500/10 text-red-300 border border-red-500/20' : 'bg-red-50 text-red-800 border border-red-200'}`}>
            {error || status?.error}
          </div>
        )}

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleReindex}
            disabled={busy}
            className={`px-3 py-1.5 text-xs font-semibold rounded ${dark ? 'bg-indigo-500/20 hover:bg-indigo-500/30 text-indigo-200 disabled:opacity-50' : 'bg-indigo-600 hover:bg-indigo-700 text-white disabled:opacity-50'}`}
            title="Re-runs chunker + embedder against docs/product_facts/. Admin only."
          >
            {busy ? 'Reindexing…' : 'Reindex now'}
          </button>
          <button
            type="button"
            onClick={refresh}
            className={`px-2 py-1.5 text-xs rounded ${dark ? 'bg-white/[0.04] hover:bg-white/[0.08] text-slate-300' : 'bg-slate-100 hover:bg-slate-200 text-slate-700'}`}
          >
            Refresh status
          </button>
          {!haveData && status && (
            <span className={`text-xs ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
              No chunks indexed yet — click Reindex to populate.
            </span>
          )}
        </div>
      </div>
    </>
  );
}
