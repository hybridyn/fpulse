# F-Pulse OSS — Backend Low-Level Design

**Reading order:** §1 module map → §2 IR types → §3 node hierarchy
→ §4 executor → §5 stores → §6 API layer → §7 pseudo-code for the
big methods.

---

## 1. Module map

```
backend/fpulse/
├── main.py                        # FastAPI app, lifespan, app_state, route includes
├── cli/__init__.py                # `fpulse` CLI entry (serve / run / list / backup / ...)
├── __main__.py                    # python -m fpulse → cli.main
│
├── ir/                            # Intermediate Representation
│   ├── schema.py                  # Workflow, Step, StepConnection, StepType enum, ...
│   ├── validator.py               # validate_workflow, validate_capabilities
│   ├── plan_diff.py               # diff two Workflow blobs
│   ├── versioning.py              # WorkflowStore (versioned, SQLite)
│   ├── lifecycle.py               # state machine (draft→tested→...)
│   ├── migrations.py              # DEPRECATED_STEP_TYPES + legacy-type remapper
│   └── node_metadata.py           # MULTI_INPUT_NODES, SIDE_EFFECT_CLASS, contract_for
│
├── nodes/                         # 99 BaseNode subclasses
│   ├── base.py                    # BaseNode, ExecutionContext
│   ├── registry.py                # @register(StepType.X) decorator + NodeRegistry
│   ├── csv_source.py, json_source ..., sources.py, db_source.py
│   ├── filter_node.py, activities.py (sort/rename/typecast/derived/lookup/...)
│   ├── aggregate.py, join.py, scd2.py, deduplicate.py
│   ├── quality.py (data_quality, schema_mapper, upsert, data_profile)
│   ├── advanced_transforms.py, transform.py, data_wrangler.py
│   ├── ai.py (embedder, llm_guardrail, semantic_router)
│   ├── flow_control.py (if/switch/loop/wait/http_request/code_script/send_email/...)
│   ├── control_extras.py (append_variable, filter_array, validation, fail, file_system, execute_sql)
│   ├── sinks.py (csv/json/excel/db/s3/kafka/api/email/delta/warehouse)
│   ├── cloud_storage.py, cloud_files.py, local_table.py
│   ├── output.py, generic.py (the new collapsed source/destination)
│   └── _sync_mode_decl.py         # shared sync_mode param helpers (X3)
│
├── connectors/                    # Beyond core node registry
│   ├── rest_framework.py          # Manifest-driven REST connector engine
│   ├── manifest_v2.py             # F0.1 cert validator + depth scorer
│   ├── manifests/*.json           # SaaS manifests (Salesforce, HubSpot, ...)
│   ├── jdbc.py, cdc.py, openapi_source.py, vector_db.py
│   └── ai_authoring.py            # "Author Connector from OpenAPI" + SSRF guard
│
├── engine/                        # Execution layer
│   ├── executor.py                # WorkflowExecutor
│   ├── checkpoint_store.py        # per-run step outcomes (resume support)
│   ├── sync_state_store.py        # per-(workflow, source) cursor watermark (P2)
│   ├── step_cache.py              # content-addressed step output cache
│   ├── execution_log.py           # ExecutionLogger (rich event stream)
│   ├── connection_pool.py         # per-run pooled DB connections
│   └── worker_pool.py             # parallel step worker pool
│
├── storage/
│   ├── database.py                # SQLite Database wrapper + schema (v31)
│   ├── migrations.py              # apply_pragmas + per-version migration runner
│   ├── providers.py, backup.py, backup_scheduler.py
│   └── storage_settings.py
│
├── datastore/                     # Managed-table catalog
│   ├── models.py                  # StorageObject, StorageTable, StorageColumn
│   ├── store.py                   # DataStore (file/table CRUD + lineage)
│   └── usage.py                   # cross-workflow reverse-dependency map
│
├── projects/                      # Workspaces / projects / ACL
│   ├── store.py, acl.py, models.py
│
├── scheduling/                    # Cron scheduler
│   ├── store.py, scheduler.py, models.py
│
├── alerts/                        # Alert rules + dispatcher
│   ├── store.py, notifier.py, models.py
│
├── monitoring/                    # Run history
│   └── store.py                   # ExecutionStore
│
├── auth/                          # Login + RBAC
│   ├── deps.py                    # require_auth, require_min_rank, current_workspace_id
│   ├── store.py, models.py        # UserStore, User
│
├── backfills/                     # Chunked replays
│   ├── orchestrator.py, store.py, windows.py, preflight.py
│   └── idempotency.py             # find_unsafe_sinks
│
├── ai/                            # AI agent + RAG + cost rates
│   ├── agent.py                   # signed system prompt + tool loop
│   ├── product_knowledge.py       # docs/product_facts/*.md RAG indexer
│   ├── rag/                       # Embedder + VectorStore
│   ├── tools/                     # 20+ agent tools (recall_history, ...)
│   └── providers/                 # anthropic_client.py, openai_client.py, ollama_client.py, ...
│
├── api/                           # 60+ FastAPI routers (one per resource)
│   ├── __init__.py                # re-exports every router
│   ├── workflows.py, schedules.py, variables.py, alerts.py, monitor.py
│   ├── credentials.py, connections.py, projects.py, folders.py
│   ├── execution.py, backfills.py, storage.py, cert_matrix.py, sync_state.py
│   ├── auth.py, agent.py, ai.py, lineage.py, contracts.py, ...
│
└── audit/, intelligence/, telemetry/, plugins/, marketplace/, ...
```

---

## 2. IR (Intermediate Representation) class diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                          Workflow                                │
│ ─────────────────────────────────────────────                   │
│ + id: str                                                       │
│ + name: str                                                     │
│ + workspace_id: str                                             │
│ + project_id: str                                               │
│ + folder_id: str | None                                         │
│ + status: PipelineStatus  (draft|tested|published|archived|...) │
│ + steps: list[Step]                                             │
│ + connections: list[StepConnection]                             │
│ + parameters: list[WorkflowParameter]                           │
│ + metadata: dict[str, Any]   # priority, published_as_node, ... │
│ + version: int  (computed on save)                              │
│ ─────────────────────────────────────────────                   │
│ + model_dump(): dict                                            │
└──────────┬──────────────────────────────────┬───────────────────┘
           │ 1..*                              │ 1..*
           ▼                                   ▼
┌──────────────────────┐         ┌────────────────────────────┐
│        Step          │         │     StepConnection         │
│ ──────────────────── │         │ ────────────────────────── │
│ + id: str            │         │ + from_step: str           │
│ + type: StepType     │         │ + to_step: str             │
│ + label: str         │         │ + from_port: str (output)  │
│ + params: dict       │         │ + to_port: str  (input)    │
│ + position:          │         └────────────────────────────┘
│       NodePosition   │
│ + risk: str          │
│ + proposed_by: str?  │   ┌────────────────────────────┐
└──────────────────────┘   │     WorkflowParameter      │
                           │ ────────────────────────── │
                           │ + name: str                │
                           │ + type: str (string|int|…) │
                           │ + default: Any             │
                           │ + required: bool           │
                           └────────────────────────────┘
```

**Run-result side** — emitted by the executor, persisted via
ExecutionStore:

```
┌─────────────────────────────────────────────────────────┐
│           WorkflowRunResult                              │
│ ──────────────────────────────────────────              │
│ + id, workflow_id, workspace_id                          │
│ + started_at, completed_at, duration_ms                  │
│ + status: success | error | partial | cancelled          │
│ + step_results: dict[step_id, StepRunResult]             │
└────────────────────┬────────────────────────────────────┘
                     │ 1..*
                     ▼
┌─────────────────────────────────────────────────────────┐
│          StepRunResult                                   │
│ ──────────────────────────────────────────              │
│ + step_id, status, row_count, duration_ms                │
│ + columns: list[str], schema_info: list[dict]            │
│ + sample_data: list[dict]                                │
│ + error: str?, error_type: StepErrorType?                │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Node hierarchy class diagram

Every executable node inherits from `BaseNode` and registers via
`@register(StepType.X)`. The registry holds `{StepType: cls}`.

```
┌─────────────────────────────────────────────────────────────┐
│                       BaseNode  (ABC)                       │
│ ─────────────────────────────────────────────              │
│ display_name: str                                           │
│ category: str                                               │
│ description: str                                            │
│                                                             │
│ + __init__(self, params: dict)                              │
│                                                             │
│ @abstractmethod                                             │
│ + execute(self, ctx: ExecutionContext)                      │
│        -> duckdb.DuckDBPyRelation                           │
│                                                             │
│ @staticmethod default_params() -> dict                      │
│ @staticmethod param_schema() -> list[dict]                  │
│                                                             │
│ # Hooks added by this session:                              │
│ + is_preview(ctx) -> bool                          (R8)     │
│ @staticmethod expected_output_schema(              (R5)     │
│       input_schemas, params) -> list[dict] | None           │
│ @staticmethod preview_message(                     (X4)     │
│       params, row_count) -> str | None                      │
└──────────────┬──────────────────────────────────────────────┘
               │
   ┌───────────┼──────────────────┬────────────────┬─────────────┐
   ▼           ▼                  ▼                ▼             ▼
┌────────┐ ┌─────────────┐  ┌────────────┐  ┌─────────────┐  ┌────────────┐
│Source  │ │ Transform   │  │   Sink     │  │ Flow ctrl   │  │ AI / sem.  │
│nodes   │ │ nodes       │  │ nodes      │  │ nodes       │  │ nodes      │
├────────┤ ├─────────────┤  ├────────────┤  ├─────────────┤  ├────────────┤
│CsvSrc  │ │ FilterNode  │  │ CsvSink    │  │ IfCondition │  │ Embedder   │
│DbSrc   │ │ Aggregate   │  │ DbSink     │  │ SwitchCase  │  │ LlmGuard   │
│ApiSrc  │ │ Join        │  │ S3Sink     │  │ ForeachLoop │  │ SemRouter  │
│S3Src   │ │ SortNode    │  │ EmailSink  │  │ UntilLoop   │  └────────────┘
│KafkaSrc│ │ DerivedCol  │  │ KafkaSink  │  │ WaitDelay   │
│GsheetS │ │ Window      │  │ ApiSink    │  │ SetVariable │  ┌────────────┐
│...     │ │ Lookup      │  │ DeltaSink  │  │ ExecPipeline│  │ Action     │
│        │ │ Union       │  │ WarehouseS │  │ HttpRequest │  ├────────────┤
│        │ │ SCD2        │  │ Webhook    │  │ CodeScript  │  │ SendEmail  │
│        │ │ Wrangler    │  │ Sharepoint │  │ Validation  │  │ SlackNotify│
│        │ │ Quality     │  │ ...        │  │ Fail        │  │ CopyData   │
│        │ │ SchemaMap   │  │            │  │ FileSystem  │  │ DeleteData │
│        │ │ Materialize │  │            │  │ ExecuteSql  │  │ GetMetadata│
│        │ │ Profile     │  │            │  └─────────────┘  └────────────┘
│        │ │ ...         │  │            │
│        │ └─────────────┘  │            │
└────────┘                  └────────────┘
```

**Key invariants** (pinned by `tests/test_node_conformance.py`):
- Every `StepType` enum value is either registered OR deprecated.
- Every registered class has `display_name`, `category`,
  `description`, `default_params()`, `param_schema()`, `execute()`.
- `SIDE_EFFECT_CLASS` keys map to real StepTypes.
- `MULTI_INPUT_NODES` (join, union, scd2, lookup) gate the canvas's
  multi-input handle.

---

## 4. Executor class diagram

```
┌─────────────────────────────────────────────────────────────┐
│                  ExecutionContext                            │
│ ──────────────────────────────────────────                  │
│ + conn: duckdb.DuckDBPyConnection                            │
│ + data_dir: str                                              │
│ + full_run: bool                                             │
│ + preview_mode: bool                  (R8 — dry-run flag)    │
│ + run_id: str | None                                         │
│ + workflow_id: str | None                                    │
│ + workflow_metadata: dict                                    │
│ + step_params: dict[step_id, dict]                           │
│ + node_labels: dict[label, step_id]                          │
│ + vars: dict[str, Any]              (workspace variables)    │
│ + app_state: dict                                            │
│ - _results: dict[step_id, DuckDBPyRelation]                  │
│                                                              │
│ + set_result(step_id, relation)                              │
│ + get_input(step_id) -> relation | None                      │
│ + get_inputs(step_ids) -> list[relation]                     │
│ + results_as_rows() -> dict[step_id, list[dict]]             │
└──────────────────────┬──────────────────────────────────────┘
                       │ used by
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                WorkflowExecutor                              │
│ ──────────────────────────────────────────                   │
│ + data_dir: str                                              │
│ + app_state: dict                                            │
│ + registry: NodeRegistry                                     │
│ + _cache: StepCache                                          │
│ + _checkpoint: CheckpointStore                               │
│ + _workflow_retries_used: int                                │
│ + workflow_retry_budget: int (default 12)                    │
│                                                              │
│ + execute_workflow(wf, *, preview_mode=False,                │
│                    preview_limit=10, run_id=None,            │
│                    triggered_by="manual")                    │
│       -> WorkflowRunResult                                   │
│                                                              │
│ + execute_step(wf, step_id, *, preview_limit=10)             │
│       -> StepRunResult                                       │
│                                                              │
│ - _topological_sort(wf) -> list[Step]                        │
│ - _build_input_map(wf) -> dict[step_id, list[upstream_id]]   │
│ - _execute_step(step, ctx, input_map, preview_limit)         │
│       -> StepRunResult     # retry/timeout/on_error wrapper  │
│ - _run_node_once(step, ctx, input_map, timeout_sec)          │
│       -> DuckDBPyRelation  # actual node dispatch            │
│ - _find_retry_targets(wf, order, input_map)                  │
│       -> dict[step_id, retry_handler_params]                 │
│ - _persist_to_cache(step, ctx, input_map, row_count)         │
└──────────────────────────────────────────────────────────────┘
```

The `_execute_step` method is the chokepoint where the Settings tab
controls (timeout, retries, on_error) take effect — see §7 pseudo
code.

---

## 5. Store layer class diagram

Every persistent resource follows the same shape:

```
┌─────────────────────────────────────────────────────────────┐
│                  <Resource>Store                             │
│ ─────────────────────────────────────────────              │
│ - _db: Database     # injected at startup via set_db        │
│                                                             │
│ + create(item)    -> Item                                   │
│ + get(id, *, workspace_id=None)   -> Item | None            │
│ + list_all(*, workspace_id=None, **filters) -> list[Item]   │
│ + update(id, patch, *, workspace_id=None)   -> Item         │
│ + delete(id, *, workspace_id=None)  -> bool                 │
└─────────────────────────────────────────────────────────────┘
                              ▲
                              │ implements
   ┌──────────────┬──────────┴───────────┬─────────────────┐
   │              │                       │                  │
WorkflowStore  ProjectStore         ScheduleStore       AlertStore
ExecutionStore VariableStore        CredentialStore     ConnectionStore
DataStore      SyncStateStore       CheckpointStore     LifecycleStore
UserStore      WorkspaceStore       FolderStore         BackfillStore
LineageStore   ContractStore        VaultStore          AuditLogger
```

Two stores added this session:

- **`SyncStateStore`** — per-(workflow, source_step) cursor watermark.
  Backed by schema v31 `sync_state` table. Best-effort: read/write
  failures log + swallow, never crash a run.

- **`CheckpointStore`** — per-(run_id, step_id) outcome + Parquet
  output_ref. Powers "Resume from step N." Already existed at
  session start.

---

## 6. API layer pattern

Every router file follows the same template:

```python
# fpulse/api/<resource>.py
from fastapi import APIRouter, Depends, HTTPException, Request
from fpulse.auth.deps import (
    current_workspace_id, require_auth, require_min_rank,
)

_AUTH = Depends(require_auth)                          # any authenticated user
_AUTHOR = Depends(require_min_rank("developer"))       # write-tier (P1 + W1)

router = APIRouter(
    prefix="/api/<resource>",
    tags=["<resource>"],
    dependencies=[_AUTH],   # router-level: blocks anonymous on every route
)

@router.get("", include_in_schema=False)               # trailing-slash alias (P7)
@router.get("/")
def list_resource(workspace_id: str = Depends(current_workspace_id)):
    return get_store().list_all(workspace_id=workspace_id)

@router.post("", dependencies=[_AUTHOR], include_in_schema=False)
@router.post("/", dependencies=[_AUTHOR])
def create_resource(body: ResourceCreate,
                    workspace_id: str = Depends(current_workspace_id)):
    return get_store().create(body, workspace_id=workspace_id)
```

The repeated `_AUTH` / `_AUTHOR` constants are module-level so the
test fixture can `app.dependency_overrides[mod._AUTH.dependency] =
lambda: fake_user` without re-importing every route.

**Workspace scoping** is enforced at the store layer, not in handlers
— `current_workspace_id` resolves the workspace from the
`X-Workspace-Id` header (with membership check) and passes it to the
store. A handler that forgets to pass it through gets the user's
default workspace, which is the safe failure mode.

---

## 7. Pseudo code for the big methods

### 7.1 `WorkflowExecutor.execute_workflow`

```pseudo
function execute_workflow(workflow, preview_mode=False, preview_limit=10):
    self._workflow_retries_used = 0           # reset workflow-level budget
    ctx = ExecutionContext(
        conn=duckdb.connect(),
        data_dir=self.data_dir,
        preview_mode=preview_mode,
        run_id=generate_uuid(),
        app_state=self.app_state,
    )
    ctx.workflow_id = workflow.id

    order = self._topological_sort(workflow)            # cycle-checked DAG sort
    input_map = self._build_input_map(workflow)         # also stamps _input_step_ports (R6)
    self._effective_hashes = self._cache.compute_effective_hashes(
        workflow, input_map,
    )

    retry_targets = self._find_retry_targets(           # nodes with retry_handler downstream
        workflow, order, input_map,
    )

    step_results = {}
    overall_status = "success"

    for step in order:
        if step.params.get("_settings", {}).get("execute_once"):
            existing = ctx._results.get(step.id)
            if existing is not None:
                step_results[step.id] = success_result(existing)
                continue                                   # already ran this turn

        # If a retry_handler dangles off this step, fold its params into the
        # step's Settings so the retry loop below picks them up.
        retry_cfg = retry_targets.get(step.id)
        if retry_cfg:
            step = step_with_settings_overlay(step, retry_cfg)

        step_result = self._execute_step(step, ctx, input_map, preview_limit)
        step_results[step.id] = step_result

        if step_result.status == "error":                  # on_error=stop → halt
            overall_status = "error"
            break

    return WorkflowRunResult(
        id=ctx.run_id,
        workflow_id=workflow.id,
        status=overall_status,
        step_results=step_results,
    )
```

### 7.2 `WorkflowExecutor._execute_step` — retry + timeout + on_error

```pseudo
function _execute_step(step, ctx, input_map, preview_limit):
    settings = step.params.get("_settings") or {}
    timeout_sec    = int(settings.get("timeout_sec", 0))
    retry_on_fail  = bool(settings.get("retry_on_fail"))
    max_retries    = int(settings.get("max_retries", 0)) if retry_on_fail else 0
    retry_delay_ms = int(settings.get("retry_delay_ms", 1000))
    retry_strategy = settings.get("retry_strategy", "fixed").lower()
    on_error       = settings.get("on_error", "stop").lower()
    execute_once   = bool(settings.get("execute_once"))             # X8 P8
    always_output  = bool(settings.get("always_output"))            # X8 P8

    if execute_once and ctx._results.get(step.id) is not None:
        return cached_result(ctx._results[step.id])

    attempt = 0
    while attempt < max_retries + 1:
        attempt += 1
        try:
            relation = self._run_node_once(step, ctx, input_map, timeout_sec)
            # always_output: synthesise a 1-row marker if relation is empty
            if always_output and is_empty(relation):
                relation = make_marker_row(relation)
            ctx.set_result(step.id, relation)
            preview = preview_relation(relation, limit=preview_limit)
            self._persist_to_cache(step, ctx, input_map, preview.total_rows)
            return success_result(preview)

        except Exception as e:
            if attempt >= max_retries + 1:
                break
            if self._workflow_retries_used >= self.workflow_retry_budget:
                break                                                # global cap
            self._workflow_retries_used += 1
            delay = retry_delay_ms
            if   retry_strategy == "linear":      delay *= attempt
            elif retry_strategy == "exponential": delay *= 2 ** (attempt - 1)
            sleep(delay / 1000)

    # All attempts failed. Apply on_error policy.
    if on_error == "continue":
        return skipped_result(reason="on_error=continue")
    if on_error == "continue_error_output":
        return error_result_payload(last_exception)
    return error_result(last_exception)                              # default: stop
```

### 7.3 `WorkflowExecutor._run_node_once` — preview short-circuit + dispatch

```pseudo
function _run_node_once(step, ctx, input_map, timeout_sec):
    # R8b — preview mode short-circuit using SIDE_EFFECT_CLASS metadata.
    if ctx.preview_mode:
        cls_label = side_effect_class_for(step.type.value)
        if cls_label == "passthrough":
            inputs = [ctx._results[u] for u in input_map[step.id]]
            return inputs[0] if inputs else empty_relation(ctx)

        if cls_label in ("transforming", "terminal"):
            # X4 — ask node class for a specific "would have done X" message
            row_count = count_upstream_rows(ctx, input_map[step.id])
            node_cls = self.registry.get(step.type)
            msg = node_cls.preview_message(step.params, row_count) \
                  or "side effect skipped (preview run)"
            return one_row_marker(step.label, status="preview_mode", message=msg)
        # pure transforms fall through and run normally

    node_cls = self.registry.get(step.type)

    raw_params = {
        **step.params,
        "_input_step_ids": input_map.get(step.id, []),
        "_step_id": step.id,
    }
    raw_params = resolve_expressions(raw_params, ctx)  # {{ ... }} placeholders

    node_instance = node_cls(params=raw_params)

    if timeout_sec > 0:
        return run_with_thread_timeout(
            target=lambda: node_instance.execute(ctx),
            timeout=timeout_sec,
        )
    return node_instance.execute(ctx)
```

### 7.4 `BaseNode.execute` for an incremental DB source (P2)

```pseudo
function DbSourceNode.execute(ctx):
    query = self.params["query"] if mode == "query" else build_table_query()
    sync_mode = self.params.get("sync_mode", "full_refresh").lower()
    watermark_col = self.params.get("watermark_column", "")
    watermark_val = self.params.get("watermark_value", "")

    if sync_mode == "incremental" and watermark_col and not watermark_val:
        # Auto-load persisted cursor — P2.
        watermark_val = self._load_sync_cursor(ctx, watermark_col)

    if watermark_col and watermark_val:
        safe = watermark_val.replace("'", "''")            # escape, not parameterise
        query = f'SELECT * FROM ({query}) WHERE "{watermark_col}" > \'{safe}\''

    rows = self._execute_real(conn_type, config, query, ctx, connection_id)
    relation = load_into_duckdb(ctx.conn, rows)

    if sync_mode == "incremental" and watermark_col:
        # Auto-save the new high-water-mark — P2.
        self._save_sync_cursor(ctx, watermark_col, len(rows))

    return relation


function _load_sync_cursor(ctx, cursor_col):
    state = sync_state_store.get(ctx.workflow_id, self.params["_step_id"])
    if state and state.cursor_column == cursor_col:
        return state.last_cursor
    return None


function _save_sync_cursor(ctx, cursor_col, rows_loaded):
    new_max = ctx.conn.sql(
        f'SELECT MAX("{cursor_col}") FROM __db_source'
    ).fetchone()[0]
    if new_max is None:
        return
    sync_state_store.upsert(SyncState(
        workflow_id=ctx.workflow_id,
        step_id=self.params["_step_id"],
        cursor_column=cursor_col,
        last_cursor=str(new_max),
        rows_last_run=rows_loaded,
    ))
```

### 7.5 Macro discovery — `/api/node-types` (R7)

```pseudo
function GET /api/node-types():
    types = registry.all_types()                          # 99 registered classes

    # Decorate with arity + side-effect + deprecation contract.
    for entry in types:
        entry.arity = contract_for(entry.type)
        entry.side_effects = side_effect_class_for(entry.type)
        ...

    # R7 — macros: any workflow whose metadata.published_as_node=true
    # surfaces as a virtual palette entry the frontend can drop on canvas.
    wf_store = app_state["store"]
    for v in wf_store.list_all():
        wf = v.workflow
        if not wf.metadata.get("published_as_node"):
            continue

        params_contract = [
            {"name": p.name, "type": p.type, "default": p.default,
             "required": p.required, "description": p.description}
            for p in (wf.parameters or [])
        ]

        types.append({
            "type": f"execute_pipeline:{wf.id}",
            "base_type": "execute_pipeline",              # frontend addNode resolves this
            "label": wf.metadata.get("published_label") or wf.name,
            "category": "macro",
            "default_params": {
                "pipeline_id": wf.id,
                "wait_for_completion": True,
                "parameters": {p.name: p.default for p in (wf.parameters or [])},
            },
            "param_schema": [
                {"name": "pipeline_id", "type": "hidden", "default": wf.id},
                *[{"name": f"parameters.{p.name}", **field_for(p)}
                  for p in (wf.parameters or [])],
            ],
            "macro": True,
            "macro_workflow_id": wf.id,
        })

    return types
```

### 7.6 Backfill preflight — `/api/executions/backfill/preflight` (P5)

```pseudo
function POST /api/executions/backfill/preflight(body):
    workflow = wf_store.get(body.pipeline_id)
    if not workflow: 404

    unsafe = find_unsafe_sinks(workflow.steps)                       # idempotency scan
    cursor_violation = check_cursor_param_usage(
        workflow,
        cursor_names = body.cursor_param_names or ["window_start", "window_end"],
    )
    windows = generate_windows(body.start_date, body.end_date,
                                body.window_size, body.window_size_hours)

    warnings, recommendations = [], []
    if unsafe:
        warnings.append(f"{len(unsafe)} sink(s) flagged unsafe for re-run")
        for sink in unsafe:
            if sink.cls == "append_risky":
                recommendations.append("Switch to merge or upsert ...")
            elif sink.cls == "external":
                recommendations.append("Set idempotency_key on external sinks ...")

    if cursor_violation:
        warnings.append("No source step references the cursor parameters")
        recommendations.append("Reference $params.window_start in the source ...")

    if len(windows) > 1000:
        warnings.append(f"This backfill will run {len(windows)} windows")

    return {
        "ok": not (unsafe or cursor_violation) and len(windows) > 0,
        "total_windows": len(windows),
        "first_window": windows[0].start.isoformat(),
        "last_window":  windows[-1].end.isoformat(),
        "unsafe_sinks": unsafe,
        "cursor_violation": cursor_violation,
        "warnings": warnings,
        "recommendations": recommendations,
    }
```

### 7.7 Storage provenance — one-shot lineage (P4)

```pseudo
function GET /api/storage/tables/{id}/provenance(table_id):
    table = datastore.get_table(table_id, workspace_id)
    if not table: 404

    source_file = None
    if table.prep_source_object_id or table.created_from_object_id:
        src = datastore.get_object(...)
        source_file = {"id": src.id, "name": src.name}

    source_workflow, last_run = None, None
    if table.prep_workflow_id:
        wf = workflow_store.get(table.prep_workflow_id)
        source_workflow = {"id": wf.id, "name": wf.name}

        # Pull the most recent execution of that workflow.
        runs = execution_store.list_by_workflow(wf.id, limit=1)
        if runs:
            last_run = runs[0]

    # Reverse dependency: pipelines that READ from this table.
    usage = compute_workspace_usage_cached(workspace_id)
    consumers = usage["tables"].get(table_id, [])

    return {
        "table_id": table_id,
        "source_file": source_file,
        "source_workflow": source_workflow,
        "source_recipe": table.prep_recipe,
        "last_run": last_run,
        "consumers": consumers,
        "consumer_count": len(consumers),
    }
```

---

## 8. Cross-cutting hooks added this session

| Hook | Lives in | Called by | Purpose |
|---|---|---|---|
| `BaseNode.expected_output_schema(input_schemas, params) -> list[dict] \| None` | `nodes/base.py` | `GET /api/workflows/{id}/step/{step}/expected-schema` (R5) | Predict columns without running. Returns None for sources whose schema is remote/dynamic. |
| `BaseNode.preview_message(params, row_count) -> str \| None` | `nodes/base.py` | `WorkflowExecutor._run_node_once` (X4) | Specific "would write N rows to X" message in preview mode. |
| `BaseNode.is_preview(ctx) -> bool` | `nodes/base.py` | Node's own `execute()` if it wants its own preview path | Lets a node short-circuit before its side-effect runs. |
| `sync_state_store.get / upsert / reset / list_for_workflow` | `engine/sync_state_store.py` | `db_source.execute`, `api_source.execute` (P2 + R1b) | Per-source cursor watermark persistence. |
| `_input_step_ports` stamped on params | `engine/executor._build_input_map` | Branching nodes (R6) | Lets a node self-filter on the edge port label it received. |

These are additive — every existing node continues to work without
implementing the new hooks.
