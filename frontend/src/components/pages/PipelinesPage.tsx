import { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { useTableColumns, type TColumn, type TColumnGroup } from '../shared/TableToolbar';
// xyflow's `Node` type shadows the global DOM `Node`, which silently
// breaks every `e.target as Node` cast in this file's event handlers.
// Alias to `FlowNode` so DOM Node remains the unqualified name.
import { ReactFlow, Background, BackgroundVariant, MarkerType, type Node as FlowNode, type Edge } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { api } from '../../api/client';
import { navigateTo, navigateToSubRoute } from '../../router';
import RunStatusSparkline, { type RunStatusKind } from '../shared/RunStatusSparkline';

// Normalise a backend status string into the sparkline's union type.
// Anything we don't recognise becomes 'unknown' so the chip falls back
// to the neutral colour rather than throwing.
function _toSparklineStatus(raw: string): RunStatusKind {
  const s = (raw || '').toLowerCase();
  if (s === 'success' || s === 'succeeded' || s === 'ok') return 'success';
  if (s === 'error' || s === 'failed' || s === 'fail') return 'error';
  if (s === 'warning' || s === 'warn' || s === 'timeout') return 'warning';
  if (s === 'running' || s === 'in_progress' || s === 'pending') return 'running';
  if (s === 'skipped' || s === 'cancelled' || s === 'canceled') return 'skipped';
  return 'unknown';
}
import { useWorkflowStore } from '../../stores/workflowStore';
import { toast } from '../Toast';
import { nodeTypes, edgeTypes } from '../nodes';
import RoleGate, { useCan } from '../../auth/RoleGate';
import ReadOnlyBanner from '../../auth/ReadOnlyBanner';
import { uiConfirm, uiPrompt } from '../../ui/dialog';
import ProjectContextBar from '../layout/ProjectContextBar';
import { PlanModal } from '../PlanModal';
import TierChip from '../shared/TierChip';
import PageHeader from '../shared/PageHeader';
import HeroCard from '../shared/HeroCard';
import { DelayedSkeleton, SkeletonTableRow } from '../shared/Skeleton';
import MoveToProjectButton from '../shared/MoveToProjectButton';
import { DensityToggle, useDensity } from '../shared/DensityToggle';
import TimeAgo from '../shared/TimeAgo';
import DetailDrawer from '../shared/DetailDrawer';
import Icon from '../shared/Icon';
import HubTabs, { WORKFLOWS_TABS } from '../HubTabs';
import PreRunBanner, { type RunSafetyMode } from '../PreRunBanner';
import RunWithParametersDialog from '../RunWithParametersDialog';
import PublishTestModal from '../PublishTestModal';
import SaveAsTemplateDialog from '../SaveAsTemplateDialog';
import type { WorkflowParameter } from '../../stores/workflowStore';
import { usePageContext } from '../../hooks/usePageContext';

/* ── Node appearance constants (mirrored from workflowStore) ── */
const _NODE_COLORS: Record<string, string> = {
  'csv-source': '#3b82f6', 'json-source': '#3b82f6', 'parquet-source': '#3b82f6',
  'excel-source': '#3b82f6', 'xml-source': '#3b82f6', 'database-source': '#3b82f6',
  'rest-api-source': '#3b82f6', 's3-source': '#3b82f6', 'kafka-source': '#3b82f6',
  'ftp-source': '#3b82f6', 'gsheets-source': '#3b82f6', 'delta-source': '#3b82f6',
  'file-source': '#3b82f6', 'saas-source': '#3b82f6', 'warehouse-source': '#3b82f6',
  transform: '#10b981', filter: '#10b981', sort: '#10b981', rename: '#10b981',
  typecast: '#10b981', 'derived-column': '#10b981', sample: '#10b981', validate: '#10b981',
  join: '#8b5cf6', union: '#8b5cf6', lookup: '#8b5cf6', deduplicate: '#8b5cf6',
  aggregate: '#8b5cf6', pivot: '#8b5cf6', unpivot: '#8b5cf6',
  'if-condition': '#eab308', switch: '#eab308', foreach: '#eab308', 'until-loop': '#eab308',
  'wait-delay': '#eab308', 'set-variable': '#eab308', 'exec-pipeline': '#eab308', 'cond-split': '#eab308',
  'http-request': '#f97316', webhook: '#f97316', 'code-script': '#f97316', 'copy-data': '#f97316',
  delete: '#f97316', 'get-metadata': '#f97316', 'send-email': '#f97316', 'slack-teams': '#f97316',
  'csv-sink': '#8b5cf6', 'json-sink': '#8b5cf6', 'parquet-sink': '#8b5cf6', 'excel-sink': '#8b5cf6',
  'database-sink': '#8b5cf6', 's3-sink': '#8b5cf6', 'kafka-sink': '#8b5cf6', 'api-sink': '#8b5cf6',
  'webhook-sink': '#8b5cf6', 'email-sink': '#8b5cf6', 'delta-sink': '#8b5cf6', 'warehouse-sink': '#8b5cf6',
};
const _NODE_ICONS: Record<string, string> = {
  'csv-source': '📄', 'json-source': '📋', 'parquet-source': '🗂️', 'excel-source': '📊',
  'database-source': '🗄️', 'rest-api-source': '🌐', 's3-source': '☁️', 'kafka-source': '📡',
  transform: '⚡', filter: '🔍', sort: '↕️', join: '🔗', union: '📎', aggregate: '📊',
  'if-condition': '🔀', switch: '🔀', foreach: '🔁', 'set-variable': '📝',
  'http-request': '🌐', 'code-script': '💻',
  'csv-sink': '📤', 'json-sink': '📤', 'parquet-sink': '📤', 'database-sink': '🗄️',
  's3-sink': '☁️', output: '📤',
};
const _NODE_CAT: Record<string, string> = {
  'csv-source': 'source', 'json-source': 'source', 'parquet-source': 'source',
  'excel-source': 'source', 'database-source': 'source', 'rest-api-source': 'source',
  's3-source': 'source', 'kafka-source': 'source', 'file-source': 'source',
  transform: 'transform', filter: 'transform', sort: 'transform',
  join: 'combine', union: 'combine', aggregate: 'combine',
  'if-condition': 'flow', switch: 'flow', foreach: 'flow',
  'csv-sink': 'output', 'json-sink': 'output', 'parquet-sink': 'output',
  'database-sink': 'output', 's3-sink': 'output', output: 'output',
};

/** Convert API workflow data to React Flow nodes + edges for preview.
 *  When stepResults is provided, nodes get execution status (running/success/error). */
function previewWorkflowToFlow(wf: any, isRunning = false, stepResults?: Record<string, any>): { nodes: FlowNode[]; edges: Edge[] } {
  const COND_COLORS: Record<string, string> = { completion: '#6366f1', success: '#22c55e', failure: '#ef4444' };
  const steps = wf.steps || [];
  const conns = wf.connections || [];

  // Build nodes — apply execution status if running
  const allRunning = stepResults?.__all_running;
  const rawNodes: FlowNode[] = steps.map((s: any) => {
    let status: string | undefined;
    if (isRunning && allRunning) {
      status = 'running';
    } else if (stepResults && stepResults[s.id]) {
      status = stepResults[s.id].status; // 'success' | 'error' | 'running' | 'skipped'
    }
    return {
      id: s.id,
      type: 'fpulseNode',
      position: { x: s.position?.x ?? 0, y: s.position?.y ?? 0 },
      data: {
        label: s.label || s.type,
        stepType: s.type,
        params: s.params || {},
        color: _NODE_COLORS[s.type] || '#94a3b8',
        icon: _NODE_ICONS[s.type] || '⬜',
        category: _NODE_CAT[s.type] || 'transform',
        status,
      },
    };
  });

  const edges: Edge[] = conns.map((c: any) => {
    const cond = c.condition || 'completion';
    const col = COND_COLORS[cond] || '#6366f1';
    return {
      id: `e-${c.from_step}-${c.to_step}`,
      source: c.from_step,
      target: c.to_step,
      type: 'custom',
      animated: false,
      data: { condition: cond },
      style: { stroke: col, strokeWidth: 2 },
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: col },
    };
  });

  // Auto-layout if positions are all at (0,0)
  const allZero = rawNodes.every(n => n.position.x === 0 && n.position.y === 0);
  if (allZero && rawNodes.length > 0) {
    // Simple left-to-right layout
    const inDeg: Record<string, number> = {};
    const kids: Record<string, string[]> = {};
    for (const n of rawNodes) { inDeg[n.id] = 0; kids[n.id] = []; }
    for (const e of edges) { if (inDeg[e.target] !== undefined) { inDeg[e.target]++; kids[e.source]?.push(e.target); } }
    const queue: string[] = [];
    const layer: Record<string, number> = {};
    for (const id of Object.keys(inDeg)) { if (inDeg[id] === 0) { queue.push(id); layer[id] = 0; } }
    const visited = new Set<string>();
    while (queue.length > 0) {
      const cur = queue.shift()!;
      if (visited.has(cur)) continue;
      visited.add(cur);
      for (const ch of (kids[cur] || [])) {
        layer[ch] = Math.max(layer[ch] || 0, (layer[cur] || 0) + 1);
        inDeg[ch]--;
        if (inDeg[ch] === 0) queue.push(ch);
      }
    }
    for (const n of rawNodes) { if (!(n.id in layer)) layer[n.id] = 0; }
    const layers: Record<number, string[]> = {};
    for (const [id, l] of Object.entries(layer)) { if (!layers[l]) layers[l] = []; layers[l].push(id); }
    for (const [li, ids] of Object.entries(layers)) {
      const layerIdx = parseInt(li);
      const totalH = (ids.length - 1) * 120;
      const startY = 80 + (rawNodes.length > 1 ? -totalH / 2 + 200 : 0);
      ids.forEach((id, idx) => {
        const node = rawNodes.find(n => n.id === id);
        if (node) node.position = { x: 80 + layerIdx * 300, y: startY + idx * 120 };
      });
    }
  }

  return { nodes: rawNodes, edges };
}

/* ── Quick Action Dialogs (Schedule / Alert / Run) ── */

const QUICK_ALERT_CONDITIONS = [
  { value: 'on_failure', label: 'On Failure', color: 'text-red-500' },
  { value: 'on_success', label: 'On Success', color: 'text-green-500' },
  { value: 'on_long_running', label: 'Long Running', color: 'text-orange-500' },
  { value: 'on_overlap', label: 'On Overlap', color: 'text-purple-500' },
];

/* ── Friendly schedule builder ──
 * Cron syntax is hidden from the OSS UI entirely (user feedback,
 * 2026-05-11: "Why are you giving cron - It is confusing"). The dialog
 * exposes a Frequency picker (Every X minutes / Hourly / Daily / Weekly)
 * and assembles the cron string under the hood. Existing schedules
 * created elsewhere are parsed back into the friendly picker on Edit;
 * anything we can't parse falls back to a safe "Daily at midnight"
 * default the user must re-confirm.
 */

type Frequency = 'minutes' | 'hourly' | 'daily' | 'weekly';

const WEEKDAYS = [
  { value: 0, label: 'Sun' }, { value: 1, label: 'Mon' }, { value: 2, label: 'Tue' },
  { value: 3, label: 'Wed' }, { value: 4, label: 'Thu' }, { value: 5, label: 'Fri' },
  { value: 6, label: 'Sat' },
];

function buildCron(opts: {
  frequency: Frequency;
  intervalMinutes: number;
  hour: number;
  minute: number;
  weekday: number;
}): string {
  const { frequency, intervalMinutes, hour, minute, weekday } = opts;
  switch (frequency) {
    case 'minutes':
      // Cron `*/N * * * *` — clamp N to [1, 59].
      return `*/${Math.min(59, Math.max(1, intervalMinutes))} * * * *`;
    case 'hourly':
      return `${minute} * * * *`;
    case 'daily':
      return `${minute} ${hour} * * *`;
    case 'weekly':
      return `${minute} ${hour} * * ${weekday}`;
  }
}

/** Weekly multi-day / multi-time builder.
 *  Produces ONE cron string per unique minute value; same-minute times
 *  collapse their hours into a comma-list (`0 9,15 * * 1,3,5`).
 *  Different-minute times can't share a cron expression, so they
 *  become separate schedules created in sequence by handleSave. */
function buildCronsWeekly(days: number[], times: Array<{ hour: number; minute: number }>): string[] {
  if (days.length === 0 || times.length === 0) return [];
  const dow = [...new Set(days)].sort((a, b) => a - b).join(',');
  const byMinute: Record<number, number[]> = {};
  for (const t of times) {
    const m = Math.min(59, Math.max(0, t.minute | 0));
    const h = Math.min(23, Math.max(0, t.hour | 0));
    if (!byMinute[m]) byMinute[m] = [];
    byMinute[m].push(h);
  }
  return Object.entries(byMinute).map(([m, hours]) => {
    const h = [...new Set(hours)].sort((a, b) => a - b).join(',');
    return `${m} ${h} * * ${dow}`;
  });
}

function describeWeekly(days: number[], times: Array<{ hour: number; minute: number }>): string {
  if (days.length === 0) return 'Pick at least one day';
  if (times.length === 0) return 'Pick at least one time';
  const dayLabel = days.length === 7
    ? 'day'
    : [...days].sort((a, b) => a - b).map(d => WEEKDAYS[d]?.label || `?`).join(', ');
  const fmtTime = (h: number, m: number) => {
    const period = h < 12 ? 'AM' : 'PM';
    const hr12 = h % 12 === 0 ? 12 : h % 12;
    return `${hr12}:${String(m).padStart(2, '0')} ${period}`;
  };
  const sortedTimes = [...times].sort((a, b) => (a.hour - b.hour) || (a.minute - b.minute));
  const timeLabel = sortedTimes.map(t => fmtTime(t.hour, t.minute)).join(', ');
  return `Every ${dayLabel} at ${timeLabel}`;
}

/** Inverse of buildCron — parse a cron string back into the friendly
 *  builder state so editing an existing schedule pre-fills the picker.
 *  Returns null when the cron doesn't match a builder-produced shape;
 *  callers should fall back to a default. Weekly mode supports
 *  comma-list values in `dow` and `h` (the multi-day / multi-time
 *  outputs from buildCronsWeekly) — they round-trip into the
 *  `weeklyDays` / `weeklyTimes` arrays.
 */
function parseCronToBuilder(cron: string): {
  frequency: Frequency;
  intervalMinutes: number;
  hour: number;
  minute: number;
  weekday: number;
  weeklyDays: number[];
  weeklyTimes: Array<{ hour: number; minute: number }>;
} | null {
  const parts = (cron || '').trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [m, h, dom, mon, dow] = parts;
  if (dom !== '*' || mon !== '*') return null;
  const everyNMin = m.match(/^\*\/(\d+)$/);
  if (everyNMin && h === '*' && dow === '*') {
    return { frequency: 'minutes', intervalMinutes: parseInt(everyNMin[1], 10), hour: 0, minute: 0, weekday: 1, weeklyDays: [1], weeklyTimes: [{ hour: 0, minute: 0 }] };
  }
  if (/^\d+$/.test(m) && h === '*' && dow === '*') {
    return { frequency: 'hourly', intervalMinutes: 15, hour: 0, minute: parseInt(m, 10), weekday: 1, weeklyDays: [1], weeklyTimes: [{ hour: 0, minute: 0 }] };
  }
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && dow === '*') {
    return { frequency: 'daily', intervalMinutes: 15, hour: parseInt(h, 10), minute: parseInt(m, 10), weekday: 1, weeklyDays: [1], weeklyTimes: [{ hour: 0, minute: 0 }] };
  }
  // Weekly — `dow` and `h` may each be a single value or a comma list.
  const weeklyRe = /^\d+(?:,\d+)*$/;
  if (/^\d+$/.test(m) && weeklyRe.test(h) && weeklyRe.test(dow)) {
    const mi = parseInt(m, 10);
    const days = dow.split(',').map(s => parseInt(s, 10)).filter(n => n >= 0 && n <= 6);
    const hours = h.split(',').map(s => parseInt(s, 10)).filter(n => n >= 0 && n <= 23);
    return {
      frequency: 'weekly',
      intervalMinutes: 15,
      hour: hours[0] ?? 0,
      minute: mi,
      weekday: days[0] ?? 1,
      weeklyDays: days.length ? days : [1],
      weeklyTimes: hours.length ? hours.map(hr => ({ hour: hr, minute: mi })) : [{ hour: 0, minute: mi }],
    };
  }
  return null;
}

function describeCron(cron: string): string {
  // Best-effort English description of the standard 5-field cron
  // expressions this builder produces. Falls back to the raw cron
  // string for anything we don't recognise — keeps the preview honest
  // when the user types a custom expression we can't parse.
  const parts = (cron || '').trim().split(/\s+/);
  if (parts.length !== 5) return cron || '—';
  const [m, h, dom, mon, dow] = parts;
  const fmtTime = (hh: string, mm: string) => {
    const hi = parseInt(hh, 10);
    const mi = parseInt(mm, 10);
    if (Number.isNaN(hi) || Number.isNaN(mi)) return `${hh}:${mm}`;
    const period = hi < 12 ? 'AM' : 'PM';
    const hr12 = hi % 12 === 0 ? 12 : hi % 12;
    return `${hr12}:${mi.toString().padStart(2, '0')} ${period}`;
  };
  // Every N minutes
  const everyNMin = m.match(/^\*\/(\d+)$/);
  if (everyNMin && h === '*' && dom === '*' && mon === '*' && dow === '*') {
    const n = parseInt(everyNMin[1], 10);
    return n === 1 ? 'Every minute' : `Every ${n} minutes`;
  }
  // Every N hours at minute M
  const everyNHr = h.match(/^\*\/(\d+)$/);
  if (/^\d+$/.test(m) && everyNHr && dom === '*' && mon === '*' && dow === '*') {
    const n = parseInt(everyNHr[1], 10);
    const minStr = parseInt(m, 10) === 0 ? '' : ` at minute ${m}`;
    return n === 1 ? `Every hour${minStr}` : `Every ${n} hours${minStr}`;
  }
  // Hourly at minute M — concise form. Earlier version included a
  // "(12:45, 1:45, 2:45…)" example tail which made the description
  // ~50 chars long; in the Pipelines table's narrow Schedule column
  // that wraps to 5+ lines and balloons row height. The example
  // illustration now lives in the Quick Schedule dialog's helper
  // text only (where space is available); the describer is one short
  // line that fits any column.
  if (/^\d+$/.test(m) && h === '*' && dom === '*' && mon === '*' && dow === '*') {
    const mm = String(parseInt(m, 10)).padStart(2, '0');
    return parseInt(m, 10) === 0
      ? 'Every hour, on the hour'
      : `Every hour at :${mm}`;
  }
  // Daily at HH:MM
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && dom === '*' && mon === '*' && dow === '*') {
    return `Every day at ${fmtTime(h, m)}`;
  }
  // Weekly on day at HH:MM
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && dom === '*' && mon === '*' && /^\d+$/.test(dow)) {
    const day = WEEKDAYS[parseInt(dow, 10)]?.label || `day ${dow}`;
    return `Every ${day} at ${fmtTime(h, m)}`;
  }
  return cron;
}

function QuickScheduleDialog({ pipelineId, pipelineName, onClose, onConfigureAlerts }: {
  pipelineId: string;
  pipelineName: string;
  onClose: () => void;
  /** Called when the user clicks "Set up failure alerts" — closes the
   *  schedule dialog and opens the Quick Alert dialog for the same pipeline. */
  onConfigureAlerts?: (id: string, name: string) => void;
}) {
  // Friendly builder state — defaults to "Daily / midnight" but the
  // RUNS preview stays hidden until `touched` flips true so the user
  // doesn't see a misleading default ("Every day at 12:00 AM") before
  // they've picked anything. Touched flips when the user clicks a
  // frequency chip or changes any field value, or when we pre-fill
  // for an existing-schedule edit.
  const [frequency, setFrequency] = useState<Frequency>('daily');
  const [intervalMinutes, setIntervalMinutes] = useState(15);
  const [hour, setHour] = useState(0);
  const [minute, setMinute] = useState(0);
  const [weekday, setWeekday] = useState(1); // Mon — used only by the LEGACY single-day weekly path (kept for backwards compat with the parser; the live UI uses weeklyDays/weeklyTimes below).
  // Weekly multi-day / multi-time state. Days are weekday numbers
  // (0=Sun..6=Sat); times are concrete hour/minute slots. Lets the
  // user say "Every Mon, Wed, Fri at 9am and 3pm" in one schedule.
  const [weeklyDays, setWeeklyDays] = useState<number[]>([1]);
  const [weeklyTimes, setWeeklyTimes] = useState<Array<{ hour: number; minute: number }>>([{ hour: 0, minute: 0 }]);
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  // 2026-05-28 — when the backend's sink-safety guard rejects the
  // schedule create with `code: unsafe_for_schedule`, we surface a
  // checkbox so the user can opt in if they understand the risk
  // (e.g. "send daily digest" pipelines legitimately want email_sink
  // to re-fire on every tick). The checkbox is only revealed AFTER
  // the first failure to keep the default-create flow uncluttered.
  const [acknowledgeUnsafe, setAcknowledgeUnsafe] = useState(false);
  // Stores the structured-error fields from the most recent failed
  // schedule attempt so the dialog can show which sink was flagged.
  const [unsafeErrInfo, setUnsafeErrInfo] = useState<{
    message: string;
    sinks: Array<{ step_id?: string; step_type?: string; idempotency?: string }>;
  } | null>(null);

  // Existing schedules for this pipeline. When present, the dialog
  // renders a manage panel (toggle / update / delete) on top of the
  // builder so the user isn't forced to create a duplicate just to
  // change the cadence.
  const [existing, setExisting] = useState<any[]>([]);
  const [loadingExisting, setLoadingExisting] = useState(true);
  const [editingScheduleId, setEditingScheduleId] = useState<string | null>(null);

  const reloadExisting = useCallback(async () => {
    setLoadingExisting(true);
    try {
      const list = await api.listSchedules({ workflow_id: pipelineId });
      setExisting(Array.isArray(list) ? list : []);
    } catch {
      setExisting([]);
    } finally {
      setLoadingExisting(false);
    }
  }, [pipelineId]);

  useEffect(() => { reloadExisting(); }, [reloadExisting]);

  // For weekly mode the builder produces 1+ cron strings (one per
  // unique minute). The preview uses the friendly multi-day/time
  // describer instead of running each cron through describeCron.
  const weeklyCrons = frequency === 'weekly'
    ? buildCronsWeekly(weeklyDays, weeklyTimes)
    : [];
  const cron = frequency === 'weekly'
    ? (weeklyCrons[0] || '')
    : buildCron({ frequency, intervalMinutes, hour, minute, weekday });
  const human = frequency === 'weekly'
    ? describeWeekly(weeklyDays, weeklyTimes)
    : describeCron(cron);

  const handleSave = async () => {
    setSaving(true);
    try {
      if (editingScheduleId) {
        // Editing an existing schedule — always PUT a single cron. For
        // weekly mode we use the first cron from the multi-value
        // builder; if the user wanted a different-minute slot they'd
        // create it as a new schedule instead.
        const cronToSave = frequency === 'weekly' ? (weeklyCrons[0] || cron) : cron;
        if (!cronToSave) {
          toast.error('Pick at least one day and time');
          setSaving(false); return;
        }
        await api.updateSchedule(editingScheduleId, {
          cron_expression: cronToSave,
          schedule_type: 'cron',
          timezone: 'UTC',
        });
        toast.success('Schedule updated', human);
        setEditingScheduleId(null);
      } else if (frequency === 'weekly') {
        // Multi-day / multi-time → one schedule per distinct minute.
        // Hours of the same minute collapse into one cron. Different
        // minutes can't share a cron expression so they become
        // separate schedules (created sequentially below).
        if (weeklyCrons.length === 0) {
          toast.error('Pick at least one day and time');
          setSaving(false); return;
        }
        for (const c of weeklyCrons) {
          await api.createSchedule({
            workflow_id: pipelineId,
            name: `${pipelineName} - Schedule`,
            schedule_type: 'cron',
            cron_expression: c,
            timezone: 'UTC',
            enabled: true,
            acknowledge_side_effects: acknowledgeUnsafe,
          });
        }
        toast.success(
          weeklyCrons.length === 1 ? 'Schedule created' : `${weeklyCrons.length} schedules created`,
          human,
        );
      } else {
        await api.createSchedule({
          workflow_id: pipelineId,
          name: `${pipelineName} - Schedule`,
          schedule_type: 'cron',
          cron_expression: cron,
          timezone: 'UTC',
          enabled: true,
          acknowledge_side_effects: acknowledgeUnsafe,
        });
        toast.success('Schedule created', human);
      }
      // On success, clear the inline error banner from any prior attempt.
      setUnsafeErrInfo(null);
      setAcknowledgeUnsafe(false);
      await reloadExisting();
    } catch (err: any) {
      // 2026-05-28 — try to parse a structured `unsafe_for_schedule`
      // error so the dialog can show WHICH sink(s) are blocked and
      // surface an acknowledge checkbox. The error's raw JSON shape
      // (`{code, message, unsafe_sinks: [...]}`) is preserved by
      // client.ts on `err.message` as JSON when no `.message` key was
      // on the top-level object — but our backend's HTTPException IS
      // a top-level object with `.message`, so `err.message` here is
      // the human string and we lose the sinks list. Re-fetch the
      // structured payload from the server only when needed.
      const raw = err?.message || '';
      const looksUnsafe =
        /unsafe_for_schedule|not safe to re-run|acknowledge_side_effects/i.test(raw);
      if (looksUnsafe) {
        // Attempt to recover the sinks list from the raw error if it
        // happens to be JSON (older `_humanizeApiError` fallback path);
        // otherwise just store the message text and let the user opt
        // in via the checkbox without per-sink detail.
        let sinks: typeof unsafeErrInfo extends infer T ? (T extends { sinks: infer S } ? S : never) : never = [] as any;
        try {
          const parsed = JSON.parse(raw);
          if (parsed && Array.isArray(parsed.unsafe_sinks)) sinks = parsed.unsafe_sinks;
        } catch { /* not JSON — no per-sink detail available */ }
        setUnsafeErrInfo({ message: raw, sinks });
      }
      toast.error('Schedule failed', raw);
    }
    setSaving(false);
  };

  const handleDelete = async (scheduleId: string) => {
    if (!await uiConfirm({ title: 'Delete schedule?', message: 'The pipeline will stop running on this schedule.', confirmLabel: 'Delete', destructive: true })) return;
    try {
      await api.deleteSchedule(scheduleId);
      toast.success('Schedule deleted');
      await reloadExisting();
    } catch (err: any) {
      toast.error('Delete failed', err.message);
    }
  };

  const handleToggle = async (scheduleId: string) => {
    try {
      await api.toggleSchedule(scheduleId);
      await reloadExisting();
    } catch (err: any) {
      toast.error('Toggle failed', err.message);
    }
  };

  const handleEditExisting = (s: any) => {
    // Pre-fill the builder with the existing schedule's cron so the
    // user can tweak it and Save Changes (which PUTs instead of POSTs).
    // Parse the cron back into friendly state; if it doesn't match a
    // builder shape (manually-edited DB row, etc.) we fall back to a
    // daily-midnight default so the picker remains usable.
    setEditingScheduleId(s.id);
    const parsed = parseCronToBuilder(s.cron_expression || '');
    if (parsed) {
      setFrequency(parsed.frequency);
      setIntervalMinutes(parsed.intervalMinutes);
      setHour(parsed.hour);
      setMinute(parsed.minute);
      setWeekday(parsed.weekday);
      setWeeklyDays(parsed.weeklyDays);
      setWeeklyTimes(parsed.weeklyTimes);
    } else {
      setFrequency('daily');
      setHour(0);
      setMinute(0);
      setWeeklyDays([1]);
      setWeeklyTimes([{ hour: 0, minute: 0 }]);
    }
    setTouched(true);
  };

  const FrequencyChip = ({ value, label }: { value: Frequency; label: string }) => (
    <button
      onClick={() => { setFrequency(value); setTouched(true); }}
      className={`px-3 py-2 rounded-lg border text-xs font-medium transition-all ${
        frequency === value
          ? 'border-emerald-300 bg-emerald-50 text-emerald-700'
          : 'border-slate-200 hover:border-slate-300 text-slate-600'
      }`}
    >
      {label}
    </button>
  );

  return (
    <>
      <div className="fixed inset-0 bg-black/20 z-[60]" onClick={onClose} />
      <div className="fixed inset-0 z-[65] flex items-center justify-center pointer-events-none">
        <div className="pointer-events-auto w-[460px] max-w-[95vw] bg-white rounded-2xl shadow-2xl border border-slate-200/60 overflow-hidden">
          <div className="px-5 py-4 border-b border-slate-200/60 flex items-center justify-between">
            <div>
              <h2 className="text-sm font-bold text-slate-800 flex items-center gap-2">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-emerald-500"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>
                Quick Schedule
              </h2>
              <p className="text-xs text-slate-400 mt-0.5">{pipelineName}</p>
            </div>
            <button onClick={onClose} className="w-7 h-7 rounded-lg flex items-center justify-center text-slate-400 hover:text-slate-600 hover:bg-slate-100">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
            </button>
          </div>

          <div className="p-5 space-y-4 max-h-[70vh] overflow-y-auto">
            {/* Existing schedules — manage panel. Empty state hides the
                section entirely so a fresh pipeline still looks clean. */}
            {!loadingExisting && existing.length > 0 && (
              <div>
                <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">
                  Active schedules ({existing.length})
                </label>
                <ul className="space-y-1.5">
                  {existing.map((s) => (
                    <li key={s.id} className={`flex items-center gap-2 px-3 py-2 rounded-lg border ${
                      s.enabled ? 'border-emerald-200 bg-emerald-50/40' : 'border-slate-200 bg-slate-50'
                    }`}>
                      <span className={`w-2 h-2 rounded-full shrink-0 ${s.enabled ? 'bg-emerald-500' : 'bg-slate-400'}`} />
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-semibold text-slate-700 truncate">
                          {describeCron(s.cron_expression || '')}
                        </div>
                      </div>
                      <button
                        onClick={() => handleEditExisting(s)}
                        className="px-2 py-1 text-xs font-medium text-slate-600 hover:text-slate-800 rounded hover:bg-white border border-transparent hover:border-slate-200"
                        title="Edit cadence"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => handleToggle(s.id)}
                        className={`px-2 py-1 text-xs font-medium rounded hover:bg-white border border-transparent hover:border-slate-200 ${
                          s.enabled ? 'text-amber-700' : 'text-emerald-700'
                        }`}
                        title={s.enabled ? 'Pause this schedule' : 'Resume this schedule'}
                      >
                        {s.enabled ? 'Pause' : 'Resume'}
                      </button>
                      <button
                        onClick={() => handleDelete(s.id)}
                        className="px-2 py-1 text-xs font-medium text-red-600 hover:text-red-700 rounded hover:bg-white border border-transparent hover:border-red-200"
                        title="Delete this schedule"
                      >
                        Delete
                      </button>
                    </li>
                  ))}
                </ul>
                <div className="mt-3 -mx-1 border-t border-slate-100" />
                <div className="text-xs text-slate-500 mt-2">
                  {editingScheduleId ? 'Editing existing schedule below.' : 'Add another schedule below, or edit/delete one above.'}
                </div>
              </div>
            )}

            {/* Frequency picker */}
            <div>
              <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">
                {editingScheduleId ? 'Edit cadence' : (existing.length > 0 ? 'Add another schedule' : 'Frequency')}
              </label>
              <div className="grid grid-cols-2 gap-2">
                <FrequencyChip value="minutes" label="Every N minutes" />
                <FrequencyChip value="hourly" label="Every hour" />
                <FrequencyChip value="daily" label="Daily" />
                <FrequencyChip value="weekly" label="Weekly" />
              </div>
            </div>

            {/* Frequency-specific controls */}
            {frequency === 'minutes' && (
              <div>
                <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">Interval (minutes)</label>
                <input
                  type="number" min={1} max={59}
                  value={intervalMinutes}
                  onChange={e => { setIntervalMinutes(parseInt(e.target.value || '1', 10)); setTouched(true); }}
                  className="w-32 px-3 py-2 text-sm border border-slate-200 rounded-lg outline-none focus:ring-2 focus:ring-emerald-200 text-slate-700"
                />
              </div>
            )}

            {frequency === 'hourly' && (
              <div>
                <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">Minute past the hour</label>
                <input
                  type="number" min={0} max={59} step={1}
                  /* 2026-05-28 — explicit `String(minute)` so React keeps
                     the input controlled with no leading-zero drift (the
                     "030" artifact some browsers leave behind when a
                     user types digits next to an existing 0). */
                  value={String(minute)}
                  onChange={e => {
                    // Strip leading zeros and any non-digit chars before
                    // parsing so "030" → 30 and the bound value stays
                    // canonical.
                    const cleaned = (e.target.value || '0').replace(/^0+(\d)/, '$1');
                    const n = parseInt(cleaned || '0', 10);
                    setMinute(Math.min(59, Math.max(0, isNaN(n) ? 0 : n)));
                    setTouched(true);
                  }}
                  className="w-32 px-3 py-2 text-sm border border-slate-200 rounded-lg outline-none focus:ring-2 focus:ring-emerald-200 text-slate-700"
                />
                <div className="mt-1 text-[11px] text-slate-500">
                  0 = on the hour (12:00, 1:00, 2:00…) · 30 = half-past (12:30, 1:30, 2:30…).
                </div>
              </div>
            )}

            {frequency === 'daily' && (
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">Hour (24h)</label>
                  <input
                    type="number" min={0} max={23}
                    value={hour}
                    onChange={e => { setHour(Math.min(23, Math.max(0, parseInt(e.target.value || '0', 10)))); setTouched(true); }}
                    className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg outline-none focus:ring-2 focus:ring-emerald-200 text-slate-700"
                  />
                </div>
                <div>
                  <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">Minute</label>
                  <input
                    type="number" min={0} max={59}
                    value={minute}
                    onChange={e => { setMinute(Math.min(59, Math.max(0, parseInt(e.target.value || '0', 10)))); setTouched(true); }}
                    className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg outline-none focus:ring-2 focus:ring-emerald-200 text-slate-700"
                  />
                </div>
              </div>
            )}

            {frequency === 'weekly' && (
              <>
                <div>
                  <div className="flex items-center justify-between mb-1.5">
                    <label className="text-xs font-bold text-slate-500 uppercase tracking-wider">Days of week</label>
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => { setWeeklyDays([1, 2, 3, 4, 5]); setTouched(true); }}
                        className="text-xs text-slate-500 hover:text-slate-700"
                        title="Mon-Fri"
                      >Weekdays</button>
                      <span className="text-slate-300">|</span>
                      <button
                        type="button"
                        onClick={() => { setWeeklyDays([0, 1, 2, 3, 4, 5, 6]); setTouched(true); }}
                        className="text-xs text-slate-500 hover:text-slate-700"
                      >All</button>
                    </div>
                  </div>
                  <div className="grid grid-cols-7 gap-1">
                    {WEEKDAYS.map(d => {
                      const on = weeklyDays.includes(d.value);
                      return (
                        <button
                          key={d.value}
                          type="button"
                          onClick={() => {
                            setWeeklyDays(prev => prev.includes(d.value)
                              ? prev.filter(v => v !== d.value)
                              : [...prev, d.value]);
                            setTouched(true);
                          }}
                          className={`py-2 rounded-lg text-xs font-semibold border transition-all ${
                            on
                              ? 'border-emerald-300 bg-emerald-50 text-emerald-700'
                              : 'border-slate-200 hover:border-slate-300 text-slate-600'
                          }`}
                        >
                          {d.label}
                        </button>
                      );
                    })}
                  </div>
                  {weeklyDays.length === 0 && (
                    <p className="text-xs text-amber-600 mt-1.5">Pick at least one day.</p>
                  )}
                </div>

                <div>
                  <div className="flex items-center justify-between mb-1.5">
                    <label className="text-xs font-bold text-slate-500 uppercase tracking-wider">Times</label>
                    <button
                      type="button"
                      onClick={() => {
                        setWeeklyTimes(prev => [...prev, { hour: 9, minute: 0 }]);
                        setTouched(true);
                      }}
                      className="text-xs font-semibold text-emerald-700 hover:text-emerald-800"
                    >+ Add time</button>
                  </div>
                  <div className="space-y-2">
                    {weeklyTimes.map((t, idx) => (
                      <div key={idx} className="flex items-center gap-2">
                        <div className="flex-1 grid grid-cols-2 gap-2">
                          <input
                            type="number" min={0} max={23}
                            value={t.hour}
                            onChange={e => {
                              const v = Math.min(23, Math.max(0, parseInt(e.target.value || '0', 10)));
                              setWeeklyTimes(prev => prev.map((x, i) => i === idx ? { ...x, hour: v } : x));
                              setTouched(true);
                            }}
                            placeholder="Hour"
                            className="px-3 py-2 text-sm border border-slate-200 rounded-lg outline-none focus:ring-2 focus:ring-emerald-200 text-slate-700"
                          />
                          <input
                            type="number" min={0} max={59}
                            value={t.minute}
                            onChange={e => {
                              const v = Math.min(59, Math.max(0, parseInt(e.target.value || '0', 10)));
                              setWeeklyTimes(prev => prev.map((x, i) => i === idx ? { ...x, minute: v } : x));
                              setTouched(true);
                            }}
                            placeholder="Minute"
                            className="px-3 py-2 text-sm border border-slate-200 rounded-lg outline-none focus:ring-2 focus:ring-emerald-200 text-slate-700"
                          />
                        </div>
                        {weeklyTimes.length > 1 && (
                          <button
                            type="button"
                            onClick={() => {
                              setWeeklyTimes(prev => prev.filter((_, i) => i !== idx));
                              setTouched(true);
                            }}
                            className="w-8 h-8 rounded-lg flex items-center justify-center text-slate-400 hover:text-red-600 hover:bg-red-50 transition-colors"
                            title="Remove this time"
                          >
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                  {editingScheduleId && weeklyTimes.length > 1 && (
                    <p className="text-xs text-amber-600 mt-1.5">
                      Editing keeps only the first time. To run on additional times, save and add another schedule.
                    </p>
                  )}
                </div>
              </>
            )}

            {/* Human-readable preview — shown only after the user has
                actually interacted with the picker. The default "Every
                day at 12:00 AM" preview on initial mount was misleading
                (user feedback 2026-05-11) — it suggested the dialog had
                already configured something. */}
            {touched && (
              <div className="px-3 py-2.5 rounded-lg bg-emerald-50 border border-emerald-200">
                <div className="text-xs font-bold text-emerald-700 uppercase tracking-wider">Runs</div>
                <div className="text-sm font-semibold text-emerald-800 mt-0.5">{human}</div>
              </div>
            )}

            {/* 2026-05-28 — Unsafe-sink acknowledge banner. Appears
                only AFTER the first failed schedule create returned
                `code: unsafe_for_schedule`. The backend's safety
                guard prevents accidentally scheduling a pipeline
                whose sinks would duplicate data (append-mode
                warehouse_sink) or re-fire side effects (email,
                webhook, slack, kafka) on every tick. The user
                acknowledges intentionally and the create proceeds
                with `acknowledge_side_effects: true`. */}
            {unsafeErrInfo && (
              <div className="px-3 py-2.5 rounded-lg bg-amber-50 border border-amber-300">
                <div className="flex items-center gap-2 text-xs font-bold text-amber-800 uppercase tracking-wider">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                    <line x1="12" y1="9" x2="12" y2="13" />
                    <line x1="12" y1="17" x2="12.01" y2="17" />
                  </svg>
                  Unsafe sink detected
                </div>
                <div className="mt-1.5 text-xs text-amber-900 leading-relaxed">
                  {unsafeErrInfo.message ||
                    "This pipeline has a sink that will duplicate data or re-fire a side effect on every scheduled tick."}
                </div>
                {unsafeErrInfo.sinks.length > 0 && (
                  <div className="mt-1.5 text-[11px] text-amber-700 font-mono">
                    Flagged sinks:{' '}
                    {unsafeErrInfo.sinks
                      .map(
                        (s) =>
                          `${s.step_id || '?'} (${s.step_type || '?'}, ${s.idempotency || '?'})`,
                      )
                      .join(' · ')}
                  </div>
                )}
                <label className="mt-2 flex items-center gap-2 cursor-pointer text-xs text-amber-900 font-medium">
                  <input
                    type="checkbox"
                    checked={acknowledgeUnsafe}
                    onChange={(e) => setAcknowledgeUnsafe(e.target.checked)}
                    className="w-3.5 h-3.5 rounded border-amber-400 text-amber-600 focus:ring-amber-300"
                  />
                  I understand — schedule anyway (e.g. this is a digest email or the sink is idempotent for other reasons).
                </label>
              </div>
            )}

            {/* Schedule-level alert cross-link removed for OSS Free.
                Pipeline-level alerts (set via the bell icon on the
                workflow row) already cover scheduled-run failures
                because the alert rule keys on workflow_id, which the
                scheduler triggers fire under. Schedule-scoped alerts
                are a Plus-tier feature. */}
          </div>

          <div className="px-5 py-3 border-t border-slate-200/60 flex items-center justify-between gap-2">
            <button
              onClick={onClose}
              className="px-4 py-2 text-xs text-slate-600 hover:text-slate-800 rounded-lg hover:bg-slate-50 font-medium"
            >
              Close
            </button>
            <div className="flex items-center gap-2">
              {editingScheduleId && (
                <button
                  onClick={() => { setEditingScheduleId(null); }}
                  className="px-3 py-2 text-xs text-slate-600 hover:text-slate-800 rounded-lg hover:bg-slate-100 font-medium"
                >
                  Cancel edit
                </button>
              )}
              {/* 2026-05-28 — when the safety-guard banner is showing
                  AND the user hasn't ticked the acknowledge box, the
                  Create button is DISABLED with a clear "Acknowledge
                  to continue" tooltip. This stops the "click Create →
                  toast appears → click Create again → same toast"
                  confusion loop. Button copy also flips to amber
                  "Schedule anyway" once the box is checked so the
                  user knows they're proceeding into the warning. */}
              {(() => {
                const blockedByUnsafe = !!unsafeErrInfo && !acknowledgeUnsafe;
                const acknowledged = !!unsafeErrInfo && acknowledgeUnsafe;
                const baseDisabled =
                  saving || !touched ||
                  (frequency === 'weekly'
                    ? (weeklyDays.length === 0 || weeklyTimes.length === 0)
                    : !cron.trim());
                const disabled = baseDisabled || blockedByUnsafe;
                const buttonClass = acknowledged
                  ? 'bg-amber-500 hover:bg-amber-600'
                  : 'bg-emerald-500 hover:bg-emerald-600';
                const buttonLabel = editingScheduleId
                  ? 'Save Changes'
                  : acknowledged
                    ? 'Schedule anyway'
                    : 'Create Schedule';
                const tooltip = blockedByUnsafe
                  ? 'Tick the "I understand" box above to proceed despite the unsafe sink warning.'
                  : undefined;
                return (
                  <button
                    onClick={handleSave}
                    disabled={disabled}
                    title={tooltip}
                    className={`px-5 py-2 text-xs text-white font-semibold rounded-lg disabled:opacity-50 disabled:cursor-not-allowed transition-all shadow-sm hover:shadow-md flex items-center gap-1.5 ${buttonClass}`}
                  >
                    {saving ? <span className="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin" /> : null}
                    {buttonLabel}
                  </button>
                );
              })()}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

/** Render the target value (email list / webhook URL) of an existing
 *  alert rule, depending on its channel. Used by the manage panel
 *  to show the user where each rule is sending notifications. */
function alertRuleTarget(rule: any): string {
  if (!rule) return '';
  if (rule.channel === 'email') {
    const emails = rule.email_addresses || [];
    return Array.isArray(emails) ? emails.join(', ') : String(emails);
  }
  if (rule.channel === 'slack') return rule.slack_webhook_url || '';
  if (rule.channel === 'teams') return rule.teams_webhook_url || '';
  if (rule.channel === 'webhook') return rule.webhook_url || '';
  return '';
}

function QuickAlertDialog({ pipelineId, pipelineName, onClose }: { pipelineId: string; pipelineName: string; onClose: () => void }) {
  const [conditions, setConditions] = useState<string[]>(['on_failure']);
  const [channel, setChannel] = useState<'email' | 'slack' | 'teams' | 'webhook'>('email');
  const [target, setTarget] = useState('');
  const [saving, setSaving] = useState(false);
  // SMTP availability check — pulls /api/health/ready once on mount.
  // When false, the email channel selector shows an amber banner so
  // the user knows the alert won't actually be delivered until they
  // set SMTP_HOST / SMTP_USER / SMTP_PASS env vars on the server.
  const [smtpConfigured, setSmtpConfigured] = useState<boolean | null>(null);
  useEffect(() => {
    fetch('/api/health/ready')
      .then((r) => r.json())
      .then((d) => setSmtpConfigured(!!d?.notifications?.smtp_configured))
      .catch(() => setSmtpConfigured(null));
  }, []);

  // Existing rules for this pipeline. Listed at the top of the
  // dialog with edit / pause / delete / test buttons so the user can
  // manage every rule from one place — same pattern as the Quick
  // Schedule dialog.
  const [existing, setExisting] = useState<any[]>([]);
  const [loadingExisting, setLoadingExisting] = useState(true);
  const [editingRuleId, setEditingRuleId] = useState<string | null>(null);

  const reloadExisting = useCallback(async () => {
    setLoadingExisting(true);
    try {
      const list = await api.listAlertRules({ workflow_id: pipelineId });
      setExisting(Array.isArray(list) ? list : []);
    } catch {
      setExisting([]);
    } finally {
      setLoadingExisting(false);
    }
  }, [pipelineId]);
  useEffect(() => { reloadExisting(); }, [reloadExisting]);

  const toggleCondition = (val: string) => {
    setConditions(prev => {
      const next = prev.includes(val) ? prev.filter(c => c !== val) : [...prev, val];
      return next.length > 0 ? next : [val];
    });
  };

  const resetForm = () => {
    setEditingRuleId(null);
    setConditions(['on_failure']);
    setChannel('email');
    setTarget('');
  };

  const handleSave = async () => {
    if (!target.trim()) { toast.warning('Enter a notification target'); return; }
    setSaving(true);
    try {
      const payload = {
        name: `${pipelineName} - Alert`,
        workflow_id: pipelineId,
        conditions,
        condition: conditions[0],
        condition_logic: 'any',
        channel,
        email_addresses: channel === 'email' ? target.split(',').map(s => s.trim()) : [],
        slack_webhook_url: channel === 'slack' ? target : '',
        teams_webhook_url: channel === 'teams' ? target : '',
        webhook_url: channel === 'webhook' ? target : '',
      };
      if (editingRuleId) {
        await api.updateAlertRule(editingRuleId, payload);
        toast.success('Alert updated', `Will notify on ${conditions.join(', ')}`);
      } else {
        await api.createAlertRule(payload);
        toast.success('Alert created', `Will notify on ${conditions.join(', ')}`);
      }
      resetForm();
      await reloadExisting();
    } catch (err: any) {
      toast.error('Alert failed', err.message);
    }
    setSaving(false);
  };

  const handleDelete = async (ruleId: string) => {
    if (!await uiConfirm({ title: 'Delete alert?', message: 'The pipeline will stop sending notifications via this rule.', confirmLabel: 'Delete', destructive: true })) return;
    try {
      await api.deleteAlertRule(ruleId);
      toast.success('Alert deleted');
      await reloadExisting();
    } catch (err: any) {
      toast.error('Delete failed', err.message);
    }
  };

  const handleToggle = async (rule: any) => {
    try {
      await api.updateAlertRule(rule.id, { ...rule, enabled: !rule.enabled });
      await reloadExisting();
    } catch (err: any) {
      toast.error('Toggle failed', err.message);
    }
  };

  const handleTest = async (ruleId: string) => {
    try {
      await api.testAlert(ruleId);
      toast.success('Test sent', 'Check the destination channel for the test notification.');
    } catch (err: any) {
      toast.error('Test failed', err.message);
    }
  };

  const handleEditExisting = (rule: any) => {
    setEditingRuleId(rule.id);
    const condList = Array.isArray(rule.conditions) && rule.conditions.length > 0
      ? rule.conditions
      : (rule.condition ? [rule.condition] : ['on_failure']);
    setConditions(condList);
    setChannel(rule.channel || 'email');
    setTarget(alertRuleTarget(rule));
  };

  return (
    <>
      <div className="fixed inset-0 bg-black/20 z-[60]" onClick={onClose} />
      <div className="fixed inset-0 z-[65] flex items-center justify-center pointer-events-none">
        {/* Resizable dialog. Default 560×auto with min/max bounds; the
            user can drag the bottom-right corner to widen for long
            email lists or stretch vertically when many active rules
            are listed. The inner content area handles its own scroll
            so the outer `overflow: hidden` keeps the rounded corners
            crisp while still letting `resize: both` render its native
            corner grabber. */}
        <div
          className="pointer-events-auto bg-white rounded-2xl shadow-2xl border border-slate-200/60 flex flex-col"
          style={{
            width: 560,
            maxWidth: '95vw',
            maxHeight: '85vh',
            minWidth: 420,
            minHeight: 320,
            resize: 'both',
            overflow: 'hidden',
          }}
        >
          <div className="px-5 py-4 border-b border-slate-200/60 flex items-center justify-between shrink-0">
            <div>
              <h2 className="text-sm font-bold text-slate-800 flex items-center gap-2">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-red-500"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" /></svg>
                Quick Alert
              </h2>
              <p className="text-xs text-slate-400 mt-0.5">{pipelineName}</p>
            </div>
            <button onClick={onClose} className="w-7 h-7 rounded-lg flex items-center justify-center text-slate-400 hover:text-slate-600 hover:bg-slate-100">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
            </button>
          </div>
          <div className="p-5 space-y-4 flex-1 overflow-y-auto min-h-0">
            {/* Existing alert rules — manage panel. Listed at the top
                so the user can see what's already wired before adding
                another rule. Each row has Edit / Pause-Resume / Test /
                Delete actions. */}
            {!loadingExisting && existing.length > 0 && (
              <div>
                <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">
                  Active rules ({existing.length})
                </label>
                <ul className="space-y-1.5">
                  {existing.map((r) => (
                    <li key={r.id} className={`flex items-center gap-2 px-3 py-2 rounded-lg border ${
                      r.enabled ? 'border-amber-200 bg-amber-50/40' : 'border-slate-200 bg-slate-50'
                    }`}>
                      <span className={`w-2 h-2 rounded-full shrink-0 ${r.enabled ? 'bg-amber-500' : 'bg-slate-400'}`} />
                      <div className="flex-1 min-w-0">
                        <div className="text-xs font-semibold text-slate-700 truncate">
                          {(r.conditions || [r.condition]).join(' · ')} <span className="text-slate-400 font-normal">via</span> {r.channel}
                        </div>
                        <div className="text-xs text-slate-500 font-mono truncate" title={alertRuleTarget(r)}>
                          {alertRuleTarget(r) || '—'}
                        </div>
                      </div>
                      <button
                        onClick={() => handleTest(r.id)}
                        className="px-2 py-1 text-xs font-medium text-blue-600 hover:text-blue-700 rounded hover:bg-white border border-transparent hover:border-blue-200"
                        title="Send a test notification"
                      >
                        Test
                      </button>
                      <button
                        onClick={() => handleEditExisting(r)}
                        className="px-2 py-1 text-xs font-medium text-slate-600 hover:text-slate-800 rounded hover:bg-white border border-transparent hover:border-slate-200"
                        title="Edit conditions / channel / target"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => handleToggle(r)}
                        className={`px-2 py-1 text-xs font-medium rounded hover:bg-white border border-transparent hover:border-slate-200 ${
                          r.enabled ? 'text-amber-700' : 'text-emerald-700'
                        }`}
                        title={r.enabled ? 'Pause this alert' : 'Resume this alert'}
                      >
                        {r.enabled ? 'Pause' : 'Resume'}
                      </button>
                      <button
                        onClick={() => handleDelete(r.id)}
                        className="px-2 py-1 text-xs font-medium text-red-600 hover:text-red-700 rounded hover:bg-white border border-transparent hover:border-red-200"
                        title="Delete this alert rule"
                      >
                        Delete
                      </button>
                    </li>
                  ))}
                </ul>
                <div className="mt-3 -mx-1 border-t border-slate-100" />
                <div className="text-xs text-slate-500 mt-2">
                  {editingRuleId ? 'Editing existing alert below.' : 'Add another rule below, or edit/delete one above.'}
                </div>
              </div>
            )}

            <div>
              <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">
                {editingRuleId ? 'Edit alert' : (existing.length > 0 ? 'Add another alert — fires when' : 'Alert When')}
              </label>
              <div className="grid grid-cols-2 gap-2">
                {QUICK_ALERT_CONDITIONS.map(c => {
                  const checked = conditions.includes(c.value);
                  return (
                    <button
                      key={c.value}
                      onClick={() => toggleCondition(c.value)}
                      className={`p-2 rounded-lg border text-xs font-medium text-left transition-all flex items-center gap-2 ${
                        checked ? 'border-red-300 bg-red-50' : 'border-slate-200 hover:border-slate-300'
                      }`}
                    >
                      <span className={`w-3.5 h-3.5 rounded border-2 flex items-center justify-center shrink-0 ${checked ? 'border-red-400 bg-red-400' : 'border-slate-300'}`}>
                        {checked && <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3"><polyline points="20 6 9 17 4 12" /></svg>}
                      </span>
                      <span className={c.color}>{c.label}</span>
                    </button>
                  );
                })}
              </div>
            </div>
            <div>
              <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">Channel</label>
              <div className="grid grid-cols-4 gap-2">
                {(['email', 'slack', 'teams', 'webhook'] as const).map(ch => (
                  <button
                    key={ch}
                    onClick={() => setChannel(ch)}
                    className={`p-2 rounded-lg border text-center text-xs font-medium transition-all capitalize ${
                      channel === ch ? 'border-red-300 bg-red-50 text-red-700' : 'border-slate-200 hover:border-slate-300 text-slate-600'
                    }`}
                  >
                    {ch}
                  </button>
                ))}
              </div>
              {/* SMTP-not-configured warning — points at the Settings
                  UI form, not env vars. The env-var fallback exists
                  for ops folks but end users shouldn't need to know
                  about it. */}
              {channel === 'email' && smtpConfigured === false && (
                <div className="mt-2 px-3 py-2.5 rounded-lg border border-amber-200 bg-amber-50 text-[12px] text-amber-800 flex items-start gap-2">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="shrink-0 mt-0.5"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
                  <div className="min-w-0 flex-1">
                    <div className="font-semibold">Email won't be delivered — SMTP isn't set up.</div>
                    <div className="mt-0.5">
                      <button
                        type="button"
                        onClick={() => {
                          try { sessionStorage.setItem('fpulse_settings_jump_to', 'smtp'); } catch {}
                          onClose();
                          navigateToSubRoute('settings', 'notifications');
                        }}
                        className="font-semibold text-amber-900 underline underline-offset-2 hover:text-amber-700"
                      >
                        Configure SMTP in Settings →
                      </button>
                      <span className="ml-1.5 text-amber-700">or pick Slack / Teams / Webhook below — those don't need SMTP.</span>
                    </div>
                  </div>
                </div>
              )}
            </div>
            <div>
              <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">
                {channel === 'email' ? 'Email Addresses'
                  : channel === 'slack' ? 'Slack Webhook URL'
                  : channel === 'teams' ? 'Microsoft Teams Webhook URL'
                  : 'Webhook URL'}
              </label>
              <input
                type="text"
                value={target}
                onChange={e => setTarget(e.target.value)}
                placeholder={
                  channel === 'email' ? 'admin@example.com, team@example.com'
                  : channel === 'slack' ? 'https://hooks.slack.com/services/...'
                  : channel === 'teams' ? 'https://outlook.office.com/webhook/...'
                  : 'https://api.example.com/webhook'
                }
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg outline-none focus:ring-2 focus:ring-red-200 text-slate-700"
              />
            </div>
          </div>
          <div className="px-5 py-3 border-t border-slate-200/60 flex items-center justify-between gap-2 shrink-0">
            <button onClick={onClose} className="px-4 py-2 text-xs text-slate-600 hover:text-slate-800 rounded-lg hover:bg-slate-50 font-medium">Close</button>
            <div className="flex items-center gap-2">
              {editingRuleId && (
                <button
                  onClick={resetForm}
                  className="px-3 py-2 text-xs text-slate-600 hover:text-slate-800 rounded-lg hover:bg-slate-100 font-medium"
                >
                  Cancel edit
                </button>
              )}
              <button
                onClick={handleSave}
                disabled={saving || !target.trim()}
                className="px-5 py-2 text-xs text-white font-semibold rounded-lg disabled:opacity-50 transition-all shadow-sm hover:shadow-md flex items-center gap-1.5 bg-red-500 hover:bg-red-600"
              >
                {saving ? <span className="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin" /> : null}
                {editingRuleId ? 'Save Changes' : 'Create Alert'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

const TAG_COLORS = [
  { name: 'blue', bg: 'bg-blue-100', text: 'text-blue-700', dot: 'bg-blue-500' },
  { name: 'green', bg: 'bg-green-100', text: 'text-green-700', dot: 'bg-green-500' },
  { name: 'amber', bg: 'bg-amber-100', text: 'text-amber-700', dot: 'bg-amber-500' },
  { name: 'red', bg: 'bg-red-100', text: 'text-red-700', dot: 'bg-red-500' },
  { name: 'purple', bg: 'bg-purple-100', text: 'text-purple-700', dot: 'bg-purple-500' },
  { name: 'pink', bg: 'bg-pink-100', text: 'text-pink-700', dot: 'bg-pink-500' },
  { name: 'slate', bg: 'bg-slate-100', text: 'text-slate-700', dot: 'bg-slate-500' },
];

interface Pipeline {
  id: string;
  name: string;
  created_at?: string;
  updated_at?: string;
  steps_count?: number;
  status?: string;
  last_run?: string;
  tags?: string[];
  steps?: any[];
  connections?: any[];
  owner_id?: string;
  owner_name?: string;
  created_by?: string;
  published_by?: string;
  published_at?: string;
  deployed_by?: string;
  deployed_at?: string;
  deployed_version?: number;
  submitted_by?: string;
  submitted_at?: string;
  approved_by?: string;
  approved_at?: string;
  description?: string;
  project_id?: string;
  version?: number;
  step_count?: number;
  rollback_from?: number;
  department?: string;
  priority?: 'low' | 'medium' | 'high' | 'critical';
  last_run_status?: string;
  last_run_duration?: number;
  // Worker-pool queue priority (1-5). Stored in metadata so the
  // execution API (`metadata.get("priority", 3)`) picks it up at
  // submit time. Distinct from the legacy `priority` string above
  // which categorizes business importance, not runtime ordering.
  metadata?: { priority?: number; [key: string]: any };
}

/* ── Deployment Queue Column Configuration ── */
interface DeployColumn {
  key: string;
  label: string;
  width: string;
  default: boolean;       // shown by default
  align?: 'left' | 'center' | 'right';
  group: 'core' | 'metadata' | 'deployment' | 'operational';
}

const DEPLOY_COLUMNS: DeployColumn[] = [
  // Core — always visible
  { key: 'pipeline',    label: 'Pipeline',      width: '1fr',   default: true,  align: 'left',   group: 'core' },
  { key: 'owner',       label: 'Owner',         width: '100px', default: true,  align: 'left',   group: 'core' },
  { key: 'status',      label: 'Status',        width: '80px',  default: true,  align: 'center', group: 'core' },
  { key: 'version',     label: 'Ver',           width: '55px',  default: true,  align: 'center', group: 'core' },
  { key: 'env',         label: 'Env',           width: '65px',  default: true,  align: 'center', group: 'core' },
  { key: 'action',      label: 'Action',        width: '140px', default: true,  align: 'right',  group: 'core' },
  // Metadata
  { key: 'department',  label: 'Department',    width: '90px',  default: false, align: 'left',   group: 'metadata' },
  { key: 'project',     label: 'Project',       width: '90px',  default: false, align: 'left',   group: 'metadata' },
  { key: 'description', label: 'Description',   width: '140px', default: false, align: 'left',   group: 'metadata' },
  { key: 'tags',        label: 'Tags',          width: '100px', default: false, align: 'left',   group: 'metadata' },
  { key: 'priority',    label: 'Priority',      width: '70px',  default: false, align: 'center', group: 'metadata' },
  // Deployment lifecycle
  { key: 'submitted',   label: 'Submitted',     width: '100px', default: true,  align: 'center', group: 'deployment' },
  { key: 'submittedBy', label: 'Submitted By',  width: '90px',  default: false, align: 'left',   group: 'deployment' },
  { key: 'approved',    label: 'Approved',       width: '90px',  default: false, align: 'center', group: 'deployment' },
  { key: 'approvedBy',  label: 'Approved By',   width: '90px',  default: false, align: 'left',   group: 'deployment' },
  { key: 'deployedAt',  label: 'Deployed At',   width: '90px',  default: false, align: 'center', group: 'deployment' },
  { key: 'deployedBy',  label: 'Deployed By',   width: '90px',  default: false, align: 'left',   group: 'deployment' },
  // Operational
  { key: 'steps',       label: 'Steps',         width: '50px',  default: true,  align: 'center', group: 'operational' },
  { key: 'schedule',    label: 'Schedule',       width: '90px',  default: true,  align: 'center', group: 'operational' },
  { key: 'created',     label: 'Created',       width: '80px',  default: false, align: 'center', group: 'operational' },
  { key: 'modified',    label: 'Modified',      width: '80px',  default: true,  align: 'center', group: 'operational' },
  { key: 'lastRun',     label: 'Last Run',      width: '80px',  default: false, align: 'center', group: 'operational' },
  { key: 'connections', label: 'Connections',    width: '70px',  default: false, align: 'center', group: 'operational' },
  // 2026-05-25 — Storage usage column. Counts files + tables this pipeline
  // references (computed from /api/storage/usage, inverted client-side).
  { key: 'storage',     label: 'Storage',        width: '80px',  default: true,  align: 'center', group: 'operational' },
];

const COLUMN_GROUPS = [
  { key: 'core', label: 'Core', icon: '◆' },
  { key: 'metadata', label: 'Metadata', icon: '◇' },
  { key: 'deployment', label: 'Deployment', icon: '▸' },
  { key: 'operational', label: 'Operational', icon: '⚙' },
];

/* ═══ Pipelines list-view column picker ═══
   Toggleable columns for the main Pipelines table (list view). The
   select checkbox, Name, and Actions columns are always rendered;
   everything else can be hidden via the Columns popover. Keys here
   must match the isVisible('key') guards on each <th>/<td> below. */
const PIPELINE_COLUMNS: TColumn[] = [
  { key: 'project',   label: 'Project',   default: true,  group: 'core' },
  { key: 'status',    label: 'Status',    default: true,  group: 'core' },
  { key: 'author',    label: 'Author',    default: true,  group: 'core' },
  { key: 'modified',  label: 'Modified',  default: true,  group: 'operational' },
  { key: 'last_run',  label: 'Last Run',  default: true,  group: 'operational' },
  { key: 'priority',  label: 'Priority',  default: true,  group: 'operational' },
  { key: 'schedule',  label: 'Schedule',  default: true,  group: 'operational' },
  { key: 'alerts',    label: 'Alerts',    default: true,  group: 'operational' },
  // 2026-05-25 — Storage usage badge. Counts files + tables this
  // pipeline references via /api/storage/usage (inverted client-side).
  { key: 'storage',   label: 'Storage',   default: true,  group: 'operational' },
];
const PIPELINE_COLUMN_GROUPS: TColumnGroup[] = [
  { key: 'core',        label: 'Core',        icon: '◆' },
  { key: 'operational', label: 'Operational', icon: '⚙' },
];

interface VersionInfo {
  version: number;
  created_by: string;
  created_at: string;
  change_summary: string;
  step_count: number;
  steps?: Array<{ id: string; type: string; label: string }>;
  connections?: Array<{ from_step: string; to_step: string }>;
  // Schema v15 — SHA-256 hash of the canonical workflow JSON. Used by
  // rollback verification + the version-list "what changed" UI.
  content_hash?: string;
}

interface VersionDiff {
  addedSteps: string[];
  removedSteps: string[];
  addedConns: number;
  removedConns: number;
}

export default function PipelinesPage({ onOpenEditor, projectId, projectName = '', onClearProject, onGoToProjects, environment: _environment, tier = 'free' }: { onOpenEditor: () => void; projectId?: string | null; projectName?: string; onClearProject?: () => void; onGoToProjects?: () => void; environment?: 'dev' | 'prod'; tier?: string }) {
  // Defaulting `environment = 'dev'` in the destructure narrows its TS type
  // to literal 'dev' for the rest of the function, which makes
  // `environment === 'prod'` an "always false" comparison error in strict
  // mode.
  //
  // 2026-05-22 v2 — the simpler `as 'dev' | 'prod'` cast still left TS
  // collapsing the type to `'dev'` inside nested IIFEs / callbacks. A
  // tiny helper with an explicit return-type annotation defeats the
  // control-flow narrowing because TS doesn't propagate literal narrowing
  // through function-call boundaries. Same runtime, but TS now treats
  // `environment` as the union everywhere it's read.
  const _toEnv = (e: 'dev' | 'prod' | undefined): 'dev' | 'prod' => e ?? 'dev';
  const environment: 'dev' | 'prod' = _toEnv(_environment);
  const isProd = environment === 'prod';
  // RBAC — does the current user have write/deploy access in this environment?
  const canEdit = useCan('edit', environment);
  const canDelete = useCan('delete', environment);
  const canDeploy = useCan('deploy', environment);
  // Approval workflow — leads/admins can approve; any dev can submit
  const canApprove = useCan('approve', 'prod');
  const canSubmitForReview = useCan('edit', 'dev');
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  // Project dropdown filter for the workflow table. 'all' means no
  // filter; any other value matches `pipelines[].project_id`. Loaded
  // once on mount from /api/projects so the dropdown stays meaningful
  // even if the active workspace has projects with no pipelines yet.
  const [projectFilter, setProjectFilter] = useState<string>('all');
  const [projectsList, setProjectsList] = useState<Array<{ id: string; name: string }>>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  // 2026-05-28 — Action column was 7-8 buttons wide, pushing the
  // table past viewport on standard 1920px screens and forcing
  // horizontal scroll. Reported in internal testing (right-edge gap + scroll
  // bar visible). Solution: collapse the trailing icon-only buttons
  // (Copy, Save as template, Delete) into a single "..." kebab so
  // the row Actions cluster shrinks from ~400px to ~280px. The
  // popover is rendered per-row and gated on this row-id state;
  // closing fires on any outside click via the document handler
  // mounted below.
  const [moreMenuFor, setMoreMenuFor] = useState<string | null>(null);
  useEffect(() => {
    if (!moreMenuFor) return;
    const onClick = (e: MouseEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && el.closest && el.closest('[data-row-more-menu]')) return;
      setMoreMenuFor(null);
    };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, [moreMenuFor]);
  // Folders inside the current project. Loaded when projectId is set,
  // reloaded after create/delete via setFolderRefreshTick.
  const [folders, setFolders] = useState<Array<{ id: string; name: string; parent_folder_id: string | null; color?: string }>>([]);
  const [folderRefreshTick, setFolderRefreshTick] = useState(0);
  const [activeFolderId, setActiveFolderId] = useState<string | null>(null);
  useEffect(() => {
    if (!projectId) {
      setFolders([]);
      setActiveFolderId(null);
      return;
    }
    let cancelled = false;
    api.listFolders(projectId)
      .then((rows: any) => {
        if (cancelled) return;
        setFolders(Array.isArray(rows) ? rows : []);
      })
      .catch(() => {
        if (!cancelled) setFolders([]);
      });
    return () => { cancelled = true; };
  }, [projectId, folderRefreshTick]);
  // Stats panel toggle — collapses the KPI grid (Total / Active /
  // Drafts / Failed / Running / Scheduled). Persisted to localStorage
  // so the user's preference survives reloads. Same pattern as the
  // Executions page Stats button.
  const [showDashboard, setShowDashboard] = useState<boolean>(() => {
    try { return localStorage.getItem('fpulse_workflows_show_stats') !== '0'; }
    catch { return true; }
  });
  useEffect(() => {
    try { localStorage.setItem('fpulse_workflows_show_stats', showDashboard ? '1' : '0'); } catch {}
  }, [showDashboard]);
  const [sortBy, setSortBy] = useState<'name' | 'updated_at' | 'status'>('updated_at');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');
  const [viewMode, setViewMode] = useState<'list' | 'grid'>('list');
  // Density (per design decision D-003 — defaults to Comfortable; persisted
  // per page in localStorage so the user's preference sticks).
  const { density, setDensity, rowPaddingClass } = useDensity('pipelines');
  const [historyPipelineId, setHistoryPipelineId] = useState<string | null>(null);
  const [historyPipelineName, setHistoryPipelineName] = useState('');
  const [versions, setVersions] = useState<VersionInfo[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [restoringVersion, setRestoringVersion] = useState<number | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [lifecycleLoading, setLifecycleLoading] = useState<Record<string, string>>({});
  // Publish modal state — opened from any of the three Publish buttons
  // (card, table row, detail drawer). The modal runs /test on open and
  // only calls /publish after the user confirms a passing result.
  const [publishTarget, setPublishTarget] = useState<{ id: string; name: string } | null>(null);
  // Inline rename state — pencil icon next to the pipeline name swaps
  // to a text input. Backend has no PATCH for partial updates, so we
  // re-fetch the workflow, mutate `name`, and PUT it back.
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [renameSaving, setRenameSaving] = useState(false);
  const handleStartRename = (p: { id: string; name: string }) => {
    setRenamingId(p.id);
    setRenameValue(p.name);
  };
  const handleCommitRename = async () => {
    const id = renamingId;
    const next = renameValue.trim();
    if (!id || !next) { setRenamingId(null); return; }
    const original = pipelines.find((p) => p.id === id)?.name || '';
    if (next === original) { setRenamingId(null); return; }
    setRenameSaving(true);
    try {
      const wf = await api.getWorkflow(id);
      const workflow = wf?.workflow || wf;
      const updated = { ...workflow, name: next };
      await api.updateWorkflow(id, updated, `Renamed to "${next}"`);
      toast.success('Renamed', `"${original}" → "${next}"`);
      fetchPipelines();
    } catch (err: any) {
      toast.error('Rename failed', err?.message || 'Could not rename pipeline');
    } finally {
      setRenameSaving(false);
      setRenamingId(null);
    }
  };
  const handleCancelRename = () => { setRenamingId(null); };
  const [estimateCache, setEstimateCache] = useState<Record<string, string>>({});
  const [estimateHover, setEstimateHover] = useState<string | null>(null);
  const [compareIdx, setCompareIdx] = useState<number | null>(null);
  const [tagFilter, setTagFilter] = useState('');
  const [pipelineTags, setPipelineTags] = useState<Record<string, string[]>>(() => {
    try { return JSON.parse(localStorage.getItem('fpulse_pipeline_tags') || '{}'); } catch { return {}; }
  });
  const [tagColors, setTagColors] = useState<Record<string, string>>(() => {
    try { return JSON.parse(localStorage.getItem('fpulse_tag_colors') || '{}'); } catch { return {}; }
  });
  const [showTagMenu, setShowTagMenu] = useState<string | null>(null);
  const [newTagInput, setNewTagInput] = useState('');
  const [showTagFilter, setShowTagFilter] = useState(false);
  const [runningPipelines, setRunningPipelines] = useState<Record<string, boolean>>({});
  const [preRunMode, setPreRunMode] = useState<RunSafetyMode>('live');
  // Parameter override flow. Set when user clicks Run on a
  // pipeline that declares parameters; the dialog collects values then
  // calls handleRunPipeline with parameter_values populated.
  const [runWithParamsTarget, setRunWithParamsTarget] = useState<{
    id: string;
    name: string;
    parameters: WorkflowParameter[];
  } | null>(null);
  const [pipelineStepResults, setPipelineStepResults] = useState<Record<string, Record<string, any>>>({});
  const [quickSchedule, setQuickSchedule] = useState<{ id: string; name: string } | null>(null);
  const [quickAlert, setQuickAlert] = useState<{ id: string; name: string } | null>(null);
  const [prodTab, setProdTab] = useState<'pending' | 'deployed' | 'failed' | 'all'>('pending');
  const [deployingIds, setDeployingIds] = useState<Set<string>>(new Set());
  const [preDeployDialog, setPreDeployDialog] = useState<{ id: string; name: string; version?: number } | null>(null);
  const [preDeployChecks, setPreDeployChecks] = useState<any>(null);
  const [preDeployLoading, setPreDeployLoading] = useState(false);
  const [preDeployExtra, setPreDeployExtra] = useState<{ schedules: any[]; alerts: any[]; credentials: any[]; connections: any[] }>({ schedules: [], alerts: [], credentials: [], connections: [] });
  const [expandedChecks, setExpandedChecks] = useState<Set<number>>(new Set());
  // ── Deploy-dialog enhancements (Apr 18 request):
  //   • View real DAG lineage — opens a read-only ReactFlow mini-canvas
  //   • Pick any prior version to redeploy — fetched lazily when the
  //     `version_change` check row is expanded
  const [dagPreview, setDagPreview] = useState<{ id: string; name: string; workflow: any } | null>(null);
  const [dagPreviewLoading, setDagPreviewLoading] = useState(false);
  // Version picker scoped to the pre-deploy dialog. Distinct from the
  // module-level `versions` / `versionsLoading` (declared above at ~L483)
  // which serve the Version History modal. Separate state = separate
  // lifecycle, so closing one doesn't wipe the other.
  const [deployVersions, setDeployVersions] = useState<any[]>([]);
  const [deployVersionsLoading, setDeployVersionsLoading] = useState(false);
  const [scheduleMap, setScheduleMap] = useState<Record<string, any>>({});
  // workflow_id → number of enabled alert rules. Powers the Alerts
  // column in the table; refreshed alongside `pipelines` so newly
  // added rules show up after the QuickAlertDialog closes.
  const [alertCountMap, setAlertCountMap] = useState<Record<string, number>>({});
  // 2026-05-25 — workflow_id → number of Storage items (files + managed
  // tables) the pipeline references. Inverted from /api/storage/usage
  // (the endpoint returns the natural index "storage_id → [pipelines]";
  // we flip it to "pipeline → count" so the row cell is O(1)).
  const [storageUsageMap, setStorageUsageMap] = useState<Record<string, number>>({});

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch('/api/storage/usage', {
          headers: {
            'X-Workspace-Id': localStorage.getItem('fpulse_workspace_id') || 'default',
            Authorization: `Bearer ${localStorage.getItem('fpulse_token') || ''}`,
          },
        });
        if (!r.ok) return;
        const usage = await r.json() as { files?: Record<string, any[]>; tables?: Record<string, any[]> };
        const inverted: Record<string, number> = {};
        for (const refs of Object.values(usage.files || {})) {
          for (const ref of refs) {
            const wf = ref?.workflow_id;
            if (wf) inverted[wf] = (inverted[wf] || 0) + 1;
          }
        }
        for (const refs of Object.values(usage.tables || {})) {
          for (const ref of refs) {
            const wf = ref?.workflow_id;
            if (wf) inverted[wf] = (inverted[wf] || 0) + 1;
          }
        }
        if (!cancelled) setStorageUsageMap(inverted);
      } catch { /* best-effort */ }
    })();
    return () => { cancelled = true; };
  }, []);
  const [detailPipeline, setDetailPipeline] = useState<Pipeline | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailData, setDetailData] = useState<any>(null);
  const [deleteModal, setDeleteModal] = useState<{ id: string; name: string } | null>(null);
  const [binItems, setBinItems] = useState<Array<{ id: string; name: string; deleted_at: string; project_id?: string; status?: string }>>(() => {
    try { return JSON.parse(localStorage.getItem('fpulse_bin') || '[]'); } catch { return []; }
  });
  const [showExportMenu, setShowExportMenu] = useState(false);
  const [showColumnPicker, setShowColumnPicker] = useState(false);
  const [visibleColumns, setVisibleColumns] = useState<Set<string>>(() => {
    try {
      const saved = localStorage.getItem('fpulse_deploy_columns');
      if (saved) return new Set(JSON.parse(saved));
    } catch {}
    return new Set(DEPLOY_COLUMNS.filter(c => c.default).map(c => c.key));
  });
  const [showExportPanel, setShowExportPanel] = useState(false);
  const columnPickerRef = useRef<HTMLDivElement>(null);
  const exportPanelRef = useRef<HTMLDivElement>(null);
  const exportMenuRef = useRef<HTMLDivElement>(null);
  const tagMenuRef = useRef<HTMLDivElement>(null);
  const tagFilterRef = useRef<HTMLDivElement>(null);
  const loadWorkflow = useWorkflowStore((s) => s.loadWorkflow);

  // Close tag menu / filter / export menu on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (tagMenuRef.current && !tagMenuRef.current.contains(e.target as Node)) setShowTagMenu(null);
      if (tagFilterRef.current && !tagFilterRef.current.contains(e.target as Node)) setShowTagFilter(false);
      if (exportMenuRef.current && !exportMenuRef.current.contains(e.target as Node)) setShowExportMenu(false);
      if (columnPickerRef.current && !columnPickerRef.current.contains(e.target as Node)) setShowColumnPicker(false);
      if (exportPanelRef.current && !exportPanelRef.current.contains(e.target as Node)) setShowExportPanel(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const getTagsForPipeline = (id: string): string[] => pipelineTags[id] || [];

  const toggleTag = (pipelineId: string, tag: string) => {
    setPipelineTags((prev) => {
      const current = prev[pipelineId] || [];
      const next = current.includes(tag) ? current.filter((t) => t !== tag) : [...current, tag];
      const updated = { ...prev, [pipelineId]: next };
      if (next.length === 0) delete updated[pipelineId];
      localStorage.setItem('fpulse_pipeline_tags', JSON.stringify(updated));
      return updated;
    });
  };

  const setTagColor = (tag: string, color: string) => {
    setTagColors((prev) => {
      const updated = { ...prev, [tag]: color };
      localStorage.setItem('fpulse_tag_colors', JSON.stringify(updated));
      return updated;
    });
  };

  const getColorForTag = (tag: string) => {
    const colorName = tagColors[tag] || 'blue';
    return TAG_COLORS.find((c) => c.name === colorName) || TAG_COLORS[0];
  };

  const allTags = useMemo(() => {
    const tags = new Set<string>();
    Object.values(pipelineTags).forEach((arr) => arr.forEach((t) => tags.add(t)));
    return [...tags].sort();
  }, [pipelineTags]);

  const addNewTag = (pipelineId: string) => {
    const tag = newTagInput.trim().toLowerCase();
    if (!tag) return;
    if (!getTagsForPipeline(pipelineId).includes(tag)) {
      toggleTag(pipelineId, tag);
      if (!tagColors[tag]) setTagColor(tag, TAG_COLORS[0].name);
    }
    setNewTagInput('');
  };

  const computeDiff = (newer: VersionInfo, older: VersionInfo): VersionDiff => {
    const newerSteps = new Set((newer.steps || []).map(s => s.label || s.type));
    const olderSteps = new Set((older.steps || []).map(s => s.label || s.type));
    const addedSteps = [...newerSteps].filter(s => !olderSteps.has(s));
    const removedSteps = [...olderSteps].filter(s => !newerSteps.has(s));
    return {
      addedSteps,
      removedSteps,
      addedConns: (newer.connections?.length || 0) - (older.connections?.length || 0),
      removedConns: Math.max(0, (older.connections?.length || 0) - (newer.connections?.length || 0)),
    };
  };

  // N4 — per-pipeline recent run statuses, keyed by workflow id.
  // Fetched once per pipelines load via /api/monitor/recent-statuses;
  // empty arrays for pipelines we couldn't get statuses for so the
  // sparkline degrades silently to "no recent runs" instead of
  // exploding.
  const [recentStatuses, setRecentStatuses] = useState<Record<string, string[]>>({});

  const fetchPipelines = async () => {
    setLoading(true);
    try {
      const params: { project_id?: string } = {};
      if (projectId) params.project_id = projectId;
      const list = await api.listWorkflows(params);
      const safeList = Array.isArray(list) ? list : [];
      setPipelines(safeList);
      // Fire the recent-statuses lookup in the background — the row
      // renders without it, the sparkline fills in when the fetch
      // returns. Limit to 14 (~ two weeks of daily runs).
      if (safeList.length > 0) {
        const ids = safeList.map((p: any) => p.id).filter(Boolean);
        api
          .getRecentStatuses(ids, 14)
          .then((data) => setRecentStatuses(data.by_workflow || {}))
          .catch(() => { /* sparkline degrades silently */ });
      } else {
        setRecentStatuses({});
      }
    } catch {
      setPipelines([]);
    }
    setLoading(false);
  };

  useEffect(() => { fetchPipelines(); }, [projectId]);

  // Load the project list once for the workflow-table Project filter
  // dropdown. Falls back to an empty list silently — the dropdown then
  // only shows "All projects" + whatever project_ids appear on the
  // pipelines themselves (computed below).
  useEffect(() => {
    api.listProjects()
      .then((p) => setProjectsList(Array.isArray(p) ? p.map((x: any) => ({ id: x.id, name: x.name || x.id })) : []))
      .catch(() => setProjectsList([]));
  }, []);

  // Publish page context for the AI Copilot — lets the agent answer
  // page-specific questions ("which are failing?", "what's deployed?")
  // without a discovery tool call. Capped to 50 inside the hook.
  usePageContext({
    page: 'pipelines.list',
    visible_ids: pipelines.map((p) => p.id),
    selected_ids: Array.from(selectedIds),
    filters: { status: statusFilter, search: searchQuery, tag: tagFilter },
    environment: environment as 'dev' | 'prod' | undefined,
    visible_items: pipelines.map((p) => ({
      id: p.id,
      name: p.name,
      kind: 'pipeline',
      status: p.last_run_status || p.status,
      meta: {
        version: p.version ?? null,
        steps: p.steps_count ?? p.step_count ?? null,
        last_run: p.last_run ?? null,
        last_run_duration_ms: p.last_run_duration ?? null,
        project_id: p.project_id ?? null,
      },
    })),
  });

  // Fetch schedules for all pipelines. Re-runs when the Quick
  // Schedule dialog closes so a newly added schedule shows up in
  // the table cell immediately, instead of staying "+ Schedule"
  // until the next pipeline list refresh.
  useEffect(() => {
    if (pipelines.length === 0) return;
    (async () => {
      try {
        const schedules = await api.listSchedules();
        const map: Record<string, any> = {};
        for (const s of (schedules || [])) {
          if (s.workflow_id && s.enabled) map[s.workflow_id] = s;
        }
        setScheduleMap(map);
      } catch { /* ignore */ }
    })();
  }, [pipelines, quickSchedule]);

  // Fetch alert rules for all pipelines so the Alerts column can show
  // a count per row. Same lifecycle as scheduleMap.
  useEffect(() => {
    if (pipelines.length === 0) return;
    (async () => {
      try {
        const rules = await api.listAlertRules();
        const map: Record<string, number> = {};
        for (const r of (rules || [])) {
          const wfId = r.workflow_id;
          if (!wfId) continue;
          if (r.enabled === false) continue;
          map[wfId] = (map[wfId] || 0) + 1;
        }
        setAlertCountMap(map);
      } catch { /* ignore — column gracefully shows 0 when the endpoint fails */ }
    })();
  }, [pipelines, quickAlert]);

  const handleOpen = async (id: string) => {
    try {
      const data = await api.getWorkflow(id);
      loadWorkflow(data);
      onOpenEditor();
    } catch (err: any) {
      toast.error('Failed to open pipeline', err.message);
    }
  };

  const handleShowDetail = async (pipeline: Pipeline) => {
    setDetailPipeline(pipeline);
    setDetailLoading(true);
    try {
      const data = await api.getWorkflow(pipeline.id);
      // Unwrap: API returns { version, workflow: { steps, connections, ... } }
      const wf = data?.workflow || data;
      setDetailData(wf);
    } catch {
      setDetailData(null);
    }
    setDetailLoading(false);
  };

  const handleDelete = (id: string, name: string) => {
    setDeleteModal({ id, name });
  };

  const saveBin = (items: typeof binItems) => {
    setBinItems(items);
    localStorage.setItem('fpulse_bin', JSON.stringify(items));
  };

  const confirmDelete = async () => {
    if (!deleteModal) return;
    // Optimistic update — drop the row from local state and close the
    // modal IMMEDIATELY so the user sees the deletion happen instantly.
    // Calling fetchPipelines() afterwards would flip `loading=true` and
    // flash the skeleton/empty state, which reads like a page reload
    // (user feedback 2026-05-12). We snapshot the row + close the modal,
    // hit the API, then either keep the optimistic state on success or
    // restore it on failure.
    const pipeline = pipelines.find(p => p.id === deleteModal.id);
    const targetId = deleteModal.id;
    const targetName = deleteModal.name;
    const previousPipelines = pipelines;
    setPipelines((prev) => prev.filter((p) => p.id !== targetId));
    setSelectedIds((prev) => { const next = new Set(prev); next.delete(targetId); return next; });
    if (detailPipeline?.id === targetId) { setDetailPipeline(null); setDetailData(null); }
    setDeleteModal(null);
    try {
      await api.deleteWorkflow(targetId);
      const binEntry = {
        id: targetId,
        name: targetName,
        deleted_at: new Date().toISOString(),
        project_id: (pipeline as any)?.project_id || 'default',
        status: pipeline?.status || 'draft',
      };
      saveBin([binEntry, ...binItems]);
      toast.success('Moved to Bin', `"${targetName}" — restore from Bin tab`);
    } catch (err: any) {
      // Roll back the optimistic delete if the server refused.
      setPipelines(previousPipelines);
      toast.error('Delete failed', err.message);
    }
  };

  const confirmArchive = async () => {
    if (!deleteModal) return;
    // Same optimistic pattern as confirmDelete — see comment there.
    // Archived rows DON'T leave the list in OSS Free (the table just
    // re-tags them), so we patch the status in place rather than
    // splicing the row out.
    const targetId = deleteModal.id;
    const targetName = deleteModal.name;
    const previousPipelines = pipelines;
    setPipelines((prev) => prev.map((p) => p.id === targetId ? { ...p, status: 'archived' } : p));
    if (detailPipeline?.id === targetId) { setDetailPipeline(null); setDetailData(null); }
    setDeleteModal(null);
    try {
      await api.archiveWorkflow(targetId);
      toast.success('Pipeline archived', `"${targetName}" moved to Archived`);
    } catch (err: any) {
      setPipelines(previousPipelines);
      toast.error('Archive failed', err.message);
    }
  };

  const handleDuplicate = async (pipeline: Pipeline) => {
    try {
      await api.clonePipeline(pipeline.id, `${pipeline.name} (copy)`);
      toast.success('Pipeline copied');
      fetchPipelines();
    } catch (err: any) {
      toast.error('Copy failed', err.message);
    }
  };

  // Save-as-template — fetches the pipeline IR (steps + connections) and
  // opens a dialog so the user can name it, add tagline/description, and
  // commit to user_templates. The dialog handles dup-name validation
  // against the names returned in `existingNames`.
  const [saveTplState, setSaveTplState] = useState<{
    open: boolean;
    pipelineName: string;
    steps: any[];
    connections: any[];
    existingNames: string[];
  }>({ open: false, pipelineName: '', steps: [], connections: [], existingNames: [] });

  // Column-visibility state — drives the "Columns" popover in the navy
  // strip. Persists to localStorage so a user's preferred set of
  // metadata columns sticks across reloads. Same hook the Connections
  // page uses (useTableColumns).
  const pipeColState = useTableColumns('fpulse_pipelines_cols', PIPELINE_COLUMNS);
  const [showPipeColumnPicker, setShowPipeColumnPicker] = useState(false);
  const pipeColumnPickerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!showPipeColumnPicker) return;
    const onDoc = (e: MouseEvent) => {
      if (pipeColumnPickerRef.current && !pipeColumnPickerRef.current.contains(e.target as Node)) {
        setShowPipeColumnPicker(false);
      }
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [showPipeColumnPicker]);

  // Worker-pool queue priority — small picker rendered in a portal so it
  // never gets clipped by table overflow. Tracks which row is open and
  // where to anchor the floating menu (computed from the trigger's
  // bounding rect at open time).
  const [priorityMenuFor, setPriorityMenuFor] = useState<string | null>(null);
  const [priorityAnchor, setPriorityAnchor] = useState<{ top: number; left: number; placement: 'below' | 'above' } | null>(null);
  const [priorityBusy, setPriorityBusy] = useState<string | null>(null);

  const openPriorityMenu = (pipelineId: string, trigger: HTMLElement) => {
    const r = trigger.getBoundingClientRect();
    const menuH = 220;
    const menuW = 220;
    const spaceBelow = window.innerHeight - r.bottom;
    const placement: 'below' | 'above' = spaceBelow >= menuH + 12 ? 'below' : 'above';
    const top = placement === 'below' ? r.bottom + 4 : r.top - menuH - 4;
    // Keep within viewport horizontally.
    let left = r.left;
    if (left + menuW > window.innerWidth - 12) left = window.innerWidth - menuW - 12;
    if (left < 12) left = 12;
    setPriorityAnchor({ top, left, placement });
    setPriorityMenuFor(pipelineId);
  };

  // Close priority popover on outside click.
  useEffect(() => {
    if (!priorityMenuFor) return;
    const onDoc = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      if (target?.closest('[data-priority-menu="true"]')) return;
      if (target?.closest('[data-priority-trigger="true"]')) return;
      setPriorityMenuFor(null);
      setPriorityAnchor(null);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [priorityMenuFor]);

  const handleSetPriority = async (pipeline: Pipeline, newPriority: number) => {
    if (priorityBusy) return;
    setPriorityBusy(pipeline.id);
    try {
      const data = await api.getWorkflow(pipeline.id);
      const wf = data?.workflow || data;
      if (!wf) throw new Error('Workflow not found');
      const nextMetadata = { ...(wf.metadata || {}), priority: newPriority };
      const updatedWf = { ...wf, metadata: nextMetadata };
      await api.updateWorkflow(pipeline.id, updatedWf, `Set run priority to P${newPriority}`);
      toast.success('Priority updated', `${pipeline.name} → P${newPriority}`);
      setPriorityMenuFor(null);
      setPriorityAnchor(null);
      fetchPipelines();
    } catch (err: any) {
      toast.error('Could not update priority', err?.message || 'Try again');
    } finally {
      setPriorityBusy(null);
    }
  };

  /**
   * R7b (2026-05-30) — Publish-as-macro toggle.
   *
   * Sets `metadata.published_as_node = true` on the workflow so the
   * /api/node-types endpoint surfaces it as a virtual palette entry
   * (type `execute_pipeline:<wf_id>`). Other pipelines can then drag
   * this one onto their canvas as a single tile and pass parameters
   * to it, instead of duplicating its node graph.
   *
   * Idempotent: clicking again removes the flag.
   */
  const handleTogglePublishMacro = async (pipeline: Pipeline) => {
    try {
      const data = await api.getWorkflow(pipeline.id);
      const wf = data?.workflow || data;
      if (!wf) throw new Error('Workflow not found');
      const currentlyPublished = !!(wf.metadata || {}).published_as_node;
      const nextMetadata = {
        ...(wf.metadata || {}),
        published_as_node: !currentlyPublished,
      };
      const updatedWf = { ...wf, metadata: nextMetadata };
      const action = currentlyPublished ? 'Unpublish' : 'Publish';
      await api.updateWorkflow(pipeline.id, updatedWf, `${action} as macro`);
      toast.success(
        `${action}ed as macro`,
        currentlyPublished
          ? `${pipeline.name} removed from the palette.`
          : `${pipeline.name} now appears in the palette under "Macro".`,
      );
      fetchPipelines();
    } catch (err: any) {
      toast.error('Could not toggle macro publication', err?.message || 'Try again');
    }
  };

  const handleSaveAsTemplate = async (pipeline: Pipeline) => {
    try {
      const data = await api.getWorkflow(pipeline.id);
      const wf = data?.workflow || data;
      const steps = (wf?.steps || []).map((s: any) => ({
        id: s.id,
        type: s.type,
        label: s.label,
        params: s.params || {},
        position: s.position || { x: 0, y: 0 },
      }));
      const connections = (wf?.connections || []).map((c: any) => ({
        from_step: c.from_step,
        to_step: c.to_step,
        ...(c.condition ? { condition: c.condition } : {}),
      }));
      let existingNames: string[] = [];
      try {
        const list = await api.listUserTemplates();
        existingNames = (list?.templates || []).map((t: any) => String(t.name || ''));
      } catch {
        // Listing failed — proceed without client-side dup hint; backend
        // will still 409 if the name collides.
      }
      setSaveTplState({
        open: true,
        pipelineName: pipeline.name || 'My template',
        steps,
        connections,
        existingNames,
      });
    } catch (err: any) {
      toast.error('Could not load pipeline', err?.message || 'Try again');
    }
  };

  const handleMoveProject = async (
    pipelineId: string,
    pipelineName: string,
    targetProjectId: string,
    targetFolderId: string | null = null,
  ) => {
    try {
      // Two-step move so the user can drop a pipeline straight into a
      // folder under a different project: first reassign the project
      // (changes project_id), then call the folder-move endpoint with
      // the resolved folder id (null = project root, no folder). The
      // second call is skipped when targetFolderId is null AND we just
      // want a plain project reassignment, but still required when the
      // pipeline was previously inside a folder and the user picks
      // "Project root" — we send folder_id=null to clear it.
      await api.moveWorkflow(pipelineId, targetProjectId);
      await api.moveWorkflowsToFolder({
        workflow_ids: [pipelineId],
        folder_id: targetFolderId,
      });
      const projectName = projectsList.find(pr => pr.id === targetProjectId)?.name || targetProjectId;
      const folderName = targetFolderId ? folders.find(f => f.id === targetFolderId)?.name : null;
      const dest = folderName ? `${projectName} / ${folderName}` : projectName;
      toast.success('Pipeline moved', `"${pipelineName}" → ${dest}`);
      fetchPipelines();
    } catch (err: any) {
      toast.error('Move failed', err.message);
    }
  };

  // ── Bulk Export helpers ──
  const downloadFile = useCallback((content: string, filename: string, type = 'text/csv') => {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }, []);

  const exportWorkflowList = (format: 'csv' | 'json') => {
    // Resolve project id → name and folder id → name once so the
    // export shows human-readable values, not opaque IDs. Falls back
    // to the raw id if a project/folder reference can't be resolved
    // (e.g. orphan pipeline whose project was deleted).
    const folderById = new Map(folders.map((f) => [f.id, f.name] as const));
    const projectById = new Map(projectsList.map((p) => [p.id, p.name] as const));
    const data = pipelines.map((p) => {
      const projectId = (p as any).project_id || '';
      const folderId = (p as any).folder_id || '';
      // pipelineTags is the canonical source — it's fetched per-pipeline
      // by the dedicated tags endpoint, so a list response that omits
      // tags doesn't mean the pipeline has none.
      const tagList = (pipelineTags[p.id] || p.tags || []) as string[];
      return {
        id: p.id,
        name: p.name,
        description: p.description || '',
        status: p.status || 'draft',
        priority: p.priority || '',
        environment: p.status === 'published' || p.status === 'success' ? 'PROD' : 'DEV',
        project_id: projectId,
        project_name: projectId ? projectById.get(projectId) || projectId : '',
        folder_id: folderId,
        folder_name: folderId ? folderById.get(folderId) || folderId : '',
        department: p.department || '',
        steps_count: p.step_count || p.steps_count || p.steps?.length || 0,
        connections_count: p.connections?.length || 0,
        version: p.version || 1,
        deployed_version: p.deployed_version || '',
        owner: p.created_by || p.owner_name || p.owner_id || '',
        created_at: p.created_at || '',
        updated_at: p.updated_at || '',
        last_run: (p as any).last_run || (p as any).last_run_at || '',
        last_run_status: p.last_run_status || '',
        last_run_duration_ms: p.last_run_duration || '',
        schedule_cron: scheduleMap[p.id]?.cron_expression || '',
        schedule_enabled: scheduleMap[p.id]?.enabled ?? '',
        schedule_timezone: scheduleMap[p.id]?.timezone || '',
        alerts_count: alertCountMap[p.id] || 0,
        tags: tagList.join('; '),
        submitted_by: p.submitted_by || '',
        submitted_at: p.submitted_at || '',
        approved_by: p.approved_by || '',
        approved_at: p.approved_at || '',
        published_by: p.published_by || '',
        published_at: p.published_at || '',
        deployed_by: p.deployed_by || '',
        deployed_at: p.deployed_at || '',
      };
    });
    if (format === 'json') {
      downloadFile(JSON.stringify(data, null, 2), 'workflows.json', 'application/json');
    } else if (data.length === 0) {
      toast.info('Nothing to export', 'No pipelines on this page');
      return;
    } else {
      // Headers derived from the row shape — adding a field above
      // automatically extends the CSV without a header-list edit.
      const headers = Object.keys(data[0]);
      const csvHeader = headers.join(',');
      const csvRows = data.map((d) =>
        headers.map((h) => `"${String((d as any)[h] ?? '').replace(/"/g, '""')}"`).join(','),
      );
      downloadFile([csvHeader, ...csvRows].join('\n'), 'workflows.csv');
    }
    toast.success('Exported', `${data.length} pipeline${data.length === 1 ? '' : 's'} (${format.toUpperCase()})`);
  };

  const exportRunHistory = async () => {
    try {
      const runs = await api.listExecutions();
      const header = 'Pipeline,Status,Started,Finished,Duration';
      const rows = (runs || []).map((r: any) => {
        const dur = r.started_at && r.finished_at
          ? `${Math.round((new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 1000)}s`
          : '';
        return `"${r.workflow_name || r.workflow_id}","${r.status}","${r.started_at || ''}","${r.finished_at || ''}","${dur}"`;
      });
      downloadFile([header, ...rows].join('\n'), 'run_history.csv');
      toast.success('Exported', 'Run history (CSV)');
    } catch (err: any) {
      toast.error('Export failed', err.message);
    }
  };

  const exportSchedules = async () => {
    try {
      const schedules = await api.listSchedules();
      const header = 'Pipeline,Schedule,Timezone,Enabled,Created';
      const rows = (schedules || []).map((s: any) =>
        `"${s.name || s.workflow_id}","${s.cron_expression || ''}","${s.timezone || 'UTC'}","${s.enabled}","${s.created_at || ''}"`
      );
      downloadFile([header, ...rows].join('\n'), 'schedules.csv');
      toast.success('Exported', 'Schedules (CSV)');
    } catch (err: any) {
      toast.error('Export failed', err.message);
    }
  };

  // ── Single Pipeline Export / Import ──
  const handleExport = async (pipeline: Pipeline) => {
    try {
      const data = await api.exportPipeline(pipeline.id);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${pipeline.name.replace(/[^a-zA-Z0-9_-]/g, '_')}.fpulse.json`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success('Pipeline exported', `Downloaded ${a.download}`);
    } catch (err: any) {
      toast.error('Export failed', err.message);
    }
  };

  // ── Column Management ──
  const toggleColumn = useCallback((key: string) => {
    setVisibleColumns(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      localStorage.setItem('fpulse_deploy_columns', JSON.stringify([...next]));
      return next;
    });
  }, []);

  const resetColumnsToDefault = useCallback(() => {
    const defaults = new Set(DEPLOY_COLUMNS.filter(c => c.default).map(c => c.key));
    setVisibleColumns(defaults);
    localStorage.setItem('fpulse_deploy_columns', JSON.stringify([...defaults]));
  }, []);

  const selectAllColumns = useCallback(() => {
    const all = new Set(DEPLOY_COLUMNS.map(c => c.key));
    setVisibleColumns(all);
    localStorage.setItem('fpulse_deploy_columns', JSON.stringify([...all]));
  }, []);

  const activeColumns = useMemo(() =>
    DEPLOY_COLUMNS.filter(c => visibleColumns.has(c.key)),
  [visibleColumns]);

  // ── Multi-Level Export ──
  const buildPipelineRow = useCallback((p: Pipeline) => ({
    id: p.id,
    name: p.name,
    description: p.description || '',
    status: p.status || 'draft',
    environment: (p.status === 'published' || p.status === 'success') ? 'PROD' : 'DEV',
    project: p.project_id || 'default',
    department: p.department || '',
    priority: p.priority || '',
    owner: p.created_by || p.owner_name || p.owner_id || '',
    steps: p.step_count || p.steps_count || 0,
    connections: p.connections?.length || 0,
    version: p.version || 1,
    deployed_version: p.deployed_version || '',
    tags: (pipelineTags[p.id] || p.tags || []).join('; '),
    submitted_by: p.submitted_by || '',
    submitted_at: p.submitted_at || '',
    approved_by: p.approved_by || '',
    approved_at: p.approved_at || '',
    deployed_by: p.deployed_by || '',
    deployed_at: p.deployed_at || '',
    published_by: p.published_by || '',
    published_at: p.published_at || '',
    schedule: scheduleMap[p.id]?.cron_expression || '',
    schedule_enabled: scheduleMap[p.id]?.enabled ?? '',
    schedule_timezone: scheduleMap[p.id]?.timezone || '',
    created_at: p.created_at || '',
    updated_at: p.updated_at || '',
    last_run: p.last_run || '',
    last_run_status: p.last_run_status || '',
    rollback_from: p.rollback_from || '',
  }), [scheduleMap, pipelineTags]);

  const exportMultiLevel = useCallback((level: 'metadata' | 'pipeline' | 'project' | 'page', format: 'csv' | 'json', pipelineId?: string) => {
    let rows: any[] = [];
    let filename = '';

    if (level === 'pipeline' && pipelineId) {
      const p = pipelines.find(x => x.id === pipelineId);
      if (!p) return;
      rows = [buildPipelineRow(p)];
      filename = `pipeline_${p.name.replace(/[^a-zA-Z0-9_-]/g, '_')}`;
    } else if (level === 'project') {
      const groups: Record<string, Pipeline[]> = {};
      pipelines.forEach(p => {
        const proj = p.project_id || 'default';
        if (!groups[proj]) groups[proj] = [];
        groups[proj].push(p);
      });
      rows = pipelines.map(buildPipelineRow);
      // Add project summary rows
      const projectSummary = Object.entries(groups).map(([proj, items]) => ({
        _type: 'PROJECT_SUMMARY',
        project: proj,
        total_pipelines: items.length,
        deployed: items.filter(p => p.status === 'published' || p.status === 'success').length,
        pending: items.filter(p => !p.status || p.status === 'draft' || p.status === 'idle').length,
        failed: items.filter(p => p.status === 'error' || p.status === 'failed').length,
        total_steps: items.reduce((a, p) => a + (p.step_count || p.steps_count || 0), 0),
      }));
      if (format === 'json') {
        downloadFile(JSON.stringify({ projects: projectSummary, pipelines: rows }, null, 2), `project_export.json`, 'application/json');
        toast.success('Exported', `Project-level export (JSON)`);
        return;
      }
      filename = 'project_export';
    } else if (level === 'page') {
      // Full page export — all pipelines + schedules + metadata
      rows = pipelines.map(buildPipelineRow);
      filename = `pipelines_page_${isProd ? 'PROD' : 'DEV'}_${new Date().toISOString().slice(0, 10)}`;
    } else {
      // metadata = full metadata export with all fields
      rows = pipelines.map(buildPipelineRow);
      filename = `pipelines_metadata_full_${new Date().toISOString().slice(0, 10)}`;
    }

    if (format === 'json') {
      downloadFile(JSON.stringify(rows, null, 2), `${filename}.json`, 'application/json');
    } else {
      if (rows.length === 0) {
        toast.info('Nothing to export', 'No data available');
        return;
      }
      const headers = Object.keys(rows[0]);
      const csvHeader = headers.join(',');
      const csvRows = rows.map(r => headers.map(h => `"${String(r[h] ?? '').replace(/"/g, '""')}"`).join(','));
      downloadFile([csvHeader, ...csvRows].join('\n'), `${filename}.csv`);
    }
    toast.success('Exported', `${level} export (${format.toUpperCase()})`);
  }, [pipelines, buildPipelineRow, isProd, downloadFile]);

  const [importModalOpen, setImportModalOpen] = useState(false);
  const [importFile, setImportFile] = useState<any>(null);
  const [importFileName, setImportFileName] = useState('');
  const [importProjectId, setImportProjectId] = useState('default');
  const [importRename, setImportRename] = useState('');
  const [importConnMap, setImportConnMap] = useState<Record<string, string>>({});
  const [importConnRefs, setImportConnRefs] = useState<string[]>([]);
  const [importProjects, setImportProjects] = useState<any[]>([]);
  const [importCredentials, setImportCredentials] = useState<any[]>([]);
  const [importing, setImporting] = useState(false);

  const handleImportFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      if (!data.pipeline || !data.pipeline.steps) {
        toast.error('Invalid file', 'Not a valid .fpulse export');
        return;
      }
      setImportFile(data);
      setImportFileName(file.name);
      setImportRename(data.pipeline.name || '');
      // Extract connection references from steps
      const connRefs = new Set<string>();
      for (const s of data.pipeline.steps || []) {
        const p = s.params || {};
        for (const key of ['connection_id', 'credential_id', 'connection_name']) {
          if (p[key]) connRefs.add(p[key]);
        }
      }
      setImportConnRefs(Array.from(connRefs));
      setImportConnMap({});
      // Load projects and credentials for the mapping UI
      const [projects, creds] = await Promise.all([
        api.listProjects().catch(() => []),
        api.listCredentials().catch(() => []),
      ]);
      setImportProjects(projects || []);
      setImportCredentials(creds || []);
      setImportModalOpen(true);
    } catch {
      toast.error('Parse error', 'Could not parse the selected file as JSON');
    }
    e.target.value = '';
  };

  const handleImportConfirm = async () => {
    if (!importFile) return;
    setImporting(true);
    try {
      const result = await api.importPipeline(
        importFile.pipeline,
        importProjectId,
        importRename,
        importConnMap,
      );
      toast.success(
        'Pipeline imported',
        `${result.steps_imported} steps, ${result.connections_imported} edges${
          result.connections_remapped ? `, ${result.connections_remapped} connections remapped` : ''
        }`,
      );
      setImportModalOpen(false);
      setImportFile(null);
      fetchPipelines();
    } catch (err: any) {
      toast.error('Import failed', err.message);
    }
    setImporting(false);
  };

  // ── Approval Workflow ──
  // Submit-for-review now opens a Plan modal first so the user previews
  // the diff against the deployed version before locking it for approval.
  const [planTarget, setPlanTarget] = useState<{ id: string; name: string } | null>(null);
  const [planSubmitting, setPlanSubmitting] = useState(false);
  const handleSubmitForReview = (id: string, name: string) => {
    setPlanTarget({ id, name });
  };
  const handlePlanConfirm = async (snapshotHash?: string) => {
    if (!planTarget) return;
    setPlanSubmitting(true);
    try {
      // Forward the AI pre-publish snapshot hash so the approval workflow
      // captures exactly which IR was reviewed at click time.
      await api.submitForReview(planTarget.id, snapshotHash);
      toast.success('Submitted for review', `"${planTarget.name}" is now pending approval${snapshotHash ? ' (snapshot recorded)' : ''}`);
      setPlanTarget(null);
      fetchPipelines();
    } catch (err: any) {
      toast.error('Submit failed', err.message);
    } finally {
      setPlanSubmitting(false);
    }
  };

  const handleApprove = async (id: string, name: string) => {
    const notes = await uiPrompt({ title: `Approve "${name}" for PROD deployment?`, message: 'Optional notes:', placeholder: 'e.g. reviewed, tests green', confirmLabel: 'Approve' });
    if (notes === null) return; // cancelled
    try {
      await api.approvePipeline(id, notes);
      toast.success('Approved', `"${name}" is ready to deploy`);
      fetchPipelines();
    } catch (err: any) {
      toast.error('Approve failed', err.message);
    }
  };

  const handleReject = async (id: string, name: string) => {
    const notes = await uiPrompt({ title: `Reject "${name}"?`, message: 'Required: reason for rejection', placeholder: 'Explain why this is being rejected', confirmLabel: 'Reject' });
    if (!notes) return;
    try {
      await api.rejectPipeline(id, notes);
      toast.success('Rejected', `"${name}" sent back for changes`);
      fetchPipelines();
    } catch (err: any) {
      toast.error('Reject failed', err.message);
    }
  };

  const handleLifecycleAction = async (id: string, action: 'test' | 'publish' | 'revoke' | 'archive' | 'restore') => {
    // Publish opens the pre-publish test card instead of hitting /publish
    // directly. The backend gates publish on a passing /test result, and
    // the user wants to see per-step output before committing.
    if (action === 'publish') {
      const pipeline = pipelines.find((p) => p.id === id);
      setPublishTarget({ id, name: pipeline?.name || 'Pipeline' });
      return;
    }
    // Revoke pulls a published pipeline back to draft. Confirm first
    // — we also pause any enabled schedules for this pipeline so the
    // operator sees them as paused in the table (matching the gate
    // that only published pipelines can be scheduled). The executor's
    // draft-gate already halts them at run time, but a paused-flag
    // makes the state visible instead of silently inert.
    if (action === 'revoke') {
      const pipeline = pipelines.find((p) => p.id === id);
      const hasSchedule = !!scheduleMap[id];
      if (!await uiConfirm({
        title: `Revoke "${pipeline?.name || 'Pipeline'}"?`,
        message: hasSchedule
          ? 'The pipeline will move back to draft and any active schedules will be paused. You can re-publish to resume scheduling.'
          : 'The pipeline will move back to draft. Triggers will stop firing until you publish again.',
        confirmLabel: 'Revoke',
        destructive: true,
      })) return;
    }
    setLifecycleLoading((prev) => ({ ...prev, [id]: action }));
    try {
      if (action === 'test') await api.testWorkflow(id);
      else if (action === 'revoke') {
        await api.revokeWorkflow(id);
        // Pause any enabled schedules for this pipeline. We fetch
        // fresh (rather than relying on scheduleMap) in case there
        // are multiple schedules per workflow — listSchedules with
        // workflow_id is the canonical source. Errors here don't
        // block the revoke; the executor still halts execution at
        // the draft-gate, but the schedule row will look "active"
        // in the table until the next refresh.
        try {
          const schedules = await api.listSchedules({ workflow_id: id });
          const enabled = (schedules || []).filter((s: any) => s.enabled);
          if (enabled.length > 0) {
            await Promise.all(enabled.map((s: any) => api.toggleSchedule(s.id)));
          }
        } catch (e) {
          // Non-fatal — log only.
          console.warn('Could not pause schedules after revoke:', e);
        }
      }
      else if (action === 'archive') await api.archiveWorkflow(id);
      else if (action === 'restore') await api.restoreWorkflow(id);
      toast.success(`Pipeline ${action} successful`);
      fetchPipelines();
      // Re-fetch schedules so the table cell shows the paused state
      // (or hides the chip entirely, since scheduleMap filters on
      // `enabled`). The existing useEffect re-runs on `quickSchedule`
      // change, so bumping that null→null is a no-op; force a
      // refetch by mutating scheduleMap directly to drop entries
      // for this workflow.
      if (action === 'revoke') {
        setScheduleMap((prev) => {
          const next = { ...prev };
          delete next[id];
          return next;
        });
      }
    } catch (err: any) {
      toast.error(`${action} failed`, err.message);
    }
    setLifecycleLoading((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
  };

  // Activate / Deactivate — three behaviors per Free vs Plus + DEV vs PROD matrix.
  // Free DEV / Plus DEV → direct toggle (no approval).
  // Plus PROD → request flow: prompt for reason, create lifecycle_toggle_request.
  const handleToggleActive = async (p: Pipeline, nextActive: boolean) => {
    const isProdGated = environment === 'prod' && tier === 'plus';
    setLifecycleLoading((prev) => ({ ...prev, [p.id]: nextActive ? 'activate' : 'deactivate' }));
    try {
      if (isProdGated) {
        // PROD path — request approval. Reason field via prompt for now;
        // a richer modal can replace this in a polish PR.
        const reason = window.prompt(
          `Why ${nextActive ? 'activate' : 'deactivate'} "${p.name}" in PROD?\n\nThis request goes to the Approver — they decide whether to flip the flag.`,
          '',
        );
        if (reason === null) {
          setLifecycleLoading((prev) => {
            const next = { ...prev }; delete next[p.id]; return next;
          });
          return;
        }
        await api.requestLifecycleToggle(
          p.id,
          nextActive ? 'activate' : 'deactivate',
          reason || '',
          'prod',
        );
        toast.success(
          'Request sent',
          `${nextActive ? 'Activate' : 'Deactivate'} request sent to approver. You'll be notified when decided.`,
        );
      } else {
        // DEV path — direct toggle.
        await api.toggleActive(p.id, nextActive, 'dev');
        toast.success(
          nextActive ? 'Pipeline activated' : 'Pipeline deactivated',
          `"${p.name}" is now ${nextActive ? 'live' : 'paused'} in DEV`,
        );
      }
      fetchPipelines();
    } catch (err: any) {
      toast.error(
        `${nextActive ? 'Activate' : 'Deactivate'} failed`,
        err?.message || 'See logs',
      );
    }
    setLifecycleLoading((prev) => {
      const next = { ...prev }; delete next[p.id]; return next;
    });
  };

  const handleRunPipeline = async (id: string, name: string, parameterValues?: Record<string, unknown>) => {
    // If the pipeline has parameters AND the caller didn't pass values,
    // intercept and open the dialog instead. Detail-fetch is async; we
    // already have it cached when the user is viewing the detail panel.
    if (!parameterValues) {
      const wf = detailData?.id === id ? detailData : null;
      const params = (wf?.parameters as WorkflowParameter[] | undefined) || [];
      if (params.length > 0) {
        setRunWithParamsTarget({ id, name, parameters: params });
        return;
      }
    }

    setRunningPipelines(prev => ({ ...prev, [id]: true }));
    // Mark all steps as "running" if we have the workflow data
    setPipelineStepResults(prev => ({ ...prev, [id]: { __all_running: true } }));
    try {
      // Pass-through the user's safety-mode selection from PreRunBanner.
      // 'sample' / 'dry_run' / 'validate_only' all change behavior; 'live'
      // matches the legacy default (full execution per env).
      const fullRun = preRunMode === 'live';
      const result = await api.runWorkflow(id, fullRun, 'dev', preRunMode, parameterValues);
      const stepResults = result.step_results || {};
      // Store step-level results for detail panel
      setPipelineStepResults(prev => ({ ...prev, [id]: stepResults }));

      // Validate-only / dry-run paths return validator output, not step results.
      // Surface their outcome directly so the user knows nothing actually ran.
      if (preRunMode === 'validate_only' || preRunMode === 'dry_run') {
        const errs = result.validator?.errors?.length || 0;
        const warns = result.validator?.warnings?.length || 0;
        const connIssues = result.connection_issues?.length || 0;
        if (errs === 0 && connIssues === 0) {
          toast.success(`"${name}" — ${preRunMode} passed`, warns ? `${warns} warning(s)` : 'no issues');
        } else {
          toast.warning(`"${name}" — ${preRunMode} found issues`, `${errs} error(s), ${connIssues} connection issue(s)`);
        }
      } else {
        const errorCount = Object.values(stepResults).filter((r: any) => r.status === 'error').length;
        if (errorCount > 0) {
          toast.warning(`"${name}" completed with errors`, `${errorCount} step(s) failed`);
        } else {
          const stepCount = Object.keys(stepResults).length;
          const modeNote = preRunMode === 'sample' ? ' (sample mode)' : '';
          toast.success(`"${name}" executed successfully${modeNote}`, `${stepCount} steps completed`);
        }
      }
      fetchPipelines();
    } catch (err: any) {
      toast.error(`"${name}" execution failed`, err.message);
      setPipelineStepResults(prev => { const n = { ...prev }; delete n[id]; return n; });
    }
    setRunningPipelines(prev => { const n = { ...prev }; delete n[id]; return n; });
  };

  const fetchEstimate = async (id: string) => {
    if (estimateCache[id]) return;
    try {
      const result = await api.estimateExecution(id);
      setEstimateCache((prev) => ({ ...prev, [id]: result?.estimated_time || result?.estimate || '~unknown' }));
    } catch {
      setEstimateCache((prev) => ({ ...prev, [id]: 'N/A' }));
    }
  };

  const handleNewPipeline = () => {
    // #1 — Templates-first landing. Reset the workflow store so the
    // canvas state from any previously-loaded pipeline is gone, then
    // land on the Templates gallery instead of the blank Editor. The
    // gallery's "Blank canvas" button still routes to the Editor for
    // users who really want a blank pipeline; everyone else picks a
    // template card. The store reset has to happen here (not in the
    // Templates page) because once we're on Templates the workflow
    // store is no longer authoritative for the canvas.
    useWorkflowStore.setState({
      nodes: [],
      edges: [],
      workflowId: null,
      workflowName: 'Untitled Pipeline',
      version: 0,
      selectedNodeId: null,
      stepResults: {},
      // Reset to empty so ChatPanel's structured empty-state (3 clickable
      // prompts) renders for "New Pipeline" the same way it does on first
      // load. A seeded welcome message would override that.
      chatMessages: [],
    });
    navigateTo('templates');
  };

  // Create a folder at the project root. Pipelines inside the current
  // project can then be assigned to this folder via the Move action.
  // We deliberately keep this to "root of project" — sub-folders are
  // managed from the Projects → Tree view, where the visual hierarchy
  // makes the parent context obvious.
  const handleNewFolder = async () => {
    if (!projectId) {
      toast.warning('Open a project first', 'Folders live inside a project — pick one from the Projects page.');
      return;
    }
    const name = await uiPrompt({
      title: 'New folder',
      message: `Create a folder inside "${projectName || 'this project'}".`,
      placeholder: 'e.g. Ingestion, Reports, Archive',
      confirmLabel: 'Create folder',
    });
    if (!name || !name.trim()) return;
    try {
      await api.createFolder({
        name: name.trim(),
        project_id: projectId,
        parent_folder_id: null,
      });
      toast.success('Folder created', name.trim());
      // Refresh so the new folder shows up in the folder strip + drawers.
      // Re-uses the same effect that loads on mount via a tick of state.
      setFolderRefreshTick((t) => t + 1);
    } catch (err: any) {
      toast.error('Could not create folder', err?.message || 'Try again');
    }
  };

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (selectedIds.size === filtered.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(filtered.map((p) => p.id)));
    }
  };

  const handleBulkDelete = async () => {
    // Defence-in-depth: the bulk-action bar is one click away from a select-all
    // wipe, so we require an explicit destructive confirm BEFORE opening the
    // chooser modal. Per docs/PAGE_BY_PAGE_AUDIT.md (P0 #1, 2026-05-19).
    const count = selectedIds.size;
    if (count === 0) return;
    const ok = await uiConfirm({
      title: `Delete ${count} pipeline${count === 1 ? '' : 's'}?`,
      message: count === 1
        ? 'The selected pipeline will be moved to Bin. Auto-removed after 30 days (Free).'
        : `All ${count} selected pipelines will be moved to Bin. Auto-removed after 30 days (Free). This cannot be easily undone — restore one at a time from the Bin tab.`,
      confirmLabel: count === 1 ? 'Delete' : `Delete ${count}`,
      destructive: true,
    });
    if (!ok) return;
    setDeleteModal({ id: '__bulk__', name: `${count} pipeline${count === 1 ? '' : 's'}` });
  };

  const confirmBulkDelete = async () => {
    const newBinEntries: typeof binItems = [];
    for (const id of selectedIds) {
      const p = pipelines.find(pp => pp.id === id);
      try {
        await api.deleteWorkflow(id);
        newBinEntries.push({
          id,
          name: p?.name || id,
          deleted_at: new Date().toISOString(),
          project_id: (p as any)?.project_id || 'default',
          status: p?.status || 'draft',
        });
      } catch {}
    }
    saveBin([...newBinEntries, ...binItems]);
    setSelectedIds(new Set());
    toast.success('Moved to Bin', `${newBinEntries.length} pipeline(s) in Bin`);
    fetchPipelines();
    setDeleteModal(null);
  };

  const openHistory = async (id: string, name: string) => {
    setHistoryPipelineId(id);
    setHistoryPipelineName(name);
    setVersionsLoading(true);
    try {
      const v = await api.getWorkflowVersions(id);
      setVersions(Array.isArray(v) ? v.reverse() : []);
    } catch {
      setVersions([]);
    }
    setVersionsLoading(false);
  };

  const restoreVersion = async (version: number) => {
    if (!historyPipelineId) return;
    setRestoringVersion(version);
    try {
      const data = await api.getWorkflowVersion(historyPipelineId, version);
      if (data?.workflow) {
        await api.updateWorkflow(historyPipelineId, data.workflow, `Restored to version ${version}`);
        fetchPipelines();
        // Refresh versions
        const v = await api.getWorkflowVersions(historyPipelineId);
        setVersions(Array.isArray(v) ? v.reverse() : []);
      }
    } catch (err) {
      console.error('Restore error:', err);
    }
    setRestoringVersion(null);
  };

  const handleSort = (col: 'name' | 'updated_at' | 'status') => {
    if (sortBy === col) {
      setSortDir((d) => d === 'asc' ? 'desc' : 'asc');
    } else {
      setSortBy(col);
      setSortDir('asc');
    }
  };

  const normalizeStatus = (s?: string) => {
    if (!s || s === 'draft') return 'draft';
    if (s === 'success' || s === 'published') return 'published';
    if (s === 'error' || s === 'failed') return 'failed';
    if (s === 'running' || s === 'testing') return 'running';
    if (s === 'archived') return 'archived';
    return s;
  };

  // Resolve project_id → display name once so the search can match
  // either the id or the human name a user is more likely to type.
  const projectNameById = (() => {
    const m = new Map<string, string>();
    for (const p of projectsList) m.set(p.id, p.name);
    return m;
  })();

  // Search is null-safe (empty/missing fields no longer throw and
  // silently filter the row out — the bug that produced an empty table
  // when the user typed something other than an exact name match) and
  // matches across the most-likely fields a user types: pipeline name,
  // pipeline id, project id, project display name, and status.
  const q = searchQuery.trim().toLowerCase();
  const matchesSearch = (p: Pipeline) => {
    if (!q) return true;
    const projectId = (p as any).project_id || '';
    const haystack = [
      p.name,
      p.id,
      projectId,
      projectNameById.get(projectId) || '',
      p.status,
    ].map((v) => String(v ?? '').toLowerCase()).join(' ');
    return haystack.includes(q);
  };

  const filtered = pipelines
    .filter(matchesSearch)
    .filter((p) => statusFilter === 'all' || normalizeStatus(p.status) === statusFilter)
    .filter((p) => projectFilter === 'all' || ((p as any).project_id || 'default') === projectFilter)
    .filter((p) => !tagFilter || getTagsForPipeline(p.id).includes(tagFilter))
    .sort((a, b) => {
      let cmp = 0;
      if (sortBy === 'name') cmp = (a.name || '').localeCompare(b.name || '');
      else if (sortBy === 'updated_at') cmp = (a.updated_at || '').localeCompare(b.updated_at || '');
      else if (sortBy === 'status') cmp = (a.status || '').localeCompare(b.status || '');
      return sortDir === 'asc' ? cmp : -cmp;
    });

  const formatRelativeTime = (ts?: string) => {
    if (!ts) return '—';
    const diff = Date.now() - new Date(ts).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    if (days < 30) return `${days}d ago`;
    return new Date(ts).toLocaleDateString();
  };

  const lifecycleStyles: Record<string, { bg: string; text: string; dot: string; label: string; animate?: boolean }> = {
    draft: { bg: 'bg-slate-50', text: 'text-slate-500', dot: 'bg-slate-400', label: 'Draft' },
    running: { bg: 'bg-blue-50', text: 'text-blue-600', dot: 'bg-blue-500', label: 'Running', animate: true },
    published: { bg: 'bg-green-50', text: 'text-green-600', dot: 'bg-green-500', label: 'Published' },
    archived: { bg: 'bg-slate-100', text: 'text-slate-400', dot: 'bg-slate-300', label: 'Archived' },
    failed: { bg: 'bg-red-50', text: 'text-red-500', dot: 'bg-red-500', label: 'Failed' },
    // Legacy statuses map to lifecycle
    success: { bg: 'bg-green-50', text: 'text-green-600', dot: 'bg-green-500', label: 'Published' },
    error: { bg: 'bg-red-50', text: 'text-red-500', dot: 'bg-red-500', label: 'Failed' },
  };

  const statusBadge = (status?: string) => {
    const s = lifecycleStyles[status || ''] || lifecycleStyles.draft;
    return (
      <span className={`inline-flex items-center gap-1.5 text-xs font-medium ${s.text} ${s.bg} px-2.5 py-1 rounded-full`}>
        <span className={`w-1.5 h-1.5 rounded-full ${s.dot} ${s.animate ? 'animate-pulse' : ''}`} />
        {s.label}
      </span>
    );
  };

  const SortIcon = ({ col }: { col: string }) => (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
      className={`ml-1 inline-block transition-transform ${sortBy === col ? 'text-pipe-600' : 'text-slate-300'} ${sortBy === col && sortDir === 'desc' ? 'rotate-180' : ''}`}
    >
      <polyline points="6 15 12 9 18 15" />
    </svg>
  );

  // ── Pre-Deploy Validation Gate (shared between DEV & PROD) ──
  const openPreDeployCheck = async (pipeline: any, version?: number) => {
    const name = pipeline.name || pipeline.id;
    setPreDeployDialog({ id: pipeline.id, name, version });
    setPreDeployLoading(true);
    setPreDeployChecks(null);
    setPreDeployExtra({ schedules: [], alerts: [], credentials: [], connections: [] });
    setExpandedChecks(new Set());

    try {
      // Run all checks in parallel
      const [checks, schedules, alerts] = await Promise.all([
        api.preDeployCheck(pipeline.id).catch(() => ({
          can_deploy: false,
          checks: [{ name: 'api_error', label: 'Pre-deploy API check', status: 'fail', detail: 'Could not reach validation endpoint' }],
          passed: 0, failed: 1, warnings: 0, total_checks: 1,
        })),
        api.listSchedules({ workflow_id: pipeline.id }).catch(() => []),
        api.listAlertRules({ workflow_id: pipeline.id }).catch(() => []),
      ]);

      // Enrich checks with schedule & alert info
      const enrichedChecks = [...(checks.checks || [])];

      // 6) Schedule check — pipeline should have a schedule for PROD
      const activeSchedules = (schedules || []).filter((s: any) => s.enabled !== false);
      if (activeSchedules.length > 0) {
        enrichedChecks.push({
          name: 'schedule',
          label: 'Schedule configured',
          status: 'pass',
          detail: `${activeSchedules.length} active schedule(s): ${activeSchedules.map((s: any) => s.cron_expression).join(', ')}`,
        });
      } else {
        enrichedChecks.push({
          name: 'schedule',
          label: 'Schedule configured',
          status: 'warning',
          detail: 'No active schedule — pipeline will only run manually in PROD',
        });
      }

      // 7) Alert rules check — pipeline should have alerts for PROD
      const activeAlerts = (alerts || []).filter((a: any) => a.enabled !== false);
      if (activeAlerts.length > 0) {
        enrichedChecks.push({
          name: 'alerts',
          label: 'Alert rules configured',
          status: 'pass',
          detail: `${activeAlerts.length} alert rule(s) active`,
        });
      } else {
        enrichedChecks.push({
          name: 'alerts',
          label: 'Alert rules configured',
          status: 'warning',
          detail: 'No alert rules — failures in PROD will go unnoticed',
        });
      }

      // 8) Pipeline has steps
      const stepCount = pipeline.step_count || pipeline.steps_count || pipeline.steps?.length || 0;
      if (stepCount === 0) {
        enrichedChecks.push({
          name: 'has_steps',
          label: 'Pipeline has nodes',
          status: 'fail',
          detail: 'Pipeline has 0 steps — nothing to deploy',
        });
        checks.can_deploy = false;
      }

      // 9) Description check (good practice)
      if (!pipeline.description) {
        enrichedChecks.push({
          name: 'description',
          label: 'Description provided',
          status: 'warning',
          detail: 'No description — recommended for PROD documentation',
        });
      }

      const passed = enrichedChecks.filter((c: any) => c.status === 'pass').length;
      const failed = enrichedChecks.filter((c: any) => c.status === 'fail').length;
      const warnings = enrichedChecks.filter((c: any) => c.status === 'warning').length;
      const canDeploy = failed === 0 && (checks.can_deploy !== false || failed === 0);

      setPreDeployChecks({
        ...checks,
        checks: enrichedChecks,
        can_deploy: canDeploy,
        passed,
        failed,
        warnings,
        total_checks: enrichedChecks.length,
      });
      setPreDeployExtra({
        schedules: schedules || [],
        alerts: alerts || [],
        credentials: [],
        connections: [],
      });
    } catch (err: any) {
      setPreDeployChecks({
        can_deploy: false,
        checks: [{ name: 'error', label: 'Validation failed', status: 'fail', detail: err?.message || 'Unknown error' }],
        passed: 0, failed: 1, warnings: 0, total_checks: 1,
      });
    } finally {
      setPreDeployLoading(false);
    }
  };

  const handleDeployPipeline = async (id: string, version?: number) => {
    setDeployingIds(prev => new Set(prev).add(id));
    setPreDeployDialog(null);
    setPreDeployChecks(null);
    try {
      await api.deployWorkflow(id, version);
      toast.success('Deployed', `Pipeline deployed to production${version ? ` (v${version})` : ''}`);
      setTimeout(() => {
        fetchPipelines();
        setDeployingIds(prev => { const s = new Set(prev); s.delete(id); return s; });
      }, 1500);
    } catch (err: any) {
      toast.error('Deploy failed', err?.message || 'Check credentials and connections');
      setDeployingIds(prev => { const s = new Set(prev); s.delete(id); return s; });
    }
  };

  // ── PROD: Deployment Queue View ──
  if (isProd) {
    // Deployment statuses based on pipeline status + deployed_version
    const deploymentStatuses = {
      pending: pipelines.filter(p => !p.status || p.status === 'draft' || p.status === 'idle'),
      deployed: pipelines.filter(p => p.status === 'published' || p.status === 'success'),
      deploying: pipelines.filter(p => p.status === 'running' || p.status === 'testing'),
      failed: pipelines.filter(p => p.status === 'error' || p.status === 'failed'),
    };

    // Group by project for deployment context
    const projectGroups = pipelines.reduce((acc, p) => {
      const proj = p.project_id || 'default';
      if (!acc[proj]) acc[proj] = [];
      acc[proj].push(p);
      return acc;
    }, {} as Record<string, any[]>);

    const handleRollback = async (id: string, toVersion: number, pipelineName?: string) => {
      const name = pipelineName || id;
      // Fetch the target version's content_hash so the confirmation dialog
      // can show exactly which signed artifact is about to go live. Small
      // payload; catch silently so a missing endpoint never blocks the flow.
      let targetHash = '';
      try {
        const vs = await api.getWorkflowVersions(id);
        const target = (Array.isArray(vs) ? vs : []).find((v: any) => v.version === toVersion);
        targetHash = target?.content_hash || '';
      } catch { /* soft-fail */ }
      const hashLine = targetHash
        ? `\n• Signed hash: ${targetHash.slice(0, 16)}…`
        : '';
      if (!(await uiConfirm({
        title: `Rollback "${name}" to version ${toVersion}?`,
        message:
          'This will:\n' +
          `• Change the live PROD version immediately\n` +
          `• All scheduled runs will use v${toVersion}\n` +
          `• The current version is preserved in history${hashLine}`,
        danger: true,
        confirmLabel: `Rollback to v${toVersion}`,
      }))) return;
      try {
        const result = await api.rollbackWorkflow(id, toVersion);
        toast.success(
          'Rolled back',
          `${name}: v${result.rolled_back_from} → v${result.rolled_back_to}`,
        );
        fetchPipelines();
      } catch (err: any) {
        // Backend returns 409 when content_hash verification fails —
        // surface the raw message so the user sees "tampered/corrupted"
        // rather than a generic failure.
        toast.error('Rollback failed', err?.message || 'Could not rollback — check version history');
      }
    };

    const prodBase = prodTab === 'all' ? pipelines :
      prodTab === 'pending' ? deploymentStatuses.pending :
      prodTab === 'deployed' ? deploymentStatuses.deployed :
      deploymentStatuses.failed;
    // Search filter applied on top of tab filter (same pattern used by the
    // DEV list view) so the new toolbar's Search input actually narrows rows.
    const prodFiltered = searchQuery.trim()
      ? prodBase.filter(p => (p.name || '').toLowerCase().includes(searchQuery.toLowerCase()))
      : prodBase;

    return (
      <div className="flex-1 flex overflow-hidden bg-canvas-bg">
      {/* Z31 — same push-don't-overlay treatment as the DEV view below. */}
      <div
        className="flex-1 overflow-auto"
        style={{ paddingRight: 'var(--fp-drawer-w, 0px)', transition: 'padding-right 250ms ease-out' }}
      >
        {/* PROD Header */}
        <div className="bg-slate-900 border-b border-slate-700">
          <div className="px-8 h-[78px] flex items-center justify-between">
            <div>
              <h1 className="text-xl font-bold text-white flex items-center gap-2">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-red-400">
                  <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" />
                </svg>
                Deployment Queue
                <span className="flex items-center gap-1.5 text-xs font-bold px-2.5 py-0.5 rounded-full bg-red-500/20 text-red-300 border border-red-500/30 uppercase tracking-wider">
                  <span className="w-1.5 h-1.5 rounded-full bg-red-400 animate-pulse" />
                  PROD
                </span>
                <TierChip tier={tier} environment={environment} />
              </h1>
              <p className="text-xs text-slate-400 mt-0.5">
                {deploymentStatuses.pending.length} pending · {deploymentStatuses.deployed.length} deployed · {deploymentStatuses.failed.length} failed
                {Object.keys(projectGroups).length > 1 && ` · ${Object.keys(projectGroups).length} projects`}
              </p>
            </div>
            {/* Columns / Export moved into the unified toolbar strip below.
                Refresh stays in the page header because it's a page-level
                action (like on DashboardPage / other list pages). */}
            <button
              onClick={fetchPipelines}
              className="px-4 py-2 text-slate-200 text-sm font-semibold rounded-lg transition-all flex items-center gap-1.5 bg-slate-800 hover:bg-slate-700 border border-slate-700"
              title="Refresh"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" /></svg>
              Refresh
            </button>
          </div>
        </div>

        <div className="w-full max-w-[1500px] mx-auto px-6 py-5">
          {/* Deployment tabs */}
          <div className="flex items-center gap-1 bg-white border border-slate-200 rounded-lg p-1 mb-5">
            {([
              { key: 'pending' as const, label: 'Pending Review', count: deploymentStatuses.pending.length, dot: 'bg-amber-400' },
              { key: 'deployed' as const, label: 'Deployed', count: deploymentStatuses.deployed.length, dot: 'bg-emerald-400' },
              { key: 'failed' as const, label: 'Failed', count: deploymentStatuses.failed.length, dot: 'bg-red-400' },
              { key: 'all' as const, label: 'All', count: pipelines.length, dot: 'bg-slate-400' },
            ]).map(tab => (
              <button
                key={tab.key}
                onClick={() => setProdTab(tab.key)}
                className={`flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg text-xs font-semibold transition-all ${
                  prodTab === tab.key ? 'bg-slate-900 text-white shadow-sm' : 'text-slate-500 hover:bg-slate-50'
                }`}
              >
                <span className={`w-2 h-2 rounded-full ${prodTab === tab.key ? 'bg-white' : tab.dot}`} />
                {tab.label}
                <span className={`text-xs px-1.5 py-0.5 rounded-full ${prodTab === tab.key ? 'bg-white/20 text-white' : 'bg-slate-100 text-slate-400'}`}>
                  {tab.count}
                </span>
              </button>
            ))}
          </div>

          {/* Pre-deployment info banner */}
          {prodTab === 'pending' && deploymentStatuses.pending.length > 0 && (
            <div className="bg-blue-50 border border-blue-200 rounded-lg px-5 py-3.5 mb-5 flex items-start gap-3">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" strokeWidth="2" className="shrink-0 mt-0.5">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              </svg>
              <div>
                <p className="text-sm font-semibold text-blue-800">Validation gate enabled</p>
                <p className="text-xs text-blue-600 mt-1 leading-relaxed">
                  Clicking <strong>Deploy to PROD</strong> runs automated validation — pipeline structure, approval status,
                  test execution history, connection configuration, schedules, and alert rules are all checked before deployment is allowed.
                  {deploymentStatuses.deployed.length > 0 && ' If a deployment fails, you can rollback to a previous version.'}
                </p>
              </div>
            </div>
          )}

          {/* Pipeline deployment cards */}
          {loading ? (
            <div className="flex items-center justify-center py-20">
              <div className="w-6 h-6 border-2 border-red-300 border-t-transparent rounded-full animate-spin" />
            </div>
          ) : prodFiltered.length === 0 ? (
            <div className="text-center py-16">
              <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-slate-100 flex items-center justify-center">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="1.5">
                  <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" />
                </svg>
              </div>
              <p className="text-sm font-semibold text-slate-600">
                {prodTab === 'pending' ? 'No pending deployments' : prodTab === 'deployed' ? 'No deployed pipelines' : prodTab === 'failed' ? 'No failed deployments' : 'No pipelines'}
              </p>
              <p className="text-xs text-slate-400 mt-1">
                {prodTab === 'pending' ? 'Pipelines created in DEV will appear here for review' : 'Deploy pipelines from the Pending Review tab'}
              </p>
            </div>
          ) : (
            <div className="bg-white rounded-lg border border-slate-200 overflow-hidden overflow-x-auto">
              {/* Unified toolbar strip — same visual treatment as TableToolbar on
                  every other list page (record count on left, Search + Columns +
                  Export + Refresh on right, navy background). Theme v2 CSS override
                  forces `bg-thead-prod-bg` / `bg-thead-dev-bg` to the correct env
                  navy. Keeps the page header clean. */}
              <div className="flex items-center justify-between px-4 py-2 bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900 border-b border-amber-400/20 min-w-fit">
                <span className="text-xs text-white/85 font-medium">
                  {prodFiltered.length} deployment{prodFiltered.length !== 1 ? 's' : ''} · {pipelines.length} total · {activeColumns.length}/{DEPLOY_COLUMNS.length} columns
                </span>
                <div className="flex items-center gap-1.5">
                  {/* Search */}
                  <div className="relative">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#cbd5e1" strokeWidth="2"
                      className="absolute left-2 top-1/2 -translate-y-1/2 pointer-events-none">
                      <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
                    </svg>
                    <input
                      value={searchQuery}
                      onChange={e => setSearchQuery(e.target.value)}
                      placeholder="Search pipelines..."
                      className="pl-7 pr-2.5 py-1.5 text-xs rounded-lg outline-none w-44 bg-white/10 border border-white/20 text-white placeholder:text-white/50 focus:ring-2 focus:ring-naples/40 focus:border-naples"
                    />
                  </div>

                  {/* Column picker */}
                  <div className="relative" ref={columnPickerRef}>
                    <button
                      onClick={() => { setShowColumnPicker(!showColumnPicker); setShowExportPanel(false); }}
                      className="px-2.5 py-1.5 text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5 bg-white/10 border border-white/20 text-white hover:bg-white/20"
                      title="Configure visible columns"
                    >
                      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></svg>
                      Columns
                    </button>
                    {showColumnPicker && (
                      <div className="absolute right-0 top-full mt-1 w-72 bg-white rounded-xl shadow-xl border border-slate-200 z-50 overflow-hidden">
                        <div className="px-4 py-3 border-b border-slate-100 flex items-center justify-between">
                          <span className="text-xs font-bold text-slate-700">Table Columns</span>
                          <div className="flex items-center gap-2">
                            <button onClick={selectAllColumns} className="text-xs text-blue-500 hover:text-blue-600 font-medium">All</button>
                            <span className="text-slate-300">|</span>
                            <button onClick={resetColumnsToDefault} className="text-xs text-slate-400 hover:text-slate-600 font-medium">Default</button>
                          </div>
                        </div>
                        <div className="max-h-[360px] overflow-y-auto py-1">
                          {COLUMN_GROUPS.map(group => {
                            const cols = DEPLOY_COLUMNS.filter(c => c.group === group.key);
                            return (
                              <div key={group.key}>
                                <div className="px-4 py-1.5 text-[9px] font-bold text-slate-400 uppercase tracking-wider bg-slate-50 flex items-center gap-1.5">
                                  <span>{group.icon}</span> {group.label}
                                </div>
                                {cols.map(col => (
                                  <label key={col.key} className="flex items-center gap-3 px-4 py-2 hover:bg-slate-50 cursor-pointer transition-colors">
                                    <input
                                      type="checkbox"
                                      checked={visibleColumns.has(col.key)}
                                      onChange={() => toggleColumn(col.key)}
                                      className="w-3.5 h-3.5 rounded border-slate-300 text-blue-500 focus:ring-blue-500/20"
                                    />
                                    <span className="text-xs text-slate-700 flex-1">{col.label}</span>
                                    {col.default && <span className="text-xs text-slate-300 font-medium">DEFAULT</span>}
                                  </label>
                                ))}
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    )}
                  </div>

                  {/* Multi-level Export */}
                  <div className="relative" ref={exportPanelRef}>
                    <button
                      onClick={() => { setShowExportPanel(!showExportPanel); setShowColumnPicker(false); }}
                      className="px-2.5 py-1.5 text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5 bg-white/10 border border-white/20 text-white hover:bg-white/20"
                    >
                      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
                      Export
                    </button>
                    {showExportPanel && (
                      <div className="absolute right-0 top-full mt-1 w-64 bg-white rounded-xl shadow-xl border border-slate-200 z-50 overflow-hidden">
                        <div className="px-4 py-3 border-b border-slate-100">
                          <span className="text-xs font-bold text-slate-700">Export Data</span>
                        </div>
                        <div className="py-1">
                          {([
                            { level: 'metadata' as const, label: 'Full Metadata', desc: 'All fields for all pipelines', icon: 'list' as const },
                            { level: 'page' as const, label: 'Page Export', desc: `Current ${isProd ? 'PROD' : 'DEV'} view`, icon: 'file-text' as const },
                            { level: 'project' as const, label: 'By Project', desc: 'Grouped by project with summary', icon: 'folder' as const },
                          ]).map(item => (
                            <div key={item.level} className="px-4 py-2.5 hover:bg-slate-50 transition-colors">
                              <div className="flex items-center gap-2 mb-1.5 text-slate-500">
                                <Icon name={item.icon} size={14} />
                                <span className="text-xs font-semibold text-slate-700">{item.label}</span>
                              </div>
                              <p className="text-xs text-slate-400 mb-2 ml-6">{item.desc}</p>
                              <div className="flex items-center gap-2 ml-6">
                                <button
                                  onClick={() => { exportMultiLevel(item.level, 'csv'); setShowExportPanel(false); }}
                                  className="px-2.5 py-1 text-xs font-semibold text-emerald-600 bg-emerald-50 border border-emerald-200 rounded hover:bg-emerald-100 transition-colors"
                                >
                                  CSV
                                </button>
                                <button
                                  onClick={() => { exportMultiLevel(item.level, 'json'); setShowExportPanel(false); }}
                                  className="px-2.5 py-1 text-xs font-semibold text-blue-600 bg-blue-50 border border-blue-200 rounded hover:bg-blue-100 transition-colors"
                                >
                                  JSON
                                </button>
                              </div>
                            </div>
                          ))}
                          <div className="border-t border-slate-100 px-4 py-2.5">
                            <div className="flex items-center gap-2 mb-1 text-slate-500">
                              <Icon name="link" size={14} />
                              <span className="text-xs font-semibold text-slate-700">Per Pipeline</span>
                            </div>
                            <p className="text-xs text-slate-400 ml-6">Right-click any row → Export pipeline</p>
                          </div>
                        </div>
                      </div>
                    )}
                  </div>

                </div>
              </div>
              {/* Canonical navy-blue header with amber text (Apr 18) */}
              <div className="flex gap-0 px-4 py-2.5 bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900 border-b-2 border-amber-400/40 text-xs font-bold text-amber-300 uppercase tracking-wider min-w-fit">
                {activeColumns.map(col => (
                  <div key={col.key} style={{ width: col.width === '1fr' ? undefined : col.width, flex: col.width === '1fr' ? '1 1 0%' : `0 0 ${col.width}` }}
                    className={col.align === 'right' ? 'text-right' : col.align === 'center' ? 'text-center' : 'text-left'}>
                    {col.label}
                  </div>
                ))}
              </div>
              {/* Dynamic table rows */}
              {prodFiltered.map((pipeline, ri) => {
                const isDeploying = deployingIds.has(pipeline.id);
                const isDeployed = pipeline.status === 'published' || pipeline.status === 'success';
                const isFailed = pipeline.status === 'error' || pipeline.status === 'failed';
                const isPending = !pipeline.status || pipeline.status === 'draft' || pipeline.status === 'idle';
                const deployedVer = pipeline.deployed_version;
                const latestVer = pipeline.version || 1;
                const hasNewerVersion = deployedVer && deployedVer < latestVer;
                const ownerName = pipeline.created_by || pipeline.owner_name || pipeline.owner_id || 'admin';
                const pTags = pipelineTags[pipeline.id] || pipeline.tags || [];

                /* Cell renderer by column key */
                const renderCell = (col: DeployColumn) => {
                  switch (col.key) {
                    case 'pipeline':
                      return (
                        <div className="flex items-center gap-2.5 min-w-0">
                          <div className={`w-2 h-2 rounded-full shrink-0 ${
                            isDeploying ? 'bg-blue-500 animate-pulse' : isDeployed ? 'bg-emerald-500' : isFailed ? 'bg-red-500' : 'bg-amber-400'
                          }`} />
                          <div className="min-w-0">
                            <span className="text-xs font-semibold text-slate-800 truncate block">{pipeline.name}</span>
                            {!visibleColumns.has('project') && <span className="text-xs text-slate-400 truncate block">{pipeline.project_id || 'default'}</span>}
                            {/* N4 — recent-statuses sparkline. Hidden
                                when we haven't fetched yet, OR when the
                                pipeline has fewer than 2 runs (a single
                                dot would just duplicate the status pill
                                rendered elsewhere). Newest on the right. */}
                            {recentStatuses[pipeline.id] && recentStatuses[pipeline.id].length >= 2 && (
                              <div className="mt-1">
                                <RunStatusSparkline
                                  statuses={recentStatuses[pipeline.id].slice().reverse().map(_toSparklineStatus)}
                                  compact
                                  pad={14}
                                />
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    case 'owner':
                      return (
                        <div className="flex items-center gap-1.5 min-w-0">
                          <div className="w-5 h-5 rounded-full bg-gradient-to-br from-violet-400 to-indigo-500 flex items-center justify-center shrink-0">
                            <span className="text-xs font-bold text-white">{ownerName.charAt(0).toUpperCase()}</span>
                          </div>
                          <span className="text-xs text-slate-600 truncate">{ownerName}</span>
                        </div>
                      );
                    case 'status':
                      return (
                        <span className={`text-xs font-bold px-1.5 py-0.5 rounded uppercase inline-flex items-center gap-1 ${
                          isDeploying ? 'bg-blue-100 text-blue-600' :
                          isDeployed ? 'bg-emerald-100 text-emerald-600' :
                          isFailed ? 'bg-red-100 text-red-600' :
                          'bg-amber-100 text-amber-600'
                        }`}>
                          {isDeploying ? 'Deploying' : isDeployed ? 'Deployed' : isFailed ? 'Failed' : 'Pending'}
                        </span>
                      );
                    case 'version':
                      return (
                        <div>
                          <span className="text-xs text-slate-600 font-mono tabular-nums">v{latestVer}</span>
                          {deployedVer && hasNewerVersion && <div className="text-xs text-amber-500 font-bold">v{deployedVer} live</div>}
                        </div>
                      );
                    case 'env':
                      return isDeployed ? (
                        <span className="inline-flex items-center gap-1 text-xs font-bold px-1.5 py-0.5 rounded bg-red-100 text-red-600 uppercase">
                          <span className="w-1 h-1 rounded-full bg-red-500" />PROD
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-xs font-bold px-1.5 py-0.5 rounded bg-blue-100 text-blue-600 uppercase">
                          <span className="w-1 h-1 rounded-full bg-blue-500" />DEV
                        </span>
                      );
                    case 'steps':
                      return <span className="text-xs text-slate-600 font-medium tabular-nums">{pipeline.step_count || 0}</span>;
                    case 'schedule':
                      return scheduleMap[pipeline.id]
                        ? <span className="text-xs text-emerald-600 font-mono">{scheduleMap[pipeline.id].cron_expression}</span>
                        : <span className="text-xs text-slate-300">—</span>;
                    case 'created':
                      return <span className="text-xs text-slate-400">{pipeline.created_at ? new Date(pipeline.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '—'}</span>;
                    case 'modified':
                      return <span className="text-xs text-slate-400">{pipeline.updated_at ? formatRelativeTime(pipeline.updated_at) : '—'}</span>;
                    case 'submitted':
                      return (
                        <div>
                          <span className="text-xs text-slate-500">{pipeline.submitted_at ? new Date(pipeline.submitted_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'}</span>
                          {pipeline.submitted_by && <div className="text-[9px] text-slate-400">{pipeline.submitted_by}</div>}
                        </div>
                      );
                    case 'submittedBy':
                      return <span className="text-xs text-slate-500 truncate">{pipeline.submitted_by || '—'}</span>;
                    case 'approved':
                      return <span className="text-xs text-slate-400">{pipeline.approved_at ? new Date(pipeline.approved_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '—'}</span>;
                    case 'approvedBy':
                      return pipeline.approved_by ? (
                        <div className="flex items-center gap-1 min-w-0">
                          <div className="w-4 h-4 rounded-full bg-emerald-100 flex items-center justify-center shrink-0">
                            <span className="text-[7px] font-bold text-emerald-600">{pipeline.approved_by.charAt(0).toUpperCase()}</span>
                          </div>
                          <span className="text-xs text-slate-500 truncate">{pipeline.approved_by}</span>
                        </div>
                      ) : <span className="text-xs text-slate-300">—</span>;
                    case 'deployedAt':
                      return <span className="text-xs text-slate-400">{pipeline.deployed_at ? new Date(pipeline.deployed_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '—'}</span>;
                    case 'deployedBy':
                      return <span className="text-xs text-slate-500 truncate">{pipeline.deployed_by || '—'}</span>;
                    case 'department':
                      return pipeline.department
                        ? <span className="text-[9px] font-semibold text-violet-600 bg-violet-50 px-1.5 py-0.5 rounded">{pipeline.department}</span>
                        : <span className="text-xs text-slate-300">—</span>;
                    case 'project':
                      return <span className="text-xs text-slate-500 truncate">{pipeline.project_id || 'default'}</span>;
                    case 'description':
                      return <span className="text-xs text-slate-400 truncate block" title={pipeline.description}>{pipeline.description || '—'}</span>;
                    case 'tags':
                      return pTags.length > 0 ? (
                        <div className="flex flex-wrap gap-0.5">
                          {pTags.slice(0, 2).map(t => (
                            <span key={t} className="text-xs font-medium px-1 py-0.5 rounded bg-slate-100 text-slate-500">{t}</span>
                          ))}
                          {pTags.length > 2 && <span className="text-xs text-slate-400">+{pTags.length - 2}</span>}
                        </div>
                      ) : <span className="text-xs text-slate-300">—</span>;
                    case 'priority':
                      return pipeline.priority ? (
                        <span className={`text-xs font-bold px-1.5 py-0.5 rounded uppercase ${
                          pipeline.priority === 'critical' ? 'bg-red-100 text-red-600' :
                          pipeline.priority === 'high' ? 'bg-orange-100 text-orange-600' :
                          pipeline.priority === 'medium' ? 'bg-yellow-100 text-yellow-600' :
                          'bg-slate-100 text-slate-500'
                        }`}>{pipeline.priority}</span>
                      ) : <span className="text-xs text-slate-300">—</span>;
                    case 'lastRun':
                      return <span className="text-xs text-slate-400">{pipeline.last_run ? formatRelativeTime(pipeline.last_run) : '—'}</span>;
                    case 'connections':
                      return <span className="text-xs text-slate-500 tabular-nums">{pipeline.connections?.length || 0}</span>;
                    case 'storage': {
                      const n = storageUsageMap[pipeline.id] || 0;
                      if (n === 0) return <span className="text-xs text-slate-300">—</span>;
                      return (
                        <span
                          className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-semibold rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 cursor-pointer hover:bg-emerald-100"
                          title={`Pipeline references ${n} workspace storage item${n === 1 ? '' : 's'} (files + managed tables). Click to open Storage.`}
                          onClick={(e) => { e.stopPropagation(); window.location.hash = 'storage'; }}
                        >
                          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v6c0 1.66 4.03 3 9 3s9-1.34 9-3V5" /><path d="M3 11v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6" /></svg>
                          {n}
                        </span>
                      );
                    }
                    case 'action':
                      return (
                        <div className="flex items-center justify-end gap-1.5">
                          {canDeploy && isDeployed && latestVer > 1 && deployedVer && deployedVer > 1 && (
                            <button onClick={() => handleRollback(pipeline.id, deployedVer - 1, pipeline.name)}
                              className="px-2 py-1 text-xs font-semibold text-orange-600 bg-orange-50 border border-orange-200 rounded hover:bg-orange-100 transition-colors"
                              title={`Rollback to v${deployedVer - 1}`}>Rollback</button>
                          )}
                          {canDeploy && isDeployed && hasNewerVersion && (
                            <button onClick={() => openPreDeployCheck(pipeline, latestVer)} disabled={isDeploying}
                              className="px-2 py-1 text-xs font-semibold text-blue-600 bg-blue-50 border border-blue-200 rounded hover:bg-blue-100 transition-colors disabled:opacity-50">
                              Update v{latestVer}</button>
                          )}
                          {canDeploy && isFailed && (
                            <button onClick={() => openPreDeployCheck(pipeline)}
                              className="px-2 py-1 text-xs font-semibold text-red-600 bg-red-50 border border-red-200 rounded hover:bg-red-100 transition-colors">Retry</button>
                          )}
                          {canDeploy && isPending && (
                            <button onClick={() => openPreDeployCheck(pipeline)} disabled={isDeploying}
                              className="px-3 py-1.5 text-xs font-bold text-white bg-emerald-500 hover:bg-emerald-600 rounded shadow-sm transition-all flex items-center gap-1 disabled:opacity-50">
                              {isDeploying ? <div className="w-2.5 h-2.5 border-2 border-white/40 border-t-white rounded-full animate-spin" />
                                : <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="20 6 9 17 4 12" /></svg>}
                              Deploy to PROD
                            </button>
                          )}
                          {isDeployed && !hasNewerVersion && (
                            <span className="text-[9px] text-emerald-500 font-semibold flex items-center gap-1">
                              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="20 6 9 17 4 12" /></svg>
                              Live</span>
                          )}
                          {/* Per-pipeline export */}
                          <button onClick={() => exportMultiLevel('pipeline', 'csv', pipeline.id)}
                            className="p-1 text-slate-300 hover:text-slate-500 transition-colors" title="Export pipeline metadata">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
                          </button>
                        </div>
                      );
                    default:
                      return <span className="text-xs text-slate-300">—</span>;
                  }
                };

                return (
                  <div key={pipeline.id} className={`flex gap-0 px-4 py-3 items-center transition-colors hover:bg-slate-50/80 min-w-fit ${
                    ri < prodFiltered.length - 1 ? 'border-b border-slate-100' : ''
                  }`}>
                    {activeColumns.map(col => (
                      <div key={col.key} style={{ width: col.width === '1fr' ? undefined : col.width, flex: col.width === '1fr' ? '1 1 0%' : `0 0 ${col.width}` }}
                        className={`${col.align === 'right' ? 'text-right flex justify-end' : col.align === 'center' ? 'text-center flex justify-center' : 'text-left'} min-w-0`}>
                        {renderCell(col)}
                      </div>
                    ))}
                  </div>
                );
              })}
              {/* Column count indicator */}
              <div className="px-4 py-2 bg-slate-50 border-t border-slate-100 flex items-center justify-between">
                <span className="text-[9px] text-slate-400">
                  {prodFiltered.length} pipeline{prodFiltered.length !== 1 ? 's' : ''} · {activeColumns.length} of {DEPLOY_COLUMNS.length} columns
                </span>
                <button onClick={() => setShowColumnPicker(true)} className="text-[9px] text-blue-500 hover:text-blue-600 font-medium">
                  Customize columns
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* ── Pre-Deploy Validation Dialog ── */}
      {preDeployDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={() => { setPreDeployDialog(null); setPreDeployChecks(null); }}>
          <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" />
          <div className="relative bg-white rounded-2xl shadow-2xl border border-slate-200 w-full max-w-2xl mx-4 overflow-hidden" onClick={e => e.stopPropagation()}>
            {/* Header */}
            <div className="bg-slate-900 px-6 py-4 flex items-center justify-between">
              <div>
                <h2 className="text-sm font-bold text-white flex items-center gap-2">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-amber-400">
                    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
                  </svg>
                  Pre-Deploy Validation
                </h2>
                <p className="text-xs text-slate-400 mt-0.5">{preDeployDialog.name}{preDeployDialog.version ? ` · v${preDeployDialog.version}` : ''}</p>
              </div>
              <button onClick={() => { setPreDeployDialog(null); setPreDeployChecks(null); }} className="text-slate-400 hover:text-white">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
              </button>
            </div>

            {/* Body */}
            <div className="px-6 py-4 max-h-[60vh] overflow-auto">
              {preDeployLoading ? (
                <div className="flex flex-col items-center justify-center py-12 gap-3">
                  <div className="w-8 h-8 border-3 border-slate-200 border-t-blue-500 rounded-full animate-spin" />
                  <p className="text-sm text-slate-500 font-medium">Running validation checks...</p>
                  <p className="text-xs text-slate-400">Checking structure, approval, connections, schedules, alerts</p>
                </div>
              ) : preDeployChecks ? (
                <div className="space-y-2">
                  {/* Summary bar */}
                  <div className={`flex items-center gap-3 p-3 rounded-lg border ${
                    preDeployChecks.can_deploy
                      ? 'bg-emerald-50 border-emerald-200'
                      : 'bg-red-50 border-red-200'
                  }`}>
                    <div className={`w-10 h-10 rounded-lg flex items-center justify-center shrink-0 ${
                      preDeployChecks.can_deploy ? 'bg-emerald-100' : 'bg-red-100'
                    }`}>
                      {preDeployChecks.can_deploy ? (
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#22c55e" strokeWidth="2.5"><polyline points="20 6 9 17 4 12" /></svg>
                      ) : (
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#ef4444" strokeWidth="2.5"><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" /></svg>
                      )}
                    </div>
                    <div>
                      <p className={`text-sm font-bold ${preDeployChecks.can_deploy ? 'text-emerald-700' : 'text-red-700'}`}>
                        {preDeployChecks.can_deploy ? 'Ready to Deploy' : 'Cannot Deploy'}
                      </p>
                      <p className={`text-xs ${preDeployChecks.can_deploy ? 'text-emerald-600' : 'text-red-600'}`}>
                        {preDeployChecks.passed} passed · {preDeployChecks.failed} failed · {preDeployChecks.warnings} warning{preDeployChecks.warnings !== 1 ? 's' : ''}
                      </p>
                    </div>
                  </div>

                  {/* Individual checks */}
                  <div className="mt-3 space-y-1.5">
                    {(preDeployChecks.checks || []).map((check: any, idx: number) => {
                      const isExpanded = expandedChecks.has(idx);
                      const toggleExpand = () => setExpandedChecks(prev => {
                        const next = new Set(prev);
                        next.has(idx) ? next.delete(idx) : next.add(idx);
                        return next;
                      });

                      // Fix action — button for FAIL/WARNING that fixes the issue
                      const getFixAction = () => {
                        if (check.status === 'pass') return null;
                        switch (check.name) {
                          case 'approval':
                            return {
                              label: check.detail?.includes('rejected') ? 'Re-submit' : 'Submit for Review',
                              icon: <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M22 2L11 13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>,
                              onClick: async () => {
                                await handleSubmitForReview(preDeployDialog!.id, preDeployDialog!.name);
                                openPreDeployCheck(preDeployDialog!, preDeployDialog!.version);
                              },
                            };
                          case 'tested':
                            return {
                              label: 'Run Pipeline',
                              icon: <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>,
                              onClick: () => { setPreDeployDialog(null); setPreDeployChecks(null); onOpenEditor?.(); },
                            };
                          case 'schedule':
                            return {
                              label: 'Add Schedule',
                              icon: <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>,
                              onClick: () => setQuickSchedule({ id: preDeployDialog!.id, name: preDeployDialog!.name }),
                            };
                          case 'alerts':
                            return {
                              label: 'Add Alert',
                              icon: <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>,
                              onClick: () => setQuickAlert({ id: preDeployDialog!.id, name: preDeployDialog!.name }),
                            };
                          case 'connections':
                            return {
                              label: check.status === 'fail' ? 'Open Editor' : 'Map Connections',
                              icon: <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>,
                              onClick: () => {
                                if (check.status === 'fail') { setPreDeployDialog(null); setPreDeployChecks(null); onOpenEditor?.(); }
                                else { navigateTo('connections'); }
                              },
                            };
                          default: return null;
                        }
                      };

                      const fixAction = getFixAction();

                      return (
                      <div key={idx} className={`rounded-lg border transition-colors ${
                        check.status === 'pass' ? 'bg-white border-emerald-100' :
                        check.status === 'fail' ? 'bg-red-50/50 border-red-200' :
                        'bg-amber-50/50 border-amber-200'
                      }`}>
                        {/* Main check row — always visible */}
                        <div className="flex items-start gap-3 px-3 py-2.5 cursor-pointer" onClick={toggleExpand}>
                          <div className="mt-0.5 shrink-0">
                            {check.status === 'pass' ? (
                              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#22c55e" strokeWidth="2.5"><circle cx="12" cy="12" r="10" /><polyline points="16 9 10.5 14.5 8 12" /></svg>
                            ) : check.status === 'fail' ? (
                              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#ef4444" strokeWidth="2.5"><circle cx="12" cy="12" r="10" /><line x1="15" y1="9" x2="9" y2="15" /><line x1="9" y1="9" x2="15" y2="15" /></svg>
                            ) : (
                              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" strokeWidth="2.5"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
                            )}
                          </div>
                          <div className="flex-1 min-w-0">
                            <p className={`text-xs font-semibold ${
                              check.status === 'pass' ? 'text-slate-700' :
                              check.status === 'fail' ? 'text-red-700' :
                              'text-amber-700'
                            }`}>{check.label}</p>
                            <p className="text-xs text-slate-500 mt-0.5 leading-relaxed">{check.detail}</p>
                          </div>
                          <div className="flex items-center gap-1.5 shrink-0">
                            {fixAction && (
                              <button
                                onClick={(e) => { e.stopPropagation(); fixAction.onClick(); }}
                                className={`flex items-center gap-1 text-[9px] font-bold px-2 py-1 rounded-md transition-colors ${
                                  check.status === 'fail'
                                    ? 'bg-red-100 text-red-700 hover:bg-red-200'
                                    : 'bg-amber-100 text-amber-700 hover:bg-amber-200'
                                }`}
                              >
                                {fixAction.icon}
                                {fixAction.label}
                              </button>
                            )}
                            <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded-full uppercase ${
                              check.status === 'pass' ? 'bg-emerald-100 text-emerald-600' :
                              check.status === 'fail' ? 'bg-red-100 text-red-600' :
                              'bg-amber-100 text-amber-600'
                            }`}>{check.status}</span>
                            {/* Expand chevron */}
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2" className={`transition-transform duration-200 ${isExpanded ? 'rotate-180' : ''}`}>
                              <polyline points="6 9 12 15 18 9" />
                            </svg>
                          </div>
                        </div>

                        {/* Expanded detail panel */}
                        {isExpanded && (
                          <div className={`px-3 pb-3 pt-0 ml-[28px] mr-3 border-t ${
                            check.status === 'pass' ? 'border-emerald-100' :
                            check.status === 'fail' ? 'border-red-100' :
                            'border-amber-100'
                          }`}>
                            <div className="mt-2 space-y-1.5">
                              {/* Structural validation — show steps & connections */}
                              {check.name === 'structural_validation' && (
                                <div className="space-y-1">
                                  <p className="text-[9px] font-bold text-slate-500 uppercase tracking-wider">Validated</p>
                                  <div className="grid grid-cols-2 gap-2 text-xs">
                                    <div className="flex items-center gap-2 px-2 py-1.5 bg-slate-50 rounded border border-slate-100">
                                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
                                      <span className="text-slate-600 font-medium">{check.detail?.match(/(\d+) steps/)?.[0] || 'Steps verified'}</span>
                                    </div>
                                    <div className="flex items-center gap-2 px-2 py-1.5 bg-slate-50 rounded border border-slate-100">
                                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                                      <span className="text-slate-600 font-medium">{check.detail?.match(/(\d+) connections/)?.[0] || 'Edges verified'}</span>
                                    </div>
                                  </div>
                                  {/* View-DAG button — opens a read-only mini-canvas so admin can
                                      visually confirm the pipeline's node-and-edge layout before
                                      approving the deploy (Apr 18 request). */}
                                  <button
                                    onClick={async () => {
                                      setDagPreviewLoading(true);
                                      try {
                                        const wf = await api.getWorkflow(preDeployDialog!.id);
                                        setDagPreview({ id: preDeployDialog!.id, name: preDeployDialog!.name, workflow: wf });
                                      } catch {
                                        toast.error('Could not load pipeline', 'Retry or open the editor directly.');
                                      } finally {
                                        setDagPreviewLoading(false);
                                      }
                                    }}
                                    disabled={dagPreviewLoading}
                                    className="mt-1 w-full flex items-center justify-center gap-1.5 text-xs font-bold px-3 py-1.5 rounded-md bg-indigo-50 text-indigo-700 border border-indigo-200 hover:bg-indigo-100 disabled:opacity-50 transition-colors"
                                  >
                                    {dagPreviewLoading ? (
                                      <>
                                        <span className="w-3 h-3 border-2 border-indigo-300 border-t-indigo-600 rounded-full animate-spin" />
                                        Loading DAG…
                                      </>
                                    ) : (
                                      <>
                                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                                          <circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="6" r="2.5" /><circle cx="12" cy="18" r="2.5" />
                                          <path d="M8 7l3 8M16 7l-3 8" />
                                        </svg>
                                        View DAG Lineage
                                      </>
                                    )}
                                  </button>
                                  <p className="text-[9px] text-slate-400 mt-1">DAG validated — no cycles, no orphaned nodes, all required params present.</p>
                                </div>
                              )}

                              {/* Approval — show who approved / status */}
                              {check.name === 'approval' && (
                                <div className="space-y-1">
                                  <p className="text-[9px] font-bold text-slate-500 uppercase tracking-wider">Approval Details</p>
                                  <div className={`flex items-center gap-2 px-2 py-1.5 rounded border text-xs ${
                                    check.status === 'pass' ? 'bg-emerald-50 border-emerald-100' : 'bg-red-50 border-red-100'
                                  }`}>
                                    {check.status === 'pass' ? (
                                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
                                    ) : (
                                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#ef4444" strokeWidth="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
                                    )}
                                    <span className={check.status === 'pass' ? 'text-emerald-700' : 'text-red-700'}>{check.detail}</span>
                                  </div>
                                  <p className="text-[9px] text-slate-400 mt-1">Pipeline must be reviewed and approved before deployment to PROD.</p>
                                </div>
                              )}

                              {/* Tested — show execution history */}
                              {check.name === 'tested' && (
                                <div className="space-y-1">
                                  <p className="text-[9px] font-bold text-slate-500 uppercase tracking-wider">Execution History</p>
                                  <div className={`flex items-center gap-2 px-2 py-1.5 rounded border text-xs ${
                                    check.status === 'pass' ? 'bg-emerald-50 border-emerald-100' : 'bg-amber-50 border-amber-100'
                                  }`}>
                                    <span className={check.status === 'pass' ? 'text-emerald-700' : 'text-amber-700'}>{check.detail}</span>
                                  </div>
                                  <p className="text-[9px] text-slate-400 mt-1">At least one successful execution in DEV is required to ensure the pipeline works end-to-end.</p>
                                </div>
                              )}

                              {/* Connections — show source/sink details */}
                              {check.name === 'connections' && (
                                <div className="space-y-1">
                                  <p className="text-[9px] font-bold text-slate-500 uppercase tracking-wider">Connection Mapping</p>
                                  {check.connections && check.connections.length > 0 ? (
                                    <div className="space-y-1">
                                      {check.connections.map((conn: any, ci: number) => (
                                        <div key={ci} className={`flex items-center gap-2 text-xs px-2 py-1.5 rounded border ${
                                          check.status === 'pass' ? 'bg-emerald-50 border-emerald-100' : 'bg-amber-50 border-amber-100'
                                        }`}>
                                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke={check.status === 'pass' ? '#10b981' : '#f59e0b'} strokeWidth="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                                          <span className="font-semibold text-slate-700 truncate">{conn.node}</span>
                                          <span className="text-slate-300">→</span>
                                          <span className="font-mono text-slate-600 truncate">{conn.connection_name}</span>
                                          <span className="text-slate-400 text-xs ml-auto uppercase px-1.5 py-0.5 bg-white rounded border border-slate-100">{conn.type?.replace(/-/g, ' ')}</span>
                                        </div>
                                      ))}
                                    </div>
                                  ) : (
                                    <div className="px-2 py-1.5 bg-slate-50 rounded border border-slate-100 text-xs text-slate-600">
                                      {check.detail}
                                    </div>
                                  )}
                                  <p className="text-[9px] text-slate-400 mt-1">Every source/sink node must have a connection or file path configured. DEV connections should be remapped to PROD equivalents in the deploy wizard.</p>
                                </div>
                              )}

                              {/* Version — show deploy version info + let admin pick
                                  a specific version to deploy (rollback / redeploy flow).
                                  Apr 18 request: admin should be able to choose which
                                  version to push to PROD, not just the latest. */}
                              {check.name === 'version_change' && (
                                <VersionChangeDetail
                                  check={check}
                                  pipelineId={preDeployDialog!.id}
                                  currentVersion={preDeployDialog!.version}
                                  versions={deployVersions}
                                  loading={deployVersionsLoading}
                                  onLoad={async () => {
                                    if (deployVersions.length > 0 || deployVersionsLoading) return;
                                    setDeployVersionsLoading(true);
                                    try {
                                      const vs = await api.getWorkflowVersions(preDeployDialog!.id);
                                      setDeployVersions(Array.isArray(vs) ? vs : []);
                                    } catch {
                                      setDeployVersions([]);
                                    } finally {
                                      setDeployVersionsLoading(false);
                                    }
                                  }}
                                  onSelect={(v: number) => {
                                    // Re-run the pre-deploy check against the chosen
                                    // version so all panels refresh with that snapshot.
                                    openPreDeployCheck({ id: preDeployDialog!.id, name: preDeployDialog!.name }, v);
                                  }}
                                />
                              )}

                              {/* Schedule — show schedule details */}
                              {check.name === 'schedule' && (
                                <div className="space-y-1">
                                  <p className="text-[9px] font-bold text-slate-500 uppercase tracking-wider">Schedule Details</p>
                                  <div className={`flex items-center gap-2 px-2 py-1.5 rounded border text-xs ${
                                    check.status === 'pass' ? 'bg-emerald-50 border-emerald-100' : 'bg-amber-50 border-amber-100'
                                  }`}>
                                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke={check.status === 'pass' ? '#10b981' : '#f59e0b'} strokeWidth="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                                    <span className={check.status === 'pass' ? 'text-emerald-700' : 'text-amber-700'}>{check.detail}</span>
                                  </div>
                                  <p className="text-[9px] text-slate-400 mt-1">Pipelines without schedules will only run when triggered manually in PROD.</p>
                                </div>
                              )}

                              {/* Alerts — show alert config */}
                              {check.name === 'alerts' && (
                                <div className="space-y-1">
                                  <p className="text-[9px] font-bold text-slate-500 uppercase tracking-wider">Alert Rules</p>
                                  <div className={`flex items-center gap-2 px-2 py-1.5 rounded border text-xs ${
                                    check.status === 'pass' ? 'bg-emerald-50 border-emerald-100' : 'bg-amber-50 border-amber-100'
                                  }`}>
                                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke={check.status === 'pass' ? '#10b981' : '#f59e0b'} strokeWidth="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
                                    <span className={check.status === 'pass' ? 'text-emerald-700' : 'text-amber-700'}>{check.detail}</span>
                                  </div>
                                  <p className="text-[9px] text-slate-400 mt-1">Without alert rules, pipeline failures in PROD will go unnoticed. Configure email/Slack/webhook alerts.</p>
                                </div>
                              )}

                              {/* Generic fallback for any other check */}
                              {!['structural_validation', 'approval', 'tested', 'connections', 'version_change', 'schedule', 'alerts'].includes(check.name) && (
                                <div className="px-2 py-1.5 bg-slate-50 rounded border border-slate-100 text-xs text-slate-600">
                                  {check.detail}
                                </div>
                              )}
                            </div>
                          </div>
                        )}
                      </div>
                      );
                    })}
                  </div>
                </div>
              ) : null}
            </div>

            {/* Footer */}
            {preDeployChecks && !preDeployLoading && (
              <div className="px-6 py-3 border-t border-slate-200 flex items-center justify-between bg-slate-50/50">
                <button
                  onClick={() => { setPreDeployDialog(null); setPreDeployChecks(null); }}
                  className="px-4 py-2 text-xs font-semibold text-slate-600 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
                >
                  Cancel
                </button>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => openPreDeployCheck(preDeployDialog, preDeployDialog.version)}
                    className="px-3 py-2 text-xs font-semibold text-slate-600 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors flex items-center gap-1.5"
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" /></svg>
                    Re-check
                  </button>
                  {preDeployChecks.can_deploy ? (
                    <button
                      onClick={() => handleDeployPipeline(preDeployDialog.id, preDeployDialog.version)}
                      disabled={deployingIds.has(preDeployDialog.id)}
                      className="px-5 py-2 text-xs font-bold text-white bg-emerald-500 hover:bg-emerald-600 rounded-lg shadow-sm hover:shadow-md transition-all flex items-center gap-1.5 disabled:opacity-50"
                    >
                      {deployingIds.has(preDeployDialog.id) ? (
                        <div className="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin" />
                      ) : (
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="20 6 9 17 4 12" /></svg>
                      )}
                      Confirm Deploy
                    </button>
                  ) : (
                    <div className="px-5 py-2 text-xs font-bold text-white bg-slate-300 rounded-lg cursor-not-allowed flex items-center gap-1.5">
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><rect x="3" y="11" width="18" height="11" rx="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></svg>
                      Deploy Blocked
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── DAG Lineage Preview ── (Apr 18)
          Read-only mini-canvas showing the live structure of the selected
          pipeline. Helps PROD admins visually verify node layout + edges
          before signing off the deploy. Uses the same ReactFlow instance
          as the editor but with drag/zoom/selection enabled (read-only
          via no interactive callbacks wired). */}
      {dagPreview && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center" onClick={() => setDagPreview(null)}>
          <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
          <div className="relative bg-white rounded-2xl shadow-2xl border border-slate-200 w-[90vw] h-[85vh] max-w-[1400px] overflow-hidden flex flex-col" onClick={e => e.stopPropagation()}>
            <div className="bg-slate-900 px-6 py-3 flex items-center justify-between shrink-0">
              <div className="flex items-center gap-2.5">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-indigo-400" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="6" r="2.5" /><circle cx="12" cy="18" r="2.5" />
                  <path d="M8 7l3 8M16 7l-3 8" />
                </svg>
                <div>
                  <h2 className="text-sm font-bold text-white">DAG Lineage &mdash; {dagPreview.name}</h2>
                  <p className="text-xs text-slate-400">Read-only preview &middot; scroll to zoom, drag to pan</p>
                </div>
              </div>
              <button onClick={() => setDagPreview(null)} className="text-slate-400 hover:text-white">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
              </button>
            </div>
            <div className="flex-1 bg-slate-50 relative">
              <DagLineagePreview workflow={dagPreview.workflow} />
            </div>
          </div>
        </div>
      )}

      </div>
    );
  }

  // ── DEV: Normal Workflows View ──
  return (
    <div className="flex-1 flex flex-col overflow-hidden bg-canvas-bg">
    <ReadOnlyBanner environment={environment} />
    <div className="flex-1 flex overflow-hidden">
    {/* Z31 (2026-05-23) — push-don't-overlay for the pipeline detail
        drawer. `--fp-drawer-w` is published by the open DetailDrawer
        (only when pushContent=true). Padding-right reflows the list +
        sticky header so the table's rightmost columns aren't clipped. */}
    <div
      className="flex-1 overflow-auto"
      style={{ paddingRight: 'var(--fp-drawer-w, 0px)', transition: 'padding-right 250ms ease-out' }}
    >
      {/* Header — canonical shared PageHeader shell:
          • LEFT:   page title cluster ("Pipelines")
          • CENTER: HubTabs — sibling tabs in the Workflows family
          • RIGHT:  page-specific actions (Stats, Import, New Pipeline) */}
      <PageHeader
        environment={environment}
        icon={
          /* 2026-05-22 — icon tinted `text-blue-500` to match the rest of
             the page-header palette. Brand-orange is reserved for the
             F-Pulse logo mark; per-page header icons follow the cool-tone
             palette. */
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-blue-500">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
          </svg>
        }
        title="Pipelines"
        titleAccessory={<TierChip tier={tier} environment={environment} />}
        subtitle={`${pipelines.length} pipeline${pipelines.length !== 1 ? 's' : ''} · ${pipelines.filter(p => p.status === 'success').length} active`}
        tabs={
          <HubTabs
            tabs={WORKFLOWS_TABS}
            active="pipelines"
            onNavigate={(p) => { window.location.hash = p; }}
            environment={environment}
          />
        }
        actions={
          <div className="flex justify-end items-center gap-2">
            {/* Stats toggle — collapses/shows the KPI grid (Total /
                Active / Drafts / Failed / Running / Scheduled). Same
                affordance as the Executions page Stats button. */}
            <button
              onClick={() => setShowDashboard(s => !s)}
              title={showDashboard ? 'Hide KPI cards' : 'Show KPI cards'}
              className={`px-4 py-2 text-sm font-medium rounded-lg border transition-colors ${
                showDashboard
                  ? 'bg-blue-50 text-blue-600 border-blue-200'
                  : 'bg-white text-slate-400 border-slate-200 hover:text-slate-600'
              }`}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="inline mr-1 -mt-0.5">
                <rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18" /><path d="M9 21V9" />
              </svg>
              Stats
            </button>
            {/* Export moved to canonical navy summary strip (Apr 18).
                The rich multi-level Export dropdown lives above the table
                so users get one Export per page. */}
            <RoleGate action="create">
              <label className="px-4 py-2 text-sm font-semibold text-indigo-700 bg-indigo-50 border border-indigo-200 rounded-lg hover:bg-indigo-100 transition-colors cursor-pointer flex items-center gap-1.5">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                Import
                <input type="file" accept=".json,.fpulse" className="hidden" onChange={handleImportFileSelect} />
              </label>
              <button
                onClick={handleNewPipeline}
                className="px-4 py-2 text-white text-sm font-bold rounded-lg shadow-sm hover:shadow-md transition-all flex items-center gap-2"
                style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
                New Pipeline
              </button>
            </RoleGate>
          </div>
        }
      />

      <ProjectContextBar
        projectId={projectId}
        projectName={projectName}
        onGoToProjects={onGoToProjects || (() => {})}
        onClear={onClearProject || (() => {})}
      />

      {/* Folder strip — only renders when this page is project-scoped.
          Click a folder chip to filter the pipelines table; "All" clears
          the filter. Empty state nudges the user to create their first
          folder via the header button. */}
      {projectId && (
        <div className="w-full max-w-[1500px] mx-auto px-6 pt-4">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">Folders</span>
            <button
              onClick={() => setActiveFolderId(null)}
              className={`px-3 py-1 text-xs font-medium rounded-full border transition-colors ${
                activeFolderId === null
                  ? 'bg-indigo-600 text-white border-indigo-600'
                  : 'bg-white text-slate-600 border-slate-300 hover:bg-slate-50'
              }`}
            >
              All
            </button>
            {folders.length === 0 ? (
              <span className="text-xs text-slate-400 italic">
                No folders yet — open the Projects page → Tree view to add one.
              </span>
            ) : (
              folders.map(f => (
                <button
                  key={f.id}
                  onClick={() => setActiveFolderId(f.id)}
                  className={`px-3 py-1 text-xs font-medium rounded-full border transition-colors flex items-center gap-1 ${
                    activeFolderId === f.id
                      ? 'bg-amber-500 text-white border-amber-500'
                      : 'bg-amber-50 text-amber-700 border-amber-200 hover:bg-amber-100'
                  }`}
                >
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                    <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
                  </svg>
                  {f.name}
                </button>
              ))
            )}
          </div>
        </div>
      )}

      <div className="w-full max-w-[1500px] mx-auto px-6 py-5">
        {/* Hero KPI cards — matches Executions / Dashboard / Pool visual
            family (HeroCard with gradient backgrounds, centered icon + value).
            DEV uses lighter 400→500 gradients; PROD uses richer 500→600.
            Wrapped in showDashboard so the Stats button in the header
            collapses the entire KPI strip. */}
        {showDashboard && (() => {
          // 2026-05-22: this IIFE renders inside the DEV branch of the
          // function — the `if (isProd) { ... return ... }` early-return
          // at line ~2832 means we only reach this code path when
          // environment === 'dev'. TS narrows `environment` accordingly,
          // so the prior `environment === 'prod'` check was dead code
          // (TS flagged it as a no-overlap comparison). Constant-fold to
          // false so the KPI gradients pick the DEV variants consistently.
          const isProd = false;
          const total = pipelines.length;
          const active = pipelines.filter(p => {
            const s = normalizeStatus(p.status);
            return s === 'success' || s === 'published';
          }).length;
          const drafts = pipelines.filter(p => normalizeStatus(p.status) === 'draft').length;
          const failed = pipelines.filter(p => normalizeStatus(p.status) === 'failed').length;
          const running = pipelines.filter(p => normalizeStatus(p.status) === 'running').length;
          const scheduled = Object.keys(scheduleMap).filter(id => pipelines.some(p => p.id === id)).length;
          return (
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 mb-5">
              <HeroCard
                gradient={isProd ? 'from-indigo-500 to-indigo-600' : 'from-indigo-400 to-indigo-500'}
                icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" /></svg>}
                label="Total"
                value={String(total)}
              />
              <HeroCard
                gradient={isProd ? 'from-emerald-500 to-emerald-600' : 'from-emerald-400 to-emerald-500'}
                icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>}
                label="Active"
                value={String(active)}
              />
              <HeroCard
                gradient={isProd ? 'from-slate-400 to-slate-500' : 'from-slate-300 to-slate-400'}
                icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /></svg>}
                label="Drafts"
                value={String(drafts)}
              />
              <HeroCard
                gradient={isProd ? 'from-red-500 to-rose-600' : 'from-red-400 to-rose-500'}
                icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>}
                label="Failed"
                value={String(failed)}
              />
              <HeroCard
                gradient={isProd ? 'from-blue-500 to-sky-600' : 'from-blue-400 to-sky-500'}
                icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><circle cx="12" cy="12" r="3" fill="currentColor" /></svg>}
                label="Running"
                value={String(running)}
              />
              <HeroCard
                gradient={isProd ? 'from-violet-500 to-purple-600' : 'from-violet-400 to-purple-500'}
                icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>}
                label="Scheduled"
                value={String(scheduled)}
              />
            </div>
          );
        })()}

        {/* Bulk actions bar (shown when items selected) */}
        {selectedIds.size > 0 && (
          <div className="flex items-center gap-2 bg-red-50 px-3 py-1.5 rounded-lg border border-red-200 mb-4">
            <span className="text-xs text-red-600 font-medium">{selectedIds.size} selected</span>
            <button onClick={handleBulkDelete} className="text-xs text-red-600 font-bold hover:text-red-700 underline">
              Delete
            </button>
            <button onClick={() => setSelectedIds(new Set())} className="text-xs text-slate-400 hover:text-slate-600 ml-1">
              Cancel
            </button>
          </div>
        )}

        {/* Status filter tabs + Tag filter */}
        <div className="flex items-center gap-1.5 mb-4">
          {/* Order: Published → Draft → All → activity states.
              Lifecycle states the user actively manages (Published,
              Draft) come first; "All" sits between the lifecycle and
              the runtime/activity tabs as a neutral reset. */}
          {([
            { key: 'published', label: 'Published', count: pipelines.filter(p => normalizeStatus(p.status) === 'published').length },
            { key: 'draft', label: 'Draft', count: pipelines.filter(p => normalizeStatus(p.status) === 'draft').length },
            { key: 'all', label: 'All', count: pipelines.length },
            { key: 'running', label: 'Running', count: pipelines.filter(p => normalizeStatus(p.status) === 'running').length },
            { key: 'failed', label: 'Failed', count: pipelines.filter(p => normalizeStatus(p.status) === 'failed').length },
            { key: 'archived', label: 'Archived', count: pipelines.filter(p => normalizeStatus(p.status) === 'archived').length },
          ] as const).map(({ key, label, count }) => (
            <button
              key={key}
              onClick={() => setStatusFilter(key)}
              className={`px-3.5 py-2 text-sm font-semibold rounded-lg transition-colors ${
                statusFilter === key
                  ? 'bg-pipe-100 text-pipe-700 shadow-sm'
                  : 'text-slate-900 font-bold hover:text-black hover:bg-slate-100'
              }`}
            >
              {label}
              {count > 0 && <span className="ml-1.5 text-xs opacity-70">{count}</span>}
            </button>
          ))}

          {/* Bin tab */}
          <div className="w-px h-5 bg-slate-200 mx-1" />
          <button
            onClick={() => setStatusFilter('bin')}
            className={`px-3.5 py-2 text-sm font-semibold rounded-lg transition-colors flex items-center gap-1.5 ${
              statusFilter === 'bin'
                ? 'bg-red-50 text-red-600 shadow-sm'
                : 'text-slate-900 font-bold hover:text-red-500 hover:bg-red-50'
            }`}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6" /><path d="M19 6l-2 14H7L5 6" /><path d="M9 6V4h6v2" /></svg>
            Bin
            {binItems.length > 0 && <span className="text-xs opacity-60">{binItems.length}</span>}
          </button>

          {/* Tag filter dropdown */}
          {allTags.length > 0 && (
            <div className="relative ml-2" ref={tagFilterRef}>
              <button
                onClick={() => setShowTagFilter(!showTagFilter)}
                className={`px-3 py-1.5 text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5 ${
                  tagFilter
                    ? 'bg-pipe-100 text-pipe-700 shadow-sm'
                    : 'text-slate-900 font-bold hover:text-slate-700 hover:bg-slate-100'
                }`}
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z" /><line x1="7" y1="7" x2="7.01" y2="7" /></svg>
                {tagFilter || 'Tags'}
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="6 9 12 15 18 9" /></svg>
              </button>
              {showTagFilter && (
                <div className="absolute top-full left-0 mt-1 w-48 rounded-lg border border-slate-200 shadow-sm bg-white shadow-lg z-30 py-1">
                  <button
                    onClick={() => { setTagFilter(''); setShowTagFilter(false); }}
                    className={`w-full px-3 py-2 text-left text-xs font-medium hover:bg-slate-50 transition-colors flex items-center justify-between ${
                      !tagFilter ? 'text-pipe-700 bg-pipe-50/50' : 'text-slate-600'
                    }`}
                  >
                    All
                    <span className="text-xs text-slate-400">{pipelines.length}</span>
                  </button>
                  {allTags.map((tag) => {
                    const color = getColorForTag(tag);
                    const count = pipelines.filter((p) => getTagsForPipeline(p.id).includes(tag)).length;
                    return (
                      <button
                        key={tag}
                        onClick={() => { setTagFilter(tag); setShowTagFilter(false); }}
                        className={`w-full px-3 py-2 text-left text-xs font-medium hover:bg-slate-50 transition-colors flex items-center justify-between ${
                          tagFilter === tag ? 'text-pipe-700 bg-pipe-50/50' : 'text-slate-600'
                        }`}
                      >
                        <span className="flex items-center gap-2">
                          <span className={`w-2 h-2 rounded-full ${color.dot}`} />
                          {tag}
                        </span>
                        <span className="text-xs text-slate-400">{count}</span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          )}
          {tagFilter && (
            <button
              onClick={() => setTagFilter('')}
              className="px-2 py-1 text-xs text-slate-400 hover:text-slate-600 rounded-lg hover:bg-slate-100 flex items-center gap-1"
            >
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
              Clear tag
            </button>
          )}
        </div>

        {/* ── BIN VIEW ── */}
        {statusFilter === 'bin' ? (
          <div className="bg-white rounded-2xl border border-slate-200 overflow-x-auto shadow-sm">
            {binItems.length === 0 ? (
              <div className="text-center py-16 px-8">
                <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-slate-100 flex items-center justify-center">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="1.5"><polyline points="3 6 5 6 21 6" /><path d="M19 6l-2 14H7L5 6" /><path d="M9 6V4h6v2" /></svg>
                </div>
                <h2 className="text-lg font-bold text-slate-700 mb-2">Bin is empty</h2>
                <p className="text-sm text-slate-500">Deleted pipelines will appear here for recovery.</p>
              </div>
            ) : (
              <>
                <div className="px-5 py-3 bg-red-50/50 border-b border-red-100 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#ef4444" strokeWidth="2"><polyline points="3 6 5 6 21 6" /><path d="M19 6l-2 14H7L5 6" /><path d="M9 6V4h6v2" /></svg>
                    <span className="text-xs font-semibold text-red-700">{binItems.length} item{binItems.length !== 1 ? 's' : ''} in Bin</span>
                  </div>
                  <button
                    onClick={() => { saveBin([]); toast.success('Bin emptied'); }}
                    className="text-xs font-medium text-red-500 hover:text-red-700 transition-colors"
                  >
                    Empty Bin
                  </button>
                </div>
                <table className="w-full border-collapse">
                  <thead>
                    <tr className="bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900 border-b-2 border-amber-400/40">
                      <th className="text-left px-4 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">Name</th>
                      <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">Project</th>
                      <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">Deleted</th>
                      <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">Auto-purge</th>
                      <th className="w-40 px-3 py-3 text-right text-xs font-bold text-amber-300 uppercase tracking-wider">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {binItems.map((item) => {
                      const deletedDate = new Date(item.deleted_at);
                      const daysLeft = Math.max(0, 30 - Math.floor((Date.now() - deletedDate.getTime()) / 86400000));
                      return (
                        <tr key={item.id} className="border-b border-slate-100 hover:bg-red-50/20 transition-colors">
                          <td className="px-4 py-3 max-w-[300px]">
                            <div className="flex items-center gap-3 min-w-0">
                              <div className="w-7 h-7 rounded-lg bg-slate-100 flex items-center justify-center shrink-0">
                                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" /></svg>
                              </div>
                              <div className="min-w-0">
                                <div className="text-sm font-medium text-slate-500 line-through truncate" title={item.name}>{item.name}</div>
                                <div className="text-xs text-slate-400 font-mono">{item.id.slice(0, 8)}</div>
                              </div>
                            </div>
                          </td>
                          <td className="px-3 py-3">
                            <span className="text-xs text-slate-400">{item.project_id || 'default'}</span>
                          </td>
                          <td className="px-3 py-3">
                            <span className="text-xs text-slate-400">{formatRelativeTime(item.deleted_at)}</span>
                          </td>
                          <td className="px-3 py-3">
                            <span className={`text-xs font-medium ${daysLeft <= 7 ? 'text-red-500' : 'text-slate-400'}`}>
                              {daysLeft} day{daysLeft !== 1 ? 's' : ''}
                            </span>
                          </td>
                          <td className="px-3 py-3 text-right">
                            <div className="flex items-center justify-end gap-1.5">
                              <button
                                onClick={() => {
                                  saveBin(binItems.filter(b => b.id !== item.id));
                                  toast.success('Removed from Bin', `"${item.name}" permanently deleted`);
                                }}
                                className="px-2 py-1 text-xs font-medium text-red-500 hover:text-red-700 hover:bg-red-50 rounded-lg transition-colors"
                              >
                                Delete Forever
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </>
            )}
          </div>
        ) : loading ? (
          // Skeleton loader (per design decision D-004 — wrapped in
          // DelayedSkeleton so we don't flicker for sub-200ms responses).
          <DelayedSkeleton>
            <div className="bg-white rounded-2xl border border-slate-200 overflow-hidden">
              <table className="w-full">
                <tbody className="divide-y divide-slate-100">
                  {Array.from({ length: 6 }).map((_, i) => (
                    <SkeletonTableRow key={i} columns={8} />
                  ))}
                </tbody>
              </table>
            </div>
          </DelayedSkeleton>
        ) : filtered.length === 0 ? (
          <div className="bg-white rounded-2xl border border-slate-200 text-center py-16 px-8">
            <div className="text-5xl mb-4">⚡</div>
            <h2 className="text-lg font-bold text-slate-700 mb-2">
              {searchQuery ? 'No matching workflows' : 'No workflows yet'}
            </h2>
            <p className="text-sm text-slate-500 mb-6 max-w-md mx-auto">
              {searchQuery ? 'Try a different search term.' : 'Create your first data pipeline. Use a template or start from scratch.'}
            </p>
            {!searchQuery && (
              <button
                onClick={handleNewPipeline}
                className="px-5 py-2.5 text-white text-sm font-semibold rounded-lg shadow-sm hover:shadow-md transition-all"
                style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
              >
                Create Pipeline
              </button>
            )}
          </div>
        ) : viewMode === 'list' ? (
          /* ── LIST VIEW ── */
          /* 2026-05-28 — switched outer overflow from `overflow-hidden`
              (which CLIPPED any column that pushed past the right edge
              — long names made the Action column invisible) to
              `overflow-x-auto` so the table gracefully scrolls
              horizontally on narrow viewports instead. Combined with
              the per-cell max-width on the Name column below, this
              should keep the Actions visible in 99% of cases and let
              users scroll to see them in the remaining 1%. */
          <div className="bg-white rounded-2xl border border-slate-200 overflow-x-auto shadow-sm">
            {/* Canonical navy summary strip (Apr 18) — matches every other data
                page. Holds record count, search, and the Export dropdown so the
                page header stays clean. */}
            <div className="flex items-center justify-between px-4 py-2 bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900 rounded-t-2xl">
              <span className="text-xs text-white/90 font-semibold">
                {filtered.length} workflow{filtered.length !== 1 ? 's' : ''} · {pipelines.length} total
              </span>
              <div className="flex items-center gap-1.5">
                <div className="relative">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#cbd5e1" strokeWidth="2"
                    className="absolute left-2 top-1/2 -translate-y-1/2 pointer-events-none">
                    <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
                  </svg>
                  <input value={searchQuery} onChange={e => setSearchQuery(e.target.value)}
                    placeholder="Search name, id, project, status..."
                    className="pl-7 pr-2.5 py-1.5 text-xs border border-white/20 rounded-lg bg-white/10 text-white placeholder:text-white/50 focus:ring-2 focus:ring-amber-300/40 focus:border-amber-300 outline-none w-72" />
                </div>
                {/* Project filter — narrows the table to a single project.
                    Built from the union of /api/projects and project_ids
                    actually present on pipelines, so a project that has
                    no pipelines yet still appears, AND a pipeline whose
                    project was deleted still has a way to be filtered. */}
                <select
                  value={projectFilter}
                  onChange={(e) => setProjectFilter(e.target.value)}
                  className="px-2 py-1.5 text-xs border border-white/20 rounded-lg bg-white/10 text-white focus:ring-2 focus:ring-amber-300/40 focus:border-amber-300 outline-none cursor-pointer"
                  title="Filter by project"
                >
                  <option value="all" className="text-slate-800">All projects</option>
                  {(() => {
                    const known = new Set(projectsList.map(p => p.id));
                    const fromPipelines = Array.from(new Set(pipelines.map(p => (p as any).project_id || 'default').filter(Boolean)));
                    const extras = fromPipelines.filter(id => !known.has(id));
                    return (
                      <>
                        {projectsList.map(p => (
                          <option key={p.id} value={p.id} className="text-slate-800">{p.name}</option>
                        ))}
                        {extras.map(id => (
                          <option key={id} value={id} className="text-slate-800">{id}</option>
                        ))}
                      </>
                    );
                  })()}
                </select>
                {/* Columns — opens a grouped checkbox popover that toggles
                    which metadata columns the user sees in the table.
                    Visibility persists to localStorage (key:
                    fpulse_pipelines_cols). Mirrors the Connections page
                    column picker. The Name + Select + Actions columns
                    are fixed and never listed here. */}
                <div className="relative" ref={pipeColumnPickerRef}>
                  <button
                    onClick={() => setShowPipeColumnPicker(v => !v)}
                    className="px-2.5 py-1.5 text-xs font-semibold text-white bg-white/10 border border-white/20 rounded-lg hover:bg-white/20 transition-colors flex items-center gap-1.5"
                    title="Configure visible columns"
                  >
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></svg>
                    Columns
                    <span className="text-[10px] text-white/70 font-normal">
                      {pipeColState.activeColumns.length}/{PIPELINE_COLUMNS.length}
                    </span>
                  </button>
                  {showPipeColumnPicker && (
                    <div className="absolute right-0 top-full mt-1 w-60 bg-white rounded-xl shadow-xl border border-slate-200 z-50 overflow-hidden">
                      <div className="px-3 py-2 border-b border-slate-100 flex items-center justify-between">
                        <span className="text-xs font-bold text-slate-700">Columns</span>
                        <div className="flex items-center gap-2">
                          <button onClick={pipeColState.selectAll} className="text-xs text-blue-500 hover:text-blue-600 font-medium">All</button>
                          <span className="text-slate-300">|</span>
                          <button onClick={pipeColState.resetToDefaults} className="text-xs text-slate-400 hover:text-slate-600 font-medium">Default</button>
                        </div>
                      </div>
                      <div className="max-h-[320px] overflow-y-auto py-0.5">
                        {PIPELINE_COLUMN_GROUPS.map(group => {
                          const cols = PIPELINE_COLUMNS.filter(c => c.group === group.key);
                          if (cols.length === 0) return null;
                          return (
                            <div key={group.key}>
                              <div className="px-3 py-1 text-xs font-bold text-slate-400 uppercase tracking-wider bg-slate-50 flex items-center gap-1.5">
                                <span>{group.icon}</span>
                                <span>{group.label}</span>
                              </div>
                              {cols.map(col => (
                                <label key={col.key} className="flex items-center gap-2.5 px-3 py-1.5 hover:bg-slate-50 cursor-pointer">
                                  <input
                                    type="checkbox"
                                    checked={pipeColState.visibleColumns.has(col.key)}
                                    onChange={() => pipeColState.toggleColumn(col.key)}
                                    className="w-3 h-3 rounded border-slate-300 text-blue-500 focus:ring-blue-500/20"
                                  />
                                  <span className="text-xs text-slate-700 flex-1">{col.label}</span>
                                  {col.default && <span className="text-[10px] text-slate-300 font-medium">DEFAULT</span>}
                                </label>
                              ))}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>
                {/* Multi-level Export (moved from page header Apr 18) */}
                <div className="relative" ref={exportMenuRef}>
                  <button
                    onClick={() => setShowExportMenu(!showExportMenu)}
                    className="px-2.5 py-1.5 text-xs font-semibold text-white bg-white/10 border border-white/20 rounded-lg hover:bg-white/20 transition-colors flex items-center gap-1.5"
                    title="Export workflows"
                  >
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                    Export
                    <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="6 9 12 15 18 9" /></svg>
                  </button>
                  {showExportMenu && (
                    <div className="absolute right-0 top-full mt-1 w-56 rounded-lg border border-slate-200 shadow-xl bg-white z-30 py-1">
                      <button
                        onClick={() => { exportWorkflowList('csv'); setShowExportMenu(false); }}
                        className="w-full px-4 py-2.5 text-left text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors flex items-center gap-2"
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                        Workflow List (CSV)
                      </button>
                      <button
                        onClick={() => { exportWorkflowList('json'); setShowExportMenu(false); }}
                        className="w-full px-4 py-2.5 text-left text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors flex items-center gap-2"
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                        Workflow List (JSON)
                      </button>
                      <div className="border-t border-slate-100 my-1" />
                      <button
                        onClick={() => { exportRunHistory(); setShowExportMenu(false); }}
                        className="w-full px-4 py-2.5 text-left text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors flex items-center gap-2"
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
                        Run History (CSV)
                      </button>
                      <button
                        onClick={() => { exportSchedules(); setShowExportMenu(false); }}
                        className="w-full px-4 py-2.5 text-left text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors flex items-center gap-2"
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                        Schedules (CSV)
                      </button>
                      <div className="border-t border-slate-100 my-1" />
                      <div className="px-4 py-1.5 text-[9px] font-bold text-slate-400 uppercase tracking-wider">Multi-Level</div>
                      <button
                        onClick={() => { exportMultiLevel('metadata', 'csv'); setShowExportMenu(false); }}
                        className="w-full px-4 py-2.5 text-left text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors flex items-center gap-2"
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
                        Full Metadata (CSV)
                      </button>
                      <button
                        onClick={() => { exportMultiLevel('metadata', 'json'); setShowExportMenu(false); }}
                        className="w-full px-4 py-2.5 text-left text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors flex items-center gap-2"
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                        Full Metadata (JSON)
                      </button>
                      <button
                        onClick={() => { exportMultiLevel('project', 'csv'); setShowExportMenu(false); }}
                        className="w-full px-4 py-2.5 text-left text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors flex items-center gap-2"
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
                        By Project (CSV)
                      </button>
                      <button
                        onClick={() => { exportMultiLevel('page', 'json'); setShowExportMenu(false); }}
                        className="w-full px-4 py-2.5 text-left text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors flex items-center gap-2"
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                        Page Export (JSON)
                      </button>
                    </div>
                  )}
                </div>
              </div>
            </div>
            <table className={`w-full border-collapse ${
              density === 'compact'
                ? '[&_tbody_td]:!py-1.5'
                : density === 'spacious'
                ? '[&_tbody_td]:!py-5'
                : ''
            }`}>
              <thead>
                <tr className="bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900 border-b-2 border-amber-400/40">
                  <th className="w-10 px-4 py-3">
                    <input
                      type="checkbox"
                      checked={selectedIds.size === filtered.length && filtered.length > 0}
                      onChange={toggleSelectAll}
                      className="w-3.5 h-3.5 rounded border-slate-300 text-pipe-600 focus:ring-pipe-300 cursor-pointer"
                    />
                  </th>
                  <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider cursor-pointer select-none hover:text-amber-200" onClick={() => handleSort('name')}>
                    Name <SortIcon col="name" />
                  </th>
                  {!projectId && pipeColState.isVisible('project') && (
                    <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">
                      Project
                    </th>
                  )}
                  {pipeColState.isVisible('status') && (
                    <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider cursor-pointer select-none hover:text-amber-200" onClick={() => handleSort('status')}>
                      Status <SortIcon col="status" />
                    </th>
                  )}
                  {pipeColState.isVisible('author') && (
                    <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">
                      Author
                    </th>
                  )}
                  {pipeColState.isVisible('modified') && (
                    <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider cursor-pointer select-none hover:text-amber-200" onClick={() => handleSort('updated_at')}>
                      Modified <SortIcon col="updated_at" />
                    </th>
                  )}
                  {pipeColState.isVisible('last_run') && (
                    <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">
                      Last Run
                    </th>
                  )}
                  {pipeColState.isVisible('priority') && (
                    <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">
                      Priority
                    </th>
                  )}
                  {pipeColState.isVisible('schedule') && (
                    <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">
                      Schedule
                    </th>
                  )}
                  {pipeColState.isVisible('alerts') && (
                    <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">
                      Alerts
                    </th>
                  )}
                  {pipeColState.isVisible('storage') && (
                    <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">
                      Storage
                    </th>
                  )}
                  <th className="w-56 px-3 py-3 text-right text-xs font-bold text-amber-300 uppercase tracking-wider">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((p) => {
                  const isSelected = selectedIds.has(p.id);
                  return (
                    <tr
                      key={p.id}
                      onClick={() => handleShowDetail(p)}
                      className={`border-b border-slate-100 hover:bg-pipe-50/30 transition-colors group cursor-pointer ${isSelected ? 'bg-pipe-50/40' : ''}`}
                    >
                      <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={() => toggleSelect(p.id)}
                          className="w-3.5 h-3.5 rounded border-slate-300 text-pipe-600 focus:ring-pipe-300 cursor-pointer"
                        />
                      </td>
                      {/* 2026-05-28 — explicit max-width on the Name
                          cell so long pipeline names (e.g. "Sales Pivot
                          + Trend Analysis (pivot/unpivot fan-out)") get
                          truncated to ellipsis instead of pushing the
                          Action column off-screen. The inner div already
                          has `min-w-0` for truncate to work, but the
                          parent <td> had no width constraint — so the
                          cell expanded to fit the longest name, and the
                          row's right edge (Actions) overflowed past the
                          viewport.

                          max-w-[300px] keeps the cell readable for
                          typical names while clipping pathological long
                          ones. The full name is restored via title=""
                          tooltip on hover. */}
                      <td className="px-3 py-3 max-w-[300px]">
                        <div
                          className="flex items-center gap-3 relative"
                          onMouseEnter={() => { setEstimateHover(p.id); fetchEstimate(p.id); }}
                          onMouseLeave={() => setEstimateHover(null)}
                        >
                          <div className="w-8 h-8 rounded-lg flex items-center justify-center shadow-sm shrink-0"
                            style={{ background: 'linear-gradient(135deg, #6366f1, #4f46e5)' }}
                          >
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5">
                              <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
                            </svg>
                          </div>
                          <div className="flex-1 min-w-0">
                            {renamingId === p.id ? (
                              <input
                                type="text"
                                autoFocus
                                value={renameValue}
                                disabled={renameSaving}
                                onClick={(e) => e.stopPropagation()}
                                onChange={(e) => setRenameValue(e.target.value)}
                                onBlur={handleCommitRename}
                                onKeyDown={(e) => {
                                  e.stopPropagation();
                                  if (e.key === 'Enter') handleCommitRename();
                                  if (e.key === 'Escape') handleCancelRename();
                                }}
                                className="text-sm font-semibold text-slate-700 bg-white border border-pipe-300 rounded-md px-2 py-1 w-full max-w-[260px] focus:outline-none focus:ring-2 focus:ring-pipe-300 disabled:opacity-50"
                              />
                            ) : (
                              <div className="text-sm font-semibold text-slate-700 group-hover:text-pipe-700 transition-colors flex items-center gap-1.5">
                                <span className="truncate" title={p.name}>{p.name}</span>
                                {canEdit && (
                                  <button
                                    onClick={(e) => { e.stopPropagation(); handleStartRename(p); }}
                                    className="opacity-0 group-hover:opacity-100 transition-opacity text-slate-400 hover:text-pipe-600 p-0.5 rounded"
                                    title="Rename pipeline"
                                  >
                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                      <path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
                                    </svg>
                                  </button>
                                )}
                              </div>
                            )}
                            <div className="flex items-center gap-1.5 mt-0.5">
                              <span className="text-xs text-slate-400 font-mono">{p.id.slice(0, 8)}</span>
                              {getTagsForPipeline(p.id).map((tag) => {
                                const c = getColorForTag(tag);
                                return (
                                  <span key={tag} className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-medium ${c.bg} ${c.text}`}>
                                    <span className={`w-1.5 h-1.5 rounded-full ${c.dot}`} />
                                    {tag}
                                  </span>
                                );
                              })}
                              {/* Add tag button */}
                              <div className="relative" onClick={(e) => e.stopPropagation()}>
                                <button
                                  onClick={() => { setShowTagMenu(showTagMenu === p.id ? null : p.id); setNewTagInput(''); }}
                                  className="w-4 h-4 rounded flex items-center justify-center text-slate-300 hover:text-slate-500 hover:bg-slate-100 transition-colors opacity-0 group-hover:opacity-100"
                                  title="Add tag"
                                >
                                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
                                </button>
                                {showTagMenu === p.id && (
                                  <div ref={tagMenuRef} className="absolute left-0 top-full mt-1 w-56 rounded-lg border border-slate-200 shadow-sm bg-white shadow-lg z-30 p-3" onClick={(e) => e.stopPropagation()}>
                                    <input
                                      type="text"
                                      value={newTagInput}
                                      onChange={(e) => setNewTagInput(e.target.value)}
                                      onKeyDown={(e) => { if (e.key === 'Enter') addNewTag(p.id); }}
                                      placeholder="New tag name..."
                                      className="w-full px-2.5 py-1.5 text-xs text-slate-700 bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-pipe-300 mb-2 placeholder:text-slate-400"
                                      autoFocus
                                    />
                                    {allTags.length > 0 && (
                                      <div className="max-h-32 overflow-auto mb-2 space-y-0.5">
                                        {allTags.map((tag) => {
                                          const checked = getTagsForPipeline(p.id).includes(tag);
                                          const c = getColorForTag(tag);
                                          return (
                                            <label key={tag} className="flex items-center gap-2 px-2 py-1 rounded-md hover:bg-slate-50 cursor-pointer text-xs">
                                              <input
                                                type="checkbox"
                                                checked={checked}
                                                onChange={() => toggleTag(p.id, tag)}
                                                className="w-3 h-3 rounded border-slate-300 text-pipe-600 focus:ring-pipe-300"
                                              />
                                              <span className={`w-2 h-2 rounded-full ${c.dot}`} />
                                              <span className="text-slate-700">{tag}</span>
                                            </label>
                                          );
                                        })}
                                      </div>
                                    )}
                                    {/* Color picker for selected/new tag */}
                                    {newTagInput.trim() && (
                                      <div className="border-t border-slate-100 pt-2 mt-1">
                                        <div className="text-xs text-slate-400 mb-1.5">Color for "{newTagInput.trim()}"</div>
                                        <div className="flex items-center gap-1.5">
                                          {TAG_COLORS.map((c) => (
                                            <button
                                              key={c.name}
                                              onClick={() => { setTagColor(newTagInput.trim().toLowerCase(), c.name); addNewTag(p.id); }}
                                              className={`w-5 h-5 rounded-full ${c.dot} hover:ring-2 hover:ring-offset-1 hover:ring-slate-300 transition-all`}
                                              title={c.name}
                                            />
                                          ))}
                                        </div>
                                      </div>
                                    )}
                                    {!newTagInput.trim() && allTags.length > 0 && (
                                      <div className="border-t border-slate-100 pt-2 mt-1">
                                        <div className="text-xs text-slate-400 mb-1.5">Change tag color</div>
                                        {getTagsForPipeline(p.id).map((tag) => (
                                          <div key={tag} className="flex items-center gap-1.5 mb-1.5">
                                            <span className="text-xs text-slate-600 w-14 truncate">{tag}</span>
                                            <div className="flex items-center gap-1">
                                              {TAG_COLORS.map((c) => (
                                                <button
                                                  key={c.name}
                                                  onClick={() => setTagColor(tag, c.name)}
                                                  className={`w-4 h-4 rounded-full ${c.dot} transition-all ${tagColors[tag] === c.name ? 'ring-2 ring-offset-1 ring-slate-400' : 'hover:ring-2 hover:ring-offset-1 hover:ring-slate-200'}`}
                                                  title={c.name}
                                                />
                                              ))}
                                            </div>
                                          </div>
                                        ))}
                                      </div>
                                    )}
                                  </div>
                                )}
                              </div>
                            </div>
                          </div>
                          {/* Execution estimate tooltip */}
                          {estimateHover === p.id && estimateCache[p.id] && (
                            <div className="absolute left-0 -bottom-8 z-10 px-2.5 py-1 bg-slate-800 text-white text-xs font-medium rounded-md shadow-lg whitespace-nowrap">
                              Est. runtime: {estimateCache[p.id]}
                            </div>
                          )}
                        </div>
                      </td>
                      {!projectId && pipeColState.isVisible('project') && (
                        <td className="px-3 py-3">
                          {(() => {
                            // Resolve project_id → name and folder_id →
                            // name so the cell shows the same labels the
                            // user sees on the Projects page tree, not
                            // raw IDs. Falls back to the id (or
                            // "Default") if a lookup misses.
                            const pid = (p as any).project_id || '';
                            const fid = (p as any).folder_id || '';
                            const projName = pid ? (projectNameById.get(pid) || pid) : 'Default';
                            const folderName = fid ? (folders.find((f) => f.id === fid)?.name || '') : '';
                            return (
                              <div className="flex flex-col gap-0.5 leading-tight">
                                <span className="text-xs text-slate-700 font-medium truncate" title={folderName ? `${projName} / ${folderName}` : projName}>
                                  {projName}
                                </span>
                                {folderName && (
                                  <span className="text-[10px] text-slate-400 truncate" title={folderName}>
                                    / {folderName}
                                  </span>
                                )}
                              </div>
                            );
                          })()}
                        </td>
                      )}
                      {pipeColState.isVisible('status') && (
                        <td className="px-3 py-3">
                          <div className="flex items-center gap-1.5">
                            {/* 2026-05-28 — running override.
                                Reported in internal testing: a running pipeline was
                                only signalled by a small Stop button in
                                the Actions column. The Status column
                                kept showing the saved lifecycle status
                                ("Published" etc.) which is technically
                                accurate but visually misleading — the
                                user can't tell at a glance that a row
                                is actively executing.

                                When the row is running (either user-
                                triggered via runningPipelines[id] OR
                                backend status === 'running'), replace
                                the saved-status badge with a prominent
                                pulsing "Running" pill + spinner. The
                                saved lifecycle (Published / Draft /
                                Failed) is still inferable from the
                                Last Run column and the Actions row. */}
                            {(!!runningPipelines[p.id] || normalizeStatus(p.status) === 'running') ? (
                              <span className="inline-flex items-center gap-1.5 text-xs font-bold uppercase tracking-wide text-blue-700 bg-blue-50 border border-blue-300 px-2.5 py-1 rounded-full shadow-[0_0_8px_rgba(59,130,246,0.35)] animate-pulse">
                                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="animate-spin">
                                  <path d="M21 12a9 9 0 1 1-6.219-8.56" />
                                </svg>
                                Running
                              </span>
                            ) : (
                              statusBadge(p.status)
                            )}
                            {/* Paused badge when the env-matching is_active flag is false.
                                Lives in the DEV branch of the function (the PROD branch
                                early-returns at ~line 2832), so the prod flag is dead here. */}
                            {(() => {
                              const flag = (p as any).is_active_dev;
                              if (flag === false) {
                                return (
                                  <span className="inline-flex items-center gap-1 text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 border border-amber-200" title="This pipeline is paused — schedules and triggers are disabled">
                                    <svg width="9" height="9" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" rx="0.5" /><rect x="14" y="4" width="4" height="16" rx="0.5" /></svg>
                                    Paused
                                  </span>
                                );
                              }
                              return null;
                            })()}
                          </div>
                        </td>
                      )}
                      {pipeColState.isVisible('author') && (
                        <td className="px-3 py-3">
                          <span className="text-xs text-slate-500 font-medium">{p.owner_name || p.owner_id || '—'}</span>
                        </td>
                      )}
                      {pipeColState.isVisible('modified') && (
                        <td className="px-3 py-3">
                          <TimeAgo value={p.updated_at} className="text-xs !text-slate-500" />
                        </td>
                      )}
                      {pipeColState.isVisible('last_run') && (
                        <td className="px-3 py-3">
                          <TimeAgo value={p.last_run} className="text-xs !text-slate-400" />
                        </td>
                      )}
                      {/* Priority cell — chip that opens a P1–P5 popover.
                          Mirrors the Schedule / Alerts cell pattern so the
                          priority sits next to its sibling operational
                          concerns. Backend stores in metadata.priority. */}
                      {pipeColState.isVisible('priority') && (
                        <td className="px-3 py-3" onClick={(e) => e.stopPropagation()}>
                          {(() => {
                            const currentPriority = (p.metadata?.priority as number | undefined) ?? 3;
                            const open = priorityMenuFor === p.id;
                            const tone =
                              currentPriority === 1 ? 'bg-red-50 text-red-700 border-red-200 hover:bg-red-100'
                              : currentPriority === 2 ? 'bg-orange-50 text-orange-700 border-orange-200 hover:bg-orange-100'
                              : currentPriority === 3 ? 'bg-slate-50 text-slate-600 border-slate-200 hover:bg-slate-100'
                              : currentPriority === 4 ? 'bg-sky-50 text-sky-700 border-sky-200 hover:bg-sky-100'
                              : 'bg-violet-50 text-violet-700 border-violet-200 hover:bg-violet-100';
                            const label = currentPriority === 1 ? 'Critical' : currentPriority === 2 ? 'High' : currentPriority === 3 ? 'Normal' : currentPriority === 4 ? 'Low' : 'Background';
                            return (
                              <button
                                data-priority-trigger="true"
                                onClick={(e) => {
                                  if (open) { setPriorityMenuFor(null); setPriorityAnchor(null); }
                                  else openPriorityMenu(p.id, e.currentTarget);
                                }}
                                disabled={priorityBusy === p.id}
                                className={`inline-flex items-center gap-1.5 text-xs font-medium border px-2 py-1 rounded-full transition-colors disabled:opacity-50 ${tone}`}
                                title="Run priority — affects worker-pool queue order when multiple pipelines run at once"
                              >
                                <span className="text-[10px] font-bold">P{currentPriority}</span>
                                <span>{label}</span>
                              </button>
                            );
                          })()}
                        </td>
                      )}
                      {pipeColState.isVisible('schedule') && (
                        <td className="px-3 py-3" onClick={(e) => e.stopPropagation()}>
                          {(() => {
                            // Only published pipelines can be scheduled — schedules
                            // need a validated, deployable artifact and Publish is
                            // the only flow that validates. Drafts / failed / archived
                            // pipelines show a disabled "Publish to schedule" hint.
                            // An existing schedule is always editable (a pipeline can
                            // be Published → Revoked back to Draft and still own a
                            // schedule that should be manageable).
                            const ns = normalizeStatus(p.status);
                            const canSchedule = ns === 'published';
                            const existing = scheduleMap[p.id];
                            if (existing) {
                              return (
                                <button
                                  onClick={() => setQuickSchedule({ id: p.id, name: p.name })}
                                  title={`Click to manage schedule (${describeCron(existing.cron_expression || '')})`}
                                  className="inline-flex items-center gap-1.5 text-xs font-medium text-emerald-700 bg-emerald-50 hover:bg-emerald-100 border border-emerald-200 px-2 py-1 rounded-full transition-colors"
                                >
                                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>
                                  {describeCron(existing.cron_expression || '')}
                                </button>
                              );
                            }
                            if (canSchedule) {
                              return (
                                <button
                                  onClick={() => setQuickSchedule({ id: p.id, name: p.name })}
                                  className="text-xs text-slate-400 hover:text-emerald-600 hover:bg-emerald-50 px-2 py-1 rounded-lg transition-colors"
                                >
                                  + Schedule
                                </button>
                              );
                            }
                            return (
                              <span
                                className="text-xs text-slate-300 px-2 py-1 cursor-not-allowed"
                                title="Publish this pipeline first — Publish runs validation, and only validated pipelines can be scheduled."
                              >
                                Publish to schedule
                              </span>
                            );
                          })()}
                        </td>
                      )}
                      {/* Alerts cell — chip with active rule count when set,
                          "+ Alert" link when none. Click opens the same
                          QuickAlertDialog as the bell icon in the Actions
                          column, so the user has two equally valid ways
                          to manage alerts for this pipeline. */}
                      {pipeColState.isVisible('alerts') && (
                        <td className="px-3 py-3 whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
                          {alertCountMap[p.id] ? (
                            <button
                              onClick={() => setQuickAlert({ id: p.id, name: p.name })}
                              title={`Click to manage alert rule${alertCountMap[p.id] === 1 ? '' : 's'}`}
                              className="inline-flex items-center gap-1.5 text-xs font-medium text-amber-700 bg-amber-50 hover:bg-amber-100 border border-amber-200 px-2 py-1 rounded-full transition-colors whitespace-nowrap"
                            >
                              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" /></svg>
                              <span>{`${alertCountMap[p.id]} ${alertCountMap[p.id] === 1 ? 'alert' : 'alerts'}`}</span>
                            </button>
                          ) : (
                            <button
                              onClick={() => setQuickAlert({ id: p.id, name: p.name })}
                              className="text-xs text-slate-400 hover:text-amber-600 hover:bg-amber-50 px-2 py-1 rounded-lg transition-colors whitespace-nowrap"
                            >
                              + Alert
                            </button>
                          )}
                        </td>
                      )}
                      {/* Storage cell — emerald chip showing how many
                          workspace storage items (files + managed tables)
                          this pipeline references. Click jumps to the
                          Storage page. Hides on 0 to keep the table calm. */}
                      {pipeColState.isVisible('storage') && (
                        <td className="px-3 py-3 whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
                          {(() => {
                            const n = storageUsageMap[p.id] || 0;
                            if (n === 0) return <span className="text-xs text-slate-300">—</span>;
                            return (
                              <button
                                onClick={() => { window.location.hash = 'storage'; }}
                                title={`Pipeline references ${n} workspace storage item${n === 1 ? '' : 's'} (files + managed tables). Click to open Storage.`}
                                className="inline-flex items-center gap-1.5 text-xs font-medium text-emerald-700 bg-emerald-50 hover:bg-emerald-100 border border-emerald-200 px-2 py-1 rounded-full transition-colors whitespace-nowrap"
                              >
                                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v6c0 1.66 4.03 3 9 3s9-1.34 9-3V5" /><path d="M3 11v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6" /></svg>
                                <span>{n}</span>
                              </button>
                            );
                          })()}
                        </td>
                      )}
                      <td className="px-3 py-3" onClick={(e) => e.stopPropagation()}>
                        {(() => {
                          const ns = normalizeStatus(p.status);
                          const isRunning = !!runningPipelines[p.id];
                          const approval = (p as any).approval_status as string;
                          return (
                            <div className="flex items-center justify-end gap-1">
                              {/* Run / Stop — flips when the pipeline is executing.
                                  2026-05-28 — icon-only was visually inconsistent
                                  with the rest of the row (Revoke / Publish /
                                  Deactivate / Edit are all text). Run is the
                                  primary action on this page, so it now gets a
                                  text label too. Style: blue-primary pill when
                                  idle, red-danger pill when running. */}
                              {isRunning || ns === 'running' ? (
                                <button
                                  onClick={async () => {
                                    try {
                                      await api.cancelExecution(p.id);
                                      toast.info('Cancelling…', `Stop signal sent to "${p.name}"`);
                                    } catch (e: any) {
                                      toast.error('Cancel failed', e?.message || 'Could not send stop signal');
                                    }
                                  }}
                                  className="px-3 py-1.5 text-xs font-bold rounded-lg flex items-center gap-1.5 text-red-600 border border-red-300 bg-red-50 hover:bg-red-100 hover:border-red-400 transition-all"
                                  title="Stop running pipeline"
                                >
                                  <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="6" y="6" width="12" height="12" rx="1" /></svg>
                                  Stop
                                </button>
                              ) : (
                                <button
                                  onClick={() => handleRunPipeline(p.id, p.name)}
                                  className="px-3 py-1.5 text-xs font-bold rounded-lg flex items-center gap-1.5 text-blue-700 border border-blue-300 bg-blue-50 hover:bg-blue-100 hover:border-blue-400 transition-all"
                                  title="Run Pipeline"
                                >
                                  <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="5 3 19 12 5 21 5 3" /></svg>
                                  Run
                                </button>
                              )}

                              {/* Alert button removed — the Alerts column
                                  ('+ Alert' link or 'N alerts' chip) is
                                  the single management surface so the
                                  table doesn't show two clickable bells
                                  per row. Both used to open the same
                                  QuickAlertDialog. */}

                              {/* Approval badge */}
                              {approval === 'pending' && (
                                <span className="px-2 py-0.5 text-[9px] font-bold text-amber-700 bg-amber-100 border border-amber-300 rounded-full" title="Awaiting review">
                                  PENDING
                                </span>
                              )}
                              {approval === 'approved' && (
                                <span className="px-2 py-0.5 text-[9px] font-bold text-emerald-700 bg-emerald-100 border border-emerald-300 rounded-full">
                                  APPROVED
                                </span>
                              )}
                              {approval === 'rejected' && (
                                <span className="px-2 py-0.5 text-[9px] font-bold text-red-700 bg-red-100 border border-red-300 rounded-full">
                                  REJECTED
                                </span>
                              )}
                              {/* Publish — for draft/failed pipelines */}
                              {canEdit && (ns === 'draft' || ns === 'failed') && !approval && (
                                <button
                                  onClick={() => handleLifecycleAction(p.id, 'publish')}
                                  disabled={!!lifecycleLoading[p.id]}
                                  className="px-2 py-1 text-xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg hover:bg-emerald-100 transition-colors disabled:opacity-50"
                                  title="Publish pipeline"
                                >
                                  {lifecycleLoading[p.id] === 'publish' ? 'Publishing...' : 'Publish'}
                                </button>
                              )}
                              {/* Revoke — pull a published pipeline back to draft.
                                  Inverse of Publish. Schedules + triggers stop
                                  firing once status flips. */}
                              {canEdit && ns === 'published' && !approval && (
                                <button
                                  onClick={() => handleLifecycleAction(p.id, 'revoke')}
                                  disabled={!!lifecycleLoading[p.id]}
                                  className="px-2 py-1 text-xs font-semibold text-slate-700 bg-white border border-slate-300 rounded-lg hover:bg-slate-50 transition-colors disabled:opacity-50"
                                  title="Revoke — move back to draft (stops schedules and triggers)"
                                >
                                  {lifecycleLoading[p.id] === 'revoke' ? 'Revoking...' : 'Revoke'}
                                </button>
                              )}
                              {/* Submit for Deploy — Plus only. PROD promotion via
                                  approvals is a Plus-tier feature; Free has no PROD
                                  environment, so the button is hidden entirely. */}
                              {tier === 'plus' && !approval && canSubmitForReview && ns === 'published' && (
                                <button
                                  onClick={() => handleSubmitForReview(p.id, p.name)}
                                  className="px-2 py-1 text-xs font-semibold text-amber-700 bg-amber-50 border border-amber-200 rounded-lg hover:bg-amber-100 transition-colors"
                                  title="Submit for deploy review"
                                >
                                  Submit for Deploy
                                </button>
                              )}

                              <div className="w-px h-5 bg-slate-200 mx-0.5" />

                              {/* Activate/Deactivate.
                                  This block lives in the DEV branch (PROD has its own
                                  early-return UI at ~line 2832), so we hard-bind to
                                  the DEV flag + direct-toggle labels. The PROD-request
                                  variant lives in the PROD render block above. */}
                              {(() => {
                                const flagKey = 'is_active_dev';
                                const isActive = (p as any)[flagKey] !== false;
                                const loadingTag = lifecycleLoading[p.id];
                                const busy = loadingTag === 'activate' || loadingTag === 'deactivate';
                                const onClick = () => handleToggleActive(p, !isActive);
                                if (isActive) {
                                  return (
                                    <button
                                      onClick={onClick}
                                      disabled={busy}
                                      className="px-2 py-1 text-xs font-semibold text-amber-700 bg-amber-50 border border-amber-200 rounded-lg hover:bg-amber-100 transition-colors disabled:opacity-50"
                                      title="Deactivate this pipeline (DEV — direct)"
                                    >
                                      {busy ? '…' : 'Deactivate'}
                                    </button>
                                  );
                                }
                                return (
                                  <button
                                    onClick={onClick}
                                    disabled={busy}
                                    className="px-2 py-1 text-xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg hover:bg-emerald-100 transition-colors disabled:opacity-50"
                                    title="Activate this pipeline (DEV — direct)"
                                  >
                                    {busy ? '…' : 'Activate'}
                                  </button>
                                );
                              })()}

                              {/* Edit */}
                              {canEdit && ns !== 'running' && (
                                <button
                                  onClick={() => handleOpen(p.id)}
                                  className="px-2 py-1 text-xs font-semibold text-pipe-600 bg-pipe-50 rounded-lg hover:bg-pipe-100 transition-colors"
                                  title="Open in Editor"
                                >
                                  Edit
                                </button>
                              )}
                              {/* Move to project — kept inline because
                                  it's already a self-contained dropdown
                                  with its own click behavior, awkward
                                  to nest inside another popover. */}
                              <MoveToProjectButton
                                currentProjectId={(p as any).project_id || 'default'}
                                currentFolderId={(p as any).folder_id || null}
                                withFolders
                                onMove={(target, folder) => handleMoveProject(p.id, p.name, target, folder)}
                              />
                              {/* 2026-05-28 — kebab "..." menu collapses
                                  Copy / Save as template / Delete (3
                                  icon-only buttons) into one trigger.
                                  Saves ~80px per row so the table fits
                                  in a standard 1920px viewport without
                                  horizontal scrolling. Menu opens above
                                  or below depending on row position;
                                  outside-click closes via the document
                                  handler at the top of the component. */}
                              {(() => {
                                // Explicit visibility booleans —
                                // computing them up front avoids the TS
                                // narrowing trap that fires when nested
                                // `ns ===` checks appear after `ns !==`
                                // sibling clauses (the compiler narrows
                                // away the matching literal types).
                                const canCopy = ns !== 'archived' && ns !== 'running';
                                const canTemplate = ns !== 'archived' && ns !== 'running';
                                const canDeleteHere = canDelete && (ns === 'draft' || ns === 'failed' || ns === 'archived');
                                const anyMenuItem = canCopy || canTemplate || canDeleteHere;
                                if (!anyMenuItem) return null;
                                return (
                                  <div className="relative" data-row-more-menu>
                                    <button
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        setMoreMenuFor(moreMenuFor === p.id ? null : p.id);
                                      }}
                                      className="w-7 h-7 rounded-lg flex items-center justify-center text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-all"
                                      title="More actions"
                                      aria-label="More actions"
                                      aria-expanded={moreMenuFor === p.id}
                                    >
                                      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none">
                                        <circle cx="5" cy="12" r="2" />
                                        <circle cx="12" cy="12" r="2" />
                                        <circle cx="19" cy="12" r="2" />
                                      </svg>
                                    </button>
                                    {moreMenuFor === p.id && (
                                      <div
                                        className="absolute right-0 top-full mt-1 w-48 bg-white rounded-lg border border-slate-200 shadow-lg z-30 py-1"
                                        onClick={(e) => e.stopPropagation()}
                                      >
                                        {canCopy && (
                                          <button
                                            onClick={() => { setMoreMenuFor(null); handleDuplicate(p); }}
                                            className="w-full flex items-center gap-2 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors"
                                          >
                                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-400"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
                                            Copy Pipeline
                                          </button>
                                        )}
                                        {canTemplate && (
                                          <button
                                            onClick={() => { setMoreMenuFor(null); handleSaveAsTemplate(p); }}
                                            className="w-full flex items-center gap-2 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors"
                                            title="Appears under Templates → User defined"
                                          >
                                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-violet-500">
                                              <rect x="3" y="3" width="7" height="7" rx="1" />
                                              <rect x="14" y="3" width="7" height="7" rx="1" />
                                              <rect x="3" y="14" width="7" height="7" rx="1" />
                                              <path d="M14 17h7" />
                                              <path d="M17.5 14v7" />
                                            </svg>
                                            Save as template
                                          </button>
                                        )}
                                        {/* 2026-06-15 — "Publish as macro" creation removed from
                                            the kebab. Pipeline reuse now goes through the
                                            "Execute Pipeline" node (drop it on the canvas → pick the
                                            child pipeline → pass parameters), which avoids the
                                            convert-first friction. We KEEP an un-publish exit for
                                            any pipeline already published as a macro so existing
                                            tiles aren't orphaned in the palette. */}
                                        {!!(p.metadata && (p.metadata as any).published_as_node) && (
                                          <button
                                            onClick={() => { setMoreMenuFor(null); handleTogglePublishMacro(p); }}
                                            className="w-full flex items-center gap-2 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors"
                                            title="Remove this pipeline's macro tile from the canvas palette. Use the Execute Pipeline node to call it instead."
                                          >
                                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-slate-400">
                                              <circle cx="12" cy="12" r="9" />
                                              <path d="M8 12h8" />
                                            </svg>
                                            Remove from palette
                                          </button>
                                        )}
                                        {canDeleteHere && (
                                          <>
                                            {(canCopy || canTemplate) && (
                                              <div className="my-1 border-t border-slate-100" />
                                            )}
                                            <button
                                              onClick={() => { setMoreMenuFor(null); handleDelete(p.id, p.name); }}
                                              className="w-full flex items-center gap-2 px-3 py-2 text-xs font-medium text-red-600 hover:bg-red-50 transition-colors"
                                            >
                                              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6" /><path d="M19 6l-2 14H7L5 6" /><path d="M9 6V4h6v2" /></svg>
                                              Delete
                                            </button>
                                          </>
                                        )}
                                      </div>
                                    )}
                                  </div>
                                );
                              })()}
                            </div>
                          );
                        })()}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {/* Footer */}
            <div className="px-4 py-2.5 bg-slate-50/60 border-t border-slate-200 flex items-center justify-between">
              <span className="text-xs text-slate-400">
                Showing {filtered.length} of {pipelines.length} workflows
              </span>
              <span className="text-xs text-slate-400">
                {selectedIds.size > 0 ? `${selectedIds.size} selected` : ''}
              </span>
            </div>
          </div>
        ) : (
          /* ── GRID VIEW ── */
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {filtered.map((p) => (
              <div
                key={p.id}
                onClick={() => handleShowDetail(p)}
                className="rounded-lg border border-slate-200 shadow-sm bg-white hover:border-pipe-300 hover:shadow-md transition-all group p-4 cursor-pointer"
              >
                <div className="flex items-start justify-between mb-3">
                  <div className="flex items-center gap-2.5 min-w-0">
                    <div className="w-8 h-8 rounded-lg flex items-center justify-center shadow-sm shrink-0"
                      style={{ background: 'linear-gradient(135deg, #6366f1, #4f46e5)' }}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5">
                        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
                      </svg>
                    </div>
                    <div className="min-w-0">
                      <div className="text-sm font-semibold text-slate-700 truncate group-hover:text-pipe-700">
                        {p.name}
                      </div>
                      <div className="text-xs text-slate-400">
                        {p.steps?.length || p.steps_count || 0} steps · {formatRelativeTime(p.updated_at)}{p.owner_name ? ` · ${p.owner_name}` : ''}
                      </div>
                    </div>
                  </div>
                </div>
                {/* Tags row */}
                <div className="flex items-center gap-1 mb-2 flex-wrap" onClick={(e) => e.stopPropagation()}>
                  {getTagsForPipeline(p.id).map((tag) => {
                    const c = getColorForTag(tag);
                    return (
                      <span key={tag} className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-medium ${c.bg} ${c.text}`}>
                        <span className={`w-1.5 h-1.5 rounded-full ${c.dot}`} />
                        {tag}
                      </span>
                    );
                  })}
                  <div className="relative">
                    <button
                      onClick={() => { setShowTagMenu(showTagMenu === p.id ? null : p.id); setNewTagInput(''); }}
                      className="w-5 h-5 rounded flex items-center justify-center text-slate-300 hover:text-slate-500 hover:bg-slate-100 transition-colors opacity-0 group-hover:opacity-100"
                      title="Add tag"
                    >
                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
                    </button>
                    {showTagMenu === p.id && (
                      <div ref={tagMenuRef} className="absolute left-0 top-full mt-1 w-56 rounded-lg border border-slate-200 shadow-sm bg-white shadow-lg z-30 p-3">
                        <input
                          type="text"
                          value={newTagInput}
                          onChange={(e) => setNewTagInput(e.target.value)}
                          onKeyDown={(e) => { if (e.key === 'Enter') addNewTag(p.id); }}
                          placeholder="New tag name..."
                          className="w-full px-2.5 py-1.5 text-xs text-slate-700 bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-pipe-300 mb-2 placeholder:text-slate-400"
                          autoFocus
                        />
                        {allTags.length > 0 && (
                          <div className="max-h-32 overflow-auto mb-2 space-y-0.5">
                            {allTags.map((tag) => {
                              const checked = getTagsForPipeline(p.id).includes(tag);
                              const c = getColorForTag(tag);
                              return (
                                <label key={tag} className="flex items-center gap-2 px-2 py-1 rounded-md hover:bg-slate-50 cursor-pointer text-xs">
                                  <input
                                    type="checkbox"
                                    checked={checked}
                                    onChange={() => toggleTag(p.id, tag)}
                                    className="w-3 h-3 rounded border-slate-300 text-pipe-600 focus:ring-pipe-300"
                                  />
                                  <span className={`w-2 h-2 rounded-full ${c.dot}`} />
                                  <span className="text-slate-700">{tag}</span>
                                </label>
                              );
                            })}
                          </div>
                        )}
                        {newTagInput.trim() && (
                          <div className="border-t border-slate-100 pt-2 mt-1">
                            <div className="text-xs text-slate-400 mb-1.5">Color for "{newTagInput.trim()}"</div>
                            <div className="flex items-center gap-1.5">
                              {TAG_COLORS.map((c) => (
                                <button
                                  key={c.name}
                                  onClick={() => { setTagColor(newTagInput.trim().toLowerCase(), c.name); addNewTag(p.id); }}
                                  className={`w-5 h-5 rounded-full ${c.dot} hover:ring-2 hover:ring-offset-1 hover:ring-slate-300 transition-all`}
                                  title={c.name}
                                />
                              ))}
                            </div>
                          </div>
                        )}
                        {!newTagInput.trim() && getTagsForPipeline(p.id).length > 0 && (
                          <div className="border-t border-slate-100 pt-2 mt-1">
                            <div className="text-xs text-slate-400 mb-1.5">Change tag color</div>
                            {getTagsForPipeline(p.id).map((tag) => (
                              <div key={tag} className="flex items-center gap-1.5 mb-1.5">
                                <span className="text-xs text-slate-600 w-14 truncate">{tag}</span>
                                <div className="flex items-center gap-1">
                                  {TAG_COLORS.map((c) => (
                                    <button
                                      key={c.name}
                                      onClick={() => setTagColor(tag, c.name)}
                                      className={`w-4 h-4 rounded-full ${c.dot} transition-all ${tagColors[tag] === c.name ? 'ring-2 ring-offset-1 ring-slate-400' : 'hover:ring-2 hover:ring-offset-1 hover:ring-slate-200'}`}
                                      title={c.name}
                                    />
                                  ))}
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
                <div className="flex items-center justify-between">
                  {statusBadge(p.status)}
                  {(() => {
                    const ns = normalizeStatus(p.status);
                    const isRunning = !!runningPipelines[p.id];
                    return (
                      <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity" onClick={(e) => e.stopPropagation()}>
                        {/* Run */}
                        <button
                          onClick={() => handleRunPipeline(p.id, p.name)}
                          disabled={isRunning}
                          className="w-6 h-6 rounded flex items-center justify-center text-blue-500 hover:text-blue-700 hover:bg-blue-50 disabled:opacity-40"
                          title={isRunning ? 'Running...' : 'Run'}
                        >
                          {isRunning ? (
                            <div className="w-3 h-3 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />
                          ) : (
                            <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="5 3 19 12 5 21 5 3" /></svg>
                          )}
                        </button>
                        {/* Schedule — published-only. Existing schedules
                            stay manageable even after a Revoke (the pipeline
                            still owns the schedule and the user needs a way
                            to delete/disable it). */}
                        {(() => {
                          const canSchedule = ns === 'published';
                          const hasExisting = !!scheduleMap[p.id];
                          if (!canSchedule && !hasExisting) {
                            return (
                              <button
                                disabled
                                className="w-6 h-6 rounded flex items-center justify-center text-slate-300 cursor-not-allowed"
                                title="Publish this pipeline first — only validated, published pipelines can be scheduled."
                              >
                                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>
                              </button>
                            );
                          }
                          return (
                            <button
                              onClick={() => setQuickSchedule({ id: p.id, name: p.name })}
                              className="w-6 h-6 rounded flex items-center justify-center text-emerald-500 hover:text-emerald-700 hover:bg-emerald-50"
                              title={hasExisting ? 'Manage schedule' : 'Schedule'}
                            >
                              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>
                            </button>
                          );
                        })()}
                        {/* Alert */}
                        <button
                          onClick={() => setQuickAlert({ id: p.id, name: p.name })}
                          className="w-6 h-6 rounded flex items-center justify-center text-red-400 hover:text-red-600 hover:bg-red-50"
                          title="Alert"
                        >
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" /></svg>
                        </button>
                        {/* Publish */}
                        {canEdit && (ns === 'draft' || ns === 'failed') && (
                          <button
                            onClick={() => handleLifecycleAction(p.id, 'publish')}
                            disabled={!!lifecycleLoading[p.id]}
                            className="w-6 h-6 rounded flex items-center justify-center text-emerald-500 hover:text-emerald-700 hover:bg-emerald-50 disabled:opacity-40"
                            title="Publish"
                          >
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="20 6 9 17 4 12" /></svg>
                          </button>
                        )}
                        {/* Submit for Deploy — Plus only (see comment above). */}
                        {tier === 'plus' && canSubmitForReview && ns === 'published' && (
                          <button
                            onClick={() => handleSubmitForReview(p.id, p.name)}
                            className="w-6 h-6 rounded flex items-center justify-center text-amber-500 hover:text-amber-700 hover:bg-amber-50"
                            title="Submit for Deploy"
                          >
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 2L11 13" /><path d="M22 2l-7 20-4-9-9-4 20-7z" /></svg>
                          </button>
                        )}
                        {/* Priority picker — small P{n} chip that opens
                            a P1–P5 popover. Updates metadata.priority on
                            the workflow; the execution API reads this
                            field at submit time to order the worker-pool
                            queue. Default rendering is P3 when not yet
                            set. */}
                        {(() => {
                          const currentPriority = (p.metadata?.priority as number | undefined) ?? 3;
                          const open = priorityMenuFor === p.id;
                          const tone =
                            currentPriority === 1 ? 'bg-red-50 text-red-700 ring-1 ring-red-200'
                            : currentPriority === 2 ? 'bg-orange-50 text-orange-700 ring-1 ring-orange-200'
                            : currentPriority === 3 ? 'bg-slate-100 text-slate-600 ring-1 ring-slate-200'
                            : currentPriority === 4 ? 'bg-sky-50 text-sky-700 ring-1 ring-sky-200'
                            : 'bg-violet-50 text-violet-700 ring-1 ring-violet-200';
                          return (
                            <div className="relative">
                              <button
                                data-priority-trigger="true"
                                onClick={() => setPriorityMenuFor(open ? null : p.id)}
                                disabled={priorityBusy === p.id}
                                className={`h-6 px-1.5 rounded text-[11px] font-bold flex items-center justify-center transition-colors hover:brightness-95 disabled:opacity-50 ${tone}`}
                                title="Run priority — affects worker-pool queue order when multiple pipelines run at once"
                              >
                                P{currentPriority}
                              </button>
                              {open && (
                                <div
                                  data-priority-menu="true"
                                  className="absolute right-0 top-full mt-1 z-20 rounded-md border border-slate-200 bg-white shadow-lg min-w-[200px]"
                                >
                                  <div className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider text-slate-500 border-b border-slate-100 bg-slate-50">
                                    Run priority
                                  </div>
                                  <div className="py-1">
                                    {[1, 2, 3, 4, 5].map((n) => {
                                      const label = n === 1 ? 'Critical' : n === 2 ? 'High' : n === 3 ? 'Normal' : n === 4 ? 'Low' : 'Background';
                                      const isCurrent = n === currentPriority;
                                      return (
                                        <button
                                          key={n}
                                          onClick={() => handleSetPriority(p, n)}
                                          className={`w-full text-left px-3 py-1.5 text-xs flex items-center gap-2 hover:bg-slate-50 ${isCurrent ? 'font-bold text-slate-800' : 'text-slate-600'}`}
                                        >
                                          <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
                                            n === 1 ? 'bg-red-50 text-red-700'
                                            : n === 2 ? 'bg-orange-50 text-orange-700'
                                            : n === 3 ? 'bg-slate-100 text-slate-600'
                                            : n === 4 ? 'bg-sky-50 text-sky-700'
                                            : 'bg-violet-50 text-violet-700'
                                          }`}>
                                            P{n}
                                          </span>
                                          <span className="flex-1">{label}</span>
                                          {isCurrent && (
                                            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" className="text-emerald-600">
                                              <polyline points="20 6 9 17 4 12" />
                                            </svg>
                                          )}
                                        </button>
                                      );
                                    })}
                                  </div>
                                  <div className="px-3 py-1.5 text-[10px] text-slate-500 border-t border-slate-100 bg-slate-50/50">
                                    Higher priority (P1) runs jump the pool queue first.
                                  </div>
                                </div>
                              )}
                            </div>
                          );
                        })()}
                        <button onClick={() => handleDuplicate(p)} className="w-6 h-6 rounded flex items-center justify-center text-slate-400 hover:text-slate-600 hover:bg-slate-100" title="Copy Pipeline">
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
                        </button>
                        <button onClick={() => handleSaveAsTemplate(p)} className="w-6 h-6 rounded flex items-center justify-center text-slate-400 hover:text-violet-600 hover:bg-violet-50" title="Save as template">
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <rect x="3" y="3" width="7" height="7" rx="1" />
                            <rect x="14" y="3" width="7" height="7" rx="1" />
                            <rect x="3" y="14" width="7" height="7" rx="1" />
                            <path d="M14 17h7" />
                            <path d="M17.5 14v7" />
                          </svg>
                        </button>
                        {canDelete && (ns === 'draft' || ns === 'failed' || ns === 'archived') && (
                          <button onClick={() => handleDelete(p.id, p.name)} className="w-6 h-6 rounded flex items-center justify-center text-slate-400 hover:text-red-500 hover:bg-red-50" title="Delete">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6" /><path d="M19 6l-2 14H7L5 6" /><path d="M9 6V4h6v2" /></svg>
                          </button>
                        )}
                      </div>
                    );
                  })()}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>

    {/* Version History Panel */}
    {historyPipelineId && (
      <div className="w-80 bg-white border-l border-slate-300 flex flex-col overflow-hidden shrink-0">
        <div className="px-4 py-3 border-b border-slate-300 flex items-center justify-between shrink-0">
          <div className="min-w-0">
            <h2 className="text-sm font-bold text-slate-800 truncate">Version History</h2>
            <p className="text-xs text-slate-400 truncate">{historyPipelineName}</p>
          </div>
          <button onClick={() => setHistoryPipelineId(null)} className="text-slate-400 hover:text-slate-600 shrink-0">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
          </button>
        </div>

        <div className="flex-1 overflow-auto">
          {versionsLoading ? (
            <div className="flex items-center justify-center py-12">
              <div className="w-5 h-5 border-2 border-pipe-300 border-t-transparent rounded-full animate-spin" />
            </div>
          ) : versions.length === 0 ? (
            <div className="text-center py-12 px-4">
              <p className="text-xs text-slate-400">No version history available.</p>
            </div>
          ) : (
            <div className="p-3 space-y-0">
              {versions.map((v, idx) => (
                <div key={v.version} className="flex gap-3 group">
                  {/* Timeline */}
                  <div className="flex flex-col items-center shrink-0">
                    <div className={`w-6 h-6 rounded-full border flex items-center justify-center text-[9px] font-bold ${
                      idx === 0
                        ? 'bg-pipe-50 border-pipe-300 text-pipe-600'
                        : 'bg-slate-50 border-slate-200 text-slate-400'
                    }`}>
                      v{v.version}
                    </div>
                    {idx < versions.length - 1 && (
                      <div className="w-px flex-1 bg-slate-200 my-1" />
                    )}
                  </div>
                  {/* Version info */}
                  <div className="pb-4 flex-1 min-w-0">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-semibold text-slate-700">
                        {idx === 0 ? 'Current' : `Version ${v.version}`}
                      </span>
                      <div className="flex items-center gap-1.5 shrink-0">
                        {v.content_hash && (
                          <span
                            className="font-mono text-[9px] text-slate-500 bg-slate-100 border border-slate-200 rounded px-1.5 py-0.5 tracking-wide"
                            title={`Content hash: ${v.content_hash}\nSHA-256 of the pipeline definition. Stable across status/deploy changes; rollback verifies this matches before applying.`}
                          >
                            {v.content_hash.slice(0, 8)}
                          </span>
                        )}
                        <span className="text-xs text-slate-400">{v.step_count} steps</span>
                      </div>
                    </div>
                    {v.change_summary && (
                      <p className="text-xs text-slate-500 mt-0.5 truncate">{v.change_summary}</p>
                    )}
                    <div className="flex items-center gap-2 mt-1">
                      <span className="text-[9px] text-slate-400">
                        {new Date(v.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                      </span>
                      <span className="text-[9px] text-slate-300">by {v.created_by}</span>
                    </div>
                    {idx > 0 && (
                      <div className="flex items-center gap-1.5 mt-1.5">
                        <button
                          onClick={() => restoreVersion(v.version)}
                          disabled={restoringVersion === v.version}
                          className="px-2.5 py-1 text-xs font-medium text-pipe-600 bg-pipe-50 rounded-md hover:bg-pipe-100 transition-colors disabled:opacity-50"
                        >
                          {restoringVersion === v.version ? 'Restoring...' : 'Restore'}
                        </button>
                        <button
                          onClick={() => setCompareIdx(compareIdx === idx ? null : idx)}
                          className={`px-2.5 py-1 text-xs font-medium rounded-md transition-colors ${
                            compareIdx === idx ? 'text-blue-700 bg-blue-100' : 'text-slate-500 bg-slate-50 hover:bg-slate-100'
                          }`}
                        >
                          {compareIdx === idx ? 'Hide diff' : 'Compare'}
                        </button>
                      </div>
                    )}
                    {/* Inline diff view */}
                    {compareIdx === idx && idx > 0 && (() => {
                      const diff = computeDiff(versions[idx - 1], v);
                      const hasChanges = diff.addedSteps.length > 0 || diff.removedSteps.length > 0 || diff.addedConns !== 0;
                      return (
                        <div className="mt-2 bg-slate-50 rounded-lg border border-slate-200 p-2.5 text-xs">
                          <div className="font-bold text-slate-600 mb-1.5">v{v.version} → v{versions[idx - 1].version}</div>
                          {!hasChanges ? (
                            <div className="text-slate-400">No structural changes detected.</div>
                          ) : (
                            <div className="space-y-1">
                              {diff.addedSteps.map((s) => (
                                <div key={s} className="flex items-center gap-1.5 text-green-600">
                                  <span className="font-bold">+</span>
                                  <span>{s}</span>
                                </div>
                              ))}
                              {diff.removedSteps.map((s) => (
                                <div key={s} className="flex items-center gap-1.5 text-red-500">
                                  <span className="font-bold">−</span>
                                  <span className="line-through">{s}</span>
                                </div>
                              ))}
                              {diff.addedConns > 0 && (
                                <div className="text-green-600">+ {diff.addedConns} connection{diff.addedConns > 1 ? 's' : ''}</div>
                              )}
                              {diff.removedConns > 0 && (
                                <div className="text-red-500">− {diff.removedConns} connection{diff.removedConns > 1 ? 's' : ''}</div>
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })()}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    )}

    {/* Pipeline Detail Drawer (right-side slide-in, was centered modal).
        Migrated to <DetailDrawer> May 5 to keep the list visible behind
        the panel — master-detail UX. Inner DAG preview / progress / PreRunBanner
        content unchanged. */}
    {detailPipeline && (() => {
      const closeDrawer = () => { setDetailPipeline(null); setDetailData(null); };
      return (
        <DetailDrawer
          open={!!detailPipeline}
          onClose={closeDrawer}
          widthPx={720}
          pushContent
          ariaLabel="Pipeline details"
          title={
            <div className="flex items-center gap-2.5">
              <div className="w-8 h-8 rounded-md flex items-center justify-center shadow-sm shrink-0"
                style={{ background: 'linear-gradient(135deg, #6366f1, #4f46e5)' }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5">
                  <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
                </svg>
              </div>
              <span className="truncate">{detailPipeline.name}</span>
            </div>
          }
          subtitle={
            <span className="flex items-center gap-2 flex-wrap">
              <span className="font-mono">{detailPipeline.id.slice(0, 8)}</span>
              <span>·</span>
              <span>{detailData?.steps?.length || detailPipeline.steps?.length || detailPipeline.steps_count || 0} steps</span>
              <span>·</span>
              {statusBadge(detailPipeline.status)}
            </span>
          }
          footer={
            <div className="flex items-center justify-between gap-2">
              <div className="text-xs text-slate-400 flex flex-wrap gap-x-3 gap-y-0.5 min-w-0 flex-1">
                <span className="truncate">Created {detailPipeline.created_at ? new Date(detailPipeline.created_at).toLocaleDateString() : '—'}{detailPipeline.owner_name ? ` by ${detailPipeline.owner_name}` : detailPipeline.owner_id ? ` by ${detailPipeline.owner_id}` : ''}</span>
                {detailPipeline.updated_at && <span>Modified <TimeAgo value={detailPipeline.updated_at} className="!text-slate-400" /></span>}
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {normalizeStatus(detailPipeline.status) === 'draft' && (
                  <button
                    onClick={() => { handleLifecycleAction(detailPipeline.id, 'publish'); closeDrawer(); }}
                    className="px-3 py-1.5 text-xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg hover:bg-emerald-100 transition-colors flex items-center gap-1"
                  >
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="20 6 9 17 4 12" /></svg>
                    Publish
                  </button>
                )}
                {tier === 'plus' && normalizeStatus(detailPipeline.status) === 'published' && (
                  <button
                    onClick={() => { handleSubmitForReview(detailPipeline.id, detailPipeline.name); closeDrawer(); }}
                    className="px-3 py-1.5 text-xs font-semibold text-amber-700 bg-amber-50 border border-amber-200 rounded-lg hover:bg-amber-100 transition-colors flex items-center gap-1"
                  >
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 2L11 13" /><path d="M22 2l-7 20-4-9-9-4 20-7z" /></svg>
                    Submit for Deploy
                  </button>
                )}
                {/* Revoke — only on published pipelines, mirrors the
                    row-level Revoke button. */}
                {normalizeStatus(detailPipeline.status) === 'published' && (
                  <button
                    onClick={() => { handleLifecycleAction(detailPipeline.id, 'revoke'); closeDrawer(); }}
                    className="px-3 py-1.5 text-xs font-semibold text-slate-700 bg-white border border-slate-300 rounded-lg hover:bg-slate-50 transition-colors flex items-center gap-1"
                    title="Revoke — move back to draft (stops schedules and triggers)"
                  >
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" /><path d="M3 3v5h5" /></svg>
                    Revoke
                  </button>
                )}
                <button
                  onClick={() => handleRunPipeline(detailPipeline.id, detailPipeline.name)}
                  className="px-3 py-1.5 text-xs font-semibold text-blue-700 bg-blue-50 border border-blue-200 rounded-lg hover:bg-blue-100 transition-colors flex items-center gap-1"
                >
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="5 3 19 12 5 21 5 3" /></svg>
                  Run
                </button>
                <button
                  onClick={() => handleDuplicate(detailPipeline)}
                  className="px-3 py-1.5 text-xs font-semibold text-slate-700 bg-slate-50 border border-slate-200 rounded-lg hover:bg-slate-100 transition-colors"
                >
                  Duplicate
                </button>
                <button
                  onClick={() => { handleOpen(detailPipeline.id); closeDrawer(); }}
                  className="px-3 py-1.5 text-xs font-bold text-white rounded-lg shadow-sm hover:shadow-md transition-all flex items-center gap-1"
                  style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
                >
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" /><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" /></svg>
                  Edit in Editor
                </button>
              </div>
            </div>
          }
        >
          {/* Body — React Flow canvas preview (kept as-is). */}
          <div className="-mx-5 -mt-5 mb-3 overflow-hidden" style={{ minHeight: '400px', height: 'calc(100vh - 320px)' }}>
            {detailLoading ? (
              <div className="flex items-center justify-center h-full">
                <div className="w-6 h-6 border-2 border-pipe-300 border-t-transparent rounded-full animate-spin" />
              </div>
            ) : detailData?.steps && detailData.steps.length > 0 ? (
              (() => {
                const dpId = detailPipeline.id;
                const isRunning = !!runningPipelines[dpId];
                const stepRes = pipelineStepResults[dpId];
                const { nodes: previewNodes, edges: previewEdges } = previewWorkflowToFlow(detailData, isRunning, stepRes);

                // Progress calculation
                const totalSteps = detailData.steps.length;
                let completedSteps = 0;
                let failedSteps = 0;
                if (stepRes && !stepRes.__all_running) {
                  for (const s of detailData.steps) {
                    const r = stepRes[s.id];
                    if (r?.status === 'success') completedSteps++;
                    else if (r?.status === 'error') failedSteps++;
                  }
                }
                const progressPct = totalSteps > 0 ? Math.round(((completedSteps + failedSteps) / totalSteps) * 100) : 0;

                return (
                  <div className="h-full w-full flex flex-col">
                    {/* Progress bar — visible when running or has results */}
                    {(isRunning || (stepRes && !stepRes.__all_running && Object.keys(stepRes).length > 0)) && (
                      <div className="px-4 py-2 bg-slate-50 border-b border-slate-200 shrink-0">
                        <div className="flex items-center justify-between mb-1.5">
                          <div className="flex items-center gap-2">
                            {isRunning ? (
                              <>
                                <div className="w-3 h-3 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />
                                <span className="text-xs font-semibold text-blue-600">Running pipeline...</span>
                              </>
                            ) : failedSteps > 0 ? (
                              <>
                                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#ef4444" strokeWidth="2.5"><circle cx="12" cy="12" r="10" /><line x1="15" y1="9" x2="9" y2="15" /><line x1="9" y1="9" x2="15" y2="15" /></svg>
                                <span className="text-xs font-semibold text-red-600">Completed with {failedSteps} error(s)</span>
                              </>
                            ) : (
                              <>
                                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#22c55e" strokeWidth="2.5"><polyline points="20 6 9 17 4 12" /></svg>
                                <span className="text-xs font-semibold text-emerald-600">All steps completed</span>
                              </>
                            )}
                          </div>
                          <span className="text-xs font-bold text-slate-500">
                            {isRunning && stepRes?.__all_running ? '0' : progressPct}%
                            <span className="font-normal text-slate-400 ml-1">({completedSteps}/{totalSteps} steps)</span>
                          </span>
                        </div>
                        <div className="w-full h-2 bg-slate-200 rounded-full overflow-hidden">
                          <div
                            className={`h-full rounded-full transition-all duration-500 ${
                              isRunning ? 'bg-blue-500 animate-pulse' :
                              failedSteps > 0 ? 'bg-red-500' : 'bg-emerald-500'
                            }`}
                            style={{ width: `${isRunning && stepRes?.__all_running ? 15 : progressPct}%` }}
                          />
                        </div>
                      </div>
                    )}

                    {/* React Flow canvas */}
                    <div className="flex-1">
                      <ReactFlow
                        nodes={previewNodes}
                        edges={previewEdges}
                        nodeTypes={nodeTypes}
                        edgeTypes={edgeTypes}
                        fitView
                        fitViewOptions={{ padding: 0.4 }}
                        proOptions={{ hideAttribution: true }}
                        nodesDraggable={false}
                        nodesConnectable={false}
                        elementsSelectable={false}
                        panOnDrag={true}
                        zoomOnScroll={true}
                        preventScrolling={true}
                        defaultEdgeOptions={{
                          type: 'custom' as const,
                          animated: isRunning,
                          style: { stroke: '#6366f1', strokeWidth: 2 },
                          markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#6366f1' },
                        }}
                      >
                        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#e2e8f0" />
                      </ReactFlow>
                    </div>
                  </div>
                );
              })()
            ) : (
              <div className="flex items-center justify-center h-full">
                <div className="text-center">
                  <div className="mb-2 flex justify-center text-slate-400">
                    <Icon name="list" size={32} strokeWidth={1.5} />
                  </div>
                  <p className="text-sm font-medium text-slate-500">No steps in this pipeline</p>
                  <p className="text-xs text-slate-400 mt-1">Open in the Editor to start building</p>
                  <button
                    onClick={() => { handleOpen(detailPipeline.id); setDetailPipeline(null); setDetailData(null); }}
                    className="mt-4 px-5 py-2 text-xs font-bold text-white rounded-lg shadow-sm"
                    style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
                  >
                    Open in Editor
                  </button>
                </div>
              </div>
            )}
          </div>

          {/* Pre-run banner — last run + estimate + Run safely toggle. */}
          <div className="mt-3">
            <PreRunBanner
              workflowId={detailPipeline.id}
              value={preRunMode}
              onChange={setPreRunMode}
              pipelineStatus={detailPipeline.status}
            />
          </div>
        </DetailDrawer>
      );
    })()}

    {/* Quick Schedule Dialog */}
    {quickSchedule && (
      <QuickScheduleDialog
        pipelineId={quickSchedule.id}
        pipelineName={quickSchedule.name}
        onConfigureAlerts={(id, name) => setQuickAlert({ id, name })}
        onClose={() => {
          setQuickSchedule(null);
          // Re-run pre-deploy validation if the dialog is still open
          if (preDeployDialog && preDeployDialog.id === quickSchedule.id) {
            openPreDeployCheck(preDeployDialog, preDeployDialog.version);
          }
        }}
      />
    )}

    {/* Quick Alert Dialog */}
    {quickAlert && (
      <QuickAlertDialog
        pipelineId={quickAlert.id}
        pipelineName={quickAlert.name}
        onClose={() => {
          setQuickAlert(null);
          // Re-run pre-deploy validation if the dialog is still open
          if (preDeployDialog && preDeployDialog.id === quickAlert.id) {
            openPreDeployCheck(preDeployDialog, preDeployDialog.version);
          }
        }}
      />
    )}

    {/* ── Delete / Archive Modal ── */}
    {deleteModal && (
      <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setDeleteModal(null)}>
        <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm mx-4" onClick={(e) => e.stopPropagation()}>
          <div className="px-6 py-5">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-full bg-red-50 flex items-center justify-center shrink-0">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#ef4444" strokeWidth="2"><polyline points="3 6 5 6 21 6" /><path d="M19 6l-2 14H7L5 6" /><path d="M10 11v6" /><path d="M14 11v6" /><path d="M9 6V4h6v2" /></svg>
              </div>
              <div>
                <h3 className="text-sm font-bold text-slate-800">
                  {deleteModal.id === '__bulk__' ? 'Remove Pipelines' : 'Remove Pipeline'}
                </h3>
                <p className="text-xs text-slate-500 mt-0.5">
                  {deleteModal.id === '__bulk__' ? (
                    <>What would you like to do with <span className="font-semibold text-slate-700">{deleteModal.name}</span>?</>
                  ) : (
                    <>What would you like to do with <span className="font-semibold text-slate-700">"{deleteModal.name}"</span>?</>
                  )}
                </p>
              </div>
            </div>

            <div className="space-y-2 mb-5">
              {/* Archive option — hidden on bulk because the handler is a silent no-op
                  ("Bulk archive not supported yet"). Surfacing only the supported
                  action prevents the confused "I clicked but nothing happened" flow. */}
              {deleteModal.id !== '__bulk__' && (
                <button
                  onClick={() => confirmArchive()}
                  className="w-full flex items-center gap-3 px-4 py-3 rounded-lg border border-slate-200 hover:border-amber-300 hover:bg-amber-50/50 transition-all text-left group"
                >
                  <div className="w-8 h-8 rounded-lg bg-amber-50 flex items-center justify-center shrink-0 group-hover:bg-amber-100">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#d97706" strokeWidth="2"><path d="M21 8v13H3V8" /><rect x="1" y="3" width="22" height="5" /><line x1="10" y1="12" x2="14" y2="12" /></svg>
                  </div>
                  <div>
                    <div className="text-xs font-semibold text-slate-700">Archive</div>
                    <div className="text-xs text-slate-400">Move to Archived. Can be restored anytime.</div>
                  </div>
                </button>
              )}

              {/* Delete to Bin option */}
              <button
                onClick={() => {
                  if (deleteModal.id === '__bulk__') {
                    confirmBulkDelete();
                  } else {
                    confirmDelete();
                  }
                }}
                className="w-full flex items-center gap-3 px-4 py-3 rounded-lg border border-slate-200 hover:border-red-300 hover:bg-red-50/50 transition-all text-left group"
              >
                <div className="w-8 h-8 rounded-lg bg-red-50 flex items-center justify-center shrink-0 group-hover:bg-red-100">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#ef4444" strokeWidth="2"><polyline points="3 6 5 6 21 6" /><path d="M19 6l-2 14H7L5 6" /><path d="M9 6V4h6v2" /></svg>
                </div>
                <div>
                  <div className="text-xs font-semibold text-red-600">Delete</div>
                  <div className="text-xs text-slate-400">
                    Move to Bin. Auto-removed after 30 days (Free) or backed up (Plus).
                  </div>
                </div>
              </button>
            </div>

            <button
              onClick={() => setDeleteModal(null)}
              className="w-full py-2 text-xs font-medium text-slate-500 hover:text-slate-700 transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    )}

    {/* ── Import Pipeline Modal ── */}
    {importModalOpen && importFile && (
      <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setImportModalOpen(false)}>
        <div className="bg-white rounded-2xl shadow-2xl w-full max-w-xl mx-4" onClick={(e) => e.stopPropagation()}>
          {/* Header */}
          <div className="px-6 py-4 border-b border-slate-200 flex items-center justify-between">
            <div>
              <h2 className="text-sm font-bold text-slate-800">Import Pipeline</h2>
              <p className="text-xs text-slate-400 mt-0.5">
                {importFileName} &middot; {importFile.pipeline?.steps?.length || 0} steps
              </p>
            </div>
            <button onClick={() => setImportModalOpen(false)} className="text-slate-400 hover:text-slate-600">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
            </button>
          </div>

          {/* Body */}
          <div className="px-6 py-5 space-y-4 max-h-[60vh] overflow-auto">
            {/* Pipeline Name */}
            <div>
              <label className="block text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">Pipeline Name</label>
              <input
                type="text"
                value={importRename}
                onChange={(e) => setImportRename(e.target.value)}
                className="w-full px-3 py-2 text-xs border border-slate-200 rounded-lg"
              />
            </div>

            {/* Target Project */}
            <div>
              <label className="block text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">Target Project</label>
              <select
                value={importProjectId}
                onChange={(e) => setImportProjectId(e.target.value)}
                className="w-full px-3 py-2 text-xs border border-slate-200 rounded-lg"
              >
                <option value="default">Default Project</option>
                {importProjects.map((p: any) => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
              </select>
            </div>

            {/* Connection Remapping */}
            {importConnRefs.length > 0 && (
              <div>
                <label className="block text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">
                  Connection Remapping
                </label>
                <p className="text-xs text-slate-400 mb-3">
                  This pipeline references connections from another system. Map them to credentials on this server.
                </p>
                <div className="space-y-2">
                  {importConnRefs.map((ref) => (
                    <div key={ref} className="flex items-center gap-2">
                      <div className="flex-1 px-3 py-2 text-xs bg-slate-50 border border-slate-200 rounded-lg text-slate-600 font-mono truncate">
                        {ref}
                      </div>
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-400 shrink-0"><polyline points="9 18 15 12 9 6" /></svg>
                      <select
                        value={importConnMap[ref] || ''}
                        onChange={(e) => setImportConnMap(prev => ({ ...prev, [ref]: e.target.value }))}
                        className="flex-1 px-3 py-2 text-xs border border-slate-200 rounded-lg"
                      >
                        <option value="">— Keep original —</option>
                        {importCredentials.map((c: any) => (
                          <option key={c.id} value={c.id}>{c.name} ({c.type})</option>
                        ))}
                      </select>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Preview */}
            <div className="bg-slate-50 rounded-lg p-3 border border-slate-200">
              <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Preview</div>
              <div className="grid grid-cols-3 gap-3 text-center">
                <div>
                  <div className="text-lg font-bold text-slate-700">{importFile.pipeline?.steps?.length || 0}</div>
                  <div className="text-xs text-slate-400">Steps</div>
                </div>
                <div>
                  <div className="text-lg font-bold text-slate-700">{importFile.pipeline?.connections?.length || 0}</div>
                  <div className="text-xs text-slate-400">Connections</div>
                </div>
                <div>
                  <div className="text-lg font-bold text-slate-700">{importConnRefs.length}</div>
                  <div className="text-xs text-slate-400">Credentials</div>
                </div>
              </div>
              {importFile.fpulse_version && (
                <div className="text-xs text-slate-400 mt-2 text-center">
                  Exported from F-Pulse v{importFile.fpulse_version}
                  {importFile.exported_at && ` on ${new Date(importFile.exported_at).toLocaleDateString()}`}
                </div>
              )}
            </div>
          </div>

          {/* Footer */}
          <div className="px-6 py-4 border-t border-slate-200 flex items-center justify-end gap-3">
            <button
              onClick={() => setImportModalOpen(false)}
              className="px-4 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-50 rounded-lg"
            >
              Cancel
            </button>
            <button
              onClick={handleImportConfirm}
              disabled={importing}
              className="px-5 py-2 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-500 rounded-lg disabled:opacity-50 transition-colors"
            >
              {importing ? 'Importing...' : 'Import to DEV'}
            </button>
          </div>
        </div>
      </div>
    )}

    {/* Plan modal — pre-Submit-for-Review preview of the diff vs. the
        currently-deployed version. Hash, summary, validator, and a
        bounded recent-runs baseline. */}
    <PlanModal
      open={planTarget !== null}
      onClose={() => setPlanTarget(null)}
      workflowId={planTarget?.id || ''}
      workflowName={planTarget?.name}
      against="deployed"
      confirmLabel="Submit for Review"
      onConfirm={handlePlanConfirm}
      busy={planSubmitting}
    />

    {/* Run-with-parameters modal — opens automatically when the user clicks
        Run on a pipeline that declares parameters. The modal
        pre-fills declared defaults; the user edits and confirms; we then
        re-call handleRunPipeline with parameter_values bypassing the
        no-params shortcut. */}
    {runWithParamsTarget && (
      <RunWithParametersDialog
        open={runWithParamsTarget !== null}
        onClose={() => setRunWithParamsTarget(null)}
        workflowName={runWithParamsTarget.name}
        parameters={runWithParamsTarget.parameters}
        busy={!!runningPipelines[runWithParamsTarget.id]}
        onRun={(values) => {
          const target = runWithParamsTarget;
          setRunWithParamsTarget(null);
          if (target) {
            handleRunPipeline(target.id, target.name, values);
          }
        }}
      />
    )}

    {/* Pre-publish test card. Runs /test on open, shows per-step results,
        and only fires /publish after the user confirms a passing test.
        On a failure the user can retry or jump straight into the editor. */}
    <PublishTestModal
      open={publishTarget !== null}
      workflowId={publishTarget?.id ?? null}
      workflowName={publishTarget?.name ?? ''}
      onClose={() => setPublishTarget(null)}
      onPublished={() => fetchPipelines()}
    />

    {/* Save-as-template modal — opens from each row's "Save as template"
        action. Persists into the user_templates table; the saved template
        appears in the Templates gallery alongside the built-in catalog. */}
    <SaveAsTemplateDialog
      open={saveTplState.open}
      pipelineName={saveTplState.pipelineName}
      steps={saveTplState.steps}
      connections={saveTplState.connections}
      existingNames={saveTplState.existingNames}
      onClose={() => setSaveTplState((s) => ({ ...s, open: false }))}
      onSaved={() => { /* listing will refresh on next gallery visit */ }}
    />

    {/* Priority popover — rendered in a portal so the menu floats above
        the table and never gets clipped by row/table overflow. Anchor
        coords are computed from the trigger button's bounding rect at
        open time; placement flips to 'above' when the row is near the
        bottom of the viewport. */}
    {priorityMenuFor && priorityAnchor && (() => {
      const target = pipelines.find((x) => x.id === priorityMenuFor);
      if (!target) return null;
      const currentPriority = (target.metadata?.priority as number | undefined) ?? 3;
      return createPortal(
        <div
          data-priority-menu="true"
          className="fixed z-[9999] rounded-md border border-slate-200 bg-white shadow-lg min-w-[220px]"
          style={{ top: priorityAnchor.top, left: priorityAnchor.left }}
        >
          <div className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider text-slate-500 border-b border-slate-100 bg-slate-50">
            Run priority
          </div>
          <div className="py-1">
            {[1, 2, 3, 4, 5].map((n) => {
              const optLabel = n === 1 ? 'Critical' : n === 2 ? 'High' : n === 3 ? 'Normal' : n === 4 ? 'Low' : 'Background';
              const isCurrent = n === currentPriority;
              return (
                <button
                  key={n}
                  onClick={() => handleSetPriority(target, n)}
                  className={`w-full text-left px-3 py-1.5 text-xs flex items-center gap-2 hover:bg-slate-50 ${isCurrent ? 'font-bold text-slate-800' : 'text-slate-600'}`}
                >
                  <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
                    n === 1 ? 'bg-red-50 text-red-700'
                    : n === 2 ? 'bg-orange-50 text-orange-700'
                    : n === 3 ? 'bg-slate-100 text-slate-600'
                    : n === 4 ? 'bg-sky-50 text-sky-700'
                    : 'bg-violet-50 text-violet-700'
                  }`}>
                    P{n}
                  </span>
                  <span className="flex-1">{optLabel}</span>
                  {isCurrent && (
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" className="text-emerald-600">
                      <polyline points="20 6 9 17 4 12" />
                    </svg>
                  )}
                </button>
              );
            })}
          </div>
          <div className="px-3 py-1.5 text-[10px] text-slate-500 border-t border-slate-100 bg-slate-50/50">
            Higher priority (P1) jumps the pool queue first.
          </div>
        </div>,
        document.body
      );
    })()}

    </div>
    </div>
  );
}

/**
 * VersionChangeDetail
 * ─────────────────────────────────────────────────────────────────────
 * Expanded panel for the `version_change` pre-deploy check. Shows the
 * current detail line AND a version picker — admins can scroll through
 * all saved versions of the pipeline and select any one to deploy
 * (redeploy / rollback flow). Selecting a version re-runs the whole
 * pre-deploy validation against that specific snapshot so every other
 * panel (steps, connections, approvals) reflects the chosen version's
 * state.
 *
 * Lazy-loads the version list on first open — we don't want to eat
 * API calls for pipelines whose admin never expands this row.
 */
function VersionChangeDetail(props: {
  check: any;
  pipelineId: string;
  currentVersion?: number;
  versions: any[];
  loading: boolean;
  onLoad: () => void;
  onSelect: (version: number) => void;
}) {
  const { check, currentVersion, versions, loading, onLoad, onSelect } = props;
  useEffect(() => { onLoad(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);
  const latest = versions[0]?.version;
  return (
    <div className="space-y-2">
      <p className="text-[9px] font-bold text-slate-500 uppercase tracking-wider">Version Info</p>
      <div className={`flex items-center gap-2 px-2 py-1.5 rounded border text-xs ${
        check.status === 'pass' ? 'bg-emerald-50 border-emerald-100' : 'bg-amber-50 border-amber-100'
      }`}>
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke={check.status === 'pass' ? '#10b981' : '#f59e0b'} strokeWidth="2">
          <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
        </svg>
        <span className={check.status === 'pass' ? 'text-emerald-700' : 'text-amber-700'}>{check.detail}</span>
      </div>

      {/* Version picker — lets admin redeploy ANY saved version, not
          just the latest. Common use case: something went wrong with
          the latest deploy, roll back to v(N-1) without reverting the
          source of truth in the editor. */}
      <div className="bg-white rounded border border-slate-200 p-2">
        <p className="text-[9px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">Choose Version to Deploy</p>
        {loading ? (
          <div className="flex items-center justify-center py-3 gap-2">
            <span className="w-3 h-3 border-2 border-slate-200 border-t-slate-500 rounded-full animate-spin" />
            <span className="text-xs text-slate-500">Loading versions…</span>
          </div>
        ) : versions.length === 0 ? (
          <p className="text-xs text-slate-400 italic py-2">No prior versions found — this will be the first deployment.</p>
        ) : (
          <div className="max-h-[160px] overflow-auto space-y-1">
            {versions.map((v: any) => {
              const isCurrent = v.version === currentVersion;
              const isLatest = v.version === latest;
              return (
                <button
                  key={v.version}
                  onClick={() => onSelect(v.version)}
                  className={`w-full flex items-center gap-2 px-2 py-1.5 text-xs rounded border transition-colors text-left ${
                    isCurrent
                      ? 'bg-indigo-50 border-indigo-300 text-indigo-800'
                      : 'bg-white border-slate-200 hover:bg-slate-50 text-slate-700'
                  }`}
                >
                  <span className={`font-mono font-bold ${isCurrent ? 'text-indigo-700' : 'text-slate-800'}`}>v{v.version}</span>
                  {isLatest && <span className="text-xs font-bold px-1.5 py-0.5 rounded-full bg-emerald-100 text-emerald-700 uppercase tracking-wider">Latest</span>}
                  {isCurrent && <span className="text-xs font-bold px-1.5 py-0.5 rounded-full bg-indigo-100 text-indigo-700 uppercase tracking-wider">Selected</span>}
                  {v.content_hash && (
                    <span
                      className="font-mono text-[9px] text-slate-500 bg-slate-100 border border-slate-200 rounded px-1 py-0.5 tracking-wide shrink-0"
                      title={`Content hash: ${v.content_hash}`}
                    >
                      {v.content_hash.slice(0, 8)}
                    </span>
                  )}
                  <span className="text-slate-500 truncate flex-1">{v.change_summary || v.created_by || 'no summary'}</span>
                  <span className="text-slate-400 text-[9px] tabular-nums shrink-0">{v.created_at ? new Date(v.created_at).toLocaleDateString() : ''}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>
      <p className="text-[9px] text-slate-400 mt-1">Each deployment creates a version snapshot. Pick any prior version to redeploy or rollback &mdash; the pre-deploy checks above re-run against the chosen snapshot.</p>
    </div>
  );
}

/**
 * DagLineagePreview
 * ─────────────────────────────────────────────────────────────────────
 * Read-only ReactFlow rendering of a workflow's nodes + edges. Used by
 * the DAG Lineage modal in the pre-deploy dialog so admins can visually
 * confirm the pipeline structure before approving the deploy.
 *
 * Hydrates from the same `workflow.steps` + `workflow.connections`
 * shape the editor uses, so it renders identically to what the user
 * last saved in the Editor — no divergence between "what admin sees"
 * and "what gets deployed".
 */
function DagLineagePreview({ workflow }: { workflow: any }) {
  const { nodes, edges } = useMemo(() => {
    const wf = workflow?.workflow || workflow;
    const steps = wf?.steps || [];
    const connections = wf?.connections || [];
    const ns: FlowNode[] = steps.map((s: any, i: number) => ({
      id: s.id,
      type: 'fpulseNode',
      position: s.position || { x: 100 + (i % 5) * 260, y: 100 + Math.floor(i / 5) * 150 },
      data: {
        label: s.label || s.type || s.id,
        stepType: s.type || 'source',
        params: s.params || {},
        color: '#94a3b8',
        icon: '⬜',
        category: 'transform',
        risk: 'low',
      },
      draggable: false,
      selectable: false,
      connectable: false,
    }));
    const es: Edge[] = connections.map((c: any, i: number) => ({
      id: `preview-e-${i}-${c.from_step}-${c.to_step}`,
      source: c.from_step,
      target: c.to_step,
      type: 'custom',
      animated: false,
      data: { condition: c.condition || 'completion' },
      style: { stroke: '#6366f1', strokeWidth: 2 },
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: '#6366f1' },
    }));
    return { nodes: ns, edges: es };
  }, [workflow]);

  return (
    <div className="w-full h-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        fitView
        fitViewOptions={{ padding: 0.2, minZoom: 0.25, maxZoom: 1.5 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnDrag
        zoomOnScroll
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="#cbd5e1" />
      </ReactFlow>
    </div>
  );
}
