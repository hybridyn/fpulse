import { useState, useEffect } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';
import { useCan } from '../../auth/RoleGate';
import TierChip from '../shared/TierChip';
import PageHeader from '../shared/PageHeader';
import TableToolbar, { useTableColumns, type TColumn, type TColumnGroup } from '../shared/TableToolbar';
import ProjectContextBar from '../layout/ProjectContextBar';
import DetailDrawer from '../shared/DetailDrawer';
import TimeAgo from '../shared/TimeAgo';
import EmptyState from '../shared/EmptyState';
import MoveToProjectButton from '../shared/MoveToProjectButton';
import Icon, { type IconName } from '../shared/Icon';
import RowActionButton from '../shared/RowActionButton';
import HubTabs, { CONNECTIONS_TABS } from '../HubTabs';
import { uiConfirm } from '../../ui/dialog';
import { usePageContext } from '../../hooks/usePageContext';

interface Credential {
  id: string;
  name: string;
  type: string;
  created_at: string;
  created_by?: string;           // user who saved the credential (email or display name)
  updated_at?: string;           // last modification timestamp
  updated_by?: string;           // user who last modified
  last_used?: string;            // last time a pipeline read this credential
  environment?: 'dev' | 'prod' | 'all';
  expires_at?: string;
  description?: string;
  username?: string;             // convenience copy of the username field for list display
  // Source: where the secret actually lives (planned vault integration,
  // Apr 18). `local` = F-Pulse-managed (current behavior). The other
  // values surface in the UI but fallback to `local` until the backend
  // vault adapter lands. See feedback on BYO Key Vault.
  source?: 'local' | 'builtin_vault' | 'azure_kv' | 'aws_sm' | 'hashi_vault' | 'gcp_sm';
}

function isCredExpired(c: Credential): boolean {
  if (!c.expires_at) return false;
  return new Date(c.expires_at) < new Date();
}

function credExpiresWithinDays(c: Credential, days: number): boolean {
  if (!c.expires_at) return false;
  const end = new Date(c.expires_at);
  const now = new Date();
  const diff = (end.getTime() - now.getTime()) / (1000 * 60 * 60 * 24);
  return diff > 0 && diff <= days;
}

function daysUntilExpiry(c: Credential): number | null {
  if (!c.expires_at) return null;
  const diff = (new Date(c.expires_at).getTime() - new Date().getTime()) / (1000 * 60 * 60 * 24);
  return Math.ceil(diff);
}

function formatDate(d: string): string {
  if (!d) return '';
  return new Date(d).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

/* Tags for visual grouping — just labels, no schema enforcement.
   Icons are line-art SVGs from the shared Icon set so they render
   consistently regardless of OS/browser font (the previous emoji
   glyphs degraded to monochrome boxes on systems without an emoji
   font). */
const CREDENTIAL_TAGS: Array<{ value: string; label: string; icon: IconName; color: string }> = [
  { value: 'database', label: 'Database', icon: 'database', color: '#336791' },
  { value: 'cloud', label: 'Cloud / Storage', icon: 'cloud', color: '#FF9900' },
  { value: 'api', label: 'API / Service', icon: 'globe', color: '#0ea5e9' },
  // v32 — AI provider keys (Anthropic/OpenAI/OpenRouter/…). Importable
  // from Insights → AI Provider via the "Use a saved credential" option.
  { value: 'ai_provider', label: 'AI Provider', icon: 'activity', color: '#8b5cf6' },
  { value: 'messaging', label: 'Messaging', icon: 'zap', color: '#a855f7' },
  { value: 'email', label: 'Email', icon: 'mail', color: '#ec4899' },
  { value: 'warehouse', label: 'Data Warehouse', icon: 'bar-chart', color: '#4285F4' },
  { value: 'other', label: 'Other', icon: 'key', color: '#94a3b8' },
];

interface FormField {
  key: string;
  value: string;
  sensitive: boolean;
}

const CRED_COLUMNS: TColumn[] = [
  { key: 'name',        label: 'Name',         default: true,  group: 'core' },
  { key: 'category',    label: 'Category',     default: true,  group: 'core' },
  { key: 'environment', label: 'Environment',  default: true,  group: 'core' },
  { key: 'source',      label: 'Source',       default: true,  group: 'core' },
  // Z25 (2026-05-23) — Used by N connections. Backed by GET /api/credentials/usage.
  { key: 'used_by',     label: 'Used by',      default: true,  group: 'core' },
  { key: 'username',    label: 'Username',     default: true,  group: 'identity' },
  { key: 'createdBy',   label: 'Created By',   default: true,  group: 'identity' },
  { key: 'created',     label: 'Created',      default: true,  group: 'audit' },
  { key: 'expiry',      label: 'Expiry',       default: true,  group: 'audit' },
  { key: 'lastUsed',    label: 'Last Used',    default: false, group: 'audit' },
  { key: 'updatedAt',   label: 'Last Modified',default: false, group: 'audit' },
  { key: 'updatedBy',   label: 'Modified By',  default: false, group: 'audit' },
  { key: 'description', label: 'Description',  default: false, group: 'details' },
];
const CRED_GROUPS: TColumnGroup[] = [
  { key: 'core',     label: 'Core',     icon: '◆' },
  { key: 'identity', label: 'Identity', icon: '◈' },
  { key: 'audit',    label: 'Audit',    icon: '◇' },
  { key: 'details',  label: 'Details',  icon: '○' },
];

/**
 * Human-readable label for where this credential's secret lives.
 * `local` = F-Pulse-managed encryption (default today).
 * Other values are surfaced for the planned BYO-vault integration —
 * until the backend adapter ships, the label is the only visible
 * difference. See the Apr 18 Key Vault discussion.
 */
const SOURCE_LABELS: Record<string, { label: string; color: string; bg: string }> = {
  local:         { label: 'Local',          color: '#64748b', bg: 'bg-slate-50 border-slate-200' },
  builtin_vault: { label: 'F-Pulse Vault',  color: '#0369a1', bg: 'bg-sky-50 border-sky-200' },
  azure_kv:      { label: 'Azure Key Vault', color: '#0078d4', bg: 'bg-blue-50 border-blue-200' },
  aws_sm:        { label: 'AWS Secrets Mgr', color: '#ff9900', bg: 'bg-amber-50 border-amber-200' },
  hashi_vault:   { label: 'HashiCorp Vault', color: '#000000', bg: 'bg-slate-100 border-slate-300' },
  gcp_sm:        { label: 'GCP Secret Mgr',  color: '#34a853', bg: 'bg-emerald-50 border-emerald-200' },
};

/**
 * Z25 (2026-05-23) — "Used by" pill + drilldown popover for the
 * Credentials table. Shows which connections depend on this credential.
 *
 * Source of truth: `GET /api/credentials/usage` returns
 *   { credential_id: [{ connection_id, name, type }] }
 */
type CredConnectionRef = { connection_id: string; name: string; type?: string };
function CredUsedByPill({
  connections,
  credName,
  onOpenConnections,
}: {
  connections: CredConnectionRef[];
  credName: string;
  onOpenConnections: () => void;
}) {
  const [open, setOpen] = useState(false);
  const count = connections.length;
  if (!count) {
    return <span className="text-slate-300 text-xs">—</span>;
  }
  return (
    <>
      <button
        onClick={(e) => { e.stopPropagation(); setOpen(true); }}
        className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-semibold rounded-full bg-blue-50 text-blue-700 hover:bg-blue-100 transition-colors"
        title={`Used by ${count} connection${count === 1 ? '' : 's'}`}
      >
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
          <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
          <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
        </svg>
        {count}
      </button>
      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-sm"
          onClick={(e) => { if (e.target === e.currentTarget) setOpen(false); }}
        >
          <div className="bg-white rounded-2xl shadow-2xl border border-slate-200/60 w-[460px] max-w-[95vw] overflow-hidden">
            <div className="px-5 py-4 border-b border-slate-200/70 bg-gradient-to-b from-slate-50 to-white">
              <div className="text-xs uppercase tracking-wide text-slate-500 font-semibold">Credential usage</div>
              <div className="text-base font-bold text-slate-900 mt-0.5 truncate">{credName}</div>
              <div className="text-xs text-slate-500 mt-1">
                {count} connection{count === 1 ? '' : 's'} reference{count === 1 ? 's' : ''} this credential.
              </div>
            </div>
            <div className="px-5 py-3 max-h-[60vh] overflow-auto">
              <ul className="divide-y divide-slate-100">
                {connections.map((c, i) => (
                  <li
                    key={`${c.connection_id}_${i}`}
                    className="flex items-center justify-between py-2.5 gap-3"
                  >
                    <div className="min-w-0 flex items-center gap-2">
                      <div className="text-sm font-medium text-slate-900 truncate">{c.name}</div>
                      {c.type && (
                        <span className="text-[10px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 border border-slate-200">
                          {c.type}
                        </span>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
            <div className="px-5 py-3 border-t border-slate-200/70 bg-slate-50/60 flex justify-end gap-2">
              <button
                onClick={() => setOpen(false)}
                className="px-4 py-2 text-sm font-medium rounded-lg text-slate-600 hover:bg-slate-200/70"
              >
                Close
              </button>
              <button
                onClick={() => { onOpenConnections(); setOpen(false); }}
                className="px-4 py-2 text-sm font-medium rounded-lg bg-blue-50 text-blue-700 border border-blue-200 hover:bg-blue-100"
              >
                Open Connections →
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

export default function CredentialsPage({ projectId, projectName = '', onClearProject, onGoToProjects, environment = 'dev', tier = 'free' }: { projectId?: string | null; projectName?: string; onClearProject?: () => void; onGoToProjects?: () => void; environment?: 'dev' | 'prod'; tier?: string } = {}) {
  const [credentials, setCredentials] = useState<Credential[]>([]);
  // Master-detail drawer state — click a row → drawer opens with the
  // credential's metadata + actions (Edit / Delete). The credential value
  // itself is NEVER shown here (Fernet-encrypted at rest, decrypted only
  // at run-time in the worker process).
  const [detailCred, setDetailCred] = useState<Credential | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const canCreate = useCan('create', environment);
  const canDelete = useCan('delete', environment);
  const [formName, setFormName] = useState('');
  const [formTag, setFormTag] = useState('other');
  const [formExpiry, setFormExpiry] = useState('');
  // Source picker (Apr 18): user chooses where the secret actually lives.
  // `local` is immediate; the vault options require a provider to be
  // configured first (see Admin → Vault Providers, planned).
  const [formSource, setFormSource] = useState<Credential['source']>('local');
  // Environment scope on a credential. 'dev' / 'prod' isolate; 'all' makes
  // it visible in both. Defaults to the current page environment so a
  // credential created while looking at DEV only appears in DEV.
  const [formEnvironment, setFormEnvironment] = useState<'dev' | 'prod' | 'all'>(environment || 'dev');
  const [formVaultReference, setFormVaultReference] = useState('');
  // When set, the Add modal switches to "Edit" mode and updates the
  // target credential instead of creating a new one. Fields are
  // pre-filled from the credential being edited.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [formFields, setFormFields] = useState<FormField[]>([
    { key: 'Username', value: '', sensitive: false },
    { key: 'Password', value: '', sensitive: true },
  ]);
  const [loading, setLoading] = useState(true);
  const colState = useTableColumns('fpulse_credentials_cols', CRED_COLUMNS);
  const [searchQuery, setSearchQuery] = useState('');

  // Z25 (2026-05-23) — lineage: which connections reference each credential.
  // Shape: { cred_id: [{ connection_id, name, type }, ...] }. Loaded in
  // parallel with the credential list so the Used-by column populates
  // on first paint instead of fetching per-row.
  const [usageMap, setUsageMap] = useState<Record<string, CredConnectionRef[]>>({});

  const loadCredentials = async () => {
    setLoading(true);
    // Safety net — never let the spinner run forever if the backend
    // hangs. If no response in 15s, drop to an empty list so the user
    // can still use the "+ Add Credential" flow. The actual network
    // error (if any) still surfaces via the catch branch below.
    const safety = setTimeout(() => setLoading(false), 15000);
    try {
      const params: { project_id?: string } = {};
      if (projectId) params.project_id = projectId;
      const [c, usage] = await Promise.all([
        api.listCredentials(params),
        api.get<Record<string, CredConnectionRef[]>>('/api/credentials/usage').catch(() => ({})),
      ]);
      clearTimeout(safety);
      setCredentials(Array.isArray(c) ? c : []);
      setUsageMap((usage as Record<string, CredConnectionRef[]>) || {});
    } catch {
      clearTimeout(safety);
      toast.error('Failed to load credentials');
      setCredentials([]);
    }
    setLoading(false);
  };

  useEffect(() => {
    loadCredentials();
  }, [projectId]);

  // Notify about expired or soon-expiring credentials.
  // 2026-05-19 (P1 #9 of PAGE_BY_PAGE_AUDIT.md): previously these toasts
  // fired on EVERY successful list load — a refresh was enough to
  // re-display both warnings, so after ~8 refreshes users muted credential
  // notifications wholesale. We now key the notify pass by the SET of
  // expired/expiring credential IDs and skip when the same set fired
  // earlier in this browser-tab session. A user who actually adds or
  // resolves a credential changes the set and re-trips the warning; a
  // simple page refresh does not.
  const [notifiedExpirySignature, setNotifiedExpirySignature] = useState<string>('');
  useEffect(() => {
    if (credentials.length === 0) return;
    const expired = credentials.filter(c => isCredExpired(c));
    const expiringSoon = credentials.filter(c => credExpiresWithinDays(c, 7));
    // Stable signature: sorted ids of {expired} ∪ {expiringSoon}.
    const signature = [
      'E:' + expired.map(c => c.id).sort().join(','),
      'S:' + expiringSoon.map(c => c.id).sort().join(','),
    ].join('|');
    if (signature === notifiedExpirySignature) return;
    setNotifiedExpirySignature(signature);
    if (expired.length > 0) {
      toast.error(
        `${expired.length} credential${expired.length > 1 ? 's' : ''} expired`,
        expired.map(c => c.name).join(', ') + ' — please renew to avoid pipeline failures'
      );
    }
    if (expiringSoon.length > 0) {
      toast.warning(
        `${expiringSoon.length} credential${expiringSoon.length > 1 ? 's' : ''} expiring soon`,
        expiringSoon.map(c => {
          const d = daysUntilExpiry(c);
          return `${c.name} (${d} day${d !== 1 ? 's' : ''})`;
        }).join(', ')
      );
    }
  }, [credentials, notifiedExpirySignature]);

  const resetForm = () => {
    setFormName('');
    setFormTag('other');
    setFormEnvironment(environment || 'dev');
    setFormExpiry('');
    setFormSource('local');
    setFormVaultReference('');
    setEditingId(null);
    setFormFields([
      { key: 'Username', value: '', sensitive: false },
      { key: 'Password', value: '', sensitive: true },
    ]);
  };

  // Picking a category can seed sensible field rows. Currently only the
  // AI Provider category does this (provider / api_key / base_url) — and
  // only on a fresh create when the user hasn't entered any field values,
  // so it never clobbers in-progress input or an edit. The `api_key` row
  // is the one the AI Provider page imports at request time.
  const selectTag = (value: string) => {
    setFormTag(value);
    if (value === 'ai_provider' && !editingId) {
      const untouched = formFields.every((f) => !f.key.trim() && !f.value.trim())
        || (formFields.length === 2
            && formFields[0].key === 'Username'
            && formFields[1].key === 'Password'
            && !formFields[0].value && !formFields[1].value);
      if (untouched) {
        setFormFields([
          { key: 'provider', value: '', sensitive: false },
          { key: 'api_key', value: '', sensitive: true },
          { key: 'base_url', value: '', sensitive: false },
        ]);
      }
    }
  };

  /**
   * Switch the Add modal into Edit mode for a specific credential.
   * Pre-fills name, tag, expiry, source, and fields from the credential
   * record. On Save, the save handler looks at `editingId` to decide
   * between POST /credentials (create) and PUT /credentials/:id (update).
   * Note: the backend masks sensitive field values on read, so those
   * appear as placeholders the user can overwrite. Leaving them unchanged
   * means the server keeps the existing stored value.
   */
  const openEditCredential = (cred: Credential) => {
    setEditingId(cred.id);
    setFormName(cred.name);
    setFormTag(cred.type || 'other');
    setFormEnvironment((cred.environment as 'dev' | 'prod' | 'all') || 'all');
    setFormExpiry(cred.expires_at ? cred.expires_at.slice(0, 10) : '');
    setFormSource(cred.source || 'local');
    setFormVaultReference((cred as any).vault_reference || '');
    // Reconstruct field rows from the masked config. Sensitive fields are
    // shown as placeholder strings so the user sees they exist without
    // the value leaking.
    const anyCred = cred as any;
    const cfg: Record<string, any> = anyCred.config || {};
    const fields: FormField[] = Object.entries(cfg).map(([k, v]) => {
      const sensitive = /password|secret|token|key/i.test(k) && !/client_id|access_key_id|api_key_id/i.test(k);
      return { key: k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()), value: String(v ?? ''), sensitive };
    });
    setFormFields(fields.length > 0 ? fields : [
      { key: 'Username', value: '', sensitive: false },
      { key: 'Password', value: '', sensitive: true },
    ]);
    setDetailCred(null);
    setShowCreate(true);
  };

  const handleCreate = async () => {
    if (!formName.trim()) return;
    const config: Record<string, any> = {};
    for (const f of formFields) {
      if (f.key.trim() && f.value.trim()) {
        config[f.key.trim().toLowerCase().replace(/ /g, '_')] = f.value.trim();
      }
    }
    try {
      const payload: any = { name: formName.trim(), type: formTag, config, environment: formEnvironment };
      if (formExpiry) payload.expires_at = formExpiry;
      // Source & vault reference — surface on the payload so the backend
      // can route to the right storage adapter. Until the vault adapters
      // land, only `local` is fully honored; other values are stored on
      // the credential record as metadata for UI display.
      if (formSource && formSource !== 'local') {
        payload.source = formSource;
        payload.vault_reference = formVaultReference.trim();
      } else {
        payload.source = 'local';
      }
      if (editingId) {
        // Edit mode — PUT updates the existing credential.
        await api.updateCredential(editingId, payload);
        toast.success('Credential updated', formName.trim());
      } else {
        await api.createCredential(payload);
        toast.success('Credential saved', formExpiry ? `Expires on ${formatDate(formExpiry)}` : undefined);
      }
      loadCredentials();
    } catch (err) {
      console.error('Create credential error:', err);
      toast.error('Failed to save credential');
    }
    setShowCreate(false);
    resetForm();
  };

  const handleDelete = async (id: string) => {
    // Credentials are pure secret material — once deleted, pipelines that
    // reference this credential will start failing at run time with an
    // unrecognised-credential error and the secret value itself is gone
    // (decryption is one-way at run time). Force an explicit confirm.
    // Per docs/PAGE_BY_PAGE_AUDIT.md (P0 #2, 2026-05-19).
    //
    // 2026-06-04 — usage-aware enhancement. The page already loads
    // /api/credentials/usage into usageMap (keyed by credential_id ->
    // CredConnectionRef[]). Surface the affected connections in the
    // dialog so the user sees concrete impact (which connections will
    // break, and transitively which pipelines those connections feed)
    // rather than abstract "any pipeline." Matches the Storage gold-
    // standard pattern (StoragePage.onDeleteFile).
    const cred = credentials.find((c) => c.id === id);
    const name = cred?.name || 'this credential';
    const usedBy = (usageMap as Record<string, CredConnectionRef[]>)?.[id] || [];
    const usageBlurb =
      usedBy.length > 0
        ? ` ${usedBy.length} connection${usedBy.length === 1 ? '' : 's'} reference this credential (${usedBy
            .slice(0, 3)
            .map((c) => c?.name || c?.connection_id || 'unnamed')
            .join(', ')}${usedBy.length > 3 ? `, +${usedBy.length - 3} more` : ''}). Any pipeline using those connections will fail on next run.`
        : ' No connections reference this credential today.';
    const ok = await uiConfirm({
      title: `Delete "${name}"?`,
      message: `The secret value cannot be recovered.${usageBlurb}`,
      confirmLabel: 'Delete',
      destructive: true,
    });
    if (!ok) return;
    try {
      await api.deleteCredential(id);
      loadCredentials();
      toast.success('Credential deleted');
    } catch (err) {
      console.error('Delete credential error:', err);
    }
  };

  const updateField = (idx: number, update: Partial<FormField>) => {
    setFormFields(prev => prev.map((f, i) => i === idx ? { ...f, ...update } : f));
  };

  const removeField = (idx: number) => {
    setFormFields(prev => prev.filter((_, i) => i !== idx));
  };

  const addField = () => {
    setFormFields(prev => [...prev, { key: '', value: '', sensitive: false }]);
  };

  // Strict env filter: a credential must explicitly be tagged `all` or match
  // the current environment. Untagged rows (legacy data with no `environment`
  // field) are hidden here — edit them to pick dev/prod/all, or run a
  // one-time backfill in the store. This enforces the no-leak rule
  // requested on 2026-04-21: PROD credentials never appear in DEV and
  // vice versa.
  const envFiltered = environment
    ? credentials.filter(c => c.environment === 'all' || c.environment === environment)
    : credentials;
  const filteredCredentials = searchQuery.trim()
    ? envFiltered.filter(c => { const q = searchQuery.toLowerCase(); return (c.name || '').toLowerCase().includes(q) || (c.type || '').toLowerCase().includes(q) || (c.id || '').toLowerCase().includes(q); })
    : envFiltered;

  const tagInfo = (type: string) =>
    CREDENTIAL_TAGS.find((t) => t.value === type) || CREDENTIAL_TAGS[CREDENTIAL_TAGS.length - 1];

  // OSS-4 (2026-05-19) — publish credential context (IDs only, never
  // names or secret material) so the Copilot can answer "which
  // credential is the postgres pipeline using?" without re-fetching.
  usePageContext({
    page: 'credentials',
    visible_ids: filteredCredentials.map((c) => c.id),
    filters: { search: searchQuery || null },
  });

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Z31 (2026-05-23) — push-don't-overlay for the credential detail
          drawer. `--fp-drawer-w` is published by the open DetailDrawer
          (pushContent=true below). Padding-right reflows the list +
          sticky header so the rightmost columns aren't clipped. */}
      <div
        className="flex-1 overflow-auto bg-canvas-bg"
        style={{ paddingRight: 'var(--fp-drawer-w, 0px)', transition: 'padding-right 250ms ease-out' }}
      >
      {/* Header — 3-col grid (matches Insights / Settings):
          • LEFT:   page title cluster ("Credentials")
          • CENTER: HubTabs — sibling tabs in the Connections family
          • RIGHT:  page-specific actions (Add Credential) */}
      <PageHeader
        environment={environment}
        icon={(
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-amber-500">
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
            <path d="M7 11V7a5 5 0 0 1 10 0v4" />
          </svg>
        )}
        title="Credentials"
        titleAccessory={<TierChip tier={tier} environment={environment} />}
        subtitle={environment === 'prod'
          ? 'Production secrets, API keys, and encrypted credentials'
          : 'Store and manage credentials for your pipelines'}
        tabs={(
          <HubTabs
            tabs={CONNECTIONS_TABS}
            active="credentials"
            onNavigate={(p) => { window.location.hash = p; }}
            environment={environment}
          />
        )}
        actions={canCreate ? (
          <button
            onClick={() => { resetForm(); setDetailCred(null); setShowCreate(true); }}
            className="px-4 py-2 text-white text-sm font-bold rounded-lg shadow-sm hover:shadow-md transition-all flex items-center gap-2"
            style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            Add Credential
          </button>
        ) : undefined}
      />

      <ProjectContextBar
        projectId={projectId}
        projectName={projectName}
        onGoToProjects={onGoToProjects || (() => {})}
        onClear={onClearProject || (() => {})}
      />

      <div className="w-full max-w-[1500px] mx-auto px-8 py-6">
        {/* Create modal */}
        {showCreate && (
          <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" onClick={() => setShowCreate(false)}>
            <div className="bg-white rounded-2xl shadow-2xl w-full max-w-3xl max-h-[85vh] overflow-auto" onClick={(e) => e.stopPropagation()}>
              <div className="px-6 py-4 border-b border-slate-200 flex items-center justify-between">
                <div>
                  <h2 className="text-lg font-bold text-slate-800">Add Credential</h2>
                  <p className="text-xs text-slate-400 mt-0.5">Store any username, password, API key, token, or secret</p>
                </div>
                <button onClick={() => { setShowCreate(false); resetForm(); }} className="text-slate-400 hover:text-slate-600">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                </button>
              </div>

              <div className="p-6 space-y-5">
                {/* Name */}
                <div>
                  <label className="block text-xs font-semibold text-slate-600 mb-1.5">Credential name</label>
                  <input
                    type="text"
                    value={formName}
                    onChange={(e) => setFormName(e.target.value)}
                    placeholder="e.g. Production DB, Stripe API, AWS Keys"
                    className="w-full px-3 py-2.5 text-sm text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-300 placeholder:text-slate-400"
                    autoFocus
                  />
                </div>

                {/* Source — where the secret is stored.
                    DEV (Apr 18 user request): only local sources are
                    shown — "Local (F-Pulse)" (encrypted DB) and the
                    built-in "F-Pulse Vault" (local-to-the-app, with
                    backup destination configured in Admin → Vaults).
                    External enterprise vaults (Azure KV / AWS SM /
                    HashiCorp / GCP) are hidden in DEV and only appear
                    in PROD — they're never configured per-dev-laptop,
                    they live on the PROD server. */}
                {/* 2026-06-02 layout: Secret source + Environment sit
                    side-by-side in a 2-column grid. In OSS Free, Secret
                    source has only the "Local (F-Pulse)" card and
                    Environment is just a "DEV" pill — both half-width
                    blocks read cleanly. Wraps to stacked on narrow
                    viewports via sm: breakpoint. */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-semibold text-slate-600 mb-1.5">
                    Secret source <span className="font-normal text-slate-400">(where the value lives)</span>
                  </label>
                  <div className="grid grid-cols-1 gap-2">
                    {(([
                      { key: 'local',         label: 'Local (F-Pulse)',  hint: 'Encrypted in F-Pulse DB', color: '#64748b', envs: ['dev', 'prod'] as const },
                    ] as const).filter(s => s.envs.includes(environment as 'dev' | 'prod'))).map(s => (
                      <button
                        key={s.key}
                        onClick={() => setFormSource(s.key as Credential['source'])}
                        className={`text-left px-3 py-2.5 rounded-lg border transition-all ${
                          formSource === s.key
                            ? 'bg-blue-50 border-blue-400 ring-2 ring-blue-200'
                            : 'bg-white border-slate-200 hover:border-slate-300 hover:bg-slate-50'
                        }`}
                      >
                        <div className="flex items-center gap-2">
                          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: s.color }} />
                          <span className="text-xs font-bold text-slate-800">{s.label}</span>
                        </div>
                        <p className="text-xs text-slate-500 mt-0.5 leading-tight">{s.hint}</p>
                      </button>
                    ))}
                  </div>
                  {/* When a vault source is chosen, ask for the reference path.
                      2026-05-19 (P2 #9 of PAGE_BY_PAGE_AUDIT.md): this branch is
                      currently unreachable on OSS because the picker above only
                      offers `local`. We keep the code so that when the vault
                      adapters (Azure KV / AWS SM / Hashi Vault / GCP SM) ship,
                      enabling them on the picker is the only change needed —
                      the form path here already handles the reference input.
                      Until then the UI doesn't tease vault options that aren't
                      backed by real adapters. */}
                  {formSource && formSource !== 'local' && (
                    <div className="mt-2">
                      <label className="block text-xs font-bold text-slate-500 uppercase tracking-wider mb-1">
                        Vault reference / secret name
                      </label>
                      <input
                        type="text"
                        value={formVaultReference}
                        onChange={(e) => setFormVaultReference(e.target.value)}
                        placeholder={
                          formSource === 'azure_kv' ? 'e.g. prod-db-password' :
                          formSource === 'aws_sm'   ? 'e.g. arn:aws:secretsmanager:…:secret:prod/db-password' :
                          formSource === 'hashi_vault' ? 'e.g. /secret/data/prod/db' :
                          formSource === 'gcp_sm'   ? 'e.g. projects/…/secrets/prod-db-password' :
                          'e.g. prod-db-password'
                        }
                        className="w-full px-3 py-2 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300 placeholder:text-slate-400"
                      />
                      <p className="text-xs text-slate-400 mt-1">
                        F-Pulse will fetch the secret from your vault at pipeline run time. The value never transits or persists through F-Pulse.
                      </p>
                    </div>
                  )}
                </div>

                {/* Environment scope — enforces the no-leak rule between
                    DEV and PROD. Default is the current page env; admins
                    can pick 'Both' explicitly when a credential is genuinely
                    shared (e.g. the same service account used across tiers).
                    2026-06-02: PROD and Both are gated to F-Pulse+ — OSS
                    Free has no PROD environment, so showing those toggles
                    in OSS was a confusing "you can't actually use this"
                    surface. In OSS Free, render a single DEV pill (no
                    toggle since there's nothing to toggle to) + a small
                    pointer to the upgrade path. */}
                <div>
                  <label className="block text-xs font-semibold text-slate-600 mb-1.5">
                    Environment <span className="font-normal text-slate-400">(visibility)</span>
                  </label>
                  {tier === 'plus' ? (
                    <>
                      <div className="flex gap-1.5">
                        {([
                          { value: 'dev',  label: 'DEV only',  color: '#10b981', desc: 'Visible only in DEV' },
                          { value: 'prod', label: 'PROD only', color: '#ef4444', desc: 'Visible only in PROD' },
                          { value: 'all',  label: 'Both',      color: '#64748b', desc: 'Visible in both DEV and PROD' },
                        ] as const).map(opt => (
                          <button
                            key={opt.value}
                            type="button"
                            onClick={() => setFormEnvironment(opt.value)}
                            title={opt.desc}
                            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-all flex items-center gap-1.5 border ${
                              formEnvironment === opt.value
                                ? 'text-white shadow-sm border-transparent'
                                : 'bg-slate-50 text-slate-600 hover:bg-slate-100 border-slate-200'
                            }`}
                            style={formEnvironment === opt.value ? { background: opt.color } : undefined}
                          >
                            {opt.label}
                          </button>
                        ))}
                      </div>
                      <p className="text-xs text-slate-400 mt-1">
                        PROD credentials never appear in DEV and vice versa. Pick "Both" only for shared secrets.
                      </p>
                    </>
                  ) : (
                    <>
                      <div className="flex items-center gap-2">
                        <span
                          className="px-3 py-1.5 rounded-lg text-xs font-semibold text-white shadow-sm border border-transparent inline-flex items-center gap-1.5"
                          style={{ background: '#10b981' }}
                        >
                          DEV
                        </span>
                        <span className="text-xs text-slate-400">
                          OSS Free runs in DEV only.
                        </span>
                      </div>
                      <p className="text-xs text-slate-400 mt-1">
                        PROD environment (and the two-gate promotion flow) is{' '}
                        <a
                          href="https://hybridyn.com/f-pulse"
                          target="_blank"
                          rel="noopener noreferrer"
                          className="underline text-indigo-600 hover:text-indigo-800"
                        >
                          F-Pulse+ only
                        </a>
                        .
                      </p>
                    </>
                  )}
                </div>
                </div>  {/* end Secret-source + Environment 2-col grid */}

                {/* Category tag — saved as `type` on the credential, surfaces
                    in the Category column + participates in the search-bar
                    filter. 2026-06-02: strengthened selected-state visual
                    feedback (ring + checkmark dot) because the previous
                    "background-color only" treatment looked ambiguous when
                    the chosen category had a dark brand colour (e.g.
                    Messaging was `#231F20` which read as "disabled").
                    Messaging swapped to `#a855f7` (violet) for the same
                    reason. */}
                <div>
                  <label className="block text-xs font-semibold text-slate-600 mb-1.5">Category <span className="font-normal text-slate-400">(for organization)</span></label>
                  <div className="flex flex-wrap gap-1.5">
                    {CREDENTIAL_TAGS.map(t => {
                      const active = formTag === t.value;
                      return (
                        <button
                          key={t.value}
                          onClick={() => selectTag(t.value)}
                          aria-pressed={active}
                          className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all flex items-center gap-1.5 border ${
                            active
                              ? 'text-white shadow-sm ring-2 ring-offset-1 ring-offset-white border-transparent'
                              : 'bg-slate-50 text-slate-600 hover:bg-slate-100 border-slate-200'
                          }`}
                          style={active ? { background: t.color, ['--tw-ring-color' as any]: t.color } : undefined}
                        >
                          {active && (
                            <svg
                              width="11" height="11" viewBox="0 0 24 24"
                              fill="none" stroke="currentColor"
                              strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round"
                              aria-hidden="true"
                            >
                              <polyline points="20 6 9 17 4 12" />
                            </svg>
                          )}
                          {!active && <Icon name={t.icon} size={12} />}
                          {t.label}
                        </button>
                      );
                    })}
                  </div>
                </div>

                {/* Dynamic key-value fields */}
                <div>
                  <label className="block text-xs font-semibold text-slate-600 mb-1.5">Fields</label>
                  <div className="space-y-2">
                    {formFields.map((field, idx) => (
                      <div key={idx} className="flex items-center gap-2">
                        {/* Field name */}
                        <input
                          type="text"
                          value={field.key}
                          onChange={(e) => updateField(idx, { key: e.target.value })}
                          placeholder="Field name"
                          className="w-[140px] px-3 py-2 text-sm text-slate-700 bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-300 placeholder:text-slate-400"
                        />
                        {/* Field value */}
                        <input
                          type={field.sensitive ? 'password' : 'text'}
                          value={field.value}
                          onChange={(e) => updateField(idx, { value: e.target.value })}
                          placeholder={field.sensitive ? '••••••' : 'Value'}
                          className="flex-1 px-3 py-2 text-sm text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-300 placeholder:text-slate-400"
                        />
                        {/* Toggle sensitive */}
                        <button
                          onClick={() => updateField(idx, { sensitive: !field.sensitive })}
                          className={`w-8 h-8 rounded-lg flex items-center justify-center transition-colors shrink-0 ${
                            field.sensitive ? 'text-amber-500 bg-amber-50 border border-amber-200' : 'text-slate-400 bg-slate-50 border border-slate-200 hover:bg-slate-100'
                          }`}
                          title={field.sensitive ? 'Sensitive (hidden)' : 'Click to mark as sensitive'}
                        >
                          {field.sensitive ? (
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94" /><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19" /><line x1="1" y1="1" x2="23" y2="23" /></svg>
                          ) : (
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" /></svg>
                          )}
                        </button>
                        {/* Remove */}
                        <button
                          onClick={() => removeField(idx)}
                          className="w-8 h-8 rounded-lg flex items-center justify-center text-slate-300 hover:text-red-500 hover:bg-red-50 transition-all shrink-0"
                          title="Remove field"
                        >
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                        </button>
                      </div>
                    ))}
                    {/* Add field button */}
                    <button
                      onClick={addField}
                      className="flex items-center gap-1.5 text-xs font-semibold text-amber-600 hover:text-amber-700 px-3 py-2 rounded-lg border-2 border-dashed border-amber-200 hover:border-amber-400 hover:bg-amber-50/50 transition-all w-full justify-center"
                    >
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
                      Add field
                    </button>
                  </div>
                  <p className="text-xs text-slate-400 mt-1.5">
                    Add any fields you need — host, port, client ID, secret, token, connection string, etc.
                  </p>
                </div>

                {/* Quick-add common field buttons */}
                <div>
                  <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1.5">Quick add</label>
                  <div className="flex flex-wrap gap-1.5">
                    {['Host', 'Port', 'Client ID', 'Client Secret', 'API Key', 'Token', 'Connection String', 'Region', 'Endpoint'].map(name => {
                      const exists = formFields.some(f => f.key.toLowerCase() === name.toLowerCase());
                      if (exists) return null;
                      const isSensitive = ['Client Secret', 'API Key', 'Token', 'Connection String'].includes(name);
                      return (
                        <button
                          key={name}
                          onClick={() => setFormFields(prev => [...prev, { key: name, value: '', sensitive: isSensitive }])}
                          className="text-xs px-2 py-1 rounded-lg bg-slate-50 text-slate-500 border border-slate-200 hover:bg-amber-50 hover:text-amber-700 hover:border-amber-200 transition-colors"
                        >
                          + {name}
                        </button>
                      );
                    })}
                  </div>
                </div>

                {/* Expiry date */}
                <div>
                  <label className="block text-xs font-semibold text-slate-600 mb-1.5">
                    Expiry date <span className="font-normal text-slate-400">(optional)</span>
                  </label>
                  <input
                    type="date"
                    value={formExpiry}
                    min={new Date().toISOString().split('T')[0]}
                    onChange={(e) => setFormExpiry(e.target.value)}
                    className="w-full px-3 py-2.5 text-sm text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-300"
                  />
                  <p className="text-xs text-slate-400 mt-1">
                    {formExpiry
                      ? `You'll be notified 7 days before expiry (${formatDate(formExpiry)})`
                      : 'When does this credential expire? You\'ll be alerted before it does.'}
                  </p>
                </div>

                {/* Actions */}
                <div className="flex gap-2 pt-1">
                  <button
                    onClick={() => { setShowCreate(false); resetForm(); }}
                    className="px-4 py-2.5 text-sm text-slate-600 bg-slate-100 rounded-lg hover:bg-slate-200 transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleCreate}
                    disabled={!formName.trim() || formFields.filter(f => f.key.trim()).length === 0}
                    className="px-5 py-2.5 text-sm text-white font-semibold rounded-lg disabled:opacity-50 transition-all shadow-sm hover:shadow-md"
                    style={{ background: 'linear-gradient(135deg, #F5A623, #D4880A)' }}
                  >
                    Save Credential
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Credentials list */}
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <div className="w-6 h-6 border-2 border-indigo-300 border-t-transparent rounded-full animate-spin" />
          </div>
        ) : filteredCredentials.length === 0 ? (
          <EmptyState
            icon={
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
                <path d="M7 11V7a5 5 0 0 1 10 0v4" />
              </svg>
            }
            title="No credentials saved"
            body="Store usernames, passwords, API keys, tokens, and connection strings securely. Credentials are reusable across all your pipelines."
            primaryCta={canCreate ? { label: '+ Add First Credential', onClick: () => { resetForm(); setDetailCred(null); setShowCreate(true); } } : undefined}
            hint="Encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256). Decrypted only at run-time."
          />
        ) : (
          // Unified navy-table container (Apr 18):
          // TableToolbar (summary strip) and the table itself live INSIDE
          // the same bordered card so the navy strip + navy column
          // header read as one continuous header — no visible break
          // between them. Summary keeps its rounded-t; the table shares
          // the same bottom rounding via overflow-hidden on the wrapper.
          <div className="rounded-lg border border-slate-200 bg-white overflow-x-auto shadow-sm">
            <TableToolbar
              data={filteredCredentials}
              columns={CRED_COLUMNS}
              columnGroups={CRED_GROUPS}
              visibleColumns={colState.visibleColumns}
              activeColumnCount={colState.activeColumns.length}
              onToggleColumn={colState.toggleColumn}
              onResetDefaults={colState.resetToDefaults}
              onSelectAll={colState.selectAll}
              searchValue={searchQuery}
              onSearchChange={setSearchQuery}
              searchPlaceholder="Search credentials..."
              exportRowBuilder={(c: Credential) => ({
                id: c.id,
                name: c.name,
                category: c.type,
                environment: c.environment || '',
                created: c.created_at,
                expiry: c.expires_at || '',
                lastUsed: c.last_used || '',
              })}
              exportFilename="credentials"
              recordLabel="credential"
            />
          <div className="overflow-x-auto">
            {/* border-collapse eliminates the default 2px border-spacing
                that otherwise leaves a hairline gap between the navy
                summary strip above and the navy thead gradient. */}
            <table className="w-full text-sm border-collapse">
              {/* Colored header — amber tint matches the Credentials icon
                  (key) and keeps the page's identity consistent. */}
              {/* Canonical table header — navy-blue gradient with amber text.
                  One consistent header style across every data table in the
                  app so users scan-read quickly; per-page identity is kept
                  in the banner above, not duplicated on every cell. */}
              <thead className="bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900 border-b-2 border-amber-400/40 text-xs uppercase tracking-wider text-amber-300 font-bold">
                <tr>
                  <th className="px-4 py-2.5 text-left">Name</th>
                  {colState.isVisible('category')    && <th className="px-3 py-2.5 text-left">Category</th>}
                  {colState.isVisible('environment') && <th className="px-3 py-2.5 text-left">Env</th>}
                  {colState.isVisible('source')      && <th className="px-3 py-2.5 text-left">Source</th>}
                  {colState.isVisible('used_by')     && <th className="px-3 py-2.5 text-left">Used by</th>}
                  {colState.isVisible('username')    && <th className="px-3 py-2.5 text-left">Username</th>}
                  {colState.isVisible('createdBy')   && <th className="px-3 py-2.5 text-left">Created By</th>}
                  {colState.isVisible('created')     && <th className="px-3 py-2.5 text-left">Created</th>}
                  {colState.isVisible('expiry')      && <th className="px-3 py-2.5 text-left">Expires</th>}
                  {colState.isVisible('lastUsed')    && <th className="px-3 py-2.5 text-left">Last Used</th>}
                  {colState.isVisible('updatedAt')   && <th className="px-3 py-2.5 text-left">Modified</th>}
                  {colState.isVisible('updatedBy')   && <th className="px-3 py-2.5 text-left">Modified By</th>}
                  {colState.isVisible('description') && <th className="px-3 py-2.5 text-left">Description</th>}
                  <th className="px-3 py-2.5 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 [&>tr:nth-child(even)]:bg-slate-50/50">
                {filteredCredentials.map((cred) => {
                  const info = tagInfo(cred.type);
                  const expired = isCredExpired(cred);
                  const expiringSoon = credExpiresWithinDays(cred, 7);
                  const daysLeft = daysUntilExpiry(cred);
                  const source = SOURCE_LABELS[cred.source || 'local'];
                  return (
                    <tr
                      key={cred.id}
                      onClick={() => setDetailCred(cred)}
                      className={`hover:bg-amber-50/60 transition-colors cursor-pointer ${expired ? 'bg-red-50/40 hover:bg-red-50/60' : expiringSoon ? 'bg-amber-50/30' : ''}`}
                    >
                      {/* Name + icon + badges */}
                      <td className="px-4 py-2.5 max-w-[300px]">
                        <div className="flex items-center gap-2.5 min-w-0">
                          <div
                            className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0"
                            style={{ background: `${info.color}15`, border: `1px solid ${info.color}30`, color: info.color }}
                          >
                            <Icon name={info.icon} size={16} />
                          </div>
                          <div className="min-w-0">
                            <div className="text-sm font-semibold text-slate-800 truncate flex items-center gap-1.5" title={cred.name}>
                              <span className="truncate">{cred.name}</span>
                              {expired && (
                                <span className="text-xs font-bold px-1.5 py-0.5 rounded-full bg-red-100 text-red-600 border border-red-200">Expired</span>
                              )}
                              {expiringSoon && (
                                <span className="text-xs font-bold px-1.5 py-0.5 rounded-full bg-amber-100 text-amber-600 border border-amber-200">
                                  {daysLeft}d
                                </span>
                              )}
                            </div>
                          </div>
                        </div>
                      </td>

                      {/* Category */}
                      {colState.isVisible('category') && (
                        <td className="px-3 py-2.5 text-slate-600 text-xs">
                          <span className="inline-flex items-center gap-1.5">
                            <span style={{ color: info.color }}>
                              <Icon name={info.icon} size={12} />
                            </span>
                            <span className="font-medium">{info.label}</span>
                          </span>
                        </td>
                      )}

                      {/* Environment */}
                      {colState.isVisible('environment') && (
                        <td className="px-3 py-2.5">
                          {cred.environment ? (
                            <span className={`text-xs font-semibold uppercase px-1.5 py-0.5 rounded-full ${
                              cred.environment === 'prod' ? 'bg-emerald-50 text-emerald-600' :
                              cred.environment === 'dev' ? 'bg-amber-50 text-amber-600' :
                              'bg-slate-50 text-slate-500'
                            }`}>
                              {cred.environment}
                            </span>
                          ) : <span className="text-slate-300 text-xs">—</span>}
                        </td>
                      )}

                      {/* Source */}
                      {colState.isVisible('source') && (
                        <td className="px-3 py-2.5">
                          {source ? (
                            <span
                              className={`text-xs font-semibold px-2 py-0.5 rounded-full border ${source.bg}`}
                              style={{ color: source.color }}
                            >
                              {source.label}
                            </span>
                          ) : <span className="text-slate-300 text-xs">—</span>}
                        </td>
                      )}

                      {/* Used by — Z25 (2026-05-23). Connection lineage:
                          click the pill to see which connections reference
                          this credential. */}
                      {colState.isVisible('used_by') && (
                        <td className="px-3 py-2.5" onClick={(e) => e.stopPropagation()}>
                          <CredUsedByPill
                            connections={usageMap[cred.id] || []}
                            credName={cred.name}
                            onOpenConnections={() => { window.location.hash = 'connections'; }}
                          />
                        </td>
                      )}

                      {/* Username */}
                      {colState.isVisible('username') && (
                        <td className="px-3 py-2.5 text-xs font-mono text-slate-700">
                          {cred.username || <span className="text-slate-300">—</span>}
                        </td>
                      )}

                      {/* Created By */}
                      {colState.isVisible('createdBy') && (
                        <td className="px-3 py-2.5 text-xs text-slate-700">
                          {cred.created_by || <span className="text-slate-300">—</span>}
                        </td>
                      )}

                      {/* Created */}
                      {colState.isVisible('created') && (
                        <td className="px-3 py-2.5 text-xs text-slate-600 tabular-nums">
                          {new Date(cred.created_at).toLocaleDateString()}
                        </td>
                      )}

                      {/* Expires */}
                      {colState.isVisible('expiry') && (
                        <td className={`px-3 py-2.5 text-xs tabular-nums ${expired ? 'text-red-600 font-semibold' : expiringSoon ? 'text-amber-600 font-semibold' : 'text-slate-600'}`}>
                          {cred.expires_at ? formatDate(cred.expires_at) : <span className="text-slate-300">—</span>}
                        </td>
                      )}

                      {/* Last Used */}
                      {colState.isVisible('lastUsed') && (
                        <td className="px-3 py-2.5 text-xs text-slate-600 tabular-nums">
                          {cred.last_used ? new Date(cred.last_used).toLocaleDateString() : <span className="text-slate-300">—</span>}
                        </td>
                      )}

                      {/* Modified */}
                      {colState.isVisible('updatedAt') && (
                        <td className="px-3 py-2.5 text-xs text-slate-600 tabular-nums">
                          {cred.updated_at ? new Date(cred.updated_at).toLocaleDateString() : <span className="text-slate-300">—</span>}
                        </td>
                      )}

                      {/* Modified By */}
                      {colState.isVisible('updatedBy') && (
                        <td className="px-3 py-2.5 text-xs text-slate-700">
                          {cred.updated_by || <span className="text-slate-300">—</span>}
                        </td>
                      )}

                      {/* Description */}
                      {colState.isVisible('description') && (
                        <td className="px-3 py-2.5 text-xs text-slate-600 max-w-xs truncate">
                          {cred.description || <span className="text-slate-300">—</span>}
                        </td>
                      )}

                      {/* Actions — Edit / Move / Delete. Each action
                          uses RowActionButton so size, resting colour, and
                          hover treatment stay consistent across rows and
                          across other list pages (Connections, Pipelines).

                          The "Test" affordance was removed (May 9 2026).
                          Credentials are pure secret material; testing
                          requires a host + port + protocol, which lives
                          on the Connection record. The Connections page
                          carries the real Test button — it resolves the
                          credential reference + connection target and
                          probes them together. The legacy
                          /credentials/{id}/test endpoint is kept for
                          backwards compatibility but no longer surfaced
                          here. */}
                      <td className="px-3 py-2.5 text-right">
                        <div className="flex items-center justify-end gap-1">
                          {canCreate && (
                            <RowActionButton
                              onClick={() => openEditCredential(cred)}
                              title="Edit credential"
                              tone="blue"
                            >
                              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
                              </svg>
                            </RowActionButton>
                          )}
                          {canCreate && (
                            <MoveToProjectButton
                              currentProjectId={(cred as any).project_id || ''}
                              allowGlobal
                              onMove={async (target) => {
                                try {
                                  await api.moveCredential(cred.id, target);
                                  toast.success('Credential moved');
                                  loadCredentials();
                                } catch (err: any) {
                                  toast.error('Move failed', err?.message || 'Could not move credential');
                                }
                              }}
                            />
                          )}
                          {canDelete && (
                            <RowActionButton
                              onClick={() => handleDelete(cred.id)}
                              title="Delete credential"
                              tone="red"
                            >
                              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <polyline points="3 6 5 6 21 6" /><path d="M19 6l-2 14H7L5 6" /><path d="M9 6V4h6v2" />
                              </svg>
                            </RowActionButton>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          </div>
        )}
      </div>
    </div>

    {/* Master-detail drawer (Phase 5 of the page-design audit).
        Click any credential row → drawer opens with metadata and
        Edit / Delete actions. The credential VALUE is never shown
        — only metadata. Fernet decryption happens only at run-time
        in the worker process, never in the UI. */}
    {detailCred && (() => {
      const t = tagInfo(detailCred.type);
      const expired = isCredExpired(detailCred);
      const expiringSoon = credExpiresWithinDays(detailCred, 7);
      const daysLeft = daysUntilExpiry(detailCred);
      return (
        <DetailDrawer
          open={!!detailCred}
          onClose={() => setDetailCred(null)}
          pushContent
          title={
            <div className="flex items-center gap-2.5">
              <div
                className="w-7 h-7 rounded-md flex items-center justify-center shrink-0"
                style={{ background: `${t.color}15`, border: `1px solid ${t.color}30`, color: t.color }}
              >
                <Icon name={t.icon} size={14} />
              </div>
              <span className="truncate">{detailCred.name}</span>
            </div>
          }
          subtitle={
            <span className="font-mono">{detailCred.id.slice(0, 16)} · {t.label}</span>
          }
          ariaLabel="Credential details"
          footer={
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setDetailCred(null)}
                className="px-3 py-1.5 text-[12px] font-semibold rounded-lg border border-slate-200 bg-white text-slate-700 hover:bg-slate-50 transition-colors"
              >
                Close
              </button>
              <button
                onClick={() => { openEditCredential(detailCred); setDetailCred(null); }}
                className="px-3 py-1.5 text-[12px] font-semibold rounded-lg bg-amber-50 text-amber-800 border border-amber-200 hover:bg-amber-100 transition-colors"
              >
                Edit
              </button>
              <button
                onClick={() => { handleDelete(detailCred.id); setDetailCred(null); }}
                className="px-3 py-1.5 text-[12px] font-semibold rounded-lg bg-red-50 text-red-700 border border-red-200 hover:bg-red-100 transition-colors"
              >
                Delete
              </button>
            </div>
          }
        >
          {/* Status banners */}
          {expired && (
            <div className="mb-4 px-3 py-2 rounded-lg bg-red-50 border border-red-200 text-[12px] text-red-700 font-semibold">
              This credential has expired — pipelines using it will fail until renewed.
            </div>
          )}
          {!expired && expiringSoon && (
            <div className="mb-4 px-3 py-2 rounded-lg bg-amber-50 border border-amber-200 text-[12px] text-amber-800 font-semibold">
              Expires in {daysLeft} day{daysLeft === 1 ? '' : 's'} — renew soon to avoid pipeline disruption.
            </div>
          )}

          {/* Metadata grid */}
          <dl className="grid grid-cols-1 gap-3 text-[12px]">
            <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
              <dt className="text-slate-500 font-medium">Category</dt>
              <dd className="text-slate-800 font-semibold">{t.label}</dd>
            </div>
            <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
              <dt className="text-slate-500 font-medium">Environment</dt>
              <dd className="text-slate-800 font-semibold uppercase">{detailCred.environment || 'all'}</dd>
            </div>
            <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
              <dt className="text-slate-500 font-medium">Source</dt>
              <dd className="text-slate-800 font-semibold">{SOURCE_LABELS[detailCred.source || 'local']?.label ?? 'Local'}</dd>
            </div>
            {detailCred.username && (
              <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
                <dt className="text-slate-500 font-medium">Username</dt>
                <dd className="text-slate-800 font-mono">{detailCred.username}</dd>
              </div>
            )}
            <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
              <dt className="text-slate-500 font-medium">Created</dt>
              <dd className="text-slate-800"><TimeAgo value={detailCred.created_at} /></dd>
            </div>
            {detailCred.created_by && (
              <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
                <dt className="text-slate-500 font-medium">Created by</dt>
                <dd className="text-slate-800">{detailCred.created_by}</dd>
              </div>
            )}
            {detailCred.updated_at && (
              <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
                <dt className="text-slate-500 font-medium">Last modified</dt>
                <dd className="text-slate-800"><TimeAgo value={detailCred.updated_at} /></dd>
              </div>
            )}
            {detailCred.last_used && (
              <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
                <dt className="text-slate-500 font-medium">Last used</dt>
                <dd className="text-slate-800"><TimeAgo value={detailCred.last_used} /></dd>
              </div>
            )}
            {detailCred.expires_at && (
              <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
                <dt className="text-slate-500 font-medium">Expires</dt>
                <dd className={`font-semibold ${expired ? 'text-red-700' : expiringSoon ? 'text-amber-800' : 'text-slate-800'}`}>
                  <TimeAgo value={detailCred.expires_at} />
                </dd>
              </div>
            )}
            {detailCred.description && (
              <div className="py-2">
                <dt className="text-slate-500 font-medium mb-1">Description</dt>
                <dd className="text-slate-700 leading-relaxed">{detailCred.description}</dd>
              </div>
            )}
          </dl>

          {/* Privacy / encryption note — solo-dev OSS pitch */}
          <div className="mt-5 px-3 py-2.5 rounded-lg bg-slate-50 border border-slate-200 text-xs text-slate-600 leading-relaxed">
            <strong className="text-slate-700">Encrypted at rest.</strong>{' '}
            The secret value is encrypted with Fernet (AES-128-CBC + HMAC-SHA256)
            and decrypted only at run-time inside the worker process.
            It never appears in the UI or logs.
          </div>
        </DetailDrawer>
      );
    })()}
    </div>
  );
}
