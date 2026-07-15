import { useState, useEffect } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { api } from '../api/client';
import { toast } from './Toast';

const SCHEDULE_PRESETS = [
  { label: 'Every hour', cron: '0 * * * *' },
  { label: 'Every 6 hours', cron: '0 */6 * * *' },
  { label: 'Daily at midnight', cron: '0 0 * * *' },
  { label: 'Daily at 6 AM', cron: '0 6 * * *' },
  { label: 'Weekly (Mon)', cron: '0 0 * * 1' },
  { label: 'Custom', cron: '' },
];

const ALERT_CONDITIONS = [
  { value: 'on_failure', label: 'On Failure', color: 'text-red-500', icon: '!' },
  { value: 'on_success', label: 'On Success', color: 'text-green-500', icon: '\u2713' },
  { value: 'on_sla_breach', label: 'On SLA Breach', color: 'text-amber-500', icon: '\u23f1' },
  { value: 'on_long_running', label: 'On Long Running', color: 'text-orange-500', icon: '\u23f3' },
  { value: 'on_overlap', label: 'On Overlap', color: 'text-purple-500', icon: '\u21c4' },
];

const ALERT_CHANNELS = [
  { value: 'email', label: 'Email' },
  { value: 'slack', label: 'Slack' },
  { value: 'teams', label: 'Teams' },
  { value: 'webhook', label: 'Webhook' },
];

const OVERLAP_POLICIES = [
  { value: 'skip', label: 'Skip', desc: 'Skip the new run if previous is still running', icon: '\u23ed' },
  { value: 'queue', label: 'Queue', desc: 'Queue the new run and execute after current finishes', icon: '\u23f3' },
  { value: 'parallel', label: 'Run Parallel', desc: 'Allow concurrent runs (may cause resource issues)', icon: '\u2016' },
  { value: 'cancel_previous', label: 'Cancel Previous', desc: 'Cancel the running execution and start fresh', icon: '\u2716' },
];

const MAX_RUNTIME_PRESETS = [
  { label: '5 min', minutes: 5 },
  { label: '15 min', minutes: 15 },
  { label: '30 min', minutes: 30 },
  { label: '1 hr', minutes: 60 },
  { label: '2 hr', minutes: 120 },
  { label: '6 hr', minutes: 360 },
  { label: '12 hr', minutes: 720 },
  { label: '24 hr', minutes: 1440 },
];

interface SaveDialogProps {
  open: boolean;
  onClose: () => void;
}

type Tab = 'details' | 'execution' | 'schedule' | 'alerts';

export default function SaveDialog({ open, onClose }: SaveDialogProps) {
  const workflowId = useWorkflowStore((s) => s.workflowId);
  const workflowName = useWorkflowStore((s) => s.workflowName);
  const nodes = useWorkflowStore((s) => s.nodes);
  const edges = useWorkflowStore((s) => s.edges);
  const ensureWorkflow = useWorkflowStore((s) => s.ensureWorkflow);
  const setWorkflowName = useWorkflowStore((s) => s.setWorkflowName);

  const [tab, setTab] = useState<Tab>('details');
  const [saving, setSaving] = useState(false);
  const [projects, setProjects] = useState<any[]>([]);

  // Details form
  const [name, setName] = useState(workflowName);
  const [description, setDescription] = useState('');
  const [projectId, setProjectId] = useState('default');
  // 2026-05-22: `status` state intentionally removed — see B2.
  // Status is server-owned; the Save Dialog never sends it.
  const [tags, setTags] = useState('');

  // Execution settings
  const [maxRuntimeMinutes, setMaxRuntimeMinutes] = useState(0); // 0 = no limit
  const [customRuntime, setCustomRuntime] = useState('');
  const [overlapPolicy, setOverlapPolicy] = useState('skip');
  const [enableTimeout, setEnableTimeout] = useState(false);
  const [enableOverlapDetection, setEnableOverlapDetection] = useState(true);

  // Schedule form
  const [enableSchedule, setEnableSchedule] = useState(false);
  const [schedulePreset, setSchedulePreset] = useState('');
  const [cronExpression, setCronExpression] = useState('');
  const [timezone, setTimezone] = useState('UTC');

  // Alert form
  const [enableAlert, setEnableAlert] = useState(false);
  const [alertConditions, setAlertConditions] = useState<string[]>(['on_failure']);
  const [alertChannel, setAlertChannel] = useState('email');
  const [alertTarget, setAlertTarget] = useState('');

  useEffect(() => {
    if (open) {
      setName(workflowName);
      api.listProjects().then(p => setProjects(Array.isArray(p) ? p : [])).catch(() => {});
    }
  }, [open, workflowName]);

  if (!open) return null;

  const toggleAlertCondition = (val: string) => {
    setAlertConditions(prev => {
      const has = prev.includes(val);
      const next = has ? prev.filter(c => c !== val) : [...prev, val];
      return next.length > 0 ? next : [val];
    });
  };

  const formatRuntime = (mins: number): string => {
    if (mins === 0) return 'No limit';
    if (mins < 60) return `${mins} minute${mins > 1 ? 's' : ''}`;
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    if (m === 0) return `${h} hour${h > 1 ? 's' : ''}`;
    return `${h}h ${m}m`;
  };

  const handleSave = async () => {
    const trimmed = name.trim();
    if (!trimmed) { toast.warning('Enter a pipeline name'); return; }
    // Reject the placeholder so the workflow list never collects
    // anonymous rows (memory rule 2026-05-09).
    if (trimmed.toLowerCase() === 'untitled pipeline') {
      toast.warning('Pick a real name', 'The placeholder "Untitled Pipeline" isn\'t allowed — pick something descriptive.');
      return;
    }
    setSaving(true);
    try {
      // Duplicate-name guard for first-save only. Edits to an existing
      // pipeline can keep its name; only NEW rows are blocked from
      // colliding with an existing pipeline's name.
      if (!workflowId) {
        try {
          const wfs = await api.listWorkflows();
          const taken = (wfs || [])
            .map((w: any) => String(w?.name || '').trim().toLowerCase())
            .filter(Boolean);
          if (taken.includes(trimmed.toLowerCase())) {
            toast.warning(
              'Name already used',
              `"${trimmed}" is already used by another pipeline. Pick a different name.`,
            );
            setSaving(false);
            return;
          }
        } catch {
          // Network glitch — let the backend make the final call.
        }
      }
      if (name !== workflowName) setWorkflowName(name);
      // `allowCreate: true` — SaveDialog is an explicit user Save action,
      // the only place (besides Toolbar Save / Modal Save+Close) where
      // creating a new pipeline row is permitted.
      const wfId = await ensureWorkflow({ allowCreate: true });
      if (!wfId) throw new Error('Failed to save workflow');

      // 2026-05-22 — merge, don't rebuild. The previous implementation
      // constructed a fresh `wf` object from form fields only, which
      // silently wiped any field the form doesn't know about:
      //   * pipeline parameters (the ${param.X} declarations)
      //   * folder_id assignment
      //   * existing metadata keys not represented in the dialog
      //   * test_results / owner / approval state (now server-protected
      //     by A1 lifecycle-field lock-down, but defence in depth)
      // The audit (PROJECT_PIPELINE_CONFIGURATION_VALIDATION.md B1)
      // called this out as a silent data-loss bug.
      //
      // New shape: fetch the current workflow, merge ONLY the fields
      // the user actually edited in this dialog, then PUT the merged
      // blob. Backend A1 lock-down strips lifecycle fields server-side
      // so even if we sent them, they wouldn't take effect — but we
      // don't send them anyway so the request body stays minimal and
      // auditable.
      let existing: any = null;
      try {
        const fetched = await api.getWorkflow(wfId);
        existing = (fetched && (fetched.workflow || fetched)) || null;
      } catch {
        // Shouldn't happen — ensureWorkflow just confirmed the id —
        // but if it does, fall through with a minimal merge target so
        // a transient read failure doesn't block save entirely. Steps/
        // edges still get persisted; we just lose the field-preservation
        // benefit for this one save.
        existing = {};
      }

      const mergedMetadata = {
        ...(existing?.metadata || {}),
        tags: tags ? tags.split(',').map(t => t.trim()).filter(Boolean)
                    : (existing?.metadata?.tags || []),
        execution_settings: {
          ...(existing?.metadata?.execution_settings || {}),
          max_runtime_minutes: enableTimeout ? maxRuntimeMinutes : 0,
          overlap_policy: enableOverlapDetection ? overlapPolicy : 'parallel',
          enable_timeout: enableTimeout,
          enable_overlap_detection: enableOverlapDetection,
        },
      };

      const wf = {
        // Preserve every field the dialog isn't editing — parameters,
        // folder_id, owner_id, lifecycle (also server-stripped by A1),
        // approval state, test_results, deployed_version, etc.
        ...(existing || {}),
        // Only the fields this dialog actually edits override.
        id: wfId,
        name: name.trim(),
        description,
        project_id: projectId,
        metadata: mergedMetadata,
        steps: nodes.map((n) => ({
          id: n.id,
          type: n.data.stepType,
          label: n.data.label,
          params: n.data.params || {},
          position: { x: n.position.x, y: n.position.y },
          risk: n.data.risk || 'low',
        })),
        connections: edges.map((e) => ({
          from_step: e.source,
          to_step: e.target,
        })),
      };
      // 2026-05-22: do NOT send status. Server-owned via A1. If the
      // SaveDialog still has a status dropdown (B2 removes it), it
      // would be silently dropped by the backend anyway, but keeping
      // it out of the body makes the contract explicit.
      delete (wf as any).status;
      await api.updateWorkflow(wfId, wf, 'Save with metadata');
      // 2026-06-05 — When the user has scan_on_save enabled (default),
      // the StewardBadge listens for this event and refreshes findings
      // immediately so a duplicate-source flagged by THIS save shows up
      // without waiting for the 60s poll. Steward off → setting=false →
      // event is a no-op consumer-side. Cheap to always dispatch.
      window.dispatchEvent(new CustomEvent('fpulse:steward-refresh'));

      // 2026-05-22 (audit D3): upsert instead of create. The previous
      // code did `api.createSchedule` / `api.createAlertRule` on
      // every save, so a user who hit Save twice ended up with two
      // identical cron rows and two identical alert rules pointed at
      // the same workflow. The upsert lane updates the workflow's
      // single "default" record in place; manually-created secondary
      // schedules / alerts are unaffected.
      if (enableSchedule && cronExpression) {
        try {
          await api.upsertDefaultSchedule(wfId, {
            workflow_id: wfId,
            project_id: projectId,
            name: `${name} - Schedule`,
            schedule_type: 'cron',
            cron_expression: cronExpression,
            timezone,
            enabled: true,
          });
        } catch {
          toast.warning('Pipeline saved but schedule upsert failed');
        }
      }

      if (enableAlert && alertConditions.length > 0 && alertTarget) {
        try {
          await api.upsertDefaultAlertRule(wfId, {
            name: `${name} - Alert`,
            workflow_id: wfId,
            // project_id is derived server-side from the workflow; we
            // still send it for backward-compat with older backends.
            project_id: projectId,
            conditions: alertConditions,
            condition: alertConditions[0],
            condition_logic: 'any',
            channel: alertChannel,
            email_addresses: alertChannel === 'email' ? alertTarget.split(',').map(s => s.trim()) : [],
            slack_webhook_url: alertChannel === 'slack' ? alertTarget : '',
            teams_webhook_url: alertChannel === 'teams' ? alertTarget : '',
            webhook_url: alertChannel === 'webhook' ? alertTarget : '',
          });
        } catch {
          toast.warning('Pipeline saved but alert upsert failed');
        }
      }

      toast.success('Pipeline saved successfully');
      onClose();
    } catch (err: any) {
      toast.error('Save failed', err.message);
    }
    setSaving(false);
  };

  const stepCount = nodes.length;
  const connectionCount = edges.length;

  return (
    <>
      <div className="fixed inset-0 bg-black/20 z-[60]" onClick={onClose} />
      <div className="fixed inset-0 z-[65] flex items-center justify-center pointer-events-none">
        <div className="pointer-events-auto w-[560px] max-w-[95vw] max-h-[85vh] bg-white rounded-2xl shadow-2xl border border-slate-200/60 flex flex-col overflow-hidden">
          {/* Header */}
          <div className="px-5 py-4 border-b border-slate-200/60 flex items-center justify-between shrink-0">
            <div>
              <h2 className="text-base font-bold text-slate-800">Save Pipeline</h2>
              <p className="text-xs text-slate-400 mt-0.5">{stepCount} nodes, {connectionCount} connections</p>
            </div>
            <button onClick={onClose} className="w-8 h-8 rounded-lg flex items-center justify-center text-slate-400 hover:text-slate-600 hover:bg-slate-100">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
            </button>
          </div>

          {/* Tabs */}
          <div className="flex border-b border-slate-200/60 shrink-0">
            {([
              { key: 'details' as Tab, label: 'Details', dot: '' },
              { key: 'execution' as Tab, label: 'Execution', dot: enableTimeout || enableOverlapDetection ? 'bg-blue-500' : '' },
              { key: 'schedule' as Tab, label: 'Schedule', dot: enableSchedule ? 'bg-emerald-500' : '' },
              { key: 'alerts' as Tab, label: 'Alerts', dot: enableAlert ? 'bg-red-500' : '' },
            ]).map(t => (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className={`flex-1 py-2.5 text-xs font-semibold transition-colors border-b-2 ${
                  tab === t.key ? 'text-blue-600 border-blue-500' : 'text-slate-400 border-transparent hover:text-slate-600'
                }`}
              >
                {t.label}
                {t.dot && <span className={`ml-1 w-1.5 h-1.5 ${t.dot} rounded-full inline-block`} />}
              </button>
            ))}
          </div>

          {/* Content */}
          <div className="flex-1 overflow-y-auto p-5 space-y-4">
            {/* DETAILS TAB */}
            {tab === 'details' && (
              <>
                <div>
                  <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">Pipeline Name</label>
                  <input
                    type="text"
                    value={name}
                    onChange={e => setName(e.target.value)}
                    className="w-full px-3 py-2 text-sm border border-slate-200 rounded-xl outline-none focus:ring-2 focus:ring-blue-200 text-slate-700"
                    autoFocus
                  />
                </div>

                <div>
                  <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">Description</label>
                  <textarea
                    value={description}
                    onChange={e => setDescription(e.target.value)}
                    rows={3}
                    placeholder="What does this pipeline do?"
                    className="w-full px-3 py-2 text-sm border border-slate-200 rounded-xl outline-none focus:ring-2 focus:ring-blue-200 text-slate-700 resize-none"
                  />
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">Project</label>
                    <select
                      value={projectId}
                      onChange={e => setProjectId(e.target.value)}
                      className="w-full px-3 py-2 text-sm border border-slate-200 rounded-xl outline-none focus:ring-2 focus:ring-blue-200 text-slate-700"
                    >
                      <option value="default">Default</option>
                      {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                    </select>
                  </div>

                  {/*
                   * 2026-05-22 — status picker removed. Status is
                   * server-owned (see backend WorkflowUpdate lock-down
                   * in workflows.py A1). Lifecycle transitions happen
                   * via the dedicated endpoints (/test, /publish,
                   * /submit-for-review, /approve, /deploy) which the
                   * Toolbar Test/Publish/Deploy buttons and the
                   * Pipelines-page Deploy modal call directly.
                   * Letting Save also flip status was the audit's
                   * privilege-escalation finding A1.
                   */}
                </div>

                <div>
                  <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">Tags (comma-separated)</label>
                  <input
                    type="text"
                    value={tags}
                    onChange={e => setTags(e.target.value)}
                    placeholder="e.g. etl, daily, orders"
                    className="w-full px-3 py-2 text-sm border border-slate-200 rounded-xl outline-none focus:ring-2 focus:ring-blue-200 text-slate-700"
                  />
                </div>
              </>
            )}

            {/* EXECUTION TAB */}
            {tab === 'execution' && (
              <>
                {/* Max Runtime */}
                <div className="rounded-xl border border-slate-200 p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm font-semibold text-slate-700 flex items-center gap-2">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-amber-500">
                          <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
                        </svg>
                        Maximum Running Time
                      </div>
                      <div className="text-xs text-slate-400 mt-0.5">Alert if pipeline exceeds this duration</div>
                    </div>
                    <button
                      onClick={() => setEnableTimeout(!enableTimeout)}
                      className={`w-10 h-5.5 rounded-full transition-colors relative ${enableTimeout ? 'bg-amber-500' : 'bg-slate-300'}`}
                    >
                      <span className={`block w-4 h-4 bg-white rounded-full shadow-sm absolute top-[3px] transition-transform ${enableTimeout ? 'translate-x-[22px]' : 'translate-x-[3px]'}`} />
                    </button>
                  </div>

                  <div className={`space-y-3 ${!enableTimeout ? 'opacity-40 pointer-events-none' : ''}`}>
                    {/* Preset buttons */}
                    <div className="flex flex-wrap gap-1.5">
                      {MAX_RUNTIME_PRESETS.map(p => (
                        <button
                          key={p.minutes}
                          onClick={() => { setMaxRuntimeMinutes(p.minutes); setCustomRuntime(''); }}
                          className={`px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all ${
                            maxRuntimeMinutes === p.minutes && !customRuntime
                              ? 'bg-amber-100 text-amber-700 border border-amber-300'
                              : 'bg-slate-50 text-slate-500 border border-slate-200 hover:border-slate-300'
                          }`}
                        >
                          {p.label}
                        </button>
                      ))}
                    </div>

                    {/* Custom input */}
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-slate-400 font-medium">Or custom:</span>
                      <input
                        type="number"
                        min="1"
                        value={customRuntime}
                        onChange={e => {
                          setCustomRuntime(e.target.value);
                          const v = parseInt(e.target.value);
                          if (v > 0) setMaxRuntimeMinutes(v);
                        }}
                        placeholder="minutes"
                        className="w-24 px-2 py-1.5 text-xs border border-slate-200 rounded-lg outline-none focus:ring-2 focus:ring-amber-200 text-slate-700"
                      />
                      <span className="text-xs text-slate-400">minutes</span>
                    </div>

                    {maxRuntimeMinutes > 0 && (
                      <div className="flex items-center gap-2 text-xs bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-amber-500 shrink-0">
                          <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
                        </svg>
                        <span className="text-amber-700">
                          Pipeline will be flagged if it runs longer than <strong>{formatRuntime(maxRuntimeMinutes)}</strong>
                        </span>
                      </div>
                    )}
                  </div>
                </div>

                {/* Overlap Detection */}
                <div className="rounded-xl border border-slate-200 p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm font-semibold text-slate-700 flex items-center gap-2">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-purple-500">
                          <path d="M16 3h5v5" /><path d="M8 3H3v5" /><path d="M12 22v-8.3a4 4 0 0 0-1.172-2.872L3 3" /><path d="m15 9 6-6" />
                        </svg>
                        Overlap Detection
                      </div>
                      <div className="text-xs text-slate-400 mt-0.5">What to do when a scheduled run triggers while previous is still running</div>
                    </div>
                    <button
                      onClick={() => setEnableOverlapDetection(!enableOverlapDetection)}
                      className={`w-10 h-5.5 rounded-full transition-colors relative ${enableOverlapDetection ? 'bg-purple-500' : 'bg-slate-300'}`}
                    >
                      <span className={`block w-4 h-4 bg-white rounded-full shadow-sm absolute top-[3px] transition-transform ${enableOverlapDetection ? 'translate-x-[22px]' : 'translate-x-[3px]'}`} />
                    </button>
                  </div>

                  <div className={`space-y-2 ${!enableOverlapDetection ? 'opacity-40 pointer-events-none' : ''}`}>
                    {OVERLAP_POLICIES.map(p => (
                      <button
                        key={p.value}
                        onClick={() => setOverlapPolicy(p.value)}
                        className={`w-full flex items-start gap-3 p-3 rounded-xl border text-left transition-all ${
                          overlapPolicy === p.value
                            ? 'border-purple-300 bg-purple-50'
                            : 'border-slate-200 hover:border-slate-300'
                        }`}
                      >
                        <span className={`text-lg shrink-0 mt-0.5 ${overlapPolicy === p.value ? '' : 'opacity-40'}`}>{p.icon}</span>
                        <div>
                          <div className={`text-xs font-semibold ${overlapPolicy === p.value ? 'text-purple-700' : 'text-slate-600'}`}>
                            {p.label}
                          </div>
                          <div className="text-xs text-slate-400 mt-0.5">{p.desc}</div>
                        </div>
                        <div className="ml-auto shrink-0 mt-1">
                          <div className={`w-4 h-4 rounded-full border-2 flex items-center justify-center ${
                            overlapPolicy === p.value ? 'border-purple-500' : 'border-slate-300'
                          }`}>
                            {overlapPolicy === p.value && <div className="w-2 h-2 bg-purple-500 rounded-full" />}
                          </div>
                        </div>
                      </button>
                    ))}

                    {overlapPolicy === 'parallel' && (
                      <div className="flex items-center gap-2 text-xs bg-red-50 border border-red-200 rounded-lg px-3 py-2">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-red-500 shrink-0">
                          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                          <line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
                        </svg>
                        <span className="text-red-600">Running pipelines in parallel may cause resource contention and data conflicts</span>
                      </div>
                    )}
                  </div>
                </div>

                {/* Summary */}
                {(enableTimeout || enableOverlapDetection) && (
                  <div className="bg-blue-50 border border-blue-200 rounded-xl p-3 space-y-1.5">
                    <div className="text-xs font-bold text-blue-600 uppercase tracking-wider">Execution Guard Summary</div>
                    {enableTimeout && maxRuntimeMinutes > 0 && (
                      <div className="text-xs text-blue-700 flex items-center gap-1.5">
                        <span className="text-amber-500">\u23f1</span>
                        Timeout alert after {formatRuntime(maxRuntimeMinutes)}
                      </div>
                    )}
                    {enableOverlapDetection && (
                      <div className="text-xs text-blue-700 flex items-center gap-1.5">
                        <span className="text-purple-500">\u21c4</span>
                        On overlap: <strong>{OVERLAP_POLICIES.find(p => p.value === overlapPolicy)?.label}</strong>
                      </div>
                    )}
                  </div>
                )}
              </>
            )}

            {/* SCHEDULE TAB */}
            {tab === 'schedule' && (
              <>
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-sm font-semibold text-slate-700">Schedule</div>
                    <div className="text-xs text-slate-400">Run this pipeline automatically on a schedule</div>
                  </div>
                  <button
                    onClick={() => setEnableSchedule(!enableSchedule)}
                    className={`w-10 h-5.5 rounded-full transition-colors relative ${enableSchedule ? 'bg-emerald-500' : 'bg-slate-300'}`}
                  >
                    <span className={`block w-4 h-4 bg-white rounded-full shadow-sm absolute top-[3px] transition-transform ${enableSchedule ? 'translate-x-[22px]' : 'translate-x-[3px]'}`} />
                  </button>
                </div>

                <div className={`space-y-4 pt-2 ${!enableSchedule ? 'opacity-50 pointer-events-none' : ''}`}>
                  <div>
                    <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">Schedule Preset</label>
                    <div className="grid grid-cols-2 gap-2">
                      {SCHEDULE_PRESETS.map(p => (
                        <button
                          key={p.label}
                          onClick={() => { setSchedulePreset(p.label); if (p.cron) setCronExpression(p.cron); }}
                          className={`p-2 rounded-xl border text-xs font-medium text-left transition-all ${
                            schedulePreset === p.label ? 'border-emerald-300 bg-emerald-50 text-emerald-700' : 'border-slate-200 hover:border-slate-300 text-slate-600'
                          }`}
                        >
                          {p.label}
                          {p.cron && <span className="block text-[9px] text-slate-400 font-mono mt-0.5">{p.cron}</span>}
                        </button>
                      ))}
                    </div>
                  </div>

                  <div>
                    <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">Cron Expression</label>
                    <input
                      type="text"
                      value={cronExpression}
                      onChange={e => { setCronExpression(e.target.value); setSchedulePreset('Custom'); }}
                      placeholder="0 */6 * * *"
                      className="w-full px-3 py-2 text-sm border border-slate-200 rounded-xl outline-none focus:ring-2 focus:ring-emerald-200 font-mono text-slate-700"
                    />
                    <p className="text-[9px] text-slate-400 mt-1">Format: minute hour day-of-month month day-of-week</p>
                  </div>

                  <div>
                    <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">Timezone</label>
                    <select
                      value={timezone}
                      onChange={e => setTimezone(e.target.value)}
                      className="w-full px-3 py-2 text-sm border border-slate-200 rounded-xl outline-none focus:ring-2 focus:ring-emerald-200 text-slate-700"
                    >
                      <option value="UTC">UTC</option>
                      <option value="America/New_York">US Eastern</option>
                      <option value="America/Chicago">US Central</option>
                      <option value="America/Los_Angeles">US Pacific</option>
                      <option value="Europe/London">London</option>
                      <option value="Europe/Berlin">Berlin</option>
                      <option value="Asia/Kolkata">India (IST)</option>
                      <option value="Asia/Tokyo">Tokyo</option>
                      <option value="Australia/Sydney">Sydney</option>
                    </select>
                  </div>
                </div>
              </>
            )}

            {/* ALERTS TAB */}
            {tab === 'alerts' && (
              <>
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-sm font-semibold text-slate-700">Alerts</div>
                    <div className="text-xs text-slate-400">Get notified when this pipeline runs</div>
                  </div>
                  <button
                    onClick={() => setEnableAlert(!enableAlert)}
                    className={`w-10 h-5.5 rounded-full transition-colors relative ${enableAlert ? 'bg-red-500' : 'bg-slate-300'}`}
                  >
                    <span className={`block w-4 h-4 bg-white rounded-full shadow-sm absolute top-[3px] transition-transform ${enableAlert ? 'translate-x-[22px]' : 'translate-x-[3px]'}`} />
                  </button>
                </div>

                <div className={`space-y-4 pt-2 ${!enableAlert ? 'opacity-50 pointer-events-none' : ''}`}>
                  <div>
                    <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">Conditions (select multiple)</label>
                    <div className="grid grid-cols-2 gap-2">
                      {ALERT_CONDITIONS.map(c => {
                        const isSelected = alertConditions.includes(c.value);
                        return (
                          <button
                            key={c.value}
                            onClick={() => toggleAlertCondition(c.value)}
                            className={`p-2 rounded-xl border text-xs font-medium text-left transition-all flex items-center gap-2 ${
                              isSelected ? 'border-red-300 bg-red-50' : 'border-slate-200 hover:border-slate-300'
                            }`}
                          >
                            <span className={`w-4 h-4 rounded border-2 flex items-center justify-center shrink-0 transition-colors ${isSelected ? 'border-red-400 bg-red-400' : 'border-slate-300'}`}>
                              {isSelected && <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3"><polyline points="20 6 9 17 4 12" /></svg>}
                            </span>
                            <span className={c.color}>{c.label}</span>
                          </button>
                        );
                      })}
                    </div>
                  </div>

                  <div>
                    <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">Channel</label>
                    <div className="flex gap-2">
                      {ALERT_CHANNELS.map(ch => (
                        <button
                          key={ch.value}
                          onClick={() => setAlertChannel(ch.value)}
                          className={`flex-1 p-2 rounded-xl border text-center text-xs font-medium transition-all ${
                            alertChannel === ch.value ? 'border-red-300 bg-red-50 text-red-700' : 'border-slate-200 hover:border-slate-300 text-slate-600'
                          }`}
                        >
                          {ch.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  <div>
                    <label className="text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5">
                      {alertChannel === 'email' ? 'Email Addresses (comma-separated)' :
                       alertChannel === 'slack' ? 'Slack Webhook URL' :
                       alertChannel === 'teams' ? 'Teams Webhook URL' : 'Webhook URL'}
                    </label>
                    <input
                      type="text"
                      value={alertTarget}
                      onChange={e => setAlertTarget(e.target.value)}
                      placeholder={
                        alertChannel === 'email' ? 'admin@example.com, team@example.com' :
                        alertChannel === 'slack' ? 'https://hooks.slack.com/services/...' :
                        alertChannel === 'teams' ? 'https://outlook.office.com/webhook/...' : 'https://api.example.com/webhook'
                      }
                      className="w-full px-3 py-2 text-sm border border-slate-200 rounded-xl outline-none focus:ring-2 focus:ring-red-200 text-slate-700"
                    />
                  </div>
                </div>
              </>
            )}
          </div>

          {/* Footer */}
          <div className="px-5 py-3 border-t border-slate-200/60 flex items-center justify-between shrink-0">
            <div className="text-xs text-slate-400">
              {workflowId ? `Updating "${name || workflowName}"` : 'Creating new pipeline'}
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={onClose}
                className="px-4 py-2 text-xs text-slate-600 hover:text-slate-800 rounded-xl hover:bg-slate-50 font-medium"
              >
                Cancel
              </button>
              <button
                onClick={handleSave}
                disabled={saving || !name.trim()}
                className="px-5 py-2 text-xs text-white font-semibold rounded-xl disabled:opacity-50 transition-all shadow-sm hover:shadow-md flex items-center gap-1.5"
                style={{ background: saving ? '#94a3b8' : 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
              >
                {saving ? (
                  <><span className="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin" />Saving...</>
                ) : (
                  <><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" /><polyline points="17 21 17 13 7 13 7 21" /><polyline points="7 3 7 8 15 8" /></svg>Save Pipeline</>
                )}
              </button>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
