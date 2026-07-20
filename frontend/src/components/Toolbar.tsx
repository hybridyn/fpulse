import { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { api } from '../api/client';
import { toast } from './Toast';
import { navigateTo } from '../router';
import SaveDialog from './SaveDialog';
import SaveAsTemplateDialog from './SaveAsTemplateDialog';
import UnsavedChangesModal from './UnsavedChangesModal';
import ParametersDialog from './ParametersDialog';
import RunWithParametersDialog from './RunWithParametersDialog';
import BackfillModal from './BackfillModal';
import { validateWorkflow, ValidationIssue } from '../utils/validateWorkflow';
import { migrateLegacySteps } from '../utils/migrateLegacyNodes';
import { useDarkMode } from '../hooks/useDarkMode';
// 2026-05-19 (OSS-8 of PAGE_BY_PAGE_AUDIT.md): the standalone
// requireNamedWorkflow helper is gone — the store's `ensureWorkflow`
// now runs the name-prompt loop internally and returns null on cancel.
// See `ensureNamedAndPersist` below for the migration shim.
import TierChip from './shared/TierChip';
import HubTabs, { WORKFLOWS_TABS } from './HubTabs';

export default function Toolbar({ tier = 'free', environment = 'dev' }: { tier?: string; environment?: 'dev' | 'prod' }) {
  const dark = useDarkMode();
  const {
    workflowName, version, isRunning, runWorkflow,
    chatOpen, setChatOpen,
    nodes, edges, workflowId, ensureWorkflow, loadWorkflow,
    setValidationErrors: setStoreValidationErrors,
    isDirty,
    parameters: pipelineParameters,
    status, setStatus,
    editorSurface,
  } = useWorkflowStore();
  const isFileDataPrep = editorSurface === 'file_data_prep';
  const isPipelineDataPrep = editorSurface === 'pipeline_data_prep';
  const surfaceCopy = isFileDataPrep
    ? {
        title: 'Data Prep',
        subtitle: 'Clean, shape, and load this file into a managed table',
        closeLabel: 'Back to Storage',
        closeTarget: 'storage' as const,
        saveLabel: 'Save Draft Recipe',
        runLabel: 'Load to Managed Table',
        runTitle: 'Run the prep recipe and write the managed table',
        savedToast: 'Data prep recipe saved',
      }
    : isPipelineDataPrep
      ? {
          title: 'Pipeline Data Prep',
          subtitle: 'Clean this source dataset every time the pipeline runs',
          closeLabel: 'Back to Connections',
          closeTarget: 'connections' as const,
          saveLabel: 'Save Pipeline',
          runLabel: 'Run Pipeline',
          runTitle: 'Run the pipeline with the data prep step',
          savedToast: 'Pipeline data prep saved',
        }
      : {
          title: 'Editor',
          subtitle: 'Build, test, and deploy pipelines visually',
          closeLabel: 'Close',
          closeTarget: 'pipelines' as const,
          saveLabel: 'Save',
          runLabel: 'Run',
          runTitle: 'Run pipeline (full dataset)',
          savedToast: 'Pipeline saved',
        };

  // Project picker + inline rename moved to <EditorContextBar /> mounted
  // inside the canvas column. Toolbar still owns the file menu, save
  // indicator, validation chip, and the action buttons (Variables /
  // Parameters / Close / Save / Run / Publish).

  // Validation chip + Save indicator both live in <EditorContextBar />
  // now (moved May 10 2026 / May 11 2026). Toolbar no longer renders
  // them — chip was overlapping the Templates sub-tab in the top nav
  // and Save indicator belongs next to the Pipeline Name where users
  // actually look for that state.
  const [showMenu, setShowMenu] = useState(false);
  const [showSaveDialog, setShowSaveDialog] = useState(false);
  const [showParamsDialog, setShowParamsDialog] = useState(false);
  // A4 (2026-06-15): when the pipeline declares parameters, the editor Run
  // button opens this dialog first so the user can override per-run instead
  // of silently using declared defaults. The validation issues computed at
  // click time are stashed so the warning toast still fires after the prompt.
  const [showRunParamsDialog, setShowRunParamsDialog] = useState(false);
  const pendingRunIssues = useRef<ValidationIssue[]>([]);
  const [showBackfillModal, setShowBackfillModal] = useState(false);
  const [showUnsavedModal, setShowUnsavedModal] = useState(false);
  const [savingForClose, setSavingForClose] = useState(false);
  // Save-as-template state — populated when the user picks the kebab
  // menu's "Save as template..." item. The dialog itself fetches
  // existing names for client-side dup check; we just hand it the
  // current canvas's nodes/edges in the IR shape it expects.
  const [saveTplState, setSaveTplState] = useState<{
    open: boolean;
    pipelineName: string;
    steps: any[];
    connections: any[];
    existingNames: string[];
  }>({ open: false, pipelineName: '', steps: [], connections: [], existingNames: [] });
  // validation errors are now stored in the workflow store and displayed on nodes
  const menuRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Close menu on outside click
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setShowMenu(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  const handleExport = async () => {
    // 2026-05-22 (audit D2) — saved pipelines export via the backend
    // /workflows/{id}/export endpoint so the artifact carries the full
    // contract (parameters, folder_id, metadata, execution_settings,
    // format_version). The previous client-only blob was a different
    // shape than backend /import expected, so exporting from the
    // canvas and importing on the Pipelines page silently dropped
    // every parameter the user had declared.
    //
    // Unsaved drafts can't hit the backend (no id yet) so we still
    // emit a local blob, but with format_version=2 + a scope flag so
    // /import can tell drafts from saved exports.
    const workflowId = useWorkflowStore.getState().workflowId;
    const filenameBase = workflowName.replace(/\s+/g, '_').toLowerCase() || 'pipeline';

    if (workflowId) {
      // Saved pipeline path — let the backend assemble the full blob.
      try {
        const blob = await api.exportPipeline(workflowId);
        const json = new Blob([JSON.stringify(blob, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(json);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${filenameBase}.fpulse.json`;
        a.click();
        URL.revokeObjectURL(url);
        toast.success('Pipeline exported', `Saved as ${a.download}`);
        setShowMenu(false);
        return;
      } catch (err: any) {
        // Fall through to the local-blob path so the user still gets
        // SOMETHING. Warn so they know the artifact isn't fully equal
        // to a backend export (e.g. parameters won't survive
        // round-trip on this file).
        toast.warning(
          'Backend export failed — wrote canvas snapshot instead',
          err?.message || 'Falling back to client-side export',
        );
      }
    }

    // Unsaved-draft path (or backend-export-failed fallback). Mark
    // scope clearly so /import can recognise it isn't a full export.
    const pipeline = {
      fpulse_version: '1.0.0',
      format_version: 2,
      export_type: 'pipeline',
      scope: 'canvas_draft',
      exported_at: new Date().toISOString(),
      pipeline: {
        name: workflowName,
        steps: nodes.map((n) => ({
          id: n.id,
          type: n.data.stepType,
          label: n.data.label,
          params: n.data.params || {},
          position: { x: n.position.x, y: n.position.y },
        })),
        connections: edges.map((e) => ({
          from_step: e.source,
          to_step: e.target,
          condition: (e.data as any)?.condition || 'completion',
        })),
      },
    };
    const blob = new Blob([JSON.stringify(pipeline, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${filenameBase}.fpulse.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast.success('Canvas draft exported', `Saved as ${a.download} (save the pipeline first for a full export with parameters)`);
    setShowMenu(false);
  };

  // 2026-05-23 (Y8) — shared importer used by both the file-input
  // ("Import from JSON file") and the Storage page's "Open in Editor"
  // handoff. Storage stores the raw bytes in sessionStorage under
  // `fpulse_pending_import` and routes here; we pick the JSON up on
  // mount via the effect below and run the same logic.
  const processImportedJson = async (data: any, sourceLabel?: string) => {
    try {
        // 2026-05-22 — accept both the flat legacy shape ({steps,
        // name, connections}) AND the format_version: 2 envelope
        // ({pipeline: {steps, name, parameters, ...}}) the backend
        // export now emits. The Toolbar export goes through the
        // backend endpoint for saved pipelines (audit D2), so users
        // are increasingly likely to import the enveloped shape.
        const envelope =
          data && typeof data === 'object' && data.pipeline &&
          Array.isArray(data.pipeline.steps);
        const pipeline = envelope ? data.pipeline : data;
        const name = pipeline?.name;
        const steps = pipeline?.steps;
        const connections = pipeline?.connections || [];
        const parameters = pipeline?.parameters || [];

        // 2026-05-22 (audit R3) — a self-contained sample / community
        // pipeline can ship a top-level `connection_definitions` array
        // listing the saved Connections the steps reference by name.
        // The import wizard idempotently creates each (skip if a
        // connection of the same name + type already exists), builds
        // a name→id map, and remaps step params' `connection_id`
        // (which initially holds the connection NAME for the import
        // contract) to the real backend ids.
        //
        // Shape:
        //   "connection_definitions": [
        //     { "name": "jsonplaceholder", "type": "rest_api",
        //       "config": { "base_url": "https://...", "auth_type": "none" } },
        //     ...
        //   ]
        const connectionDefs: any[] = Array.isArray(data?.connection_definitions)
          ? data.connection_definitions
          : Array.isArray(pipeline?.connection_definitions)
            ? pipeline.connection_definitions
            : [];

        if (!Array.isArray(steps) || !name) {
          toast.error('Invalid file', 'File does not contain a valid F-Pulse pipeline');
          return;
        }

        // Provision connections (if any) before loading the canvas
        // so name→id remap can run in one pass below.
        const nameToId: Record<string, string> = {};
        if (connectionDefs.length > 0) {
          let existing: any[] = [];
          try {
            existing = await api.listConnections() as any[];
          } catch {
            existing = [];
          }
          // Build a fast lookup of existing connections by (lower-name + type).
          const existingByKey: Record<string, string> = {};
          for (const c of existing || []) {
            const key = `${String(c.name || '').toLowerCase()}::${c.type || ''}`;
            if (c.id) existingByKey[key] = c.id;
            // Also map by name alone so a typo'd type still finds it.
            const nameKey = String(c.name || '').toLowerCase();
            if (c.id && !existingByKey[nameKey]) existingByKey[nameKey] = c.id;
          }

          let created = 0;
          let reused = 0;
          for (const def of connectionDefs) {
            if (!def || !def.name || !def.type) continue;
            const lowerName = String(def.name).toLowerCase();
            const key = `${lowerName}::${def.type}`;
            const existingId = existingByKey[key] || existingByKey[lowerName];
            if (existingId) {
              nameToId[def.name] = existingId;
              reused++;
              continue;
            }
            try {
              const newConn = await api.createConnection({
                name: def.name,
                type: def.type,
                config: def.config || {},
                description: def.description || `Auto-created by import of "${name}"`,
                capabilities: def.capabilities || undefined,
              } as any);
              if (newConn?.id) {
                nameToId[def.name] = newConn.id;
                created++;
              }
            } catch (err: any) {
              toast.warning(
                'Connection auto-create failed',
                `Could not create "${def.name}" (${err?.message || 'unknown error'}). Pipeline will load without it; create the connection manually in the Connections page.`,
              );
            }
          }

          if (created > 0 || reused > 0) {
            toast.info(
              'Connections provisioned',
              `${created} created${reused ? `, ${reused} reused` : ''} for this pipeline.`,
            );
          }
        }

        // 2026-05-22 — run the legacy-node migration on imported
        // steps (audit O5 / R3). The backend has the canonical
        // migration (fpulse/ir/migrations.py) but it only fires on
        // workflow-store load/save paths. The Toolbar JSON import
        // bypasses that, so older sample files would have landed on
        // the canvas with legacy types.
        const migrated = migrateLegacySteps(steps as any[]);
        if (migrated.remapCount > 0) {
          const sample = migrated.remaps.slice(0, 3)
            .map((r) => `${r.from} → ${r.to}`)
            .join('; ');
          const more = migrated.remapCount > 3
            ? ` and ${migrated.remapCount - 3} more`
            : '';
          toast.info(
            'Modernized legacy nodes on import',
            `${migrated.remapCount} node${migrated.remapCount === 1 ? '' : 's'} rewritten (${sample}${more}).`,
          );
        }

        // Remap connection NAMES in step params → actual backend ids.
        // The sample/import contract is that `params.connection_id`
        // initially holds the connection's NAME (so the file is
        // human-readable + portable), and the import wizard rewrites
        // each to the actual id after provisioning.
        const remappedSteps = migrated.steps.map((s: any) => {
          if (!s || typeof s !== 'object') return s;
          const params = { ...(s.params || {}) };
          const cid = params.connection_id;
          if (cid && typeof cid === 'string' && nameToId[cid]) {
            params.connection_id = nameToId[cid];
          }
          return { ...s, params };
        });

        // CRITICAL — discard the file's `id` so the canvas is treated
        // as a fresh unsaved workflow and Run routes through the
        // ephemeral executor (see audit notes in workflowStore.ts).
        loadWorkflow({
          workflow: {
            id: null,
            name,
            steps: remappedSteps,
            connections,
            parameters,
            metadata: pipeline?.metadata || {},
          },
          version: 1,
        });
        useWorkflowStore.getState().setDirty(true);
        const importedSurface = useWorkflowStore.getState().editorSurface;
        const importedIsFilePrep = importedSurface === 'file_data_prep';
        const importedIsPipelinePrep = importedSurface === 'pipeline_data_prep';
        toast.success(
          importedIsFilePrep ? 'Data Prep opened' : importedIsPipelinePrep ? 'Pipeline Data Prep opened' : 'Pipeline imported',
          importedIsFilePrep
            ? `Loaded "${name}"${sourceLabel ? ` from ${sourceLabel}` : ''}. Configure prep steps, then load to a managed table.`
            : importedIsPipelinePrep
              ? `Loaded "${name}"${sourceLabel ? ` from ${sourceLabel}` : ''}. Configure prep steps; they will run every pipeline execution.`
            : `Loaded "${name}"${sourceLabel ? ` from ${sourceLabel}` : ''} with ${remappedSteps.length} step${remappedSteps.length === 1 ? '' : 's'}. Save to add it to the Pipelines list.`,
        );
      } catch {
        toast.error('Import failed', 'Could not parse pipeline file');
      }
  };

  const handleImport = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async (ev) => {
      try {
        const data = JSON.parse(ev.target?.result as string);
        await processImportedJson(data);
      } catch {
        toast.error('Import failed', 'Could not parse pipeline file');
      }
    };
    reader.readAsText(file);
    // Reset input so same file can be imported again
    e.target.value = '';
    setShowMenu(false);
  };

  // 2026-05-23 (Y8) — Storage → Editor handoff. The StoragePreviewDrawer
  // sets sessionStorage['fpulse_pending_import'] to the raw JSON text
  // and navigates to #editor. We pick it up here on mount, run it
  // through the same import path the file-input uses, and clear the
  // key so it doesn't fire again on the next render.
  useEffect(() => {
    let pending: string | null = null;
    let source: string | null = null;
    try {
      pending = sessionStorage.getItem('fpulse_pending_import');
      source = sessionStorage.getItem('fpulse_pending_import_source');
    } catch {
      return;
    }
    if (!pending) return;
    try {
      sessionStorage.removeItem('fpulse_pending_import');
      sessionStorage.removeItem('fpulse_pending_import_source');
    } catch { /* ignore */ }
    try {
      const data = JSON.parse(pending);
      void processImportedJson(data, source || undefined);
    } catch (err) {
      toast.error('Open in Editor failed', `Could not parse the pipeline JSON: ${(err as Error).message || err}`);
    }
    // Mount-only — the sessionStorage key is one-shot per navigation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Push validation errors to the store so nodes highlight in red */
  const showValidationOnNodes = (errors: ValidationIssue[]): void => {
    const byNode: Record<string, string[]> = {};
    for (const e of errors) {
      if (!byNode[e.nodeId]) byNode[e.nodeId] = [];
      byNode[e.nodeId].push(e.message);
    }
    setStoreValidationErrors(byNode);
    toast.error('Validation failed', `${errors.length} ${errors.length === 1 ? 'issue' : 'issues'} found — check highlighted nodes`);
  };

  /** Run the pipeline after validation has passed. Shared by the direct
   *  Run path (no declared parameters) and the Run-with-parameters dialog.
   *  `issues` are warnings-only at this point (errors already blocked). */
  const finishRun = async (issues: ValidationIssue[], parameterValues?: Record<string, unknown>) => {
    setStoreValidationErrors({});
    if (issues.length > 0) {
      toast.warning('Warnings', `${issues.length} warning${issues.length > 1 ? 's' : ''} found, running anyway.`);
    }
    await runWorkflow(true, parameterValues);
    if (isFileDataPrep) {
      const latest = useWorkflowStore.getState();
      const results = Object.values(latest.stepResults || {});
      const hasError = results.some((r: any) => r?.status === 'error');
      const allCanvasNodesSucceeded = latest.nodes.length > 0 && latest.nodes.every((n) => n.data?.status === 'success');
      if (!hasError && allCanvasNodesSucceeded) {
        try { localStorage.setItem('fpulse_storage_tab', 'tables'); } catch { /* ignore */ }
        navigateTo('storage');
      }
    }
  };

  // Open the "Save as template" dialog with the live canvas. This is
  // the most natural entry point for creating a user-defined template:
  // the user is already editing a working pipeline. The dialog is also
  // available from the Pipelines table-view row actions for already-saved
  // pipelines.
  const handleSaveAsTemplate = async () => {
    setShowMenu(false);
    if (nodes.length === 0) {
      toast.warning('Nothing to save', 'Add at least one node to the canvas before saving as a template.');
      return;
    }
    const steps = nodes.map((n: any) => ({
      id: n.id,
      type: (n.data as any)?.stepType || n.type,
      label: (n.data as any)?.label || '',
      params: (n.data as any)?.params || {},
      position: n.position || { x: 0, y: 0 },
    }));
    const connections = edges.map((e: any) => ({
      from_step: e.source,
      to_step: e.target,
      ...(e.data?.condition ? { condition: e.data.condition } : {}),
    }));
    let existingNames: string[] = [];
    try {
      const list = await api.listUserTemplates();
      existingNames = (list?.templates || []).map((t: any) => String(t.name || ''));
    } catch {
      // Backend list failed — proceed; backend will 409 on collision.
    }
    setSaveTplState({
      open: true,
      pipelineName: workflowName || 'My template',
      steps,
      connections,
      existingNames,
    });
  };

  // Save is intentionally validation-free — product decision: Save must always
  // persist work-in-progress. Validation is enforced on Deploy (and Run),
  // not Save. Clear any prior red node highlights so a previous failed
  // Deploy doesn't keep marking nodes after the user saves a fix.
  const handleSave = () => {
    setStoreValidationErrors({});
    setShowSaveDialog(true);
    setShowMenu(false);
  };

  /**
   * Name-prompt + uniqueness loop, then persist. The name-prompt now
   * lives inside the store's `ensureWorkflow({allowCreate:true})` action
   * (OSS-8, 2026-05-19) — a null return means the user cancelled, any
   * truthy id means we persisted. Toolbar Save / ConfigPanel Test Node /
   * Canvas Sample all converge on the same store-owned rule
   * (2026-05-09: no silent "Untitled Pipeline" rows).
   *
   * Returns `true` if persisted, `false` if the user cancelled.
   */
  const ensureNamedAndPersist = async (): Promise<boolean> => {
    const id = await ensureWorkflow({ allowCreate: true });
    return !!id;
  };

  const handleQuickSave = async () => {
    setStoreValidationErrors({});
    try {
      const ok = await ensureNamedAndPersist();
      if (ok) toast.success(surfaceCopy.savedToast);
    } catch {
      toast.error('Save failed', 'Could not save pipeline to server');
    }
  };

  /**
   * Close the editor and go back to the Workflows list.
   *
   * Three paths:
   *   1. Nothing unsaved → navigate immediately (no modal).
   *   2. Dirty → open UnsavedChangesModal; user picks Save / Discard / Cancel.
   *   3. Dirty + user picks Save → persist via ensureWorkflow(), then navigate.
   *
   * Routing: App.tsx uses hash-based routing (#pipelines is the Workflows
   * page). Setting location.hash fires the hashchange listener which
   * updates the page state — no router library needed.
   */
  const navigateToWorkflows = () => navigateTo(surfaceCopy.closeTarget);

  // Always raise a confirmation popup on Close — whether or not the
  // workflow is dirty. When clean the modal collapses to a simple
  // "Close this pipeline?" confirm (Close / Cancel). This prevents
  // accidental clicks from yanking the user out of their canvas.
  const handleCloseEditor = () => {
    setShowUnsavedModal(true);
  };

  const handleModalSaveAndClose = async () => {
    // No validation here — Save is always allowed (validation runs on Deploy).
    // But we DO run the name-prompt + uniqueness check (via
    // ensureNamedAndPersist) so the "Save and close" path can't sneak an
    // "Untitled Pipeline" row past the dedup rule. If the user cancels the
    // name prompt, stay on the modal so they can pick Discard or Cancel.
    setStoreValidationErrors({});
    setSavingForClose(true);
    try {
      const ok = await ensureNamedAndPersist();
      if (!ok) {
        // User cancelled the name prompt — leave the modal open so they
        // can choose Discard / Cancel without an awkward state where we
        // navigated away with nothing saved.
        return;
      }
      toast.success(surfaceCopy.savedToast);
      setShowUnsavedModal(false);
      // Wipe per-pipeline state so re-entering the editor lands on a
      // blank canvas instead of re-rendering the just-closed pipeline.
      useWorkflowStore.getState().resetWorkflow();
      navigateToWorkflows();
    } catch {
      toast.error('Save failed', 'Could not save pipeline. Stay here and try again, or discard to leave without saving.');
    } finally {
      setSavingForClose(false);
    }
  };

  const handleModalDiscard = () => {
    // User accepted losing edits — clear dirty flag so the
    // beforeunload guard (added in App.tsx) doesn't fire again,
    // AND reset the per-pipeline state so re-entering the editor
    // starts fresh.
    useWorkflowStore.getState().setDirty(false);
    useWorkflowStore.getState().resetWorkflow();
    setShowUnsavedModal(false);
    navigateToWorkflows();
  };

  // Keyboard: Ctrl+W / Cmd+W → close, Ctrl+S / Cmd+S → quick save.
  // We intercept these inside the editor only (Toolbar is editor-scoped).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'w') {
        e.preventDefault();
        handleCloseEditor();
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === 's' || e.key === 'S')) {
        e.preventDefault();
        handleQuickSave();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isDirty]);

  // Browser tab close / reload protection — only when dirty.
  // beforeunload can't show our custom modal, but it forces the
  // browser's native "Reload site? Changes you made may not be saved"
  // dialog which is enough to stop an accidental Ctrl+R.
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (!isDirty) return;
      e.preventDefault();
      // Chrome needs returnValue set; Firefox uses preventDefault alone.
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [isDirty]);

  return (
    // 78px banner — matches the canonical <PageHeader> chrome the sibling
    // Workflows pages (Pipelines / Executions / Templates) render, so the
    // Editor doesn't read as a different page: same [1fr_auto_1fr] grid,
    // items-center, gap-4, px-8, DEV gradient / dark #0F172A, no shadow.
    // The Project + Pipeline-name ribbon is rendered separately by
    // <EditorContextBar /> mounted inside the canvas column (so its width
    // tracks the canvas, not the full window).
    // 2026-07-03: reverted the 2026-06-10 responsive asymmetric grid back
    // to the symmetric [1fr_auto_1fr] so the HubTabs strip sits at the
    // TRUE page centre at every width, exactly like the sibling pages (it
    // drifted off-centre below xl before). The overflow that workaround
    // guarded against was caused by the "Variables" button, since removed
    // (see note below), and the action labels now collapse to icons below
    // xl — so the right cluster fits its track.
    <div className={`h-[78px] grid grid-cols-[1fr_auto_1fr] items-center px-8 gap-4 shrink-0 relative z-40 border-b ${dark ? 'bg-[#0F172A] border-white/[0.06]' : 'bg-gradient-to-b from-slate-200 to-slate-300 border-slate-400/70'}`}>
      {/* LEFT group — page title + subtitle (vertically stacked, same
          pattern Insights / Settings use). Inner flex is `items-center`
          to match <PageHeader>'s title cluster so the title + subtitle
          sit at the same vertical position as the sibling Workflows
          pages. (Project picker + Workflow Name live in
          <EditorContextBar /> now, so there's no second baseline row to
          align here anymore.) */}
      <div className="min-w-0 flex items-center gap-3">
        <div className="shrink-0">
          <h1 className={`text-xl font-bold flex items-center gap-2 ${dark ? 'text-white' : 'text-slate-800'}`}>
            {/*
              2026-05-22 — switched from the AI sparks / lightning-bolt
              polygon to a pencil glyph. The canonical icon table
              (memory rule `feedback_fpulse_icon_consistency.md`,
              2026-05-12) says Editor = pencil; the lightning bolt is
              the AI / sparks symbol and was misleading the user into
              reading the Editor page header as "AI-powered Editor"
              rather than "the page where you edit pipelines." Every
              other AI / sparkle affordance on this app remains a
              lightning bolt — only the Editor's page-title glyph
              changed.
            */}
            {/* 2026-05-22 — icon tint changed from `text-pulse-500`
                (brand orange) to `text-blue-500` to match the rest
                of the page-header palette (Dashboard / Executions /
                Help / Pipelines). Brand-orange is reserved for the
                sidebar logo + login mark; per-page H1 icons follow
                the cool-tone palette so no single page screams. */}
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-blue-500">
              <path d="M12 20h9" />
              <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
            </svg>
            {surfaceCopy.title}
            <TierChip tier={tier} environment={environment} />
          </h1>
          <p className={`text-xs mt-0.5 truncate ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
            {surfaceCopy.subtitle}
          </p>
        </div>

      </div>

      {/* Workflows submenu — sibling tabs centered at the true page
          midpoint via the auto-width middle grid column. */}
      <div className="flex justify-center items-center">
        <HubTabs
          tabs={WORKFLOWS_TABS}
          active="editor"
          onNavigate={(p) => { window.location.hash = p; }}
          environment={environment}
          dark={dark}
        />
      </div>

      {/* RIGHT group — save indicator, validation chip, and the
          Variables / Parameters / Close / Save / Run / Publish
          actions. Wrapped in a 1fr grid column matching the LEFT
          width so the centre column stays page-centered. */}
      <div className="flex justify-end items-center gap-2 min-w-0">

      {/* Save indicator was here — moved to <EditorContextBar /> so it
          sits next to the Pipeline Name where users look for save
          state. The indicator state itself lives in EditorContextBar
          now (lastSavedAt + the minute-ticker). */}

      {/* Validation chip moved to <EditorContextBar /> on 2026-05-11.
          It was overlapping the Templates sub-tab in the workflows
          nav row; the editor-context ribbon sits below that row and
          has plenty of horizontal space next to the Save indicator. */}

      {/* Spacer — push actions to right */}
      <div className="flex-1" />

      {/* RIGHT group — File menu (Save/Export/Import) */}
      <div ref={menuRef} className="relative">
        <button
          onClick={() => setShowMenu(!showMenu)}
          className={`p-1.5 rounded-lg transition-colors ${dark ? 'text-slate-400 hover:text-slate-200 hover:bg-white/[0.06]' : 'text-slate-400 hover:text-slate-600 hover:bg-slate-100'}`}
          title="Pipeline actions"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="1" /><circle cx="12" cy="5" r="1" /><circle cx="12" cy="19" r="1" />
          </svg>
        </button>

        {showMenu && (
          <div className="absolute right-0 top-full mt-1 bg-white border border-slate-200 rounded-xl shadow-xl py-1 w-48 z-50">
            <button
              onClick={handleSave}
              className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-slate-600 hover:bg-slate-50 hover:text-slate-800"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
                <polyline points="17 21 17 13 7 13 7 21" /><polyline points="7 3 7 8 15 8" />
              </svg>
              Save with details…
            </button>
            <button
              onClick={handleExport}
              className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-slate-600 hover:bg-slate-50 hover:text-slate-800"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              Export as JSON
            </button>
            <button
              onClick={() => fileInputRef.current?.click()}
              className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-slate-600 hover:bg-slate-50 hover:text-slate-800"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="17 8 12 3 7 8" /><line x1="12" y1="3" x2="12" y2="15" />
              </svg>
              Import from JSON
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept=".json,.fpulse.json"
              onChange={handleImport}
              className="hidden"
            />
            <div className="border-t border-slate-100 my-1" />
            <button
              onClick={() => {
                if (navigator.clipboard) {
                  const pipeline = JSON.stringify({
                    name: workflowName, version,
                    steps: nodes.map((n) => ({ id: n.id, type: n.data.stepType, label: n.data.label, params: n.data.params || {}, position: n.position })),
                    connections: edges.map((e) => ({ from_step: e.source, to_step: e.target, condition: (e.data as any)?.condition || 'completion' })),
                  });
                  navigator.clipboard.writeText(pipeline);
                  toast.info('Copied to clipboard');
                }
                setShowMenu(false);
              }}
              className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-slate-600 hover:bg-slate-50 hover:text-slate-800"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
              </svg>
              Copy as JSON
            </button>
            <div className="border-t border-slate-100 my-1" />
            <button
              onClick={handleSaveAsTemplate}
              className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-slate-600 hover:bg-violet-50 hover:text-violet-700"
              title="Save the current canvas as a reusable template (lives under Templates → User defined)"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="3" width="7" height="7" rx="1" />
                <rect x="14" y="3" width="7" height="7" rx="1" />
                <rect x="3" y="14" width="7" height="7" rx="1" />
                <path d="M14 17h7" />
                <path d="M17.5 14v7" />
              </svg>
              Save as template…
            </button>
            {!isFileDataPrep && (
              <>
                <div className="border-t border-slate-100 my-1" />
                <button
                  onClick={() => {
                    setShowMenu(false);
                    if (!workflowId) {
                      toast.warning(
                        'Save first',
                        'Save the pipeline before launching a backfill — backfill needs a persisted pipeline_id.',
                      );
                      return;
                    }
                    setShowBackfillModal(true);
                  }}
                  className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-slate-600 hover:bg-violet-50 hover:text-violet-700"
                  title="Re-run this pipeline once per time window over a historical date range"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="3" y="4" width="18" height="18" rx="2" />
                    <line x1="16" y1="2" x2="16" y2="6" />
                    <line x1="8" y1="2" x2="8" y2="6" />
                    <line x1="3" y1="10" x2="21" y2="10" />
                    <polyline points="9 14 11 16 15 12" />
                  </svg>
                  Backfill…
                </button>
              </>
            )}
          </div>
        )}
      </div>

      {/* 2026-06-11 — removed the "Variables" button. It was local-only
          state (never persisted to the workflow, never used at run) and
          redundant with Parameters. The two real mechanisms are:
          Parameters (${param.x}, declared + supplied at run) and runtime
          $vars (Append Variable / Lookup, read via {{ $vars.x }}). One
          concept in the toolbar = less to misunderstand. */}

      {/* Pipeline Parameters — typed run-time inputs the Run dialog / API /
          schedule can override. Referenced in any field as ${param.NAME}. */}
      <button
        onClick={() => setShowParamsDialog(true)}
        className={`px-4 py-2 text-sm font-medium rounded-lg transition-colors flex items-center gap-1.5 ${
          pipelineParameters.length > 0
            ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
            : 'bg-slate-100 text-slate-600 hover:bg-slate-200 border border-transparent'
        }`}
        title="Pipeline Parameters — typed run-time inputs"
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z" />
          <line x1="9" y1="12" x2="15" y2="12" />
          <line x1="12" y1="9" x2="12" y2="15" />
        </svg>
        <span className="hidden xl:inline">Parameters</span>
        {pipelineParameters.length > 0 && (
          <span className="text-[9px] bg-emerald-200 text-emerald-700 px-1 py-0.5 rounded font-bold">
            {pipelineParameters.length}
          </span>
        )}
      </button>

      {/* Close editor — sits directly next to Save so the two primary
          exit paths (save-and-leave / just-leave) are adjacent. The
          click always opens UnsavedChangesModal — dirty state inside
          the modal decides whether the user sees Save/Discard/Cancel
          or a simple Close/Cancel confirm. */}
      <button
        onClick={handleCloseEditor}
        className={`px-4 py-2 text-sm font-semibold rounded-lg transition-colors flex items-center gap-1.5 border ${
          isDirty
            ? 'bg-amber-50 text-amber-700 border-amber-200 hover:bg-amber-100 hover:border-amber-300'
            : 'bg-slate-50 text-slate-600 border-slate-200 hover:bg-slate-100 hover:border-slate-300'
        }`}
        title={isDirty ? `${surfaceCopy.closeLabel} - you have unsaved changes (Ctrl+W)` : `${surfaceCopy.closeLabel} (Ctrl+W)`}
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="19" y1="12" x2="5" y2="12" />
          <polyline points="12 19 5 12 12 5" />
        </svg>
        <span className="hidden xl:inline">{surfaceCopy.closeLabel}</span>
        {isDirty && (
          <span
            className="w-1.5 h-1.5 rounded-full bg-amber-500"
            title="Unsaved changes"
          />
        )}
      </button>

      {/* Save button — one-click quick save as draft. No dialog, no
          name prompt. The name lives in the toolbar header and is the
          user's choice; saving never re-prompts. Use the kebab menu's
          "Save with details" for schedule/alerts/execution settings. */}
      <button
        onClick={handleQuickSave}
        className="px-4 py-2 text-sm font-semibold rounded-lg transition-colors flex items-center gap-1.5 bg-blue-50 text-blue-700 border border-blue-200 hover:bg-blue-100 hover:border-blue-300"
        title={isFileDataPrep ? 'Save this data prep recipe as a draft (Ctrl+S)' : isPipelineDataPrep ? 'Save the pipeline with this data prep step (Ctrl+S)' : 'Save as draft (Ctrl+S)'}
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
          <polyline points="17 21 17 13 7 13 7 21" /><polyline points="7 3 7 8 15 8" />
        </svg>
        <span className="hidden xl:inline">{surfaceCopy.saveLabel}</span>
      </button>

      {/* Run / Stop button — flips while a workflow is executing.
          Stop hits the existing cancel endpoint that the Pool page uses,
          so users have an escape hatch from a stuck or runaway run. */}
      {isRunning ? (
        <button
          onClick={async () => {
            if (!workflowId) return;
            try {
              await api.cancelExecution(workflowId);
              toast.info('Cancelling…', 'Stop signal sent. Running steps will finish current operation, then halt.');
            } catch (e: any) {
              toast.error('Cancel failed', e?.message || 'Could not send stop signal.');
            }
          }}
          className="px-4 py-2 text-white text-sm font-semibold rounded-lg transition-colors flex items-center gap-1.5 shadow-sm"
          style={{ background: 'linear-gradient(135deg, #dc2626, #991b1b)' }}
          title="Stop the running pipeline (cancels execution)"
        >
          <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" stroke="none">
            <rect x="6" y="6" width="12" height="12" rx="1" />
          </svg>
          Stop
        </button>
      ) : (() => {
          const totalSteps = nodes.length;
          const completedSteps = nodes.filter(
            (n) => n.data?.status === 'success' || n.data?.status === 'error',
          ).length;
          const runningSteps = nodes.filter((n) => n.data?.status === 'running').length;
          const inflight = Math.min(completedSteps + (runningSteps > 0 ? 1 : 0), totalSteps);
          return (
            <button
              onClick={async () => {
                const issues = validateWorkflow(nodes, edges, pipelineParameters, workflowId);
                const errors = issues.filter(i => i.level === 'error');
                if (errors.length > 0) {
                  // 2026-06-10: Run now surfaces failures the same way
                  // Publish and Sample do — red rings on the failing
                  // nodes + the validation panel with the issue list.
                  // The old count-only toast ("3 errors found") gave no
                  // path to WHAT or WHERE.
                  showValidationOnNodes(errors);
                  useWorkflowStore.getState().openValidationPanel(issues);
                  return;
                }
                // A4: prompt for parameter overrides when the pipeline
                // declares any — otherwise the editor Run silently uses
                // declared defaults (overrides were only reachable from the
                // Pipelines page / API / schedule before).
                if (pipelineParameters.length > 0) {
                  pendingRunIssues.current = issues;
                  setShowRunParamsDialog(true);
                  return;
                }
                await finishRun(issues);
              }}
              disabled={isRunning}
              className="px-4 py-2 text-white text-sm font-semibold rounded-lg transition-colors flex items-center gap-1.5 shadow-sm disabled:opacity-70 disabled:cursor-wait min-w-[110px] justify-center"
              style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
              title={isRunning ? 'Run in progress - please wait' : surfaceCopy.runTitle}
            >
              {isRunning ? (
                <>
                  <span className="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin shrink-0" />
                  <span className="tabular-nums">Running {inflight}/{totalSteps}…</span>
                </>
              ) : (
                <>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" stroke="none">
                    <polygon points="5 3 19 12 5 21 5 3" />
                  </svg>
                  {surfaceCopy.runLabel}
                </>
              )}
            </button>
          );
        })()}

      {/* Publish button — gated lifecycle step for both tiers. Full
          node-level + end-to-end validation, then save → test → publish
          chained in one click. Sets status to 'published' on success,
          which unlocks scheduling (Free) and Deploy/PROD promotion
          (Plus). Name comes from the toolbar header — never re-prompted. */}
      {!isFileDataPrep && (
      <button
        onClick={async () => {
          const issues = validateWorkflow(nodes, edges, pipelineParameters, workflowId);
          const errors = issues.filter(i => i.level === 'error');
          if (errors.length > 0) {
            showValidationOnNodes(errors);
            useWorkflowStore.getState().openValidationPanel(issues);
            return;
          }
          setStoreValidationErrors({});
          try {
            // Publish reuses the same name-prompt + uniqueness path as
            // Save so a fresh canvas can't slip through and produce an
            // "Untitled Pipeline" row. If the user cancels the name
            // prompt, just bail — no error toast needed.
            const ok = await ensureNamedAndPersist();
            if (!ok) return;
            const id = useWorkflowStore.getState().workflowId;
            if (!id) throw new Error('no workflow id after save');
            // Backend gates /publish on a passing /test result. Run
            // /test first so first-time publish actually succeeds.
            const testResult = await api.testWorkflow(id);
            if (testResult?.status !== 'success') {
              const reason = testResult?.test_results?.error || testResult?.error || 'Test run did not succeed';
              toast.error('Publish blocked', `End-to-end test failed: ${reason}`);
              return;
            }
            await api.publishWorkflow(id);
            setStatus('published');
            toast.success(
              'Pipeline published',
              tier === 'plus'
                ? 'Marked as live. Deploy to PROD is now available.'
                : 'Marked as live. Schedules and triggers are now active.',
            );
          } catch (e: any) {
            toast.error('Publish failed', e?.message || 'Could not publish pipeline. Check server logs.');
          }
        }}
        disabled={isRunning}
        className="px-4 py-2 text-sm font-semibold rounded-lg transition-colors flex items-center gap-1.5 bg-gradient-to-r from-amber-500 to-orange-500 text-white hover:from-amber-600 hover:to-orange-600 shadow-sm disabled:opacity-50"
        title="Validate, test end-to-end, and publish"
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M22 2L11 13" />
          <polygon points="22 2 15 22 11 13 2 9 22 2" />
        </svg>
        Publish
      </button>
      )}

      {/* Deploy to PROD — Plus only, and only enabled for a clean
          published pipeline. Editing a published pipeline returns it
          to draft (see ensureWorkflow), so the user must republish
          before the next promotion. */}
      {!isFileDataPrep && tier === 'plus' && (() => {
        const isPublished = status === 'published';
        const deployBlockedReason = environment === 'prod'
          ? 'Switch to DEV to deploy'
          : !isPublished
            ? 'Publish the pipeline before deploying to PROD'
            : isDirty
              ? 'Save and republish before deploying'
              : 'Deploy pipeline to PROD (via Approvals)';
        const canDeploy = environment !== 'prod' && isPublished && !isDirty && !isRunning;
        return (
          <button
            onClick={async () => {
              if (environment === 'prod') {
                toast.warning('Already in PROD', 'Switch to DEV to deploy changes to production.');
                return;
              }
              if (!isPublished) {
                toast.warning('Not published', 'Publish the pipeline first — Deploy promotes a published pipeline to PROD.');
                return;
              }
              // Defensive validation — pipelines should already be clean
              // post-publish, but a save back to draft could have introduced
              // errors before the user gets here.
              const issues = validateWorkflow(nodes, edges, pipelineParameters, workflowId);
              const errors = issues.filter(i => i.level === 'error');
              if (errors.length > 0) {
                showValidationOnNodes(errors);
                useWorkflowStore.getState().openValidationPanel(issues);
                return;
              }
              setStoreValidationErrors({});
              try {
                // 2026-05-22 (audit E2) — previously this only saved
                // the workflow and lied to the user that it was
                // "submitted for PROD deployment." The full deploy
                // flow goes: save → pre-deploy-check → submit-for-
                // review → /approve → /deploy. The Pipelines page
                // has the full UI for that; from the canvas we do
                // the safer subset: save, run pre-deploy-check, and
                // (if it passes) submit-for-review. The user then
                // approves + deploys from the Pipelines / Approvals
                // page. That makes the button's promise honest.
                const wfId = await ensureWorkflow();
                if (!wfId) throw new Error('Save failed');
                const check = await api.preDeployCheck(wfId).catch((e: any) => ({
                  can_deploy: false,
                  blocking_reasons: [e?.message || 'pre-deploy check failed'],
                }));
                if (!check?.can_deploy) {
                  const reasons = (check?.blocking_reasons || ['unknown']).join('; ');
                  toast.warning(
                    'Pre-deploy check failed',
                    `${reasons} — open the Pipelines page to see the full check + remediate.`,
                  );
                  return;
                }
                await api.submitForReview(wfId);
                toast.success(
                  'Submitted for review',
                  'Pipeline saved + submitted for approval. Approve & Deploy from the Approvals page or the Pipelines list.',
                );
              } catch (e: any) {
                toast.error('Deploy failed', e?.message || 'Could not submit pipeline for deployment');
              }
            }}
            disabled={!canDeploy}
            className="px-4 py-2 text-sm font-semibold rounded-lg transition-colors flex items-center gap-1.5 bg-gradient-to-r from-emerald-500 to-emerald-600 text-white hover:from-emerald-600 hover:to-emerald-700 shadow-sm disabled:opacity-50 disabled:cursor-not-allowed"
            title={deployBlockedReason}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="16 16 12 12 8 16" /><line x1="12" y1="12" x2="12" y2="21" />
              <path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3" />
            </svg>
            Deploy
          </button>
        );
      })()}
      </div>{/* /RIGHT group */}

      {showSaveDialog && <SaveDialog open={showSaveDialog} onClose={() => setShowSaveDialog(false)} />}
      <SaveAsTemplateDialog
        open={saveTplState.open}
        pipelineName={saveTplState.pipelineName}
        steps={saveTplState.steps}
        connections={saveTplState.connections}
        existingNames={saveTplState.existingNames}
        onClose={() => setSaveTplState((s) => ({ ...s, open: false }))}
        onSaved={() => { /* gallery refreshes on next visit */ }}
      />
      <ParametersDialog open={showParamsDialog} onClose={() => setShowParamsDialog(false)} />
      <RunWithParametersDialog
        open={showRunParamsDialog}
        onClose={() => setShowRunParamsDialog(false)}
        workflowName={workflowName}
        parameters={pipelineParameters}
        busy={isRunning}
        onRun={(values) => {
          setShowRunParamsDialog(false);
          void finishRun(pendingRunIssues.current, values);
        }}
      />
      <BackfillModal
        open={showBackfillModal}
        onClose={() => setShowBackfillModal(false)}
        onSubmitted={(backfillId) => {
          // Surface a deep-link the user can follow once Executions
          // page picks up the new Backfills tab.
          try {
            sessionStorage.setItem('fpulse_focused_backfill_id', backfillId);
          } catch { /* ignore */ }
        }}
      />

      {/* Unsaved-changes prompt — mounted unconditionally so its own
          `open` prop controls visibility (keeps animations smooth
          and keyboard listeners attached only while open). */}
      <UnsavedChangesModal
        open={showUnsavedModal}
        dirty={isDirty}
        saving={savingForClose}
        workflowName={workflowName}
        itemLabel={isFileDataPrep ? 'data prep recipe' : 'pipeline'}
        returnLabel={isFileDataPrep ? 'Storage' : isPipelineDataPrep ? 'Connections' : 'the Workflows list'}
        saveActionLabel={isFileDataPrep ? 'Save Draft Recipe & Close' : 'Save & Close'}
        closeActionLabel={surfaceCopy.closeLabel}
        onSaveAndClose={handleModalSaveAndClose}
        onDiscard={handleModalDiscard}
        onCancel={() => setShowUnsavedModal(false)}
      />

    </div>
  );
}
