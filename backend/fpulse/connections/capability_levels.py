"""Capability-level inspector for the connector registry.

The connector ecosystem has several independent surfaces — declaration,
form, tester, catalog, source-runtime, sink-runtime, manifest, certified.
A connector can be present in one and absent from another, and the gaps
matter:

  * Declared but not testable → "Test connection" silently no-ops.
  * Catalog-only             → user can browse but not configure → confusing.
  * Testable but no runtime  → user can save + test, then can't use it.
  * Manifest-only            → ships as `saas_connector` template but
                               isn't a first-class saved connection.

This module computes those levels by introspecting the live registries
at call-time. It's a read-only inspector — it doesn't register anything
or mutate state. Used by:

  * ``GET /api/v1/catalog/connectors`` — exposes ``capability_level`` so
    the public catalog matches honesty rules.
  * ``docs/CONNECTOR_GAP_INVESTIGATION_2026_05_23.md`` — the audit that
    motivated this T2 module asked for a single computed view of where
    a type sits in the maturity matrix.
  * The conformance test (a future addition) — to fail CI when a type
    drops below the level it shipped at.

Frontend ``form`` capability isn't introspectable from the backend
(picker + field-config live in TS). The ``FRONTEND_FORM_TYPES`` set
below is the backend's mirror; keep it in lockstep with the picker
entries in ``frontend/src/components/pages/ConnectionsPage.tsx`` and
the audit's gap analysis will catch drift.
"""

from __future__ import annotations

from typing import Iterable

from fpulse.connections.models import CONNECTION_TYPES


# ── Backend mirror of the frontend picker ─────────────────────────────────
#
# The frontend's ConnectionsPage.tsx is the source of truth for whether a
# connector renders a config form. This set mirrors the picker entries so
# the backend can compute ``capability_level: "form"`` honestly. Update
# this whenever a new entry is added to the picker (or removed). The
# audit at docs/CONNECTOR_GAP_INVESTIGATION_2026_05_23.md flagged the
# drift between backend-known and frontend-pickable types — this set is
# the resolution.

FRONTEND_FORM_TYPES: frozenset[str] = frozenset({
    # Relational DBs
    "postgresql", "mysql", "mssql", "oracle", "sqlite", "mariadb",
    "db2", "sap_hana", "teradata",
    # Cloud warehouses
    "snowflake", "bigquery", "redshift", "databricks", "synapse",
    "clickhouse", "duckdb", "trino", "presto", "athena",
    # NoSQL
    "mongodb", "cassandra", "couchbase", "dynamodb", "cosmosdb",
    "neo4j", "arangodb", "firebase",
    # Search / cache
    "redis", "elasticsearch", "opensearch", "memcached", "solr",
    # Object / cloud storage
    "s3", "gcs", "azure_blob", "adls_gen2", "minio",
    # File systems
    "ftp", "sftp", "hdfs", "local_file",
    # Streaming / messaging
    "kafka", "rabbitmq", "pulsar", "kinesis", "eventhub",
    "sqs", "pubsub", "nats",
    # APIs
    "rest_api", "graphql", "odata", "soap", "microsoft_graph",
    # SaaS / business apps
    "salesforce", "hubspot", "zendesk", "jira", "servicenow",
    "sap", "netsuite", "dynamics365", "workday",
    # 2026-05-23 — Oracle / SAP product families surfaced in the picker.
    "oracle_fusion", "oracle_bip",
    "sap_s4hana", "sap_successfactors",
    # Legacy `oracle_api` stays selectable until it's removed from the
    # CONNECTION_TYPES list — kept here so a saved legacy row continues
    # to render its form when re-opened.
    "oracle_api",
    # 2026-05-23 (W1) — manifest-promoted SaaS.
    "github", "shopify", "stripe", "notion", "asana",
    # Collaboration
    "sharepoint", "onedrive", "gdrive", "dropbox", "box",
    "gsheet", "slack", "teams",
    # Notifications (write-only forms)
    "smtp", "sendgrid", "twilio", "pagerduty",
    # Observability
    "datadog", "splunk", "newrelic", "prometheus", "grafana",
    # Vector / AI
    "pinecone", "weaviate", "qdrant", "chroma", "pgvector", "milvus",
    # Catch-all
    "custom",
})


# ── Certified set ─────────────────────────────────────────────────────────
#
# Hand-curated. A connector lands here when:
#   1. It has form + tester + catalog + at least one runtime
#   2. The tester has been exercised against a real instance
#   3. There's a sample pipeline in samples/ exercising it end-to-end
#   4. The connector class has docs in docs/connectors/<type>.md
#
# Bump cautiously — claiming "certified" without backing erodes trust.
# Use the cert-matrix endpoint for the more granular F0.1 v2 view; this
# set is the boolean roll-up that backs the public catalog's bar chart.

CERTIFIED_TYPES: frozenset[str] = frozenset({
    "postgresql",
    "mysql",
    "mssql",
    "sqlite",
    "s3",
    "rest_api",
    "microsoft_graph",
})


# ── Computed level ───────────────────────────────────────────────────────


def _real_catalog_types() -> set[str]:
    """Returns types whose catalog provider is a real implementation
    (not a planned-placeholder _unsupported)."""
    # Late import — catalog_extensions registers on import side-effect
    # and we don't want to force-load it at module import time.
    from fpulse.connections.catalog import _REAL_TYPES  # type: ignore
    return set(_REAL_TYPES)


def _testable_types() -> set[str]:
    from fpulse.connections.tester import ConnectionTester  # type: ignore
    return set(ConnectionTester._TESTERS.keys())


def _source_runtime_types() -> set[str]:
    """Connector types reachable from a Source node at runtime.

    Two paths in the runtime today:
      1. Generic Source node dispatches via SOURCE_MAP keyed on
         ``connector_type`` — keys are short names ("rest_api", "s3").
      2. Legacy specific source nodes (CSV_SOURCE, DB_SOURCE, …) used
         pre-2026-05-22; back-compat for old workflows after the
         migrate_legacy_node_types() rewrite.

    A connection's ``type`` shares the SOURCE_MAP key namespace where
    they overlap (postgresql is "database" inside the map, etc.) — the
    helper translates the connection type to the runtime key.
    """
    from fpulse.nodes.generic import SOURCE_MAP  # type: ignore
    # Map connection-type identifiers → runtime dispatch keys
    conn_to_runtime: dict[str, str] = {
        # Databases all route through "database"
        "postgresql": "database", "mysql": "database", "mssql": "database",
        "oracle": "database", "sqlite": "database", "mariadb": "database",
        "snowflake": "database", "bigquery": "database",
        "redshift": "database", "databricks": "database",
        "clickhouse": "database", "duckdb": "database",
        # APIs
        "rest_api": "rest_api",
        # Object storage
        "s3": "s3", "azure_blob": "azure_blob", "gcs": "gcs",
        # Streaming
        "kafka": "kafka",
        # Files
        "ftp": "ftp",
        # Collaboration
        "sharepoint": "sharepoint", "onedrive": "onedrive",
        "gsheet": "gsheet",
        # Microsoft Graph (generic)
        "microsoft_graph": "microsoft_graph",
        # Delta
        "delta": "delta",
    }
    out: set[str] = set()
    for ct in CONNECTION_TYPES:
        rk = conn_to_runtime.get(ct, ct)
        if rk in SOURCE_MAP:
            out.add(ct)
    return out


def _sink_runtime_types() -> set[str]:
    from fpulse.nodes.generic import DEST_MAP  # type: ignore
    conn_to_runtime: dict[str, str] = {
        "postgresql": "database", "mysql": "database", "mssql": "database",
        "oracle": "database", "sqlite": "database", "mariadb": "database",
        "snowflake": "warehouse", "bigquery": "warehouse",
        "redshift": "warehouse", "databricks": "warehouse",
        "clickhouse": "database",
        "rest_api": "rest_api",
        "s3": "s3", "azure_blob": "azure_blob", "gcs": "gcs",
        "kafka": "kafka",
        "sharepoint": "sharepoint", "onedrive": "onedrive",
        "smtp": "email", "sendgrid": "email",
        "delta": "delta",
    }
    out: set[str] = set()
    for ct in CONNECTION_TYPES:
        rk = conn_to_runtime.get(ct, ct)
        if rk in DEST_MAP:
            out.add(ct)
    return out


def _manifest_types() -> set[str]:
    """Connectors that ship as a v1 REST manifest. These appear in the
    SaaS-connector template list but aren't necessarily first-class
    saved connections — the T2 audit is exactly about closing that gap.
    """
    try:
        from fpulse.connectors.rest_framework import load_manifests
        return set(load_manifests().keys())
    except Exception:  # pragma: no cover — never break the catalog over an import
        return set()


def capability_levels(conn_type: str) -> dict[str, bool]:
    """Per-surface flags for a single connector type.

    Returns a dict with every level boolean so the frontend can render a
    full maturity table without follow-up calls. Callers wanting a
    single string roll-up should use ``maturity_label()``.
    """
    return {
        "declared":       conn_type in CONNECTION_TYPES,
        "form":           conn_type in FRONTEND_FORM_TYPES,
        "testable":       conn_type in _testable_types(),
        "catalog":        conn_type in _real_catalog_types(),
        "source_runtime": conn_type in _source_runtime_types(),
        "sink_runtime":   conn_type in _sink_runtime_types(),
        "manifest":       conn_type in _manifest_types(),
        "certified":      conn_type in CERTIFIED_TYPES,
    }


def maturity_label(levels: dict[str, bool]) -> str:
    """Roll a level dict up into a single label for UI badges.

    Ordering matters — pick the highest level whose preconditions hold,
    so a connector with no runtime surfaces but a working tester reads
    as "testable" rather than the more aspirational "certified".
    """
    if levels.get("certified"):
        return "certified"
    has_runtime = levels.get("source_runtime") or levels.get("sink_runtime")
    if levels.get("form") and levels.get("testable") and has_runtime:
        return "production"
    if levels.get("form") and levels.get("testable"):
        return "configurable"
    if levels.get("form"):
        return "form_only"
    if levels.get("manifest"):
        return "manifest_only"
    if levels.get("declared"):
        return "declared_only"
    return "unknown"


def all_levels() -> list[dict]:
    """Bulk variant used by the catalog endpoint.

    One pass over CONNECTION_TYPES so the helper introspections happen
    once per request instead of once per type.
    """
    testable = _testable_types()
    catalog = _real_catalog_types()
    src = _source_runtime_types()
    sink = _sink_runtime_types()
    manifests = _manifest_types()

    out: list[dict] = []
    for ct in CONNECTION_TYPES:
        levels = {
            "declared":       True,
            "form":           ct in FRONTEND_FORM_TYPES,
            "testable":       ct in testable,
            "catalog":        ct in catalog,
            "source_runtime": ct in src,
            "sink_runtime":   ct in sink,
            "manifest":       ct in manifests,
            "certified":      ct in CERTIFIED_TYPES,
        }
        out.append({
            "type": ct,
            "levels": levels,
            "label": maturity_label(levels),
        })
    return out


__all__ = [
    "CERTIFIED_TYPES",
    "FRONTEND_FORM_TYPES",
    "all_levels",
    "capability_levels",
    "maturity_label",
]
