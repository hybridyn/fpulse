"""
Public read-only catalog endpoints — no auth required.

Inspired by OpenRouter's public `/api/v1/models` catalog. Lets anyone
(including search engines, integration partners, the agent's
inspect_connections tool, and external docs) read what F-Pulse supports
without an account.

Mounted on /api/v1 (not /api) so it sits next to a stable versioned
namespace separate from the auth'd endpoints under /api.

Sources of truth:
  - Connectors:  fpulse.connections.models.CONNECTION_TYPES
  - Step types:  fpulse.ir.schema.StepType (the IR enum)
  - Changelog:   the in-file _CHANGELOG list below; refresh per release.

Catalog layer ONLY — no IR / RBAC / agent-loop changes. Tier mapping is
declared as metadata so consumers can render their own tier filter on
top of the same response.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from fpulse.connections.capability_levels import (
    all_levels as _all_capability_levels,
    capability_levels as _capability_levels_for,
    maturity_label as _maturity_label,
)
from fpulse.connections.models import CONNECTION_TYPES
from fpulse.ir.schema import StepType


router = APIRouter(prefix="/api/v1/catalog", tags=["catalog-public"])


# ---------------------------------------------------------------------------
# Connector tier mapping. Most connectors are OSS-tier; SAP / NetSuite /
# Workday / Dynamics365 / ServiceNow + the heavyweight enterprise warehouses
# are F-Pulse+ tier per the existing pricing posture. Keeping it declarative
# so the public catalog is honest about what's free vs paid.
# ---------------------------------------------------------------------------

_PLUS_ONLY_CONNECTORS = {
    "sap", "sap_hana", "netsuite", "workday", "dynamics365", "servicenow",
    "salesforce", "informix", "teradata", "db2",
}

# ---------------------------------------------------------------------------
# Capability flags per connector. Used by:
#   - Agent (decide whether a connector can serve as source / sink / both)
#   - UI filtering ("show me only streaming-capable destinations")
#   - Marketplace install gates (capability-based requirements)
#
# Default = read + write (most relational DBs / warehouses). Streaming + cdc
# are explicit opt-ins for connectors that genuinely support them.
# ---------------------------------------------------------------------------

_DEFAULT_CAPS = ["read", "write"]
_STREAMING_CAPS = ["read", "write", "stream"]
_READ_ONLY_CAPS = ["read"]

_CONNECTOR_CAPABILITIES: dict[str, list[str]] = {
    # Streaming systems — bidirectional + native stream protocol
    "kafka": _STREAMING_CAPS, "pulsar": _STREAMING_CAPS, "kinesis": _STREAMING_CAPS,
    "eventhub": _STREAMING_CAPS, "rabbitmq": _STREAMING_CAPS, "pubsub": _STREAMING_CAPS,
    "sqs": _STREAMING_CAPS, "nats": _STREAMING_CAPS,
    # Read-only sources (SaaS APIs that don't expose a write path in OSS)
    "salesforce": _READ_ONLY_CAPS, "hubspot": _READ_ONLY_CAPS,
    "zendesk": _READ_ONLY_CAPS, "jira": _READ_ONLY_CAPS,
    "servicenow": _READ_ONLY_CAPS, "workday": _READ_ONLY_CAPS,
    "netsuite": _READ_ONLY_CAPS, "dynamics365": _READ_ONLY_CAPS,
    "sap": _READ_ONLY_CAPS, "sap_hana": _READ_ONLY_CAPS,
    # Everything else falls through to read+write below.
}


# Per-connector versions. Bump when a breaking change to params shape lands.
# `deprecated` entries should also list `replaced_by` so the agent + UI can
# guide the user to the new one before the deprecation date.
_CONNECTOR_VERSIONS: dict[str, dict[str, Any]] = {
    # Format: { connector_id: { version, deprecated, deprecates_at, replaced_by } }
    # Default for unlisted: v1, not deprecated.
}

_CONNECTOR_CATEGORIES: dict[str, str] = {
    # Relational
    "postgresql": "relational", "mysql": "relational", "mssql": "relational",
    "oracle": "relational", "sqlite": "relational", "mariadb": "relational",
    "db2": "relational", "sap_hana": "relational", "teradata": "relational",
    "informix": "relational", "cockroachdb": "relational",
    # NoSQL
    "mongodb": "nosql", "cassandra": "nosql", "couchbase": "nosql",
    "dynamodb": "nosql", "cosmosdb": "nosql", "neo4j": "nosql",
    "arangodb": "nosql", "firebase": "nosql",
    # Warehouses
    "snowflake": "warehouse", "bigquery": "warehouse", "redshift": "warehouse",
    "databricks": "warehouse", "synapse": "warehouse", "clickhouse": "warehouse",
    "duckdb": "warehouse", "trino": "warehouse", "presto": "warehouse",
    "athena": "warehouse",
    # Object storage
    "s3": "object_storage", "gcs": "object_storage", "azure_blob": "object_storage",
    "adls_gen2": "object_storage", "minio": "object_storage",
    # File systems
    "ftp": "filesystem", "sftp": "filesystem", "hdfs": "filesystem",
    "local_file": "filesystem",
    # Streaming
    "kafka": "streaming", "rabbitmq": "streaming", "pulsar": "streaming",
    "kinesis": "streaming", "eventhub": "streaming", "sqs": "streaming",
    "pubsub": "streaming", "nats": "streaming",
    # APIs
    "rest_api": "api", "graphql": "api", "odata": "api", "soap": "api",
    "microsoft_graph": "api",
    # SaaS
    "salesforce": "saas", "hubspot": "saas", "zendesk": "saas",
    "jira": "saas", "servicenow": "saas", "sap": "saas",
    "netsuite": "saas", "dynamics365": "saas", "workday": "saas",
    # Collaboration
    "sharepoint": "collaboration", "onedrive": "collaboration",
    "gdrive": "collaboration", "dropbox": "collaboration", "box": "collaboration",
    "gsheet": "collaboration", "slack": "collaboration", "teams": "collaboration",
}


@router.get("/connectors")
def list_connectors() -> dict[str, Any]:
    """All supported connector types with tier + category + capabilities.

    No auth, no rate limit — designed to be hit by docs sites, marketplace
    pages, the agent's inspect_connections tool, and external integrations.
    Shape mirrors OpenRouter's `/api/v1/models`.

    Each entry carries:
      - id           Stable identifier (versioned). Use this for marketplace installs.
      - name         The connector type slug (= legacy CONNECTION_TYPES value).
      - category     UI filter bucket (relational / warehouse / streaming / …).
      - tier         "oss" (free) or "plus" (Plus-only).
      - capabilities ["read", "write"] | ["read", "write", "stream"] | ["read"]
      - version      "v1" by default; bumps when params shape changes.
      - deprecated   true once retirement is announced; pair with `replaced_by`.
      - deprecates_at ISO date; null when not deprecated.
      - replaced_by  ID of the successor connector (null when none).
    """
    # 2026-05-23 (T2): compute capability levels once per request so each
    # entry carries per-surface flags + a maturity label without N round
    # trips through the introspector. Levels surface where each type sits
    # in the registry maturity matrix (declared → form → testable →
    # catalog → runtime → certified).
    levels_index = {row["type"]: row for row in _all_capability_levels()}

    items: list[dict[str, Any]] = []
    for c in CONNECTION_TYPES:
        ver_meta = _CONNECTOR_VERSIONS.get(c, {})
        version = ver_meta.get("version", "v1")
        # Stable ID format: <name>_<version>. Survives renames; future
        # marketplace installs keyed off id, not name.
        stable_id = f"{c}_{version}"
        capabilities = _CONNECTOR_CAPABILITIES.get(c, _DEFAULT_CAPS)
        levels_row = levels_index.get(c, {"levels": {}, "label": "declared_only"})
        items.append({
            "id": stable_id,
            "name": c,
            "category": _CONNECTOR_CATEGORIES.get(c, "other"),
            "tier": "plus" if c in _PLUS_ONLY_CONNECTORS else "oss",
            "capabilities": capabilities,
            "version": version,
            "deprecated": bool(ver_meta.get("deprecated", False)),
            "deprecates_at": ver_meta.get("deprecates_at"),
            "replaced_by": ver_meta.get("replaced_by"),
            # T2 (2026-05-23): per-surface capability levels.
            "capability_levels": levels_row["levels"],
            "maturity": levels_row["label"],
        })
    items.sort(key=lambda x: (x["category"], x["name"]))

    # 2026-05-22: include the verification breakdown so consumers don't
    # interpret `count: 92` as "92 production-grade connectors". The
    # truth is more nuanced — most are sdk_validated (code looks right,
    # never run live), a handful are integration_verified, none are yet
    # production_ready per the strict rubric. Match the shape that
    # `/api/connectors/cert-matrix` exposes so external integrations can
    # rely on a single honest summary.
    verification_summary: dict[str, int] = {
        "sdk_validated": 0,
        "integration_verified": 0,
        "production_ready": 0,
        "n_a": 0,
        "unspecified": 0,
    }
    try:
        # Read provider META (the ProviderMeta dataclass carries the
        # `verification` field) — NOT _PROVIDERS, whose values are catalog
        # factory FUNCTIONS with no `verification` attribute. Reading the
        # functions made every connector fall back to "unspecified".
        from fpulse.connections.catalog import _PROVIDER_META  # type: ignore
        for meta in (_PROVIDER_META or {}).values():
            state = getattr(meta, "verification", None) or "unspecified"
            verification_summary[state] = verification_summary.get(state, 0) + 1
    except Exception:  # noqa: BLE001 — best-effort
        pass

    # 2026-05-23 (T2): maturity roll-up so callers can show a one-line
    # health bar without iterating every entry. The shape is parallel to
    # verification_summary above for symmetry.
    maturity_summary: dict[str, int] = {}
    for it in items:
        m = it.get("maturity") or "unknown"
        maturity_summary[m] = maturity_summary.get(m, 0) + 1

    return {
        "connectors": items,
        "count": len(items),
        # Honest sub-totals so callers don't conflate "registered" with
        # "verified". See docs/connectors.md for the full rubric.
        "verification_summary": verification_summary,
        "maturity_summary": maturity_summary,
        "categories": sorted({c["category"] for c in items}),
        "capabilities_index": sorted({cap for it in items for cap in it["capabilities"]}),
        "doc_url": "https://docs.fpulse.io/connectors",
        "cert_matrix_url": "/api/connectors/cert-matrix",
        # v4 — adds capability_levels + maturity per entry, maturity_summary roll-up (T2 2026-05-23).
        "schema_version": "v4",
    }


@router.get("/step-types")
def list_step_types() -> dict[str, Any]:
    """All step (node) types the IR + executor supports.

    Useful for connector marketplace + integration tests + community
    template authors who want to know what's available without spelunking
    the source.
    """
    # Friendly category buckets — same taxonomy used by ai/embedded.py.
    SOURCES = {"csv_source", "db_source", "api_source", "json_source",
               "parquet_source", "excel_source", "xml_source", "s3_source",
               "adls_gen2_source"}
    ROW_TRANSFORMS = {"filter", "transform", "deduplicate", "sort", "rename",
                      "typecast", "derived_column"}
    SET_TRANSFORMS = {"aggregate", "join", "lookup", "union", "pivot",
                      "unpivot", "window"}
    QUALITY = {"sample", "validate", "conditional_split"}
    SINKS = {"output", "file_sink", "db_sink", "csv_sink", "json_sink",
             "excel_sink", "s3_sink", "kafka_sink", "api_sink", "delta_sink",
             "warehouse_sink", "adls_gen2_sink"}

    def _category(t: str) -> str:
        if t in SOURCES: return "source"
        if t in ROW_TRANSFORMS: return "row_transform"
        if t in SET_TRANSFORMS: return "set_transform"
        if t in QUALITY: return "quality"
        if t in SINKS: return "destination"
        return "other"

    # 2026-05-22: enrich each entry with the canonical contract so the
    # public catalog matches what /api/node-types returns. External
    # consumers (docs sites, agent inspect tools, template authors) get
    # the same arity / side-effects / deprecation metadata without
    # having to call two endpoints.
    from fpulse.ir.node_metadata import contract_for, side_effect_class_for
    from fpulse.ir.migrations import DEPRECATED_STEP_TYPES

    items = []
    for st in StepType:
        contract = contract_for(st.value)
        dep = DEPRECATED_STEP_TYPES.get(st.value)
        items.append({
            "id": st.value,
            "category": _category(st.value),
            "arity": {
                "required": contract["required"],
                "optional": contract["optional"],
                "variadic": contract["variadic"],
            },
            "side_effects": side_effect_class_for(st.value),
            "deprecated": dep is not None,
            "deprecation_reason": dep.reason if dep else None,
            "replaced_by": dep.replaced_by if dep else None,
        })
    items.sort(key=lambda x: (x["category"], x["id"]))
    return {
        "step_types": items,
        "count": len(items),
        "categories": sorted({i["category"] for i in items}),
        # Bumped to reflect the canonical-contract enrichment.
        "schema_version": "v2",
    }


# ---------------------------------------------------------------------------
# Public changelog — minimal, hand-curated. Shipped milestones only;
# version is a date so there's no semver coordination needed.
# ---------------------------------------------------------------------------

_CHANGELOG: list[dict[str, Any]] = [
    {
        "version": "2026-05-23",
        "date": "2026-05-23",
        "highlights": [
            "Storage page — first-class workspace data estate with three sub-tabs (Files / Managed Tables / Pipeline Outputs). Replaces ad-hoc folder conventions.",
            "Managed local tables — Parquet-backed tables addressable as schema.name from new local_table_source / local_table_sink IR nodes. Sink supports replace / append / merge modes.",
            "Upload dialog with scope picker — Global vs Project, with project + folder dropdowns. Drag-drop + description + size cap.",
            "Usage tracking — every file and managed table shows 'Used by N pipelines' pill. Destructive actions (Delete, Drop, Replace) gated by usage warnings.",
            "Replace bytes in place — POST /api/storage/file/{id}/replace swaps bytes without changing the object_id, so downstream pipelines pick up new data on next run.",
            "JSON document fallback in preview — non-tabular JSON (configs, OpenAPI specs, F-Pulse pipeline exports) renders as a JSON tree instead of crashing with malformed-JSON.",
            "Pipeline-shape detection — a workflow JSON uploaded to Storage by mistake gets an amber 'Open in Editor' banner that hands it off to the Workflows import flow.",
            "Oracle product family — oracle_fusion (REST across FSCM / HCM / CRM) + oracle_bip (BI Publisher). Replaces vague legacy oracle_api (kept as back-compat alias).",
            "SAP product family — sap_s4hana (OData v2/v4) + sap_successfactors (HRIS). Legacy sap kept as alias of sap_s4hana.",
            "Microsoft Graph — first-class connector for any Graph endpoint (users / groups / sites / drives / teams / planner / outlook). Client-credentials OAuth.",
            "Promoted to first-class — GitHub, Shopify, Stripe, Notion, Asana now have testers + catalog providers (were manifest-only).",
            "DB2 / SAP HANA / Teradata / Athena / Presto picker entries — these were backend-only before; now visible to users.",
            "Manifest runtime — default_query / default_headers / pagination.type='url' now honored. Six shipped manifests (sap_odata, servicenow, netsuite, dynamics365, twilio, ms_teams) had these fields silently dropped before.",
            "Connector adapter modules — odata (v2/v4 aware) + rest (config-driven pagination) for future first-class connectors that don't need a manifest file.",
            "Capability levels in /api/v1/catalog/connectors — per-surface flags (declared / form / testable / catalog / source_runtime / sink_runtime / manifest / certified) + maturity_summary roll-up.",
        ],
        "deprecated": [
            "oracle_api → use oracle_fusion (still loads as alias)",
            "sap → use sap_s4hana (still loads as alias)",
        ],
    },
    {
        "version": "2026-04-30",
        "date": "2026-04-30",
        "highlights": [
            "Pipeline parameters with ${param.X} substitution and run-time overrides via API/UI/Schedule",
            "Per-execution compute + memory tracking (peak_memory_mb, cpu_seconds) via psutil sampler",
            "Replay endpoint POST /api/execute/replay/{execution_id}",
            "OpenRouter as a 4th cloud provider (single key, 100+ models)",
            "Public catalog endpoints (/api/v1/catalog/{connectors,step-types,changelog})",
        ],
        "deprecated": [],
    },
    {
        "version": "2026-04-29",
        "date": "2026-04-29",
        "highlights": [
            "F-Pulse AI completion arc Tier A+B+C shipped: SSE streaming, RCA wiring, mailing summaries, pre-publish card, builder LLM, pre-run banner, transform helper, connection-test diagnose, post-run summary, metrics dashboard LLM, pre-run validation",
            "Provider price comparison + recommendation (live OpenRouter feed, hardcoded fallback)",
            "Cloud-first shift: Ollama removed from user-facing recommendation surfaces",
            "Token streaming for Ollama (NDJSON pass-through with tools)",
            "Copilot-style dock: side drawer + tabs (Chat / Suggestions / History) + slash commands + action chips",
        ],
        "deprecated": [
            "Apr 29 Ollama-first stance (reversed; backend support preserved as escape hatch)",
        ],
    },
]


@router.get("/changelog")
def get_changelog() -> dict[str, Any]:
    """Public changelog of shipped milestones + deprecations.

    Read-only; updated per release. Mirrors OpenRouter's "what changed"
    page so users + integrators see what's stable, what's new, and what's
    being phased out before it breaks them.
    """
    return {
        "entries": _CHANGELOG,
        "count": len(_CHANGELOG),
        "doc_url": "https://docs.fpulse.io/changelog",
    }
