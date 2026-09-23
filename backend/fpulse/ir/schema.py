"""
F-Pulse Intermediate Representation (IR) Schema.

The IR is the single source of truth for all workflows.
It is engine-agnostic — stores operations, not implementations.
The F-Pulse runtime executes the IR on DuckDB.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

# PR 3 — IR Governance Split. Plus-tier approval/deploy/PROD-toggle state
# lives in a separate model so the OSS Workflow IR stays clean. See
# fpulse/ir/governance.py for the full rationale and field set.
from fpulse.ir.governance import WorkflowGovernance


class StepType(str, Enum):
    # Generic source/destination. The concrete connector is selected at
    # runtime from params["connector_type"] and delegated to the matching
    # source or sink node.
    SOURCE = "source"
    DESTINATION = "destination"

    # Sources
    CSV_SOURCE = "csv_source"
    DB_SOURCE = "db_source"
    API_SOURCE = "api_source"        # Read rows from a REST/HTTP endpoint
    JSON_SOURCE = "json_source"      # Read JSON files
    PARQUET_SOURCE = "parquet_source" # Read Parquet files
    EXCEL_SOURCE = "excel_source"    # Read Excel (.xlsx) files
    XML_SOURCE = "xml_source"        # Read XML files, parse to rows
    S3_SOURCE = "s3_source"          # Read from S3-compatible object storage
    KAFKA_SOURCE = "kafka_source"    # Read from a Kafka topic
    FTP_SOURCE = "ftp_source"        # Read from an FTP server
    GSHEET_SOURCE = "gsheet_source"  # Google Sheets via public CSV export
    DELTA_SOURCE = "delta_source"    # Read Delta Lake tables via Parquet
    # Cloud object storage (each cloud has its own auth + URI scheme — separate nodes)
    ADLS_GEN2_SOURCE = "adls_gen2_source"   # Azure Data Lake Storage Gen2 (abfss://) — hierarchical, AAD/SAS/Key auth
    AZURE_BLOB_SOURCE = "azure_blob_source" # Azure Blob Storage (wasbs://) — flat, SAS/Key/AAD auth
    GCS_SOURCE = "gcs_source"               # Google Cloud Storage (gs://) — service account / HMAC auth
    # Universal file node — auto-detects format from extension
    FILE_SOURCE = "file_source"             # CSV/JSON/Parquet/Excel/XML/NDJSON/TSV — sniffs by extension
    # SaaS document storage (each has its own SDK / OAuth flow — separate nodes)
    SHAREPOINT_SOURCE = "sharepoint_source" # Microsoft Graph: /sites/{site}/drives/{drive}/items
    ONEDRIVE_SOURCE = "onedrive_source"     # Microsoft Graph: /me/drive or /users/{id}/drive
    GDRIVE_SOURCE = "gdrive_source"         # Google Drive API v3
    DROPBOX_SOURCE = "dropbox_source"       # Dropbox API v2
    BOX_SOURCE = "box_source"               # Box API v2
    # Microsoft Graph (generic) — 2026-05-22. First-class connector
    # for any Graph resource: /users, /groups, /sites, /drives,
    # /teams, /planner/*, /me/messages, etc. SharePoint/OneDrive
    # nodes above stay for their file-flavored UX; this is the
    # general-purpose JSON-rows reader for arbitrary Graph endpoints.
    MS_GRAPH_SOURCE = "microsoft_graph_source"
    # 2026-05-23 (Y3): managed-table source/sink. Reads/writes Parquet
    # tables under {DATA_DIR}/tables/{ws}/{schema}/{name}/. The Storage
    # page is the matching UI surface for browsing + promoting files
    # into these tables.
    LOCAL_TABLE_SOURCE = "local_table_source"
    LOCAL_TABLE_SINK = "local_table_sink"

    # Row-level transforms
    FILTER = "filter"                 # Drops rows that don't match a predicate
    TRANSFORM = "transform"           # Apply SQL/expression to compute new column values
    DEDUPLICATE = "deduplicate"       # Removes duplicate rows by key with keep-first/keep-last strategies
    SORT = "sort"                     # Order rows by one or more keys
    RENAME = "rename"                 # Rename columns (project/select with aliases)
    TYPECAST = "typecast"             # Convert column data types
    DERIVED_COLUMN = "derived_column" # Add a computed column from an expression

    # Set-level transforms
    AGGREGATE = "aggregate"           # Group-by with aggregate functions (sum/count/avg/...)
    JOIN = "join"                     # Combine two inputs on a key (inner/left/right/full)
    LOOKUP = "lookup"                 # Enrich rows by looking up a reference table
    UNION = "union"                   # Stack multiple inputs vertically
    PIVOT = "pivot"                   # Long → wide on a category column
    UNPIVOT = "unpivot"               # Wide → long, melting columns into rows
    WINDOW = "window"                 # Window functions (rank/lag/running totals over partitions)

    # Quality & Control
    SAMPLE = "sample"                 # Take a random or first-N subset of rows
    VALIDATE = "validate"             # Assert expectations on data; fail or quarantine on mismatch
    CONDITIONAL_SPLIT = "conditional_split"  # Route rows to multiple outputs based on predicates

    # Flow Control
    IF_CONDITION = "if_condition"         # Branch on a boolean expression
    SWITCH_CASE = "switch_case"           # Branch on a value matched against multiple cases
    FOREACH_LOOP = "foreach_loop"         # Iterate over an array, running child steps per item
    # 2026-06-11 — true per-item loop (per-row loop):
    # runs a saved sub-pipeline ONCE PER input row, injecting the row as
    # the sub-pipeline's parameters. Distinct from FOREACH_LOOP, which is a
    # row-batch/chunk processor.
    FOREACH_PIPELINE = "foreach_pipeline"
    UNTIL_LOOP = "until_loop"             # Repeat child steps until a condition is true
    WAIT_DELAY = "wait_delay"             # Pause execution for a fixed duration
    SET_VARIABLE = "set_variable"         # Assign a value to a workflow variable
    APPEND_VARIABLE = "append_variable"   # Push a value onto an array variable
    FILTER_ARRAY = "filter_array"         # Filter an array variable by a predicate
    VALIDATION = "validation"             # Wait until a file/dataset exists and meets size/age criteria
    FAIL = "fail"                         # Halt the run with an error
    EXECUTE_PIPELINE = "execute_pipeline" # Invokes another workflow as a sub-pipeline
    # 2026-06-11 — Lookup *activity* (distinct from the LOOKUP
    # transformation above). Reads reference row(s) from its upstream and
    # captures firstRow / rows / count into $vars for downstream control
    # flow (watermarks, config values, row-count gates), consumed via
    # {{ $vars.<name>.firstRow.<col> }} expressions.
    LOOKUP_ACTIVITY = "lookup_activity"

    # File & SQL primitives
    FILE_SYSTEM = "file_system"           # File system operations (copy/move/delete)
    EXECUTE_SQL_TASK = "execute_sql_task" # Run an arbitrary SQL statement against a connection

    # Action Nodes
    HTTP_REQUEST = "http_request"         # Make an outbound HTTP request
    WEBHOOK_TRIGGER = "webhook_trigger"   # Start a workflow from an inbound webhook
    CODE_SCRIPT = "code_script"           # Run a user-provided code snippet
    SEND_EMAIL = "send_email"             # Send an email via SMTP
    SLACK_NOTIFY = "slack_notify"         # Post a message to a Slack channel
    COPY_DATA = "copy_data"               # Identity pass-through from source to sink
    DELETE_DATA = "delete_data"           # Delete rows matching a predicate
    GET_METADATA = "get_metadata"         # Introspect schema/metadata of a source

    # Destinations
    OUTPUT = "output"                 # File output (Parquet/CSV/JSON)
    DB_SINK = "db_sink"               # Write rows to a database table
    CSV_SINK = "csv_sink"             # Write CSV files
    JSON_SINK = "json_sink"           # Write JSON files
    EXCEL_SINK = "excel_sink"         # Write Excel (.xlsx) files
    S3_SINK = "s3_sink"              # Upload to S3/MinIO
    KAFKA_SINK = "kafka_sink"        # Send to Kafka topic
    API_SINK = "api_sink"            # POST data to REST API
    WEBHOOK_SINK = "webhook_sink"    # POST to webhook URL
    EMAIL_SINK = "email_sink"        # Send data via email (SMTP)
    DELTA_SINK = "delta_sink"        # Write as Parquet (Delta-compatible)
    WAREHOUSE_SINK = "warehouse_sink"  # Write to data warehouse with schema evolution
    FTP_SINK = "ftp_sink"               # Upload a file to an FTP / FTPS / SFTP server
    # Cloud object storage sinks
    ADLS_GEN2_SINK = "adls_gen2_sink"   # Azure Data Lake Storage Gen2 sink
    AZURE_BLOB_SINK = "azure_blob_sink" # Azure Blob Storage sink
    GCS_SINK = "gcs_sink"               # Google Cloud Storage sink
    # Universal file sink — auto-formats by extension
    FILE_SINK = "file_sink"             # CSV/JSON/Parquet/Excel — picks writer from extension
    # SaaS document storage sinks
    SHAREPOINT_SINK = "sharepoint_sink"
    ONEDRIVE_SINK = "onedrive_sink"
    GDRIVE_SINK = "gdrive_sink"
    DROPBOX_SINK = "dropbox_sink"
    BOX_SINK = "box_sink"

    # Connector Framework (S1-S5: declarative connectors for SaaS, JDBC, CDC, Vector)
    REST_CONNECTOR = "rest_connector"   # Manifest-driven REST/SaaS connector (Salesforce, HubSpot, Stripe, ...)
    SAAS_CONNECTOR = "saas_connector"   # Universal SaaS node — picks any loaded manifest at runtime
    JDBC_SOURCE = "jdbc_source"         # Generic JDBC/warehouse source via dialect registry
    JDBC_SINK = "jdbc_sink"             # Generic JDBC/warehouse sink via dialect registry
    CDC_SOURCE = "cdc_source"           # Debezium-style change data capture source
    OPENAPI_SOURCE = "openapi_source"   # Generic OpenAPI/Swagger driven source
    VECTOR_SOURCE = "vector_source"     # Vector DB source (Pinecone/Weaviate/Qdrant/Chroma/pgvector)
    VECTOR_SINK = "vector_sink"         # Vector DB sink with embedding support

    # Quality / Shape primitives (Sprint B)
    DATA_QUALITY = "data_quality"       # Declarative rule-based row validator + DLQ split
    UPSERT = "upsert"                   # Idempotent merge: insert/update on key, no duplicates
    SCHEMA_MAPPER = "schema_mapper"     # Source-to-target field mapping with type coercion
    DATA_PROFILE = "data_profile"       # Column statistics (null %, distinct, min/max, top values)

    # AI primitives (Sprint C)
    EMBEDDER = "embedder"               # Text column → vector column (openai/cohere/st/hash)
    LLM_GUARDRAIL = "llm_guardrail"     # PII/profanity/prompt-injection routing
    SEMANTIC_ROUTER = "semantic_router" # Classify rows into labels via embeddings or LLM

    # High-impact additions (node refinement sprint)
    FLATTEN_EXPLODE = "flatten_explode" # Flatten nested JSON / explode arrays into rows
    MATERIALIZE = "materialize"         # Save intermediate result to temp table (cache/checkpoint)
    RETRY_HANDLER = "retry_handler"     # Visual error handling: retry N times with backoff

    # Sprint 1 / Gate 1 — slowly-changing dimension (Type 2)
    SCD2 = "scd2"                       # Type-2 SCD: track historical versions per business key

    # Stepwise visible transform (see docs/design-data-wrangler-node.md)
    DATA_WRANGLER = "data_wrangler"     # Ordered list of inline sub-steps with per-step preview


class PipelineStatus(str, Enum):
    """Lifecycle status of a pipeline/workflow."""
    DRAFT = "draft"
    TESTING = "testing"
    PUBLISHED = "published"
    ARCHIVED = "archived"
    FAILED = "failed"  # Last test/run failed


class NodePosition(BaseModel):
    x: float = 0
    y: float = 0


class StepConnection(BaseModel):
    """Edge between two steps."""
    from_step: str
    to_step: str
    from_port: str = "output"
    to_port: str = "input"
    # Optional user-chosen table name for the consuming node. Multi-input nodes
    # (notably SQL Transform) register each incoming relation under this alias
    # so SQL can read a stable, user-named table instead of the sanitized
    # upstream label. None → fall back to the sanitized label (unchanged).
    alias: Optional[str] = None


class Step(BaseModel):
    """A single node in the workflow canvas."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    type: StepType
    label: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    position: NodePosition = Field(default_factory=NodePosition)
    risk: str = "low"  # low | medium | high
    # D4 (2026-05-26) — Provenance label. 'copilot' = the Copilot
    # proposed this step (via /api/ai/agent or the planner); 'human'
    # = the user added it manually via the canvas/palette; None =
    # legacy step (predates this field) or unknown. The deterministic
    # kernel does not branch on this — it's metadata for the UI's
    # "who suggested what" affordance + IR replay diffs.
    proposed_by: str | None = None  # "copilot" | "human" | None


class WorkflowParameter(BaseModel):
    """Typed pipeline parameter.

    Defines a typed input variable the user can override per-run. Step
    params reference these via ``${param.<name>}`` placeholders which are
    resolved at execution time by ``executor._resolve_parameters``.

    Example:
        parameters: [
          { name: "dataset", type: "string", default: "orders",
            description: "Source CSV file name" },
          { name: "batch_size", type: "int", default: 1000 },
          { name: "run_date", type: "string",
            default: "${utcnow:%Y-%m-%d}" },
        ]
    """
    name: str
    type: str = "string"  # "string" | "int" | "float" | "bool" | "json"
    default: Any = None
    description: str = ""
    required: bool = False


class Workflow(BaseModel):
    """The complete IR for a pipeline."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "Untitled Pipeline"
    description: str = ""
    project_id: str = "default"
    # Optional nested folder within the project. None = pipeline lives
    # at the project root. Folders form a tree (see fpulse.folders);
    # this field stores the leaf folder id.
    folder_id: str | None = None
    # Tenant boundary — which workspace this pipeline lives in. Set
    # automatically from the caller's X-Workspace-Id on create; legacy
    # rows back-filled to 'default' by the v5 schema migration. This
    # is denormalised into workflow_versions.workspace_id as well so
    # the list/filter query can use an index without joining through
    # the workflow's project.
    workspace_id: str = "default"
    steps: list[Step] = Field(default_factory=list)
    connections: list[StepConnection] = Field(default_factory=list)
    # Typed pipeline parameters — inputs the user (or an API caller) can
    # override per run. References inside step params via ${param.<name>}
    # are resolved at execution time. Empty list = pipeline takes no inputs.
    parameters: list[WorkflowParameter] = Field(default_factory=list)
    # E2.1 (2026-06-08, docs/design/executor-maturity-1.2.md) - optional
    # workflow-level RetryPolicy. When set + enabled, the executor's
    # per-step retry loop consults `should_retry(failure_class, attempt)`
    # before scheduling another attempt. Default None = current behaviour
    # (per-step retry settings drive everything). The IR field is a plain
    # dict to avoid pulling fpulse.engine.* imports into the schema
    # module - the executor resolves it via
    # `fpulse.engine.retry_policy.resolve_workflow_policy(workflow)`.
    retry_policy: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Lifecycle fields
    status: PipelineStatus = PipelineStatus.DRAFT
    published_at: datetime | None = None
    published_by: str | None = None
    test_results: dict[str, Any] | None = None  # Last test run summary
    # Deployment tracking — which version is live in PROD
    deployed_version: int | None = None  # None = latest published
    deployed_at: datetime | None = None
    deployed_by: str | None = None
    rollback_from: int | None = None  # Previous version if this was a rollback
    # Ownership — who created and who maintains this pipeline
    owner_id: str = ""  # User who created it
    owner_name: str = ""
    # D4 (2026-05-26) — Pipeline-level provenance. 'copilot' = drafted
    # whole by the agent; 'human' = built manually; 'template' = loaded
    # from a template; 'imported' = uploaded JSON. Defaults to None for
    # legacy rows. Pairs with Step.proposed_by for per-node detail.
    origin: str | None = None  # "copilot" | "human" | "template" | "imported" | None

    # ── Documentation (self-documenting pipelines) ────────────────────
    # First-class documentation. All optional + empty-default, so every
    # workflow persisted before this field existed round-trips unchanged
    # (backfill-safe — no migration, no forced re-save).
    #   * business_purpose — the WHY of this pipeline, one line. The
    #     publish path requires it so nothing goes live without a stated
    #     purpose (enforced at the publish action, never retroactively —
    #     already-published rows are untouched).
    #   * readme           — freeform Markdown shown beside the canvas and
    #     folded verbatim into the generated Markdown doc export.
    #   * tags             — promoted from the previously-freeform
    #     metadata["tags"] blob to a first-class, filterable list (the
    #     inventory report at reports/inventory.py already reads a top-level
    #     `tags` key; this makes that field real). Legacy metadata tags are
    #     hoisted on load by _hoist_legacy_tags below.
    business_purpose: str = ""
    readme: str = ""
    tags: list[str] = Field(default_factory=list)

    # ── Governance (PR 3) ─────────────────────────────────────────────
    # F-Pulse+ approval / deploy / PROD-toggle state. Always None on
    # OSS workflows — OSS is a single-user tool with no approval flow.
    # See ir/governance.py for the full sub-model and migration notes.
    governance: WorkflowGovernance | None = None

    # ── Deprecated inline governance fields ───────────────────────────
    # These mirror the WorkflowGovernance sub-model and remain populated
    # on the v1.x wire format for back-compat with any caller that
    # hasn't migrated to `governance` yet. New code MUST read/write
    # `governance` instead — these inline fields will be removed in a
    # future major version. Workflow.normalize_governance() (below) is
    # the helper that keeps the two paths in sync at save time.
    submitted_for_review: bool = False
    submitted_by: str | None = None
    submitted_at: datetime | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    approval_status: str = ""
    approval_notes: str = ""
    approval_stage: str = ""
    sandbox_approved_at: datetime | None = None
    sandbox_approved_by: str | None = None
    sandbox_approval_notes: str = ""
    deploy_approved_at: datetime | None = None
    deploy_approved_by: str | None = None
    deploy_approval_notes: str = ""
    deploy_evidence_sandbox_run_id: str | None = None
    is_active_dev: bool = True
    is_active_prod: bool = True

    @model_validator(mode="after")
    def _hoist_legacy_tags(self) -> "Workflow":
        """Promote a legacy ``metadata['tags']`` list to the first-class
        ``tags`` field.

        Older workflows (and any hand-edited blob) that stashed tags in
        the freeform metadata dict surface them as first-class on the
        next load — no data migration required. Only fires when ``tags``
        is still empty, so an explicit first-class value always wins.
        Cheap (a dict lookup); safe on the executor/parse hot path.
        """
        if not self.tags and isinstance(self.metadata, dict):
            legacy = self.metadata.get("tags")
            if isinstance(legacy, list):
                self.tags = [str(t).strip() for t in legacy if str(t).strip()]
        return self

    def normalize_governance(self) -> "Workflow":
        """Sync the deprecated inline governance fields with the new
        ``governance`` sub-model.

        Direction:
          * If ``governance`` is set, copy ITS values down onto the
            inline fields so legacy readers see the latest state.
          * If ``governance`` is None but ANY inline field is non-default,
            populate ``governance`` from the inline fields. This lets
            workflows migrated from the v1.0 inline layout pick up the
            new sub-model on the next save without losing data.

        Returns self (in-place) for fluent use.
        """
        from fpulse.ir.governance import (
            WorkflowGovernance as _WG,
            is_empty_governance,
        )

        if self.governance is not None:
            g = self.governance
            self.submitted_for_review = g.submitted_for_review
            self.submitted_by = g.submitted_by
            self.submitted_at = g.submitted_at
            self.approved_by = g.approved_by
            self.approved_at = g.approved_at
            self.approval_status = g.approval_status
            self.approval_notes = g.approval_notes
            self.approval_stage = g.approval_stage
            self.sandbox_approved_at = g.sandbox_approved_at
            self.sandbox_approved_by = g.sandbox_approved_by
            self.sandbox_approval_notes = g.sandbox_approval_notes
            self.deploy_approved_at = g.deploy_approved_at
            self.deploy_approved_by = g.deploy_approved_by
            self.deploy_approval_notes = g.deploy_approval_notes
            self.deploy_evidence_sandbox_run_id = g.deploy_evidence_sandbox_run_id
            self.is_active_dev = g.is_active_dev
            self.is_active_prod = g.is_active_prod
            return self

        # No governance set — promote any non-default inline values up.
        candidate = _WG(
            submitted_for_review=self.submitted_for_review,
            submitted_by=self.submitted_by,
            submitted_at=self.submitted_at,
            approved_by=self.approved_by,
            approved_at=self.approved_at,
            approval_status=self.approval_status,
            approval_notes=self.approval_notes,
            approval_stage=self.approval_stage,
            sandbox_approved_at=self.sandbox_approved_at,
            sandbox_approved_by=self.sandbox_approved_by,
            sandbox_approval_notes=self.sandbox_approval_notes,
            deploy_approved_at=self.deploy_approved_at,
            deploy_approved_by=self.deploy_approved_by,
            deploy_approval_notes=self.deploy_approval_notes,
            deploy_evidence_sandbox_run_id=self.deploy_evidence_sandbox_run_id,
            is_active_dev=self.is_active_dev,
            is_active_prod=self.is_active_prod,
        )
        if not is_empty_governance(candidate):
            self.governance = candidate
        return self


class WorkflowVersion(BaseModel):
    """A versioned snapshot of a workflow IR."""
    version: int = 1
    workflow: Workflow
    created_by: str = "user"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    change_summary: str = ""


# ── N10: Deployment object (2026-05-26) ────────────────────────────────
#
# Prefect-style "Deployment" — the unifying object that ties a pipeline
# to the parameters, schedule, and worker pool it runs against. Today
# these pieces live across separate rows (workflows, schedules, pool
# allocation); the Deployment lets one pipeline run in N different
# contexts (e.g. nightly-prod, hourly-dev) without N copies of the IR.
#
# Round 1 ships only the data model. Round 2 will:
#   - persist deployments in the datastore (similar to schedules)
#   - resolve schedules to deployments + parameter sets
#   - add CRUD endpoints under /api/deployments
#   - wire into the Pipelines page row actions
#
# Out of scope for OSS: multi-worker-pool routing. OSS uses a single
# in-process pool; the worker_pool field exists for forward-compat
# with the Plus pool-allocation routing but defaults to "default" and
# isn't enforced.


class RecipeStep(BaseModel):
    """One sub-step inside a Recipe.

    Mirrors the shape the existing `data_wrangler` node already accepts
    in its `steps` param so an existing Wrangler node can be saved as
    a Recipe without translation:

      {op: "filter",   params: {condition: "x > 0"}, enabled: true}
      {op: "rename",   params: {mappings: {old: new}}, enabled: true}
      {op: "typecast", params: {casts: {amount: "DOUBLE"}}, enabled: true}
      ...

    Operations supported are whatever the wrangler runtime supports —
    enforcing that here would couple this model to the runtime. The
    Recipe runner validates ops at execution time and reports unknown
    ops as a clean error.
    """
    op: str
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    name: str = ""  # optional human label, e.g. "Trim whitespace"


class Recipe(BaseModel):
    """V2 — a reusable transform sequence addressable by id.

    The same recipe can be applied by multiple pipelines (or by a
    File Data Prep one-shot load). Once a recipe is saved, edits
    propagate to every pipeline that references it on the next run.
    Pipelines that want a snapshot for stability can copy the recipe
    inline into a `data_wrangler` node instead of referencing it.

    Round 1 stores the steps + ownership; lineage of which pipelines
    reference this recipe is computed on demand by scanning workflow
    IRs (similar to the Used-By computation already done for managed
    tables).

    Fields:
      * ``id``           — opaque uuid
      * ``workspace_id`` — tenant boundary
      * ``name``         — human label, unique per workspace
      * ``description``  — free-text purpose note
      * ``steps``        — ordered list of RecipeStep
      * ``tags``         — free-form labels
      * ``owner_*``      — who created / maintains
    """
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    workspace_id: str = "default"
    name: str
    description: str = ""
    steps: list[RecipeStep] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    owner_id: str = ""
    owner_name: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Deployment(BaseModel):
    """A named bundle of (workflow + parameters + schedule + worker pool).

    A workflow can have multiple deployments — one per environment /
    cadence (nightly-prod, hourly-dev, on-demand-backfill). The
    runtime executes the workflow's IR with this deployment's
    parameters when the schedule fires (or when the deployment is
    invoked manually).

    Fields:
      * ``id``             — opaque uuid
      * ``workspace_id``   — tenant boundary
      * ``workflow_id``    — the IR this deployment runs
      * ``name``           — human label, unique per (workspace, workflow)
      * ``description``    — free-text purpose note
      * ``parameters``     — overrides for the workflow's declared
                             ``Workflow.parameters`` at run time
      * ``schedule``       — optional inline schedule (cron + tz). When
                             null the deployment runs on demand only.
      * ``worker_pool``    — pool name to enqueue runs into. OSS default
                             is "default" — Plus uses this to route to
                             a dedicated pool per deployment.
      * ``enabled``        — kill switch; disables schedule firing
                             without deleting the row
      * ``environment``    — "dev" or "prod"; Plus uses this with RBAC
                             to gate who can deploy where
      * ``created_at`` / ``updated_at`` — audit timestamps
    """
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    workspace_id: str = "default"
    workflow_id: str
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] | None = None  # {cron: "0 2 * * *", timezone: "UTC"}
    worker_pool: str = "default"
    enabled: bool = True
    environment: str = "dev"  # "dev" | "prod"
    # Ownership + audit
    owner_id: str = ""
    owner_name: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # N10 round 4 — scheduler dedup. Set to the time the scheduler
    # fired this deployment's last cron-due execution. The next tick
    # uses it (along with the cron expression) to decide whether to
    # fire again, the same way the existing schedule store tracks
    # last_run_at. None on never-run deployments.
    last_run_at: datetime | None = None


class StepErrorType(str, Enum):
    """Reason-code taxonomy for ``StepRunResult.error_type`` (PR 7).

    Plain-text ``error`` strings are unsearchable and unactionable. The
    Executions UI uses ``error_type`` to render a colour-coded badge and
    offer one-click "Fix" CTAs (e.g. CREDENTIAL_EXPIRED → "Open
    credential", DUCKDB_OOM → "Reduce sample size").

    Defaults to UNKNOWN when raising code didn't supply one — that way
    legacy nodes don't need an upgrade to keep working.
    """
    # User-supplied configuration is wrong or missing required fields.
    INVALID_CONFIG = "invalid_config"
    # Outbound HTTP / DB / S3 / API call failed (timeout, DNS, TLS, 5xx).
    NETWORK_ERROR = "network_error"
    # DuckDB hit its memory_limit and couldn't spill further.
    DUCKDB_OOM = "duckdb_oom"
    # Credential the connector tried to use was rejected (401/403) or
    # expired (OAuth refresh failed).
    CREDENTIAL_EXPIRED = "credential_expired"
    # Step was cancelled by the watchdog after exceeding its time budget.
    TIMEOUT = "timeout"
    # Upstream step failed; this step couldn't run.
    UPSTREAM_FAILED = "upstream_failed"
    # Workflow-level validation rejected the IR before execution started.
    VALIDATION_FAILED = "validation_failed"
    # Catch-all for raises that didn't supply a type. UI shows "Unknown
    # error" — the long-form ``error`` string is still surfaced.
    UNKNOWN = "unknown"


class StepRunResult(BaseModel):
    """Result of executing a single step."""
    step_id: str
    status: str = "pending"  # pending | running | success | error
    row_count: int = 0
    columns: list[str] = Field(default_factory=list)
    sample_data: list[dict[str, Any]] = Field(default_factory=list)
    schema_info: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    # PR 7 — categorised error reason. Populated when ``status == "error"``;
    # None on success. New code that raises should set this; legacy code
    # that doesn't gets bucketed as UNKNOWN by the executor wrapper.
    error_type: StepErrorType | None = None
    # E1.1 (2026-06-08, docs/design/executor-maturity-1.2.md) — broader
    # retry-policy classification. Populated by the executor's error
    # handler via fpulse.engine.failure_class.classify_error(). Distinct
    # from error_type (the executor's narrow taxonomy); this is the
    # wider "is this retryable?" classification the retry policy reads.
    # Values are FailureClass enum strings: transient / dependency /
    # data_quality / user_input / fatal / unknown. None on success.
    failure_class: str | None = None
    duration_ms: float = 0


class WorkflowRunResult(BaseModel):
    """Result of running an entire workflow."""
    workflow_id: str
    status: str = "pending"
    step_results: dict[str, StepRunResult] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    duration_ms: float = 0
