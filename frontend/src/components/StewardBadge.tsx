/**
 * StewardBadge — the in-app surface for the F-Pulse Steward.
 *
 * The Steward is the OSS reliability + learning layer (Archeologist
 * 1.1; Autopsy/Foreseer/Curator/Optimizer in later releases). See
 * `backend/fpulse/steward/__init__.py` for the architectural rules and
 * `docs/steward/overview.md` for the user-facing explanation.
 *
 * Renders as a header peer of the notification bell. Three tabs:
 *   1. Findings — current open findings with Dismiss + Resolve
 *   2. Memory   — the learning log (audit trail + persistent
 *                 occurrence counts; proves the Steward IS learning)
 *   3. Settings — per-workspace tunables (enable, min_severity,
 *                 scan_on_save, escalate_after_n_occurrences, etc.)
 *
 * Hard rules carried from the backend:
 *   - The Steward never mutates user data. UI actions either suppress
 *     (Dismiss) or close (Resolve) the finding itself — they don't
 *     touch workflows or connections.
 *   - Failures are silent — the Steward is a "nice to have" surface,
 *     not a critical path. A backend hiccup must not break the header.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { askCopilot } from '../hooks/useAgentChatStore';

// ── Types matching backend/fpulse/steward/models.py ─────────────────
type Severity = 'p1' | 'p2' | 'p3';
// 2026-06-08 - Kind widened to `string` rather than a narrow union.
// Previously only 6 kinds were listed; the active set is now 19+ after
// the overnight ship (governance + cost + node + data + quality kinds).
// Using `string` lets the backend ship new kinds without forcing a
// frontend type-bump on every release. KIND_LABEL below maps the known
// ones to human labels; unknown ones fall back to the raw enum value.
type Kind = string;
type Level =
  | 'pipeline' | 'node' | 'connector' | 'data'
  | 'architecture' | 'governance' | 'cost';
type Status = 'open' | 'dismissed' | 'resolved' | 'stale';

interface Finding {
  id: string;
  workspace_id: string;
  kind: Kind;
  severity: Severity;
  status: Status;
  title: string;
  body: string;
  evidence: Record<string, any>;
  proposed_actions: Array<{ label: string; action: string; params?: any }>;
  first_seen: string;
  last_seen: string;
  occurrences: number;
  // R7 (2026-06-06) — confidence richness, optional for backward-compat
  // with older payloads.
  confidence?: 'low' | 'medium' | 'high';
  confidence_score?: number;
  evidence_count?: number;
  baseline_window?: string;
  level?: string;
}

interface FindingsResponse {
  workspace_id: string;
  count: number;
  findings: Finding[];
}

interface StewardSettingsT {
  enabled: boolean;
  min_severity: Severity;
  scan_on_save: boolean;
  auto_stale_days: number;
  escalate_after_n_occurrences: number;
  escalate_min_hours_since_first: number;
  notify_on_finding: boolean;
  notify_min_severity: Severity;
}

interface MemoryEvent {
  ts: string;
  scan_id: string | null;
  kind: 'emit' | 'dismiss' | 'resolve';
  finding_id: string;
  finding_kind?: string;
  signature: string;
  severity_at_emit?: Severity;
  reason?: string;
  evidence_summary?: { workflow_count?: number; workflow_ids?: string[] };
}

interface MemoryResponse {
  workspace_id: string;
  events: MemoryEvent[];
  persistent_occurrences: Record<string, number>;
}

interface StewardBadgeProps {
  signedIn: boolean;
  isProd?: boolean;
}

// 2026-06-08 - every active FindingKind gets a human-readable label.
// Maintained in lockstep with backend/fpulse/steward/models.py FindingKind
// enum. New kinds default to the raw enum value (still readable) if a
// label hasn't been added here yet.
const KIND_LABEL: Record<string, string> = {
  // Architecture (Archeologist - 1.1)
  duplicate_source:        'Duplicate source',
  duplicate_pipeline:      'Duplicate pipeline',
  redundant_transfer:      'Redundant transfer',
  lineage_cascade:         'Lineage cascade',
  // Connector (Connector-health - 1.1.x)
  connector_auth_failure:  'Auth failing',
  connector_unreachable:   'Unreachable',
  connector_rate_limit:    'Rate-limited',
  credential_near_expiry:  'Credential expiring',
  // Data
  schema_drift:            'Schema drift',
  null_spike:              'Null spike',
  duplicate_key_spike:     'Duplicate key',
  volume_anomaly:          'Volume anomaly',
  freshness_miss:          'Freshness miss',
  partition_missing:       'Partition missing',
  quality_check_failed:    'Quality check',
  // Governance
  env_crossing:            'Env crossing',
  unapproved_destination:  'Unapproved sink',
  pii_leak:                'PII leak',
  credential_sprawl:       'Credential sprawl',
  // Cost
  warehouse_waste:         'Warehouse waste',
  cost_drift:              'Cost drift',
  cost_recommendation:     'Cost recommendation',
  // Node
  empty_output:            'Empty output',
  join_explosion:          'Join explosion',
  join_collapse:           'Join collapse',
  filter_dropped_all:      'Filter dropped all',
  dedupe_collapse:         'Dedupe over-removal',
  orphaned_table:          'Unused managed table',
  cast_failure:            'Cast failure',
  row_count_delta:         'Row count changed',
  // Pipeline
  sla_breach:              'SLA breach',
  partial_output:          'Partial output',
  retry_storm:             'Retry storm',
  failure_rca:             'Failure analysis',
  // User-defined (rules engine)
  user_defined:            'Custom rule',
};

const SEVERITY_BADGE: Record<Severity, string> = {
  p1: 'bg-red-100 text-red-700 border-red-300',
  p2: 'bg-amber-100 text-amber-700 border-amber-300',
  p3: 'bg-slate-100 text-slate-700 border-slate-300',
};

// 2026-06-08 - per-level colors. Every level on screen used to be
// violet (the Steward brand colour); reviewer noted that washed out
// the at-a-glance "what layer is this watching?" signal once we had
// findings across 6+ levels. Each level now gets its own pill style.
const LEVEL_BADGE: Record<string, string> = {
  architecture: 'bg-violet-50 text-violet-700 border border-violet-200',
  data:         'bg-blue-50 text-blue-700 border border-blue-200',
  connector:    'bg-amber-50 text-amber-700 border border-amber-200',
  governance:   'bg-rose-50 text-rose-700 border border-rose-200',
  cost:         'bg-emerald-50 text-emerald-700 border border-emerald-200',
  node:         'bg-indigo-50 text-indigo-700 border border-indigo-200',
  pipeline:     'bg-slate-100 text-slate-700 border border-slate-200',
};

const LEVEL_BADGE_DEFAULT = 'bg-violet-50 text-violet-700 border border-violet-200';

type Tab = 'findings' | 'memory' | 'settings' | 'coverage' | 'rules';

// Coverage — proof of WHAT the Steward watches (GET /api/steward/coverage).
// The detector list is the real active-detector registry; the counts come
// from the live scan, so the page can't claim coverage the engine lacks.
interface CoverageDetector {
  kind: string;
  label: string;
  level: string;
  mode: 'state' | 'event' | 'run';
  description: string;
  open: number;
  // Per-detector config (rung 1): is it on, and any severity override.
  enabled: boolean;
  severity_override: Severity | null;
  // Tunable numeric thresholds (rung 1.5) — only on detectors that expose them.
  thresholds?: {
    key: string; label: string; default: number;
    min: number; max: number; step: number; value: number;
  }[];
}
interface CoverageResponse {
  workspace_id: string;
  enabled: boolean;
  last_scan: string;
  detector_count: number;
  open_total: number;
  by_severity: { p1: number; p2: number; p3: number };
  by_level: Record<string, number>;
  detectors: CoverageDetector[];
}

// User-defined rules (rung 2). Declarative rules authored in-app + stored
// as YAML files; the SQL/expression escape hatch stays a Plus feature.
interface UserRuleT {
  id: string;
  title: string;
  description?: string;
  level: string;
  severity: Severity;
  enabled: boolean;
}
interface RulesResponse {
  rules: UserRuleT[];
  errors: { path: string; message: string }[];
  rules_dir: string;
  count: number;
}

// ── Per-kind evidence renderers ─────────────────────────────────────
// 2026-06-08 - each detector ships its own evidence shape; rendering
// them generically loses signal. These small helpers convert evidence
// dicts into structured chips/lists below the finding body. Backward-
// compatible: any kind without a renderer simply doesn't add UI.
function renderKindEvidence(f: Finding): React.ReactNode {
  const ev = f.evidence || {};
  switch (f.kind) {
    case 'schema_drift': {
      const changes: Array<{ kind: string; column_name: string; old_type?: string; new_type?: string }> =
        Array.isArray(ev.changes) ? ev.changes : [];
      if (changes.length === 0) return null;
      return (
        <div className="mt-2 space-y-1">
          {changes.slice(0, 6).map((c, i) => {
            const tone =
              c.kind === 'dropped' ? 'bg-red-50 text-red-700 border-red-200'
              : c.kind === 'type_changed' ? 'bg-amber-50 text-amber-700 border-amber-200'
              : 'bg-emerald-50 text-emerald-700 border-emerald-200';
            return (
              <div key={i} className={`text-[12px] px-2 py-1 rounded border ${tone}`}>
                <span className="font-semibold uppercase text-[10px] mr-1.5">{c.kind}</span>
                <span className="font-mono">{c.column_name}</span>
                {c.kind === 'type_changed' && (
                  <span className="text-[11px] ml-2 opacity-80">
                    {c.old_type} → {c.new_type}
                  </span>
                )}
                {c.kind === 'added' && c.new_type && (
                  <span className="text-[11px] ml-2 opacity-80">({c.new_type})</span>
                )}
              </div>
            );
          })}
          {changes.length > 6 && (
            <div className="text-[11px] text-slate-500">+{changes.length - 6} more</div>
          )}
        </div>
      );
    }
    case 'volume_anomaly': {
      // 2026-06-08 — foreseer baseline-variance evidence: show the
      // learned baseline -> the anomalous value + direction + % change.
      const dir = ev.direction === 'spike' ? 'spike' : 'drop';
      const cur = typeof ev.current_rows_read === 'number' ? ev.current_rows_read : null;
      const base = typeof ev.baseline_median === 'number' ? ev.baseline_median : null;
      const pct = typeof ev.pct_change === 'number' ? ev.pct_change : null;
      if (cur === null || base === null) return null;
      const cls = dir === 'spike'
        ? 'bg-amber-50 text-amber-700 border-amber-200'
        : 'bg-rose-50 text-rose-700 border-rose-200';
      return (
        <div className={`mt-2 text-[12px] inline-flex items-center gap-2 px-2 py-1 rounded border ${cls}`}>
          <span className="font-semibold uppercase text-[10px]">{dir}</span>
          <span className="font-mono">{base.toLocaleString()} → {cur.toLocaleString()} rows</span>
          {pct !== null && (
            <span className="font-semibold">({pct > 0 ? '+' : ''}{Math.round(pct)}%)</span>
          )}
        </div>
      );
    }
    case 'null_spike':
    case 'duplicate_key_spike':
    case 'freshness_miss':
    case 'partition_missing':
    case 'quality_check_failed': {
      const check = ev.check ? String(ev.check) : '';
      const col = ev.column ? String(ev.column) : '';
      const failed = typeof ev.failed_count === 'number' ? ev.failed_count : null;
      const total = typeof ev.total_rows === 'number' ? ev.total_rows : null;
      if (!check && failed === null) return null;
      return (
        <div className="mt-2 text-[12px] inline-flex items-center gap-2 px-2 py-1 rounded bg-blue-50 text-blue-700 border border-blue-200">
          {check && <span className="font-mono font-semibold">{check}</span>}
          {col && <span className="font-mono opacity-80">{col}</span>}
          {failed !== null && (
            <span className="font-semibold">
              {failed}{total !== null ? ` / ${total}` : ''} failed
            </span>
          )}
        </div>
      );
    }
    case 'env_crossing': {
      const byEnv: Record<string, string[]> = ev.connections_by_env || {};
      const envs: string[] = Array.isArray(ev.envs) ? ev.envs : Object.keys(byEnv);
      if (envs.length === 0) return null;
      return (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {envs.map((e) => (
            <span key={e} className="text-[12px] px-2 py-0.5 rounded bg-rose-50 text-rose-700 border border-rose-200">
              <span className="font-semibold uppercase text-[10px] mr-1">{e}</span>
              {byEnv[e] ? byEnv[e].join(', ') : ''}
            </span>
          ))}
        </div>
      );
    }
    case 'unapproved_destination': {
      const unapproved: string[] = ev.unapproved_connections || [];
      if (unapproved.length === 0) return null;
      return (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {unapproved.slice(0, 5).map((c) => (
            <span key={c} className="text-[12px] px-2 py-0.5 rounded bg-rose-50 text-rose-700 border border-rose-200 font-mono">
              {c}
            </span>
          ))}
          {unapproved.length > 5 && (
            <span className="text-[11px] text-slate-500">+{unapproved.length - 5} more</span>
          )}
        </div>
      );
    }
    case 'warehouse_waste': {
      const streak = ev.streak_length;
      const runs: string[] = ev.recent_run_ids || [];
      return (
        <div className="mt-2 text-[12px] inline-flex items-center gap-2 px-2 py-1 rounded bg-emerald-50 text-emerald-700 border border-emerald-200">
          <span className="font-semibold">{streak} zero-output runs</span>
          {runs.length > 0 && (
            <span className="font-mono opacity-80">latest: {runs[0]}</span>
          )}
        </div>
      );
    }
    case 'empty_output': {
      const wfName = ev.workflow_name || ev.workflow_id;
      const nodeName = ev.node_label || ev.node_id;
      return (
        <div className="mt-2 text-[12px] inline-flex items-center gap-2 px-2 py-1 rounded bg-indigo-50 text-indigo-700 border border-indigo-200">
          <span className="font-mono font-semibold">{nodeName}</span>
          <span className="opacity-70">in</span>
          <span className="font-mono">{wfName}</span>
          {typeof ev.streak_length === 'number' && (
            <span className="opacity-80">× {ev.streak_length}</span>
          )}
        </div>
      );
    }
    case 'connector_auth_failure':
    case 'connector_unreachable':
    case 'connector_rate_limit': {
      const name = ev.connection_name || ev.connection_id;
      const streak = ev.consecutive_failures;
      const cls = ev.last_error_class;
      return (
        <div className="mt-2 text-[12px] inline-flex items-center gap-2 px-2 py-1 rounded bg-amber-50 text-amber-700 border border-amber-200">
          <span className="font-mono font-semibold">{name}</span>
          {cls && <span className="font-mono opacity-80">{cls}</span>}
          {typeof streak === 'number' && <span className="opacity-80">streak {streak}</span>}
        </div>
      );
    }
    case 'credential_near_expiry': {
      const days = ev.days_until_expiry;
      const name = ev.connection_name || ev.connection_id;
      return (
        <div className="mt-2 text-[12px] inline-flex items-center gap-2 px-2 py-1 rounded bg-amber-50 text-amber-700 border border-amber-200">
          <span className="font-mono font-semibold">{name}</span>
          {typeof days === 'number' && <span className="opacity-80">expires in {days} day{days === 1 ? '' : 's'}</span>}
        </div>
      );
    }
    case 'user_defined': {
      const ruleId = ev.rule_id;
      if (!ruleId) return null;
      return (
        <div className="mt-2 text-[12px] inline-flex items-center gap-2 px-2 py-1 rounded bg-violet-50 text-violet-700 border border-violet-200">
          <span className="opacity-70">rule</span>
          <span className="font-mono font-semibold">{ruleId}</span>
        </div>
      );
    }
    default: {
      // Raw-evidence fallback (2026-06-08 hardening). A finding kind
      // with no bespoke renderer — a newly-added detector, or a backend
      // evidence-key rename the UI hasn't caught up to — should still
      // surface its evidence rather than silently showing nothing.
      // Render scalar evidence fields as compact key:value chips.
      const entries = Object.entries(ev).filter(
        ([, v]) => v != null && typeof v !== 'object',
      );
      if (entries.length === 0) return null;
      return (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {entries.slice(0, 6).map(([k, v]) => (
            <span
              key={k}
              className="text-[11px] px-2 py-0.5 rounded bg-slate-50 text-slate-600 border border-slate-200 font-mono"
            >
              {k}: {String(v)}
            </span>
          ))}
        </div>
      );
    }
  }
}

export default function StewardBadge({ signedIn, isProd = false }: StewardBadgeProps) {
  const [findings, setFindings] = useState<Finding[]>([]);
  const [open, setOpen] = useState(false);
  // Findings render collapsed (title + ~2 lines) by default; the operator
  // expands the ones they care about. Keeps the panel scannable instead of a
  // wall of multi-paragraph bodies + evidence chips. Tracks expanded ids.
  const [expandedFindings, setExpandedFindings] = useState<Set<string>>(new Set());
  const toggleFindingExpanded = (id: string) =>
    setExpandedFindings((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  const [tab, setTab] = useState<Tab>('findings');
  // 2026-06-08 - level filter for the findings list. 'all' = no filter.
  // Set when user clicks a level chip in the strip above the list.
  const [levelFilter, setLevelFilter] = useState<string>('all');
  // 2026-06-08 - resolve-with-fix-note modal. When non-null, modal is
  // open for the finding with this id; submission posts the fix_note
  // through the resolve→lesson capture loop.
  const [resolveModalForId, setResolveModalForId] = useState<string | null>(null);
  const [resolveModalNote, setResolveModalNote] = useState<string>('');
  // 2026-06-08 - one-shot toast confirming "your fix-note became a
  // PROPOSED lesson"; auto-clears after 5s.
  const [lessonToast, setLessonToast] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 2026-06-06 — scan-progress feedback (per user feedback "difficult
  // to see any progress"). Track the last successful scan timestamp +
  // count so the dropdown header can show "Last scanned 14:23:08 — 3
  // findings", and a short-lived flash that confirms a manual
  // Re-scan just completed.
  const [lastScanAt, setLastScanAt] = useState<Date | null>(null);
  const [lastScanCount, setLastScanCount] = useState<number | null>(null);
  const [scanFlash, setScanFlash] = useState<string | null>(null);
  const [settings, setSettings] = useState<StewardSettingsT | null>(null);
  const [memory, setMemory] = useState<MemoryResponse | null>(null);
  const [coverage, setCoverage] = useState<CoverageResponse | null>(null);
  // Rules tab (rung 2) — list + a small in-app create form.
  const [rules, setRules] = useState<RulesResponse | null>(null);
  const [ruleForm, setRuleForm] = useState({
    id: '', title: '', severity: 'p2' as Severity, level: 'pipeline',
    matchType: 'name' as 'name' | 'node', matchValue: '', recommend: '',
  });
  const [showRuleForm, setShowRuleForm] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const refreshFindings = useCallback(async () => {
    if (!signedIn) return;
    try {
      const r = await api.get<FindingsResponse>('/api/steward/findings?status=open');
      setFindings(r.findings || []);
      setError(null);
      // 2026-06-06 — record scan freshness so the header can show
      // "Last scanned at 14:23:08 — 3 findings" instead of giving
      // the user no signal that a scan even happened.
      setLastScanAt(new Date());
      setLastScanCount((r.findings || []).length);
    } catch {
      // Silent — header surface must not break on backend hiccups.
    }
  }, [signedIn]);

  const refreshSettings = useCallback(async () => {
    try {
      const r = await api.get<{ settings: StewardSettingsT }>('/api/steward/settings');
      setSettings(r.settings);
    } catch {
      // ignore
    }
  }, []);

  const refreshMemory = useCallback(async () => {
    try {
      const r = await api.get<MemoryResponse>('/api/steward/memory?limit=200');
      setMemory(r);
    } catch {
      // ignore
    }
  }, []);

  const refreshCoverage = useCallback(async () => {
    try {
      const r = await api.get<CoverageResponse>('/api/steward/coverage');
      setCoverage(r);
    } catch {
      // ignore — coverage is a proof surface, never a critical path
    }
  }, []);

  const refreshRules = useCallback(async () => {
    try {
      setRules(await api.get<RulesResponse>('/api/steward/rules'));
    } catch {
      // ignore — never a critical path
    }
  }, []);

  // Poll findings every 60s while tab visible.
  useEffect(() => {
    if (!signedIn) return;
    refreshFindings();
    let interval: number | undefined;
    const start = () => {
      if (interval !== undefined) return;
      interval = window.setInterval(refreshFindings, 60000);
    };
    const stop = () => {
      if (interval === undefined) return;
      clearInterval(interval);
      interval = undefined;
    };
    const onVisibility = () => {
      if (document.hidden) stop();
      else { refreshFindings(); start(); }
    };
    if (!document.hidden) start();
    document.addEventListener('visibilitychange', onVisibility);
    // Other surfaces (e.g. SaveDialog after a successful workflow save)
    // can request an immediate Steward re-scan by dispatching this
    // event. SaveDialog already wires it when settings.scan_on_save is
    // true. The Steward also re-derives from current scan on every
    // findings request, so no extra plumbing needed elsewhere.
    window.addEventListener('fpulse:steward-refresh', refreshFindings);
    // 2026-06-05 — Clicking a Steward notification in the bell deep-links
    // here. The notification handler dispatches `fpulse:steward-open`
    // after navigating to #dashboard; we then auto-open the dropdown on
    // the Findings tab so the user lands in context immediately.
    const onOpen = () => {
      setTab('findings');
      setOpen(true);
      // Refresh so the user sees the latest state, not a stale cache.
      refreshFindings();
    };
    window.addEventListener('fpulse:steward-open', onOpen as EventListener);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('fpulse:steward-refresh', refreshFindings);
      window.removeEventListener('fpulse:steward-open', onOpen as EventListener);
    };
  }, [refreshFindings, signedIn]);

  // Lazy-load settings + memory on first tab switch (don't pay the
  // cost until the user actually wants to see them).
  useEffect(() => {
    if (!open) return;
    if (tab === 'settings' && settings === null) refreshSettings();
    if (tab === 'memory' && memory === null) refreshMemory();
    if (tab === 'coverage' && coverage === null) refreshCoverage();
    if (tab === 'rules' && rules === null) refreshRules();
  }, [open, tab, settings, memory, coverage, rules, refreshSettings, refreshMemory, refreshCoverage, refreshRules]);

  // Close-on-outside-click (capture phase so React Flow handlers
  // can't trap us).
  useEffect(() => {
    if (!open) return;
    const handler = (e: Event) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', handler, true);
    document.addEventListener('pointerdown', handler, true);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', handler, true);
      document.removeEventListener('pointerdown', handler, true);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const handleForceScan = async () => {
    setLoading(true);
    setScanFlash(null);  // clear any prior flash before we start
    const t0 = performance.now();
    try {
      // The /scan endpoint returns {count: N} but refreshFindings()
      // also gives us the count via the findings array — so we use
      // that as the source of truth (avoids a count mismatch if a
      // background scan happens between the two calls).
      await api.post<{count?: number}>('/api/steward/scan');
      await refreshFindings();
      if (tab === 'memory') await refreshMemory();
      const ms = Math.round(performance.now() - t0);
      // Use the freshly-set count, not what we read from POST /scan
      // (refreshFindings has already updated state by now).
      const n = findings.length;  // pre-update count — see flash below
      setScanFlash(
        `Re-scan complete in ${ms} ms` +
        (n === 0 ? ' — no findings' : ` — ${n} finding${n === 1 ? '' : 's'}`)
      );
      // Auto-clear the flash after 4 seconds so it doesn't squat
      // forever. The persistent "Last scanned at ..." footer keeps
      // the info around.
      window.setTimeout(() => setScanFlash(null), 4000);
    } catch (e: any) {
      setError(e?.message || 'Re-scan failed');
    } finally {
      setLoading(false);
    }
  };

  const handleDismiss = async (id: string) => {
    const reason = window.prompt(
      'Optional: why is this duplicate intentional? (DR replication, data-vault layering, etc.) The Steward stores this so the future Curator sub-agent can learn from your reasoning.',
      ''
    );
    if (reason === null) return;  // user cancelled
    setLoading(true);
    try {
      await api.post(`/api/steward/findings/${encodeURIComponent(id)}/dismiss`, { reason });
      setFindings((curr) => curr.filter((f) => f.id !== id));
      if (memory !== null) await refreshMemory();
    } catch (e: any) {
      setError(e?.message || 'Dismiss failed');
    } finally {
      setLoading(false);
    }
  };

  // 2026-06-08 — accepts optional fix_note. When supplied, the backend
  // sanitises it (5-regex secret sweep) and files it as a PROPOSED
  // Memory-Layer lesson (resolve→lesson capture loop). Empty / omitted
  // → identical to the pre-loop behaviour. Returned lesson_id is
  // surfaced via a toast so the operator knows their note landed.
  const handleResolve = async (id: string, fixNote?: string) => {
    setLoading(true);
    try {
      const body: Record<string, any> = {};
      if (fixNote && fixNote.trim()) body.fix_note = fixNote.trim();
      const r = await api.post<{ resolved?: boolean; lesson_id?: string | null }>(
        `/api/steward/findings/${encodeURIComponent(id)}/resolve`,
        body,
      );
      setFindings((curr) => curr.filter((f) => f.id !== id));
      if (r?.lesson_id) {
        setLessonToast(`PROPOSED lesson captured — review in Memory tab.`);
        setTimeout(() => setLessonToast(null), 5000);
      }
      if (memory !== null) await refreshMemory();
    } catch (e: any) {
      setError(e?.message || 'Resolve failed');
    } finally {
      setLoading(false);
    }
  };

  const updateSetting = async (patch: Partial<StewardSettingsT>) => {
    setLoading(true);
    try {
      const r = await api.put<{ settings: StewardSettingsT }>('/api/steward/settings', patch);
      setSettings(r.settings);
      // Settings change can affect what findings are visible (min_severity)
      await refreshFindings();
    } catch (e: any) {
      setError(e?.message || 'Update failed');
    } finally {
      setLoading(false);
    }
  };

  // Per-detector config (rung 1). The /settings PUT replaces the whole
  // `detectors` map, so we rebuild the FULL map of non-default detectors
  // (disabled or severity-overridden) with this patch applied, then save.
  const updateDetector = async (
    kind: string,
    patch: { enabled?: boolean; severity?: Severity | null; thresholds?: Record<string, number> },
  ) => {
    if (!coverage) return;
    type DetCfg = { enabled?: boolean; severity?: Severity; thresholds?: Record<string, number> };
    const next: Record<string, DetCfg> = {};
    for (const d of coverage.detectors) {
      const isThis = d.kind === kind;
      const enabled = isThis && patch.enabled !== undefined ? patch.enabled : d.enabled;
      const sev = (isThis && patch.severity !== undefined ? patch.severity : d.severity_override) || null;
      // Keep only thresholds that differ from the built-in default.
      const thr: Record<string, number> = {};
      for (const t of d.thresholds || []) {
        const v = isThis && patch.thresholds && patch.thresholds[t.key] !== undefined
          ? patch.thresholds[t.key]
          : t.value;
        if (typeof v === 'number' && !Number.isNaN(v) && v !== t.default) thr[t.key] = v;
      }
      if (enabled === false || sev || Object.keys(thr).length) {
        next[d.kind] = {};
        if (enabled === false) next[d.kind].enabled = false;
        if (sev) next[d.kind].severity = sev;
        if (Object.keys(thr).length) next[d.kind].thresholds = thr;
      }
    }
    // Optimistic update so toggles/selects/inputs feel instant.
    setCoverage({
      ...coverage,
      detectors: coverage.detectors.map((d) =>
        d.kind === kind
          ? {
              ...d,
              enabled: patch.enabled !== undefined ? patch.enabled : d.enabled,
              severity_override:
                patch.severity !== undefined ? patch.severity : d.severity_override,
              thresholds: d.thresholds?.map((t) =>
                patch.thresholds && patch.thresholds[t.key] !== undefined
                  ? { ...t, value: patch.thresholds![t.key] }
                  : t,
              ),
            }
          : d,
      ),
    });
    try {
      await api.put('/api/steward/settings', { detectors: next });
      await refreshCoverage();
      await refreshFindings();   // disabling a detector changes what's shown
    } catch (e: any) {
      setError(e?.message || 'Update failed');
      await refreshCoverage();   // revert optimistic state on failure
    }
  };

  // Rung 2 — create / delete a declarative rule (OSS Free).
  const createRule = async () => {
    const f = ruleForm;
    const id = f.id.trim();
    if (!id || !f.title.trim()) { setError('A rule needs an id and a title'); return; }
    if (!f.matchValue.trim()) { setError('Add a match condition (name contains, or node type)'); return; }
    const match: Record<string, unknown> =
      f.matchType === 'name'
        ? { name_contains: f.matchValue.trim() }
        : { has_node: { type: f.matchValue.trim() } };
    const body = {
      id, title: f.title.trim(), level: f.level, severity: f.severity, match,
      recommend: f.recommend.split('\n').map((s) => s.trim()).filter(Boolean),
    };
    setLoading(true);
    try {
      await api.post('/api/steward/rules', body);
      setShowRuleForm(false);
      setRuleForm({ id: '', title: '', severity: 'p2', level: 'pipeline', matchType: 'name', matchValue: '', recommend: '' });
      await refreshRules();
      await refreshFindings();
    } catch (e: any) {
      setError(e?.message || 'Could not save rule');
    } finally { setLoading(false); }
  };

  const deleteRule = async (id: string) => {
    setLoading(true);
    try {
      await api.delete(`/api/steward/rules/${id}`);
      await refreshRules();
      await refreshFindings();
    } catch (e: any) {
      setError(e?.message || 'Could not delete rule');
    } finally { setLoading(false); }
  };

  if (!signedIn) return null;

  // 2026-06-07 — "Steward disabled" recovery affordance.
  //
  // Was: `if (settings?.enabled === false) return null;` — the badge
  // vanished entirely when disabled, leaving NO path back into Steward
  // from the UI. To re-enable, the user had to call
  // `PUT /api/steward/settings {enabled:true}` directly or hand-edit
  // settings.json on disk. That's a recovery cliff for a single-toggle
  // setting that lives INSIDE Steward's own panel (so disabling it
  // also closes the door to re-enabling it).
  //
  // Now: render a muted eye-off button with the same 9x9 footprint as
  // the live badge. Single click flips `enabled` back to true. No
  // dropdown, no count, no findings render — just the recovery path.
  // Once re-enabled, the live badge returns on the next render cycle.
  if (settings?.enabled === false) {
    return (
      <div className="relative shrink-0">
        <button
          onClick={() => updateSetting({ enabled: true })}
          disabled={loading}
          className="w-9 h-9 rounded-lg flex items-center justify-center border border-slate-300 bg-slate-100 text-slate-400 hover:bg-slate-200 hover:text-slate-600 hover:border-slate-400 transition-colors disabled:opacity-60 disabled:cursor-wait"
          title="Steward is off — click to enable"
          aria-label="Steward is off — click to enable"
        >
          {/* Eye-off glyph: keeps continuity with the live badge's eye
              icon, the diagonal slash carries the "disabled" semantics. */}
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round">
            <path d="M2 12s3-7 10-7c2 0 3.7.5 5.2 1.3" />
            <path d="M22 12s-3 7-10 7c-2 0-3.7-.5-5.2-1.3" />
            <path d="M9 9a3 3 0 0 0 6 6" />
            <line x1="3" y1="3" x2="21" y2="21" />
          </svg>
        </button>
      </div>
    );
  }

  const count = findings.length;

  return (
    <div className="relative shrink-0" ref={rootRef}>
      <button
        onClick={() => setOpen((v) => !v)}
        // 2026-06-05 — Steward gets a distinctive violet→indigo gradient
        // so it's never confused with the bell (white/amber) or the env
        // toggle (emerald/red). Violet is the colour we've used for
        // every Steward-related accent (count badge, finding pills,
        // header text), so the header chip ties it together. Slight
        // inset highlight + outer glow when there are findings give it
        // permanent presence — even at count=0 you can tell at a glance
        // "this is the Steward". White-icon contrast holds across both
        // DEV and PROD chrome.
        className={`w-9 h-9 rounded-lg flex items-center justify-center relative border transition-all ${
          isProd
            ? open
              ? 'bg-gradient-to-b from-violet-500 to-indigo-700 text-white border-violet-400 shadow-[0_0_14px_rgba(139,92,246,0.55),inset_0_1px_0_rgba(255,255,255,0.25)]'
              : 'bg-gradient-to-b from-violet-600/70 to-indigo-800/70 text-violet-100 border-violet-500/60 hover:from-violet-500 hover:to-indigo-700 hover:text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.18)]'
            : open
              ? 'bg-gradient-to-b from-violet-500 to-indigo-600 text-white border-violet-400 shadow-[0_0_14px_rgba(139,92,246,0.6),inset_0_1px_0_rgba(255,255,255,0.35)]'
              : 'bg-gradient-to-b from-violet-500 to-indigo-600 text-white border-violet-400 shadow-[0_2px_8px_rgba(139,92,246,0.35),inset_0_1px_0_rgba(255,255,255,0.3)] hover:from-violet-400 hover:to-indigo-500 hover:shadow-[0_0_16px_rgba(139,92,246,0.55),inset_0_1px_0_rgba(255,255,255,0.4)]'
        }`}
        title="Steward — duplicate detection + reliability findings"
        aria-label="Steward findings"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round">
          <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
        {count > 0 && (
          <span
            className={`absolute -top-1 -right-1 min-w-[20px] h-[20px] px-1 rounded-full text-xs font-bold flex items-center justify-center ring-2 ring-white shadow-sm leading-none ${
              findings.some((f) => f.severity === 'p1')
                // P1 → red pill so escalations read with the same
                // visual urgency as the bell's unread red.
                ? 'bg-red-500 text-white'
                // Non-P1 → amber pill on the violet base. Amber gives
                // the contrast a plain violet-on-violet pill would lose.
                : 'bg-amber-400 text-amber-950'
            }`}
          >
            {count > 9 ? '9+' : count}
          </span>
        )}
      </button>

      {open && (
        // 2026-06-06 — width 480 → 580 → 640. Height also bumped so
        // the Settings tab fits ALL 8 controls + help text without a
        // scrollbar on a standard 1080p display. Caps at 92vw / 92vh
        // so the dropdown never overflows the viewport on small
        // screens; on those it falls back to a scrollbar gracefully.
        // 2026-06-06 — thicker outer line + flowing animated gradient
        // (per user feedback). Implementation: wrap the dropdown in a
        // 3px-padded div whose background is a multi-stop linear
        // gradient (violet -> indigo -> fuchsia -> violet). Animate
        // background-position via the @keyframes block below so the
        // colors flow around the perimeter continuously. The inner
        // white panel sits at a slightly smaller corner radius so
        // the animated border shows through evenly on every edge.
        //
        // 3px reads as "deliberate frame" without overwhelming the
        // content; 8s cycle is slow enough to feel ambient (not
        // distracting) but fast enough that the user sees motion when
        // their eye lands on it.
        <div
          className="absolute right-0 top-full mt-2 z-50 rounded-xl"
          style={{
            width: 'min(92vw, 640px)',
            padding: '3px', // visible "outer line" thickness
            background:
              'linear-gradient(120deg, #8b5cf6 0%, #6366f1 25%, #d946ef 50%, #6366f1 75%, #8b5cf6 100%)',
            backgroundSize: '300% 300%',
            animation: 'stewardBorderFlow 8s ease-in-out infinite',
            boxShadow:
              '0 24px 50px -12px rgba(15, 23, 42, 0.28), ' +    // ambient lift
              '0 8px 24px -8px rgba(139, 92, 246, 0.45), ' +    // violet glow
              '0 0 0 1px rgba(255, 255, 255, 0.55)',            // crisp outer rim against page bg
          }}
        >
          {/* Keyframe injected inline so the animation works without
              touching the global stylesheet. Defined once per mount;
              React de-dupes if multiple panels open. */}
          <style>{`
            @keyframes stewardBorderFlow {
              0%   { background-position:   0% 50%; }
              50%  { background-position: 100% 50%; }
              100% { background-position:   0% 50%; }
            }
          `}</style>
          <div className="bg-white rounded-[9px] overflow-hidden">
          {/* Header */}
          <div className="px-5 py-3 border-b border-slate-100 flex items-center justify-between">
            <div>
              <div className="text-sm font-bold text-slate-800">Steward</div>
              <div className="text-[13px] text-slate-500">
                Read-only reliability + learning layer
              </div>
            </div>
            {tab === 'findings' && (
              <button
                onClick={handleForceScan}
                disabled={loading}
                className={`text-xs font-semibold rounded-md px-2.5 py-1 border transition-all flex items-center gap-1.5 ${
                  loading
                    ? 'bg-violet-100 text-violet-700 border-violet-300 cursor-wait'
                    : 'text-violet-700 border-violet-200 hover:bg-violet-50 hover:border-violet-300'
                }`}
                title="Re-run all Steward detectors against the current workflow set"
              >
                {loading ? (
                  <>
                    {/* Spinning circle — gives the user something
                        moving so they know work is happening */}
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none"
                         className="animate-spin"
                         stroke="currentColor" strokeWidth="3" strokeLinecap="round">
                      <path d="M21 12a9 9 0 1 1-6.219-8.56" />
                    </svg>
                    Scanning…
                  </>
                ) : (
                  <>
                    {/* Refresh icon — universally recognized */}
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none"
                         stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M3 12a9 9 0 0 1 15-6.7l3 2.7" />
                      <path d="M21 3v6h-6" />
                      <path d="M21 12a9 9 0 0 1-15 6.7l-3-2.7" />
                      <path d="M3 21v-6h6" />
                    </svg>
                    Re-scan
                  </>
                )}
              </button>
            )}
          </div>

          {/* 2026-06-06 — Last-scanned line. Persistent (until next
              scan replaces it) so the user always knows how fresh
              the current findings are. Hidden until the first scan
              completes to avoid a misleading "Last scanned: never". */}
          {lastScanAt && (
            <div className="px-5 py-1.5 bg-slate-50/60 border-b border-slate-100 text-[12px] text-slate-500 flex items-center justify-between">
              <span>
                Last scanned at{' '}
                <span className="font-mono text-slate-700">
                  {lastScanAt.toLocaleTimeString()}
                </span>
                {lastScanCount !== null && (
                  <span>
                    {' · '}
                    <span className="font-semibold text-slate-700">
                      {lastScanCount}
                    </span>
                    {' '}finding{lastScanCount === 1 ? '' : 's'}
                  </span>
                )}
              </span>
              {/* Auto-refresh hint so the user knows scans happen
                  even without clicking Re-scan. */}
              <span className="text-slate-400">auto every 60s</span>
            </div>
          )}

          {/* 2026-06-06 — Ephemeral "scan complete" flash. Auto-clears
              after 4s. Distinct from the persistent footer above so the
              moment-of-completion gets its own visual beat. */}
          {scanFlash && (
            <div className="px-5 py-2 bg-emerald-50 border-b border-emerald-100 text-xs text-emerald-700 flex items-center gap-1.5">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
                   stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="20 6 9 17 4 12" />
              </svg>
              {scanFlash}
            </div>
          )}

          {/* Tab strip */}
          <div className="flex border-b border-slate-100 bg-slate-50/60">
            {([
              ['findings', `Findings${count > 0 ? ` (${count})` : ''}`],
              ['coverage', 'Coverage'],
              ['rules', 'Rules'],
              ['memory', 'Memory'],
              ['settings', 'Settings'],
            ] as const).map(([key, label]) => (
              <button
                key={key}
                onClick={() => setTab(key)}
                // 2026-06-06 (R7) — beefier active state per reviewer
                // feedback. Was: py-2 + faint border + same bg. Now:
                // py-2.5 + thicker border + subtle violet tint on the
                // active tab so the selection feels deliberate.
                className={`flex-1 px-3 py-2.5 text-xs font-semibold transition-colors ${
                  tab === key
                    ? 'text-violet-700 bg-violet-50/60 border-b-[3px] border-violet-600 -mb-px'
                    : 'text-slate-500 hover:text-slate-700 hover:bg-slate-100/50'
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          {error && (
            <div className="px-5 py-2 bg-red-50 border-b border-red-100 text-xs text-red-700">
              {error}
            </div>
          )}

          {/* Tab body */}
          {/* 2026-06-06 — was 70vh / max 720. Bumped again so the
              Settings tab's 8 controls + their help text all fit
              without a scrollbar on a typical 1080p display (and on
              anything taller — caps at 88vh to leave a small gap
              above the bottom of the viewport). On short laptops we
              fall back to the scrollbar gracefully — the only thing
              that ever scrolls is the Memory tab's event stream when
              it grows past ~30 entries. */}
          <div className="overflow-y-auto" style={{ maxHeight: 'min(960px, 88vh)' }}>
            {tab === 'coverage' && (
              coverage === null ? (
                <div className="px-5 py-12 text-center text-sm text-slate-400">Loading coverage…</div>
              ) : (
                <div className="px-5 py-4 space-y-4">
                  {/* Summary — counts are from the live scan, not hardcoded */}
                  <div className="rounded-lg border border-slate-200 bg-slate-50/60 p-3">
                    <div className="flex items-center justify-between">
                      <div>
                        <div className="text-sm font-bold text-slate-800">Watching {coverage.detector_count} checks</div>
                        <div className="text-[11px] text-slate-500 mt-0.5">
                          {coverage.enabled ? 'Active' : 'Steward is off — enable it in Settings'}
                          {coverage.last_scan ? ` · last scan ${new Date(coverage.last_scan).toLocaleTimeString()}` : ''}
                        </div>
                      </div>
                      <div className="text-right">
                        <div className="text-lg font-bold text-slate-800 tabular-nums">{coverage.open_total}</div>
                        <div className="text-[10px] uppercase tracking-wider text-slate-400">open</div>
                      </div>
                    </div>
                    {coverage.open_total > 0 && (
                      <div className="mt-2 flex items-center gap-1.5">
                        {coverage.by_severity.p1 > 0 && <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-red-100 text-red-700">P1 · {coverage.by_severity.p1}</span>}
                        {coverage.by_severity.p2 > 0 && <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-amber-100 text-amber-800">P2 · {coverage.by_severity.p2}</span>}
                        {coverage.by_severity.p3 > 0 && <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-slate-200 text-slate-600">P3 · {coverage.by_severity.p3}</span>}
                      </div>
                    )}
                  </div>

                  {/* Detectors grouped by observability level */}
                  {(() => {
                    const LEVEL_ORDER = ['data', 'node', 'connector', 'pipeline', 'architecture', 'governance', 'cost'];
                    const LEVEL_LABEL: Record<string, string> = { pipeline: 'Pipeline', node: 'Node', connector: 'Connector', data: 'Data', architecture: 'Architecture', governance: 'Governance', cost: 'Cost' };
                    const MODE_CHIP: Record<string, { cls: string; title: string }> = {
                      state: { cls: 'bg-slate-100 text-slate-500', title: 'Re-derived from current state on every scan' },
                      event: { cls: 'bg-blue-50 text-blue-600', title: 'Recorded the moment something happens' },
                      run: { cls: 'bg-violet-50 text-violet-600', title: 'Fed by completed pipeline runs' },
                    };
                    const byLevel: Record<string, CoverageDetector[]> = {};
                    for (const d of coverage.detectors) (byLevel[d.level] ||= []).push(d);
                    const levels = [
                      ...LEVEL_ORDER.filter((l) => byLevel[l]),
                      ...Object.keys(byLevel).filter((l) => !LEVEL_ORDER.includes(l)),
                    ];
                    return levels.map((lvl) => (
                      <div key={lvl}>
                        <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400 mb-1">{LEVEL_LABEL[lvl] || lvl}</div>
                        <div className="space-y-1">
                          {byLevel[lvl].map((d) => {
                            const m = MODE_CHIP[d.mode] || MODE_CHIP.state;
                            return (
                              <div key={d.kind} className={`flex items-start gap-2 px-2.5 py-2 rounded-md border border-slate-100 hover:bg-slate-50/60 ${d.enabled ? '' : 'opacity-60'}`}>
                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={d.enabled ? '#16a34a' : '#94a3b8'} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 mt-0.5"><polyline points="20 6 9 17 4 12" /></svg>
                                <div className="min-w-0 flex-1">
                                  <div className="flex items-center gap-1.5 flex-wrap">
                                    <span className="text-xs font-semibold text-slate-700">{d.label}</span>
                                    <span className={`text-[9px] font-semibold uppercase tracking-wider px-1 py-0.5 rounded ${m.cls}`} title={m.title}>{d.mode}</span>
                                    {d.enabled && d.open > 0 && <span className="text-[9px] font-bold px-1 py-0.5 rounded bg-rose-100 text-rose-700" title="Open findings from this detector">{d.open} open</span>}
                                    {!d.enabled && <span className="text-[9px] font-bold px-1 py-0.5 rounded bg-slate-200 text-slate-500" title="This detector is off — its findings are hidden">off</span>}
                                    {d.severity_override && <span className="text-[9px] font-bold px-1 py-0.5 rounded bg-violet-100 text-violet-700" title="Severity overridden from the detector default">&rarr; {d.severity_override.toUpperCase()}</span>}
                                  </div>
                                  <div className="text-[11px] text-slate-500 leading-snug">{d.description}</div>
                                  {/* Tunable thresholds (rung 1.5) — save on blur */}
                                  {d.enabled && d.thresholds && d.thresholds.length > 0 && (
                                    <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
                                      {d.thresholds.map((t) => (
                                        <label key={`${t.key}-${t.value}`} className="flex items-center gap-1 text-[10px] text-slate-500" title={t.label}>
                                          <span className="whitespace-nowrap">{t.label}</span>
                                          <input
                                            type="number"
                                            min={t.min}
                                            max={t.max}
                                            step={t.step}
                                            defaultValue={t.value}
                                            onBlur={(e) => {
                                              const v = Number(e.target.value);
                                              if (!Number.isNaN(v) && v !== t.value) updateDetector(d.kind, { thresholds: { [t.key]: v } });
                                            }}
                                            className="w-20 border border-slate-200 rounded px-1 py-0.5 text-slate-700 tabular-nums"
                                          />
                                          {t.value !== t.default && (
                                            <button
                                              type="button"
                                              onClick={() => updateDetector(d.kind, { thresholds: { [t.key]: t.default } })}
                                              className="text-[9px] text-violet-600 hover:underline"
                                              title={`Reset to default (${t.default})`}
                                            >
                                              reset
                                            </button>
                                          )}
                                        </label>
                                      ))}
                                    </div>
                                  )}
                                </div>
                                {/* Per-detector controls (rung 1): severity override + on/off */}
                                <div className="flex items-center gap-1 shrink-0">
                                  <select
                                    value={d.severity_override || ''}
                                    onChange={(e) => updateDetector(d.kind, { severity: (e.target.value || null) as Severity | null })}
                                    disabled={!d.enabled}
                                    title="Override the severity of this detector's findings"
                                    className="text-[10px] border border-slate-200 rounded px-1 py-0.5 bg-white text-slate-600 disabled:opacity-40"
                                  >
                                    <option value="">Default</option>
                                    <option value="p1">P1</option>
                                    <option value="p2">P2</option>
                                    <option value="p3">P3</option>
                                  </select>
                                  <button
                                    type="button"
                                    onClick={() => updateDetector(d.kind, { enabled: !d.enabled })}
                                    title={d.enabled ? 'Disable this detector' : 'Enable this detector'}
                                    className={`text-[10px] font-bold px-1.5 py-0.5 rounded border transition-colors ${d.enabled ? 'border-emerald-200 text-emerald-700 bg-emerald-50 hover:bg-emerald-100' : 'border-slate-200 text-slate-400 bg-slate-50 hover:bg-slate-100'}`}
                                  >
                                    {d.enabled ? 'On' : 'Off'}
                                  </button>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    ));
                  })()}

                  <div className="text-[10px] text-slate-400 leading-snug pt-1 border-t border-slate-100">
                    Only detectors that actually run are listed; counts come from the latest scan.
                    Turn a detector <b>Off</b> or change its severity to make Coverage match your
                    workspace. The Steward advises — it never changes your pipelines.
                  </div>
                </div>
              )
            )}
            {tab === 'rules' && (
              rules === null ? (
                <div className="px-5 py-12 text-center text-sm text-slate-400">Loading rules…</div>
              ) : (
                <div className="px-5 py-4 space-y-3">
                  <div className="flex items-center justify-between gap-2">
                    <div>
                      <div className="text-sm font-bold text-slate-800">Custom rules</div>
                      <div className="text-[11px] text-slate-500">Your own checks — they run alongside the built-in detectors.</div>
                    </div>
                    <button type="button" onClick={() => setShowRuleForm((v) => !v)} className="text-xs font-semibold px-2.5 py-1 rounded-lg border border-violet-200 text-violet-700 bg-violet-50 hover:bg-violet-100 shrink-0">
                      {showRuleForm ? 'Cancel' : '+ New rule'}
                    </button>
                  </div>

                  {showRuleForm && (
                    <div className="rounded-lg border border-violet-200 bg-violet-50/40 p-3 space-y-2">
                      <div className="grid grid-cols-2 gap-2">
                        <input placeholder="rule_id (a-z, _, -)" value={ruleForm.id} onChange={(e) => setRuleForm({ ...ruleForm, id: e.target.value })} className="text-xs border border-slate-200 rounded px-2 py-1" />
                        <input placeholder="Title" value={ruleForm.title} onChange={(e) => setRuleForm({ ...ruleForm, title: e.target.value })} className="text-xs border border-slate-200 rounded px-2 py-1" />
                      </div>
                      <div className="grid grid-cols-2 gap-2">
                        <select value={ruleForm.severity} onChange={(e) => setRuleForm({ ...ruleForm, severity: e.target.value as Severity })} className="text-xs border border-slate-200 rounded px-2 py-1 bg-white">
                          <option value="p1">P1</option><option value="p2">P2</option><option value="p3">P3</option>
                        </select>
                        <select value={ruleForm.level} onChange={(e) => setRuleForm({ ...ruleForm, level: e.target.value })} className="text-xs border border-slate-200 rounded px-2 py-1 bg-white">
                          {['pipeline', 'node', 'data', 'connector', 'architecture', 'governance', 'cost'].map((l) => <option key={l} value={l}>{l}</option>)}
                        </select>
                      </div>
                      <div className="flex items-center gap-2">
                        <select value={ruleForm.matchType} onChange={(e) => setRuleForm({ ...ruleForm, matchType: e.target.value as 'name' | 'node' })} className="text-xs border border-slate-200 rounded px-2 py-1 bg-white shrink-0">
                          <option value="name">Name contains</option>
                          <option value="node">Has node type</option>
                        </select>
                        <input placeholder={ruleForm.matchType === 'name' ? 'e.g. prod' : 'e.g. db_sink'} value={ruleForm.matchValue} onChange={(e) => setRuleForm({ ...ruleForm, matchValue: e.target.value })} className="flex-1 min-w-0 text-xs border border-slate-200 rounded px-2 py-1" />
                      </div>
                      <textarea placeholder="Recommended actions (one per line)" value={ruleForm.recommend} onChange={(e) => setRuleForm({ ...ruleForm, recommend: e.target.value })} rows={2} className="w-full text-xs border border-slate-200 rounded px-2 py-1" />
                      <button type="button" onClick={createRule} disabled={loading} className="text-xs font-bold px-3 py-1.5 rounded-lg bg-violet-600 text-white hover:bg-violet-700 disabled:opacity-50">Create rule</button>
                    </div>
                  )}

                  {rules.errors.length > 0 && (
                    <div className="rounded-lg border border-amber-200 bg-amber-50 p-2 text-[11px] text-amber-800">
                      <div className="font-semibold mb-1">{rules.errors.length} rule file(s) failed to load:</div>
                      {rules.errors.map((er, i) => <div key={i} className="truncate" title={`${er.path}: ${er.message}`}>• {er.message}</div>)}
                    </div>
                  )}

                  {rules.rules.length === 0 ? (
                    <div className="text-[12px] text-slate-400 py-4 text-center">No custom rules yet. Click “+ New rule” to add one.</div>
                  ) : (
                    <div className="space-y-1">
                      {rules.rules.map((r) => (
                        <div key={r.id} className="flex items-center gap-2 px-2.5 py-2 rounded-md border border-slate-100">
                          <span className={`text-[9px] font-bold px-1 py-0.5 rounded ${SEVERITY_BADGE[r.severity]}`}>{r.severity.toUpperCase()}</span>
                          <div className="min-w-0 flex-1">
                            <div className="text-xs font-semibold text-slate-700 truncate">{r.title}</div>
                            <div className="text-[10px] text-slate-400 truncate">{r.id} · {r.level}{r.enabled ? '' : ' · disabled'}</div>
                          </div>
                          <button type="button" onClick={() => deleteRule(r.id)} title="Delete this rule" className="text-[10px] font-semibold px-1.5 py-0.5 rounded border border-rose-200 text-rose-600 bg-rose-50 hover:bg-rose-100 shrink-0">Delete</button>
                        </div>
                      ))}
                    </div>
                  )}

                  <div className="text-[10px] text-slate-400 leading-snug pt-1 border-t border-slate-100">
                    Rules save as YAML you can version-control or hand-edit for advanced matches.
                  </div>
                </div>
              )
            )}
            {tab === 'findings' && (
              count === 0 ? (
                <div className="px-5 py-12 text-center text-sm text-slate-400">
                  <div className="mb-2">No open findings</div>
                  <div className="text-[13px] text-slate-400">
                    When the Steward spots duplicate sources, duplicate pipelines,
                    or other reliability concerns, they appear here.
                  </div>
                </div>
              ) : (
                <>
                  {/* 2026-06-08 — level filter strip. Findings span up to
                      7 observability levels now; this lets users narrow
                      to one layer at a time without scrolling past
                      everything else. "All" remains the default. Only
                      levels actually PRESENT in the current finding set
                      are surfaced as chips — keeps the strip tight. */}
                  {(() => {
                    const presentLevels = Array.from(
                      new Set(findings.map((f) => f.level).filter(Boolean))
                    ) as string[];
                    if (presentLevels.length <= 1) return null;
                    const counts: Record<string, number> = {};
                    findings.forEach((f) => {
                      if (f.level) counts[f.level] = (counts[f.level] || 0) + 1;
                    });
                    const chipBase = 'text-[11px] font-medium px-2 py-0.5 rounded-full border transition-colors';
                    return (
                      <div className="px-5 pt-2 pb-2 flex flex-wrap gap-1.5 border-b border-slate-100 bg-slate-50/50">
                        <button
                          onClick={() => setLevelFilter('all')}
                          className={`${chipBase} ${
                            levelFilter === 'all'
                              ? 'bg-slate-800 text-white border-slate-800'
                              : 'bg-white text-slate-600 border-slate-200 hover:border-slate-400'
                          }`}
                        >
                          All ({findings.length})
                        </button>
                        {presentLevels.sort().map((lvl) => {
                          const active = levelFilter === lvl;
                          return (
                            <button
                              key={lvl}
                              onClick={() => setLevelFilter(active ? 'all' : lvl)}
                              className={`${chipBase} capitalize ${
                                active
                                  ? (LEVEL_BADGE[lvl] || LEVEL_BADGE_DEFAULT) + ' ring-2 ring-offset-1'
                                  : 'bg-white text-slate-600 border-slate-200 hover:border-slate-400'
                              }`}
                            >
                              {lvl} ({counts[lvl] || 0})
                            </button>
                          );
                        })}
                      </div>
                    );
                  })()}
                  {findings
                    .filter((f) => levelFilter === 'all' || f.level === levelFilter)
                    .map((f) => {
                  const fExpanded = expandedFindings.has(f.id);
                  // Only offer expand when there's meaningfully more to show
                  // than the clamped 2 lines (long body, multiple paragraphs,
                  // or evidence chips worth revealing).
                  const fLong = !!f.body && (f.body.length > 140 || f.body.includes('\n'));
                  return (
                  // 2026-06-06 (R7) — tighter Findings card per
                  // reviewer convergence on the density disconnect
                  // between Memory (dense) and Findings (loose).
                  // py-4 -> py-2.5, mt-1.5 -> mt-1, mt-1 -> mt-0.5.
                  <div key={f.id} className="px-5 py-2.5 border-b border-slate-100 last:border-b-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className={`text-[12px] font-bold px-1.5 py-0.5 rounded border ${SEVERITY_BADGE[f.severity]}`}>
                        {f.severity.toUpperCase()}
                      </span>
                      <span className="text-[12px] font-semibold text-slate-500 uppercase tracking-wide">
                        {KIND_LABEL[f.kind] || f.kind}
                      </span>
                      {/* R7: confidence chip — surface the calibration
                          signal so users learn to trust HIGH-confidence
                          findings more than LOW. Backward-compat with
                          older payloads via the optional fields. */}
                      {f.confidence && (
                        <span
                          className={`text-[11px] font-semibold px-1.5 py-0.5 rounded ${
                            f.confidence === 'high' ? 'bg-emerald-100 text-emerald-700'
                            : f.confidence === 'medium' ? 'bg-amber-100 text-amber-700'
                            : 'bg-slate-100 text-slate-600'
                          }`}
                          title={
                            (typeof f.confidence_score === 'number'
                              ? `Confidence score: ${(f.confidence_score * 100).toFixed(0)}%`
                              : '')
                            + (f.evidence_count ? ` · Evidence: ${f.evidence_count} observations` : '')
                            + (f.baseline_window ? ` · Baseline: ${f.baseline_window}` : '')
                          }
                        >
                          {f.confidence.toUpperCase()}
                        </span>
                      )}
                      {/* R7: level chip — at-a-glance "what layer is
                          this watching?". Only shown when explicitly
                          present so backward-compat is preserved. */}
                      {f.level && (
                        <span className={`text-[11px] font-medium px-1.5 py-0.5 rounded capitalize ${LEVEL_BADGE[f.level] || LEVEL_BADGE_DEFAULT}`}>
                          {f.level}
                        </span>
                      )}
                      {f.occurrences > 1 && (
                        <span className="text-[12px] font-semibold text-violet-600 ml-auto">
                          seen in {f.occurrences} scans
                        </span>
                      )}
                    </div>
                    <div className="mt-1 text-sm font-semibold text-slate-800">{f.title}</div>
                    <div className={`mt-0.5 text-xs text-slate-600 leading-relaxed whitespace-pre-line ${fExpanded ? '' : 'line-clamp-2'}`}>
                      {f.body}
                    </div>
                    {fLong && (
                      <button
                        onClick={() => toggleFindingExpanded(f.id)}
                        aria-expanded={fExpanded}
                        className="mt-0.5 text-[11px] font-semibold text-violet-600 hover:text-violet-800"
                      >
                        {fExpanded ? 'Show less' : 'Show more'}
                      </button>
                    )}
                    {/* Detail (provenance + evidence chips + per-kind evidence)
                        shows only when expanded — collapsed card stays at title
                        + ~2 lines so the panel reads as a scannable list. */}
                    {fExpanded && (<>
                    {/* Provenance line — when first detected + how sure, so a
                        finding reads as evidence-backed, not just an alert. */}
                    {(f.first_seen || f.confidence || f.baseline_window) && (
                      <div className="mt-1 text-[11px] text-slate-400 flex flex-wrap gap-x-2">
                        {f.first_seen && (
                          <span title={`First seen ${f.first_seen}; last seen ${f.last_seen}`}>
                            Detected {(() => { try { return new Date(f.first_seen).toLocaleDateString(); } catch { return f.first_seen.slice(0, 10); } })()}
                          </span>
                        )}
                        {f.confidence && (
                          <span>
                            · {f.confidence} confidence
                            {typeof f.confidence_score === 'number' ? ` (${Math.round(f.confidence_score * 100)}%)` : ''}
                          </span>
                        )}
                        {typeof f.evidence_count === 'number' && f.evidence_count > 0 && (
                          <span>· {f.evidence_count} signal{f.evidence_count === 1 ? '' : 's'}</span>
                        )}
                        {f.baseline_window && <span>· baseline: {f.baseline_window}</span>}
                      </div>
                    )}
                    {Array.isArray(f.evidence?.workflows) && f.evidence.workflows.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1">
                        {f.evidence.workflows.slice(0, 4).map((w: any) => (
                          <span
                            key={w.id}
                            className="text-[12px] px-1.5 py-0.5 bg-slate-100 text-slate-700 rounded border border-slate-200"
                            title={w.id}
                          >
                            {w.name}
                          </span>
                        ))}
                        {f.evidence.workflows.length > 4 && (
                          <span className="text-[12px] px-1.5 py-0.5 text-slate-500">
                            +{f.evidence.workflows.length - 4} more
                          </span>
                        )}
                      </div>
                    )}
                    {/* 2026-06-08 — per-kind evidence renderer: schema_drift
                        changes, quality assertion detail, env_crossings,
                        unapproved-dest list, cost streak, etc. Returns
                        null for kinds without a custom renderer. */}
                    {renderKindEvidence(f)}
                    </>)}
                    <div className="mt-3 flex items-center gap-2">
                      <button
                        onClick={() => {
                          // Open the resolve modal so the operator can
                          // optionally drop a fix_note (→ PROPOSED
                          // Memory-Layer lesson via the resolve→lesson
                          // capture loop). They can still hit Resolve
                          // empty for the original behaviour.
                          setResolveModalForId(f.id);
                          setResolveModalNote('');
                        }}
                        disabled={loading}
                        className="text-xs font-semibold text-emerald-700 hover:text-emerald-800 disabled:opacity-50"
                        title="I've taken action - close this finding and optionally capture how you fixed it as a lesson"
                      >
                        Mark resolved...
                      </button>
                      <span className="text-slate-300">·</span>
                      <button
                        onClick={() => handleDismiss(f.id)}
                        disabled={loading}
                        className="text-xs font-semibold text-slate-600 hover:text-slate-800 disabled:opacity-50"
                        title="This pattern is intentional — never flag again"
                      >
                        Dismiss (intentional)
                      </button>
                      {/* 2026-06-16 — hand the finding to the Copilot. Opens
                          the assistant dock pre-filled with the finding inline
                          (so it can advise even before calling the backend
                          list_steward_findings tool). Closes this dropdown so
                          the dock isn't rendered behind it. */}
                      <span className="text-slate-300">·</span>
                      <button
                        onClick={() => {
                          const label = KIND_LABEL[f.kind] || f.kind;
                          askCopilot(
                            `The Steward flagged a ${label} finding (${f.severity.toUpperCase()}): ` +
                            `"${f.title}".\n\n${f.body}\n\n` +
                            `What does this mean, and what's the best way to resolve it?`,
                          );
                          setOpen(false);
                        }}
                        className="text-xs font-semibold text-violet-700 hover:text-violet-900 ml-auto inline-flex items-center gap-1"
                        title="Ask the Copilot to explain this finding and suggest a fix"
                      >
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M12 3a9 9 0 0 0-9 9c0 1.6.4 3.1 1.2 4.4L3 21l4.6-1.2A9 9 0 1 0 12 3Z" />
                        </svg>
                        Ask Copilot
                      </button>
                    </div>
                  </div>
                    );
                  })}
                </>
              )
            )}

            {tab === 'memory' && (
              memory === null ? (
                <div className="px-5 py-12 text-center text-xs text-slate-400">Loading audit trail…</div>
              ) : memory.events.length === 0 ? (
                <div className="px-5 py-12 text-center text-sm text-slate-400">
                  <div className="mb-2">No events yet</div>
                  <div className="text-[13px]">
                    Every emit, dismiss, and resolve is logged here. The
                    Steward uses this trail to escalate findings you keep
                    ignoring and to flag rebounded ones.
                  </div>
                </div>
              ) : (
                <div>
                  {/* Persistent-occurrence overview — the proof */}
                  {Object.keys(memory.persistent_occurrences).length > 0 && (
                    <div className="px-5 py-3 bg-violet-50/50 border-b border-violet-100">
                      <div className="text-[12px] font-bold text-violet-800 uppercase tracking-wide mb-1.5">
                        Persistent occurrence counts
                      </div>
                      <div className="text-[13px] text-violet-700">
                        Distinct scans in which each signature has surfaced.
                        Crossing the escalation threshold ({settings?.escalate_after_n_occurrences ?? 5})
                        bumps severity one step.
                      </div>
                      <div className="mt-2 space-y-0.5">
                        {Object.entries(memory.persistent_occurrences)
                          .sort((a, b) => b[1] - a[1])
                          .slice(0, 5)
                          .map(([sig, n]) => (
                            <div key={sig} className="flex items-center justify-between text-[13px]">
                              <code className="font-mono text-violet-900">{sig.slice(0, 12)}…</code>
                              <span className={`font-bold ${n >= (settings?.escalate_after_n_occurrences ?? 5) ? 'text-red-600' : 'text-slate-700'}`}>
                                {n} scans
                              </span>
                            </div>
                          ))}
                      </div>
                    </div>
                  )}
                  {/* Event stream */}
                  <div className="divide-y divide-slate-100">
                    {memory.events.map((ev, i) => (
                      <div key={i} className="px-5 py-2.5 text-[13px]">
                        <div className="flex items-center gap-2">
                          <span className={`font-bold uppercase tracking-wide text-[9px] px-1.5 py-0.5 rounded ${
                            ev.kind === 'emit' ? 'bg-violet-100 text-violet-700' :
                            ev.kind === 'dismiss' ? 'bg-slate-200 text-slate-700' :
                            'bg-emerald-100 text-emerald-700'
                          }`}>
                            {ev.kind}
                          </span>
                          <code className="font-mono text-slate-500">{ev.signature?.slice(0, 12)}…</code>
                          <span className="text-slate-400 ml-auto">{ev.ts.slice(11, 19)} UTC</span>
                        </div>
                        {ev.reason && (
                          <div className="mt-1 text-slate-600 italic">"{ev.reason}"</div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )
            )}

            {tab === 'settings' && (
              settings === null ? (
                <div className="px-5 py-12 text-center text-xs text-slate-400">Loading settings…</div>
              ) : (
                <div className="px-5 py-3 space-y-3">
                  <Toggle
                    label="Enable Steward"
                    help="Master kill-switch. Turn off if the Steward is noisy for your workspace size."
                    checked={settings.enabled}
                    onChange={(v) => updateSetting({ enabled: v })}
                    disabled={loading}
                  />
                  <Toggle
                    label="Scan on save"
                    help="Re-scan after every workflow save. Sub-50ms on typical OSS workspaces; no executor impact."
                    checked={settings.scan_on_save}
                    onChange={(v) => updateSetting({ scan_on_save: v })}
                    disabled={loading}
                  />
                  <Select
                    label="Minimum severity"
                    help="Hide findings below this severity. P3 shows everything."
                    value={settings.min_severity}
                    options={[['p3', 'P3 (info — show everything)'], ['p2', 'P2 (review-worthy)'], ['p1', 'P1 (production-blocker only)']]}
                    onChange={(v) => updateSetting({ min_severity: v as Severity })}
                    disabled={loading}
                  />
                  <NumberField
                    label="Escalate after N occurrences"
                    help="When a finding has been surfaced in this many separate scans without resolution, its severity bumps one step (P3→P2→P1). Lower = more aggressive."
                    value={settings.escalate_after_n_occurrences}
                    min={2}
                    max={50}
                    onChange={(v) => updateSetting({ escalate_after_n_occurrences: v })}
                    disabled={loading}
                  />
                  <NumberField
                    label="Escalate min hours since first"
                    help="Minimum age (hours) of the FIRST emit before severity escalation kicks in. Combined with the count threshold, this prevents a 60-second cron pipeline from racing to P1 in 5 minutes. Default 24h (one operator workday). Set to 0 to disable the time clamp."
                    value={settings.escalate_min_hours_since_first}
                    min={0}
                    max={720}
                    onChange={(v) => updateSetting({ escalate_min_hours_since_first: v })}
                    disabled={loading}
                  />
                  <NumberField
                    label="Auto-stale days"
                    help="Findings untouched for this many days auto-age into 'stale' and hide from the default view."
                    value={settings.auto_stale_days}
                    min={1}
                    max={365}
                    onChange={(v) => updateSetting({ auto_stale_days: v })}
                    disabled={loading}
                  />
                  {/* ── Notification section ── */}
                  <div className="pt-2.5 border-t border-slate-200">
                    <div className="text-[12px] font-bold text-violet-700 uppercase tracking-wide mb-2.5 flex items-center gap-1.5">
                      {/* Bell icon makes the section break VISIBLE at
                          a glance so a user scanning the settings tab
                          knows there's a distinct sub-area below. */}
                      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
                        <path d="M13.73 21a2 2 0 0 1-3.46 0" />
                      </svg>
                      Notification bell
                    </div>
                    <div className="space-y-3">
                      <Toggle
                        label="Notify on new findings"
                        help="Write a row to the notification bell when a new or newly-escalated finding appears. De-dup is enforced — re-scans of unchanged findings never spam the bell."
                        checked={settings.notify_on_finding}
                        onChange={(v) => updateSetting({ notify_on_finding: v })}
                        disabled={loading}
                      />
                      <Select
                        label="Minimum severity to notify"
                        help="Bell pings only at this severity or higher. Default P2 keeps info-only P3 findings in the eye-icon badge without spamming the bell."
                        value={settings.notify_min_severity}
                        options={[['p3', 'P3 (everything)'], ['p2', 'P2 (review-worthy and up)'], ['p1', 'P1 only (least noisy)']]}
                        onChange={(v) => updateSetting({ notify_min_severity: v as Severity })}
                        disabled={loading || !settings.notify_on_finding}
                      />
                    </div>
                  </div>
                </div>
              )
            )}
          </div>

          {/* 2026-06-06 (R7) — footer reminder bumped from "silent
              quiet grey" to "visible trust statement" per reviewer
              feedback (Review 2: "important trust statement... should
              probably have a bit more contrast or a small icon
              treatment"). The shield icon + slightly darker text +
              violet accent reads as "this is the read-only guarantee"
              instead of vanishing into the chrome. */}
          <div className="px-5 py-2.5 border-t border-slate-100 bg-violet-50/60 text-[12px] text-slate-700 flex items-center justify-center gap-1.5">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#7c3aed" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
            </svg>
            <span><strong className="font-semibold text-violet-700">Read-only.</strong> Steward never modifies pipelines.</span>
          </div>
          {/* close inner white panel */}
          </div>
        {/* close gradient-border wrapper */}
        </div>
      )}

      {/* 2026-06-08 — resolve-with-fix-note modal. Rendered at the
          root of the badge component (not inside the dropdown) so it
          floats above everything when active. The backend already
          accepts `fix_note` in the resolve POST body and turns
          non-empty notes into a PROPOSED lesson via the resolve→
          lesson capture loop (shipped 2026-06-07). */}
      {resolveModalForId && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black/40"
          onClick={() => setResolveModalForId(null)}
        >
          <div
            className="bg-white rounded-xl shadow-2xl w-[min(520px,92vw)] p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#059669" strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round">
                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" />
              </svg>
              <div className="text-sm font-semibold text-slate-800">Mark resolved</div>
            </div>
            <div className="mt-3 text-[13px] text-slate-600 leading-relaxed">
              Closing this finding. If you'd like, describe what fixed it - your note
              becomes a <strong>PROPOSED lesson</strong> in the Memory tab that a teammate
              can approve. Future operators searching for the same failure will find it.
            </div>
            <textarea
              autoFocus
              value={resolveModalNote}
              onChange={(e) => setResolveModalNote(e.target.value)}
              placeholder="e.g. Consolidated wf-A and wf-B onto wf-A; deleted wf-B after stakeholder sign-off."
              rows={4}
              className="mt-3 w-full text-[13px] px-3 py-2 border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-violet-500 focus:border-violet-500 resize-none"
            />
            <div className="mt-2 text-[11px] text-slate-500">
              Sanitised on the backend (AWS keys / bearer tokens / passwords /
              URI creds / private IPs all redacted) before any storage.
            </div>
            <div className="mt-4 flex items-center justify-end gap-2">
              <button
                onClick={() => setResolveModalForId(null)}
                className="text-xs font-semibold text-slate-600 hover:text-slate-800 px-3 py-1.5 rounded"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  const id = resolveModalForId;
                  const note = resolveModalNote;
                  setResolveModalForId(null);
                  setResolveModalNote('');
                  if (id) void handleResolve(id, note);
                }}
                disabled={loading}
                className="text-xs font-semibold bg-emerald-600 hover:bg-emerald-700 text-white px-3 py-1.5 rounded disabled:opacity-50"
              >
                {resolveModalNote.trim() ? 'Resolve + capture lesson' : 'Resolve (no lesson)'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 2026-06-08 — one-shot lesson-captured toast. Auto-clears 5s
          after lesson_id appears in the resolve response. Top-right
          slot so it doesn't fight the dropdown for attention. */}
      {lessonToast && (
        <div className="fixed top-4 right-4 z-[110] bg-emerald-600 text-white text-[13px] px-4 py-2 rounded-md shadow-lg flex items-center gap-2">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="20 6 9 17 4 12" />
          </svg>
          {lessonToast}
        </div>
      )}
    </div>
  );
}

// ── Small inline controls for the Settings tab ──────────────────────

function Toggle({
  label, help, checked, onChange, disabled,
}: { label: string; help: string; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <div>
      <div className="flex items-center justify-between gap-3">
        <label className="text-xs font-semibold text-slate-800">{label}</label>
        <button
          onClick={() => onChange(!checked)}
          disabled={disabled}
          className={`relative w-10 h-5 rounded-full transition-colors ${checked ? 'bg-violet-500' : 'bg-slate-300'} disabled:opacity-50`}
          aria-pressed={checked}
        >
          <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform ${checked ? 'translate-x-5' : ''}`} />
        </button>
      </div>
      <div className="mt-0.5 text-[12px] text-slate-500 leading-snug">{help}</div>
    </div>
  );
}

function Select({
  label, help, value, options, onChange, disabled,
}: { label: string; help: string; value: string; options: [string, string][]; onChange: (v: string) => void; disabled?: boolean }) {
  // 2026-06-06 — label + dropdown on the same row (Toggle pattern),
  // help text full-width below. The previous `w-full` select stretched
  // to the dropdown's full 640px which read as broken layout for a
  // 3-option severity picker.
  return (
    <div>
      <div className="flex items-center justify-between gap-3">
        <label className="text-xs font-semibold text-slate-800">{label}</label>
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          className="text-xs border border-slate-300 rounded px-2 py-1.5 bg-white disabled:opacity-50 max-w-[260px]"
        >
          {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </div>
      <div className="mt-0.5 text-[12px] text-slate-500 leading-snug">{help}</div>
    </div>
  );
}

function NumberField({
  label, help, value, min, max, onChange, disabled,
}: { label: string; help: string; value: number; min: number; max: number; onChange: (v: number) => void; disabled?: boolean }) {
  // 2026-06-06 — same as Select: row layout with a compact input.
  // Numbers in here are at most 3 digits (5, 24, 30, max 720) — they
  // do not need 600px of width.
  const [local, setLocal] = useState(String(value));
  useEffect(() => { setLocal(String(value)); }, [value]);
  return (
    <div>
      <div className="flex items-center justify-between gap-3">
        <label className="text-xs font-semibold text-slate-800">{label}</label>
        <input
          type="number"
          value={local}
          min={min}
          max={max}
          onChange={(e) => setLocal(e.target.value)}
          onBlur={() => {
            const n = parseInt(local, 10);
            if (!Number.isNaN(n) && n >= min && n <= max) onChange(n);
            else setLocal(String(value));
          }}
          disabled={disabled}
          className="text-xs border border-slate-300 rounded px-2 py-1.5 bg-white disabled:opacity-50 w-24 text-right tabular-nums"
        />
      </div>
      <div className="mt-0.5 text-[12px] text-slate-500 leading-snug">{help}</div>
    </div>
  );
}



