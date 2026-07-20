# F-Pulse OSS — Frontend Low-Level Design

**Reading order:** §1 module map → §2 component tree → §3 state
(Zustand) → §4 React Flow canvas → §5 ConfigPanel pattern →
§6 routing → §7 API client → §8 pseudo code.

---

## 1. Module map

```
frontend/src/
├── App.tsx                        # routing + layout shell + lazy boundaries
├── main.tsx                       # ReactDOM.render entry
│
├── components/
│   ├── Toolbar.tsx                # top bar — Save / Test / Run / mode toggle
│   ├── EditorContextBar.tsx       # breadcrumb + workspace switcher
│   ├── Sidebar.tsx                # left nav (Dashboard / Pipelines / ...)
│   ├── Canvas.tsx                 # React Flow canvas — nodes/edges/handles
│   ├── ConfigPanel.tsx            # right rail — per-node configuration (huge file, 84+ subcomponents)
│   ├── PreviewPanel.tsx           # bottom rail — last-run row preview
│   ├── CodeEditorPanel.tsx        # Monaco-based code editor (lazy)
│   ├── ChatPanel.tsx              # AI Copilot chat (lazy)
│   ├── ModulesPanel.tsx           # left palette — drag-source for nodes
│   ├── BackfillModal.tsx          # date-range modal + preflight panel (F3)
│   ├── OnboardingWizard.tsx       # first-run flow (gated by localStorage)
│   ├── OSSProductionPlaceholder.tsx
│   ├── GlobalSearch.tsx           # Cmd+K palette
│   ├── OllamaRecommendationBanner.tsx
│   ├── FloatingAgentWidget.tsx    # bottom-right AI chat bubble
│   ├── Toast.tsx                  # toast notifications + toast helper
│   ├── ErrorBoundary.tsx, PanelErrorBoundary.tsx
│   │
│   ├── nodes/                     # React Flow custom node components
│   │   ├── FpulseNode.tsx         # the rendered DAG node (icon, label, handles)
│   │   ├── StickyNote.tsx         # canvas annotation
│   │   └── index.ts               # nodeTypes map for React Flow
│   │
│   ├── shared/
│   │   ├── CertChips.tsx          # connector cert badges (F5)
│   │   ├── ErrorBanner.tsx
│   │   └── ColumnPicker.tsx       # chip-toggle column selector
│   │
│   ├── data-wrangler/             # Stepwise data prep
│   │   └── DataWranglerConfig.tsx
│   │
│   ├── agent/                     # AI agent UI
│   │   ├── FloatingAgentWidget.tsx
│   │   └── ...
│   │
│   └── pages/                     # All lazy-loaded route components
│       ├── DashboardPage.tsx           (eager — default landing)
│       ├── LoginPage.tsx               (eager)
│       ├── PipelinesPage.tsx           (lazy)
│       ├── TemplatesPage.tsx
│       ├── ExecutionsPage.tsx
│       ├── ConnectionsPage.tsx
│       ├── CredentialsPage.tsx
│       ├── ProjectsPage.tsx
│       ├── StoragePage.tsx
│       ├── StoragePreviewDrawer.tsx    # the drawer with the F2 provenance card
│       ├── LineagePage.tsx
│       ├── HelpPage.tsx
│       ├── TrustPage.tsx
│       ├── CertMatrixPage.tsx
│       ├── ActivityPage.tsx
│       ├── ReportsPage.tsx
│       ├── AIPage.tsx
│       ├── AccountPage.tsx
│       ├── SettingsPage.tsx
│       ├── NotificationsPage.tsx
│       ├── ExecutionPoolPage.tsx
│       └── ExtractionPage.tsx
│
├── stores/
│   ├── workflowStore.ts           # Zustand — THE big store (nodes/edges/save/run/undo)
│   ├── projectStore.ts
│   ├── connectionStore.ts
│   └── ...
│
├── api/
│   ├── client.ts                  # fetch wrapper + typed methods (api.getWorkflow, ...)
│   └── types.ts                   # response shapes
│
├── hooks/
│   ├── useUpstreamSchema.ts       # propagates column schema to ConfigPanel
│   ├── usePageContext.ts
│   └── useWorkspace.ts
│
├── utils/
│   ├── nodeArity.ts               # MULTI_INPUT_NODES + contractFor
│   ├── nodeMetadata.ts            # category icons, colours, legacy mapping
│   ├── validateWorkflow.ts        # client-side pre-save validator
│   ├── modulesPanelData.ts        # palette curation per level
│   ├── routePrefetch.ts           # warm chunks on hover
│   ├── hiddenNodeTypes.ts         # nodes hidden behind generic source/destination
│   └── idempotency.ts             # client classification mirror
│
├── ui/
│   ├── dialog.tsx                 # DialogRoot + uiConfirm (replaces window.confirm)
│   ├── Icon.tsx, Tooltip.tsx, Toggle.tsx, Select.tsx
│   └── ...
│
├── types.ts                       # Workflow, Step, Connection, ... mirror of backend IR
└── index.css                      # Tailwind directives
```

---

## 2. Component tree (top-level)

```
<App>                                              # router + layout
 ├─ <DialogRoot/>                                  # confirm modals (uiConfirm)
 ├─ <Toast/>                                       # toast container
 ├─ <Sidebar/>                                     # left nav (eager)
 │
 ├─ <Suspense fallback=<LoadingFallback/>>
 │   ├─ if route="/dashboard":   <DashboardPage/>
 │   ├─ if route="/pipelines":   <PipelinesPage/>
 │   ├─ if route="/storage":     <StoragePage/>
 │   │                               └─ <StoragePreviewDrawer/>
 │   ├─ if route="/lineage":     <LineagePage/>
 │   ├─ if route="/connections": <ConnectionsPage/>
 │   ├─ if route="/credentials": <CredentialsPage/>
 │   ├─ if route="/executions":  <ExecutionsPage/>
 │   └─ if route="/editor":      <EditorLayout>
 │                                  ├─ <Toolbar/>
 │                                  ├─ <EditorContextBar/>
 │                                  ├─ <ModulesPanel/>             # left palette
 │                                  ├─ <Canvas/>                   # React Flow
 │                                  │     └─ <FpulseNode/> (per-node)
 │                                  ├─ <ConfigPanel/>              # right rail
 │                                  │     └─ <XxxConfig/> (per-stepType)
 │                                  ├─ <PreviewPanel/>             # bottom rail
 │                                  └─ <ChatPanel/>                # right-of-rail
 │
 ├─ <GlobalSearch/>                                # Cmd+K modal (lazy)
 ├─ <OnboardingWizard/>                            # first-run only (lazy)
 ├─ <OllamaRecommendationBanner/>                  # conditional (lazy)
 ├─ <FloatingAgentWidget/>                         # bottom-right chat (lazy)
 └─ <CopyrightFooter/>                             # bottom strip
```

**Eager** on first paint: `App`, `Sidebar`, `DashboardPage`,
`LoginPage`, `Toolbar`, `EditorContextBar`, `ErrorBoundary`,
`Toast`, `CopyrightFooter`.

Everything else is lazy via React.lazy + Suspense (split into ~30
chunks; main bundle is 722 KB post X2).

---

## 3. State — Zustand store class diagram

```
┌────────────────────────────────────────────────────────────────┐
│                         WorkflowStoreState                      │
│ ────────────────────────────────────────────────              │
│  // IR mirror                                                   │
│ + workflowId: string | null                                     │
│ + workflowName: string                                          │
│ + workflowDescription: string                                   │
│ + workflowMetadata: Record<string, any>                         │
│ + workflowStatus: "draft" | "tested" | "published" | ...        │
│ + workflowParameters: WorkflowParameter[]                       │
│ + nodes: Node[]            // React Flow nodes                  │
│ + edges: Edge[]            // React Flow edges                  │
│                                                                 │
│  // UI / editor state                                           │
│ + selectedNodeId: string | null                                 │
│ + activeTab: "parameters" | "mapping" | "settings"              │
│ + codeEditorOpen: boolean                                       │
│ + dirty: boolean                                                │
│ + lastSavedAt: string | null                                    │
│ + undoStack: WorkflowSnapshot[]                                 │
│ + redoStack: WorkflowSnapshot[]                                 │
│                                                                 │
│  // Run / preview state                                         │
│ + lastRunResult: WorkflowRunResult | null                       │
│ + stepPreviews: Record<step_id, StepPreview>                    │
│ + runningSteps: Set<string>                                     │
│                                                                 │
│  // Actions (mutators)                                          │
│ + addNode(type, position?)                                      │
│ + deleteNode(nodeId)                                            │
│ + updateNodeParams(nodeId, patch)                               │
│ + updateNodeLabel(nodeId, label)                                │
│ + onConnect(edgeParams)                                         │
│ + onNodesChange(changes) / onEdgesChange(changes)               │
│ + setSelectedNode(nodeId)                                       │
│ + setActiveTab(tab)                                             │
│ + setCodeEditorOpen(open)                                       │
│ + pushUndoState() / undo() / redo()                             │
│                                                                 │
│ + saveWorkflow(opts?)         → POST /api/workflows or PUT      │
│ + loadWorkflow(workflowId)    → GET  /api/workflows/{id}        │
│ + runWorkflow()               → POST /api/execute/workflow/{id} │
│ + runStep(nodeId)             → POST /api/execute/workflow/    │
│                                  {id}/step/{nodeId}             │
│ + previewRun()                → POST .../execute?preview=true   │
└────────────────────────────────────────────────────────────────┘
```

The store is the single source of truth for canvas state. React Flow
nodes/edges live here so undo/redo and persistence are uniform.

---

## 4. React Flow canvas integration

```
┌──────────────────────────────────────────────────────────────┐
│                       <Canvas/>                               │
│                                                              │
│  const { nodes, edges, onNodesChange, onEdgesChange,         │
│           onConnect, addNode } = useWorkflowStore();         │
│                                                              │
│  return (                                                    │
│    <ReactFlow                                                │
│      nodes={nodes}                                           │
│      edges={edges}                                           │
│      nodeTypes={{ fpulseNode: FpulseNode,                    │
│                   stickyNote: StickyNote }}                  │
│      onNodesChange={onNodesChange}                           │
│      onEdgesChange={onEdgesChange}                           │
│      onConnect={onConnect}                                   │
│      onDrop={handleDrop}    // from ModulesPanel             │
│      onPaneClick={() => store.setSelectedNode(null)}         │
│    >                                                         │
│      <Background/> <Controls/> <MiniMap/>                    │
│    </ReactFlow>                                              │
│  );                                                          │
└──────────────────────────────────────────────────────────────┘
```

**Custom node** (`FpulseNode`) renders:
- a coloured header with the category icon
- the node's display label (editable inline)
- 0/1/many input handles (driven by `MULTI_INPUT_NODES` /
  `contractFor`)
- 1 output handle (always)
- side-effect badge (`SIDE_EFFECT_CLASS` → coloured pill)
- error indicator + last-run row count (if a run has happened)

**Edge handling** — connections store `from_port`/`to_port` so
branching nodes (if_condition, switch_case) can label outputs
("true", "false", "case_a"). The executor now reads these via
`_input_step_ports` (R6).

---

## 5. ConfigPanel pattern

`ConfigPanel.tsx` is the largest file in the frontend (~10K lines)
because it contains one `XxxConfig` component per StepType. The
shape is uniform:

```
ConfigPanel
 ├── Tab bar:  [ Parameters | Mapping (sinks only) | Settings ]
 │
 ├── Parameters tab:
 │     ├── ConnectionPicker (if this node needs credentials)
 │     ├── <XxxConfig> dispatch by stepType:
 │     │     ├── csv_source       → <CsvSourceConfig/>
 │     │     ├── db_source        → <DbSourceConfig/> (+ <SyncModeField/> F1)
 │     │     ├── filter           → <FilterConfig/>
 │     │     ├── aggregate        → <AggregateConfig/>
 │     │     ├── join             → <JoinConfig/>
 │     │     ├── derived_column   → <DerivedColumnConfig/> (R2 window mode)
 │     │     ├── data_wrangler    → <DataWranglerConfig/>
 │     │     ├── ... (84 more)
 │     │     └── DEFAULT → <DynamicConfig/> driven by /api/node-types param_schema
 │     │
 │     └── ColumnMapper (for sinks: source → destination column map)
 │
 ├── Mapping tab (sinks only): standalone <ColumnMapper/> + chip strip
 │
 └── Settings tab: per-node execution policy (P8)
       ├── execute_once toggle
       ├── retry_on_fail + max_retries + retry_delay_ms + retry_strategy
       ├── on_error select (stop / continue / continue_error_output)
       ├── timeout_sec
       ├── always_output toggle
       ├── notes + display_note toggle
       └── (writes everything to params._settings — executor reads it)
```

**ConfigProps contract** every Config component receives:

```ts
interface ConfigProps {
  params: Record<string, any>;          // current step.params
  nodeId: string;                        // step.id
  onChange: (nodeId: string, patch: Record<string, any>) => void;
  columns?: { name: string; type: string }[];  // upstream column schema
  upstreamNodes?: UpstreamMeta[];               // for multi-input (Join, Transform)
  allAncestors?: UpstreamMeta[];                // for Transform's $('label') refs
  isSink?: boolean;                              // for cloud connector configs
  sourceTypes?: ...;                             // for ColumnMapper
  mappingOnly?: "hide" | "only";                 // tab-mode hint
}
```

This contract makes Config components highly reusable — the schema
flows from `useUpstreamSchema` hook → ConfigPanel → component.

---

## 6. Routing

Hash-based routing (no react-router). The hash determines the
mounted page; the editor uses `/editor/{workflow_id}`.

```ts
// App.tsx (simplified)
const [route, setRoute] = useState(window.location.hash.slice(1) || 'dashboard');

useEffect(() => {
  const onHashChange = () => setRoute(window.location.hash.slice(1) || 'dashboard');
  window.addEventListener('hashchange', onHashChange);
  return () => window.removeEventListener('hashchange', onHashChange);
}, []);

if (route.startsWith('editor/')) return <Editor workflowId={route.split('/')[1]}/>;
if (route === 'pipelines')       return <PipelinesPage/>;
if (route === 'storage')         return <StoragePage/>;
// ...
```

`routePrefetch.ts` warms the React.lazy chunks on link hover so the
navigation is instant.

---

## 7. API client pattern

```ts
// frontend/src/api/client.ts (simplified)
class ApiClient {
  private base = '/api';
  private token = () => localStorage.getItem('fpulse_token') || '';
  private workspace = () => localStorage.getItem('fpulse_workspace_id') || 'default';

  async get<T>(path: string): Promise<T> {
    const r = await fetch(this.base + path, {
      headers: this.headers(),
      credentials: 'include',
    });
    if (!r.ok) throw new ApiError(r.status, await r.text());
    return r.json();
  }

  async post<T>(path: string, body: any): Promise<T> { /* ... */ }
  async put<T>(path: string, body: any): Promise<T>  { /* ... */ }
  async delete<T>(path: string): Promise<T>          { /* ... */ }

  // Typed convenience methods (just thin wrappers):
  getWorkflow(id: string)               { return this.get(`/workflows/${id}`); }
  updateWorkflow(id, wf, summary='')    { return this.put(`/workflows/${id}`, { workflow: wf, change_summary: summary }); }
  createBackfill(body)                  { return this.post('/executions/backfill', body); }
  getCertMatrix()                       { return this.get('/connectors/cert-matrix'); }
  // ... ~80 methods total

  private headers() {
    return {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${this.token()}`,
      'X-Workspace-Id': this.workspace(),
    };
  }
}

export const api = new ApiClient();
```

Three things always travel together: Bearer token, workspace id,
JSON content-type. Components import `api` and call typed methods.

---

## 8. Pseudo code for the big UI flows

### 8.1 `workflowStore.addNode` — drag from palette + macro resolution

```pseudo
function addNode(type, position?):
    pushUndoState()
    id = random_short_id()
    stepType = type
    initialParams = {}
    label = humanize(type)
    category = NODE_CATEGORY[type] || "transform"

    // R7 — Macros + virtual connector types like "execute_pipeline:wf-123"
    // are resolved by reading the cached /api/node-types payload.
    backendTypes = window.__fpulse_node_types or []
    meta = backendTypes.find(t => t.type == type)
    if meta:
        if meta.base_type:      stepType = meta.base_type
        if meta.default_params: initialParams = clone(meta.default_params)
        if meta.label:          label = meta.label
        if meta.category:       category = meta.category

    if not position:
        existing = state.nodes
        position = existing.length > 0
            ? { x: max(existing.x) + 280, y: avg(existing.y) }
            : { x: 100, y: 100 }

    node = {
        id, type: "fpulseNode",
        position,
        data: {
            label, stepType,
            params: initialParams,
            color: NODE_COLORS[stepType],
            icon: NODE_ICONS[stepType],
            category,
            risk: "low",
        },
    }
    state.nodes = [...state.nodes, node]
```

### 8.2 `BackfillModal` preflight side effect (F3)

```pseudo
useEffect(() => {
    if (!open || !workflowId || windowCount <= 0):
        setPreflightResult(null); return

    handle = setTimeout(async () => {
        setPreflightLoading(true)
        try:
            res = await api.post('/executions/backfill/preflight', {
                pipeline_id: workflowId,
                start_date, end_date,
                window_size, window_size_hours: customHours,
                cursor_param_names: [startParam, endParam],
                concurrency, on_failure,
                parameter_values: {},
                acknowledge_side_effects: acknowledgeSideEffects,
            })
            setPreflightResult(res)
        catch:
            setPreflightResult(null)
        finally:
            setPreflightLoading(false)
    }, 350)                          # debounce so dates dragging doesn't spam

    return () => clearTimeout(handle)
}, [open, workflowId, startDate, endDate, windowSize, ...])
```

### 8.3 `SyncModeField` — Last cursor + Reset State (F1)

```pseudo
function SyncModeField({ params, nodeId, onChange }):
    workflowId = useWorkflowStore(s => s.workflowId)
    syncMode = params.sync_mode
              ?? (params.incremental === true ? "incremental" : "full_refresh")

    [stored, setStored] = useState(null)

    useEffect(() => {
        if syncMode != "incremental": setStored(null); return
        fetch(`/api/sync-state/${workflowId}/${nodeId}`)
          .then(r => r.json())
          .then(body => setStored(body.state))
    }, [workflowId, nodeId, syncMode])

    onResetState = async () => {
        if not confirm("Reset cursor? Next run reads everything."): return
        await fetch(`/api/sync-state/${workflowId}/${nodeId}`, { method: "DELETE" })
        setStored(null)
    }

    return:
        <Select label="Sync Mode"
                value={syncMode}
                options={["full_refresh", "incremental", "cdc"]}
                onChange={v => onChange(nodeId, { sync_mode: v, incremental: undefined })} />

        if syncMode == "incremental":
            <TextInput label="Cursor Column" value={params.watermark_column} ... />
            <TextInput label="Manual cursor override (optional)" value={params.watermark_value} ... />
            <Card>
                <header>Sync state  <button onClick={onResetState}>Reset state</button></header>
                if stored:
                    <div>Last cursor: <code>{stored.last_cursor}</code></div>
                    <div>Last run:   {format(stored.last_run_at)}</div>
                    <div>Rows last run: {stored.rows_last_run}</div>
                else:
                    <div>No prior sync — first run will read everything.</div>
            </Card>

        if syncMode == "cdc":
            <Banner>Use the dedicated CDC Source node for log-based replication.</Banner>
```

### 8.4 ConfigPanel — Settings tab writes `params._settings`

```pseudo
function ConfigPanel({ node }):
    [activeTab, setActiveTab] = useState("parameters")
    settings = node.data.params._settings || {}

    updateSettings = (key, value) =>
        updateNodeParams(node.id, { _settings: { ...settings, [key]: value } })

    if activeTab == "settings":
        return:
            <Toggle label="Execute Once" value={settings.execute_once}
                    onChange={v => updateSettings("execute_once", v)} />

            <Toggle label="Retry On Fail" value={settings.retry_on_fail}
                    onChange={v => updateSettings("retry_on_fail", v)} />

            if settings.retry_on_fail:
                <NumberInput label="Max Retries"
                             value={settings.max_retries ?? 3}
                             onChange={v => updateSettings("max_retries", v)} />
                <NumberInput label="Retry Delay (ms)" ... />
                <Select      label="Retry Strategy"
                             options={["fixed", "linear", "exponential"]}
                             value={settings.retry_strategy ?? "exponential"}
                             onChange={v => updateSettings("retry_strategy", v)} />

            <Select label="On Error"
                    options={["stop", "continue", "continue_error_output"]}
                    value={settings.on_error ?? "stop"}
                    onChange={v => updateSettings("on_error", v)} />

            <NumberInput label="Timeout (seconds)"
                         value={settings.timeout_sec ?? 300}
                         onChange={v => updateSettings("timeout_sec", v)} />

            <Toggle label="Always Output Data" value={settings.always_output}
                    onChange={v => updateSettings("always_output", v)} />

            <TextArea label="Notes" value={settings.notes ?? ""} ... />
            <Toggle label="Display Note in Flow" value={settings.display_note} ... />
```

These values flow to the backend via the next `saveWorkflow()` call,
get persisted in the workflow blob, and read by
`WorkflowExecutor._execute_step` at run time.

---

## 9. Build-time architecture

Vite builds two trees in `dist/`:

```
dist/
├── index.html                   # references the main chunk + assets
├── assets/
│   ├── index-<hash>.js          # 722 KB — first-paint bundle
│   ├── index-<hash>.css         # Tailwind compiled output
│   ├── ConfigPanel-<hash>.js    # 289 KB
│   ├── PipelinesPage-<hash>.js  # 231 KB
│   ├── ConnectionsPage-<hash>.js # 181 KB
│   ├── ExecutionsPage-<hash>.js # 102 KB
│   ├── ... ~30 more chunks
│   └── images, fonts
```

The backend serves `dist/` as static via `StaticFiles` mounted at
`/` (after all `/api/*` routes), so a single Docker container ships
both frontend and backend at port 8001.
