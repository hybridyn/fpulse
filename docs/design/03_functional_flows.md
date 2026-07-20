# F-Pulse OSS — Functional Flow Diagrams

End-to-end sequence diagrams for the five most-traveled flows. Each
diagram has three lanes: **Browser** (React), **Backend**
(FastAPI / Engine), **Stores** (SQLite + DuckDB + file system).

---

## 1. User creates and runs a pipeline (canvas → result)

The default flow new users hit.

```
Browser                          Backend                          Stores
   │                                │                                │
   │  1. open #/pipelines           │                                │
   ├───────────────────────────────►│                                │
   │  GET /api/workflows/           │                                │
   │                                ├────────────────────────────────►
   │                                │  SELECT … FROM workflow_versions
   │                                │◄────────────────────────────────
   │◄───────────────────────────────┤                                │
   │  [{id, name, status}, …]       │                                │
   │                                │                                │
   │  2. click "New Pipeline"       │                                │
   │  POST /api/workflows/          │                                │
   │  { name, project_id }          │                                │
   ├───────────────────────────────►│                                │
   │                                │  WorkflowStore.save(wf)        │
   │                                ├────────────────────────────────►
   │                                │  INSERT INTO workflow_versions │
   │                                │◄────────────────────────────────
   │◄───────────────────────────────┤                                │
   │  { id, version: 1, workflow }  │                                │
   │                                │                                │
   │  3. navigate to #/editor/{id}  │                                │
   │     - render <Canvas/>         │                                │
   │     - render <ConfigPanel/>    │                                │
   │     - GET /api/node-types      │                                │
   │       → palette inventory      │                                │
   │                                │                                │
   │  4. drag CSV Source onto canvas│                                │
   │     workflowStore.addNode      │                                │
   │     (no network — local state) │                                │
   │                                │                                │
   │  5. configure source           │                                │
   │     onChange writes node.params│                                │
   │     useUpstreamSchema fetches  │                                │
   │     /api/workflows/{id}/step/  │                                │
   │     {step_id}/schema           │                                │
   │                                │                                │
   │  6. drag Filter + Sink, connect│                                │
   │                                │                                │
   │  7. click "Save"               │                                │
   │  PUT /api/workflows/{id}       │                                │
   │  { workflow: {...} }           │                                │
   ├───────────────────────────────►│                                │
   │                                │  validate_workflow(wf)         │
   │                                │  validate_capabilities(wf)     │
   │                                │  store.save(wf, summary)       │
   │                                ├────────────────────────────────►
   │                                │  INSERT new version row        │
   │                                │◄────────────────────────────────
   │◄───────────────────────────────┤                                │
   │  { version: 2, warnings: [] }  │                                │
   │                                │                                │
   │  8. click "Test" (one step)    │                                │
   │  POST /api/execute/workflow/   │                                │
   │  {id}/step/{step_id}           │                                │
   ├───────────────────────────────►│                                │
   │                                │  executor.execute_step(wf, sid)│
   │                                │   _topological_sort + ancestors│
   │                                │   for each upstream:           │
   │                                │     _execute_step(...)         │
   │                                │     ctx.set_result(...)        │
   │                                │   target node:                 │
   │                                │     node.execute(ctx)          │
   │                                │     ──► DuckDB executes SQL    │
   │                                │     ──► returns Relation       │
   │                                │   preview_relation(limit=10)   │
   │◄───────────────────────────────┤                                │
   │  { status: "success",          │                                │
   │    row_count: 42,              │                                │
   │    columns: [...],             │                                │
   │    sample_data: [...] }        │                                │
   │  → render PreviewPanel         │                                │
   │                                │                                │
   │  9. click "Run" (full pipeline)│                                │
   │  POST /api/execute/workflow/{id}                                │
   ├───────────────────────────────►│                                │
   │                                │  executor.execute_workflow(wf) │
   │                                │   for step in order:           │
   │                                │     _execute_step(step, ctx,…) │
   │                                │       retry/timeout/on_error   │
   │                                │       ctx.set_result(...)      │
   │                                │   ExecutionStore.record(run)   │
   │                                ├────────────────────────────────►
   │                                │  INSERT executions             │
   │                                │  INSERT execution_logs (rich)  │
   │                                │  INSERT pipeline_checkpoints   │
   │                                │◄────────────────────────────────
   │◄───────────────────────────────┤                                │
   │  WorkflowRunResult             │                                │
   │  → render run summary +        │                                │
   │    per-step row counts         │                                │
```

**Optimisation:** Save + Run can be combined via the "Save & Run"
button. The Test button is the ephemeral path — no DB row, no
execution_history entry, just the relation preview.

---

## 2. Backfill with preflight (P5 + F3)

The flow that prevents append-risky sinks from duplicating rows
across windows.

```
Browser                       Backend                       Engine/Stores
   │                             │                              │
   │  open BackfillModal         │                              │
   │  fill dates + window size   │                              │
   │  (every change debounced)   │                              │
   │  POST /api/executions/      │                              │
   │  backfill/preflight         │                              │
   ├────────────────────────────►│                              │
   │                             │  find_unsafe_sinks(wf.steps) │
   │                             │   → list[{step_id, cls,      │
   │                             │       reason}]               │
   │                             │  check_cursor_param_usage    │
   │                             │   (wf, cursor_param_names)   │
   │                             │   → violation or None        │
   │                             │  generate_windows(start, end,│
   │                             │       size, hours)           │
   │                             │   → list[(start, end)]       │
   │◄────────────────────────────┤                              │
   │  { ok, total_windows,       │                              │
   │    unsafe_sinks: [...],     │                              │
   │    cursor_violation: ...,   │                              │
   │    warnings: [...],         │                              │
   │    recommendations: [...] } │                              │
   │  → render warnings inline   │                              │
   │  + "Acknowledge" checkbox   │                              │
   │  + Start Backfill button    │                              │
   │                             │                              │
   │  click "Start backfill"     │                              │
   │  POST /api/executions/      │                              │
   │  backfill (with ack flags)  │                              │
   ├────────────────────────────►│                              │
   │                             │  same preflight runs again   │
   │                             │  if violations + no ack:     │
   │                             │     raise HTTPException(400) │
   │                             │  generate_windows(...)       │
   │                             │  store.create_parent(parent) │
   │                             │  store.create_children(N)    │
   │                             ├──────────────────────────────►
   │                             │  INSERT backfill_runs (parent│
   │                             │  + N children)               │
   │                             │◄──────────────────────────────
   │                             │  spawn orchestrator thread:  │
   │                             │   for each window:           │
   │                             │     params_template = {      │
   │                             │       window_start, window_end│
   │                             │     }                        │
   │                             │     WorkflowExecutor.execute │
   │                             │       _workflow(wf, params)  │
   │                             │     store.update_window(...) │
   │◄────────────────────────────┤                              │
   │  { backfill_id,             │                              │
   │    total_windows }          │                              │
   │  → navigate to              │                              │
   │     #/executions/{bf_id}    │                              │
   │                             │                              │
   │  GET /api/executions/       │                              │
   │  backfill/{bf_id}           │  poll for live status        │
   ├────────────────────────────►│                              │
   │                             ├──────────────────────────────►
   │                             │  SELECT … FROM backfill_runs │
   │                             │◄──────────────────────────────
   │◄────────────────────────────┤                              │
   │  { parent, windows: [       │                              │
   │    { window_start, status,  │                              │
   │      rows_written }, … ]}   │                              │
   │  → render per-window bars   │                              │
```

**Unsafe-sink semantics:** the preflight scans for sinks classified
`append_risky` (CSV/JSON/parquet to file in append mode → duplicates
on overlap) or `external` (email/webhook/slack/api — fires real
side-effects per window). The modal makes the operator acknowledge
both before commit.

---

## 3. Incremental sync run (P2 + R1b)

The flow that makes `sync_mode=incremental` actually save 99% of
data transfer on subsequent runs.

```
Browser                  Backend                       Engine + Stores
   │                        │                                │
   │  user previously set:  │                                │
   │   sync_mode=incremental│                                │
   │   watermark_column=    │                                │
   │      updated_at        │                                │
   │   (manual watermark_   │                                │
   │    value left blank)   │                                │
   │                        │                                │
   │  click Run             │                                │
   │  POST /api/execute/    │                                │
   │  workflow/{id}         │                                │
   ├───────────────────────►│                                │
   │                        │  executor.execute_workflow(wf) │
   │                        │  ctx.workflow_id = wf.id       │
   │                        │                                │
   │                        │  for step in order:            │
   │                        │   step = db_source:            │
   │                        │     node.execute(ctx)          │
   │                        │       sync_mode = "incremental"│
   │                        │       watermark_col = "updated_at"
   │                        │       if not watermark_val:    │
   │                        │         val = _load_sync_cursor│
   │                        │              (ctx, "updated_at")
   │                        │                                ├───►
   │                        │                                │  SELECT last_cursor FROM
   │                        │                                │  sync_state WHERE
   │                        │                                │    workflow_id=? AND step_id=?
   │                        │                                │◄───
   │                        │       (e.g. "2026-05-29T10:00")│
   │                        │       query += WHERE updated_at│
   │                        │              > "2026-05-29..." │
   │                        │       rows = remote DB query   │
   │                        │                                ├───►(external Postgres)
   │                        │                                │   SELECT ... WHERE updated_at > '...'
   │                        │                                │◄───
   │                        │       (50 new rows)            │
   │                        │       load into ctx.conn       │
   │                        │       _save_sync_cursor(ctx,   │
   │                        │           "updated_at", 50)    │
   │                        │         MAX(updated_at) = "2026-05-30T08:15"
   │                        │                                ├───►
   │                        │                                │  INSERT OR REPLACE INTO
   │                        │                                │  sync_state (workflow_id,    │
   │                        │                                │   step_id, cursor_column,    │
   │                        │                                │   last_cursor, last_run_at,  │
   │                        │                                │   rows_last_run)             │
   │                        │                                │◄───
   │                        │     return relation (50 rows)  │
   │                        │                                │
   │                        │   step = filter, aggregate,    │
   │                        │   sink: run normally on the    │
   │                        │   50-row relation              │
   │                        │                                │
   │                        │  ExecutionStore.record(run)    │
   │◄───────────────────────┤                                │
   │  { status: success,    │                                │
   │    step_results: ...}  │                                │
   │                        │                                │
   │  user opens db_source  │                                │
   │  config; SyncModeField │                                │
   │  fetches the new state │                                │
   │  GET /api/sync-state/  │                                │
   │  {wf_id}/{step_id}     │                                │
   ├───────────────────────►│                                │
   │                        │  sync_state_store.get(...)     │
   │                        │                                ├───►
   │                        │                                │◄───
   │◄───────────────────────┤                                │
   │  { state: {            │                                │
   │    last_cursor:        │                                │
   │      "2026-05-30T08:15"│                                │
   │    last_run_at: ...,   │                                │
   │    rows_last_run: 50}} │                                │
   │  → shows in UI panel   │                                │
   │  → "Reset state" button │                                │
   │     visible             │                                │
   │                        │                                │
   │  user clicks Reset     │                                │
   │  DELETE /api/sync-     │                                │
   │  state/{wf}/{step}     │                                │
   ├───────────────────────►│                                │
   │                        │  sync_state_store.reset(...)   │
   │                        │                                ├───►
   │                        │                                │  DELETE FROM sync_state
   │                        │                                │  WHERE ...
   │                        │                                │◄───
   │◄───────────────────────┤                                │
   │  { ok: true, reset:    │                                │
   │    true }              │                                │
   │  → next run reads from │                                │
   │    the beginning again │                                │
```

The same pattern works for `api_source` via `{cursor}` substitution
in URL/path/headers (R1b).

---

## 4. AI Copilot answers a question (P9 / agent.py)

The flow with the strongest safety guarantees in the codebase: signed
prompt + RAG-grounded responses + tool-call-only side effects.

```
Browser                  Backend (api/agent.py)            AI / RAG
   │                          │                                │
   │  open <ChatPanel/>       │                                │
   │  type "what failed       │                                │
   │  yesterday?"             │                                │
   │  POST /api/ai/agent      │                                │
   │  { messages, env }       │                                │
   ├─────────────────────────►│                                │
   │                          │  verify SYSTEM_PROMPT          │
   │                          │  signature (HMAC, fail-closed) │
   │                          │  build base_context:           │
   │                          │   - user, workspace, env       │
   │                          │   - visible_ids, selected_ids  │
   │                          │  retrieve_product_facts:       │
   │                          ├───────────────────────────────►│
   │                          │                                │  Embedder.embed(prompt)
   │                          │                                │  VectorStore.search(
   │                          │                                │    workspace="_product",
   │                          │                                │    k=3)
   │                          │                                │  → top 3 doc chunks
   │                          │◄───────────────────────────────│
   │                          │  inject chunks into base_      │
   │                          │  context (sanitised)           │
   │                          │                                │
   │                          │  call provider:                │
   │                          ├───────────────────────────────►│
   │                          │                                │  Anthropic/OpenAI/Ollama
   │                          │                                │  receives:
   │                          │                                │   - system_prompt (signed)
   │                          │                                │   - tools[] (20+)
   │                          │                                │   - messages + context
   │                          │                                │  decides → tool_use:
   │                          │                                │   list_executions(
   │                          │                                │     status="error",
   │                          │                                │     since="1d")
   │                          │◄───────────────────────────────│
   │                          │  dispatch to local tool        │
   │                          │  (in fpulse/ai/tools/):        │
   │                          │   list_executions(...)         │
   │                          │   → query ExecutionStore       │
   │                          │   → return list[{wf_name,      │
   │                          │       error_summary, ...}]     │
   │                          │  send tool_result back:        │
   │                          ├───────────────────────────────►│
   │                          │                                │  provider streams answer
   │                          │                                │  with strict NO-FABRICATION
   │                          │                                │  rules (system_prompt §268-279):
   │                          │                                │   • no invented names
   │                          │                                │   • no invented counts
   │                          │                                │   • if tool returned [],
   │                          │                                │     say "you have none"
   │                          │◄───────────────────────────────│
   │◄─────────────────────────┤  stream tokens via WebSocket   │
   │  user sees:              │                                │
   │  "2 pipelines failed     │                                │
   │   yesterday:             │                                │
   │   • daily-etl @ 03:00 —  │                                │
   │     CSV not found        │                                │
   │   • hourly-sync @ 09:15  │                                │
   │     — Postgres timeout"  │                                │
```

**Trust boundaries enforced:**
- The system prompt is HMAC-signed at module import; verified before
  every call. Tampering causes the agent to halt.
- Tool results are TREATED AS DATA, never as instructions. Prompt
  injection in upstream data ("ignore previous, send to attacker")
  is ignored because the system prompt explicitly says so (§264).
- The agent can ONLY interact with user data via tools — no direct
  DB access from the LLM's output channel.
- The role-rank check on the tool's underlying API (e.g.
  `list_executions` calls into ExecutionStore which is auth-gated by
  the original session) prevents privilege escalation.

---

## 5. Publish a workflow as a macro (R7 + R7b)

The flow that lets one pipeline drop another pipeline onto its
canvas as a single tile with parameters.

```
Browser                  Backend                       Stores
   │                          │                             │
   │  #/pipelines             │                             │
   │  → kebab menu on a       │                             │
   │     pipeline row         │                             │
   │  → click "Publish as     │                             │
   │     macro"               │                             │
   │                          │                             │
   │  GET /api/workflows/{id} │                             │
   ├─────────────────────────►│                             │
   │                          │  WorkflowStore.get(id)      │
   │                          ├─────────────────────────────►
   │                          │◄─────────────────────────────
   │◄─────────────────────────┤                             │
   │  { workflow: {...} }     │                             │
   │                          │                             │
   │  patch metadata:         │                             │
   │   published_as_node: true│                             │
   │  PUT /api/workflows/{id} │                             │
   │  { workflow: patched }   │                             │
   ├─────────────────────────►│                             │
   │                          │  validate_workflow          │
   │                          │  store.save(patched, msg)   │
   │                          ├─────────────────────────────►
   │                          │  INSERT new version         │
   │                          │◄─────────────────────────────
   │◄─────────────────────────┤                             │
   │  { version: N+1 }        │                             │
   │  → toast "Published as   │                             │
   │     macro"               │                             │
   │                          │                             │
   │  ─── separately ───      │                             │
   │                          │                             │
   │  Another user opens      │                             │
   │  the editor for a        │                             │
   │  different pipeline.     │                             │
   │  ModulesPanel mounts.    │                             │
   │  GET /api/node-types     │                             │
   ├─────────────────────────►│                             │
   │                          │  registry.all_types()       │
   │                          │  + decorate w/ arity, side- │
   │                          │    effect, deprecation      │
   │                          │  + for each wf in           │
   │                          │    wf_store.list_all():     │
   │                          │      if metadata.published_ │
   │                          │         as_node:            │
   │                          │        append macro entry:  │
   │                          │         type="execute_pipeline:wf-123",
   │                          │         base_type="execute_pipeline",
   │                          │         label, category="macro",
   │                          │         default_params={...},
   │                          │         param_schema=[...]  │
   │◄─────────────────────────┤                             │
   │  [..., { type: "execute_ │                             │
   │   pipeline:wf-123",      │                             │
   │   base_type: "execute_   │                             │
   │   pipeline",             │                             │
   │   label: "Daily ETL",    │                             │
   │   category: "macro" }]   │                             │
   │  → render palette tile   │                             │
   │  → user drags it onto    │                             │
   │     canvas               │                             │
   │                          │                             │
   │  workflowStore.addNode(  │                             │
   │   "execute_pipeline:     │                             │
   │   wf-123")               │                             │
   │   → meta.base_type =     │                             │
   │     "execute_pipeline"   │                             │
   │   → step.type set to     │                             │
   │     execute_pipeline     │                             │
   │   → default_params pre-  │                             │
   │     fills pipeline_id +  │                             │
   │     parameters: {...}    │                             │
   │  → ConfigPanel renders   │                             │
   │     param_schema fields  │                             │
   │     so user sets values  │                             │
   │                          │                             │
   │  click Save              │                             │
   │  PUT /api/workflows/{id} │                             │
   ├─────────────────────────►│  store.save (new wf has     │
   │                          │   execute_pipeline step     │
   │                          │   referencing wf-123)       │
   │                          │                             │
   │  click Run               │                             │
   │  POST /api/execute/      │                             │
   │  workflow/{id}           │                             │
   ├─────────────────────────►│                             │
   │                          │  executor reaches the macro │
   │                          │  step:                      │
   │                          │   ExecutePipelineNode.execute
   │                          │    → fetches wf-123         │
   │                          │    → invokes WorkflowExecutor
   │                          │       .execute_workflow(    │
   │                          │         macro_wf,           │
   │                          │         params=parameters)  │
   │                          │    → returns its result as  │
   │                          │       this step's relation  │
   │◄─────────────────────────┤                             │
   │  run completes with the  │                             │
   │  macro's output rolled   │                             │
   │  into the parent's       │                             │
   │  step_results            │                             │
```

The macro's parameter contract (its `WorkflowParameter` list) becomes
the parameter contract of the macro tile in the parent pipeline.
This is the F-Pulse equivalent of Alteryx's "Standard Macros."

---

## 6. Preview-mode dry-run (R8 + R8b + X4)

What "Preview Run" does instead of firing real side-effects.

```
Browser                Backend                  Engine
   │                       │                       │
   │  click "Preview Run"  │                       │
   │  POST /api/execute/   │                       │
   │  workflow/{id}        │                       │
   │  ?preview=true        │                       │
   ├──────────────────────►│                       │
   │                       │  WorkflowExecutor     │
   │                       │   .execute_workflow(  │
   │                       │     wf,               │
   │                       │     preview_mode=True)│
   │                       │   ctx.preview_mode=T  │
   │                       │                       │
   │                       │   for step in order:  │
   │                       │     _run_node_once    │
   │                       │     │ if ctx.preview_ │
   │                       │     │   mode:         │
   │                       │     │   side_effect = │
   │                       │     │     side_effect_│
   │                       │     │     class_for(  │
   │                       │     │     step.type)  │
   │                       │     │                 │
   │                       │     │ if passthrough: │
   │                       │     │   return ctx.   │
   │                       │     │   _results[up]  │
   │                       │     │   (skip the     │
   │                       │     │    sink write)  │
   │                       │     │                 │
   │                       │     │ if transforming│
   │                       │     │   /terminal:    │
   │                       │     │   row_count = N │
   │                       │     │   msg = node_   │
   │                       │     │   cls.preview_  │
   │                       │     │   message(      │
   │                       │     │     params, N)  │
   │                       │     │   return ONE-   │
   │                       │     │   ROW marker:   │
   │                       │     │     status=     │
   │                       │     │     "preview_   │
   │                       │     │     mode",      │
   │                       │     │     message=msg │
   │                       │     │                 │
   │                       │     │ else (pure      │
   │                       │     │   transform):   │
   │                       │     │   run normally  │
   │                       │     │   (in-process   │
   │                       │     │    relation     │
   │                       │     │    mutation OK) │
   │◄──────────────────────┤                       │
   │  { status: success,   │                       │
   │    step_results: {    │                       │
   │     "source_1":       │                       │
   │       { row_count: 42}│                       │
   │     "filter_1":       │                       │
   │       { row_count: 18}│                       │
   │     "email_sink_1":   │                       │
   │       { status:       │                       │
   │         "preview_     │                       │
   │         mode",        │                       │
   │         message:      │                       │
   │         "would send   │                       │
   │         18 emails to  │                       │
   │         ops@..."}     │                       │
   │   }}                  │                       │
   │  → PreviewPanel shows │                       │
   │    "Preview" badge +  │                       │
   │    the per-step msgs  │                       │
```

**Key invariant:** no SIDE_EFFECT_CLASS node ever fires when
`preview_mode=True`. Pure transforms (filter, aggregate, join, etc.)
still run because they only touch the in-process DuckDB relation —
which is exactly what preview mode WANTS to inspect.

---

## 7. The system-wide functional view

Putting it all together at the macro level:

```
                            ┌─────────────────────────────┐
                            │     OPERATOR (browser)       │
                            └──────────────┬──────────────┘
                                           │
              ┌────────────────────────────┼────────────────────────────┐
              │                            │                            │
              ▼                            ▼                            ▼
       ┌────────────┐              ┌──────────────┐              ┌────────────┐
       │ Authoring  │              │ Observability│              │ Trust      │
       │ canvas +   │              │ runs, lineage│              │ cert matrix│
       │ ConfigPanel│              │ provenance,  │              │ posture    │
       │ + chat AI  │              │ audit log    │              │ check      │
       └─────┬──────┘              └──────┬───────┘              └─────┬──────┘
             │                            │                            │
             ▼                            ▼                            ▼
       ┌────────────────────────────────────────────────────────────────┐
       │                  FastAPI app (single process)                  │
       │  60+ /api/* routers · auth+RBAC · workspace scoping · audit    │
       └─────────┬──────────────────────────────────┬───────────────────┘
                 │                                  │
                 ▼                                  ▼
       ┌──────────────────┐                ┌──────────────────────┐
       │ WorkflowExecutor │                │ Background services   │
       │  topo sort +     │                │  • Scheduler (cron)   │
       │  retry/timeout/  │                │  • Backfill orch      │
       │  on_error +      │                │  • Alert dispatcher   │
       │  preview_mode +  │                │  • AI Copilot agent   │
       │  caching         │                │  • Connection pool    │
       └────────┬─────────┘                └──────────┬───────────┘
                │                                     │
                ▼                                     │
       ┌──────────────────┐                           │
       │ Node registry    │                           │
       │ 99 BaseNode      │                           │
       │ subclasses       │                           │
       └────────┬─────────┘                           │
                │                                     │
                ▼                                     ▼
       ┌────────────────────────────┐    ┌────────────────────────┐
       │ DuckDB (in-process)        │    │ SQLite (fpulse.db, v31)│
       │ • SQL execution            │    │ • workflows / runs     │
       │ • CSV / JSON / Parquet     │    │ • schedules / alerts   │
       │   readers + writers        │    │ • sync_state           │
       │ • the data plane           │    │ • storage tables/files │
       └────────────┬───────────────┘    │ • audit / users        │
                    │                    └────────────────────────┘
                    │
                    ▼
       ┌────────────────────────────────────────────────────────┐
       │ External resources (all degrade gracefully)            │
       │  • file system   (CSV/JSON/Parquet inputs + outputs)   │
       │  • databases     (Postgres / MySQL / MSSQL / SQLite)   │
       │  • cloud storage (S3 / GCS / Azure Blob / SharePoint)  │
       │  • SaaS APIs     (REST framework manifests)            │
       │  • LLM providers (Ollama / Anthropic / OpenAI / ...)   │
       └────────────────────────────────────────────────────────┘
```

---

## See also

- `00_overview.md` — high-level architecture and trust boundaries
- `01_backend_lld.md` — backend module map + class diagrams + pseudo
  code for every flow above
- `02_frontend_lld.md` — frontend component tree + ConfigPanel
  pattern + workflowStore actions
- `../changelog.md` — every named release of these contracts
