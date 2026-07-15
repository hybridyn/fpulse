"""Connection models — reusable saved connections with parameterized reports."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Connection Types ──

CONNECTION_TYPES = [
    # ── Relational Databases ──
    "postgresql", "mysql", "mssql", "oracle", "sqlite", "mariadb",
    "db2", "sap_hana", "teradata", "informix", "cockroachdb",
    # ── NoSQL / Document / Graph ──
    "mongodb", "cassandra", "couchbase", "dynamodb", "cosmosdb",
    "neo4j", "arangodb", "firebase",
    # ── Cloud Data Warehouses ──
    "snowflake", "bigquery", "redshift", "databricks", "synapse",
    "clickhouse", "duckdb", "trino", "presto", "athena",
    # ── Key-value / Search / Cache ──
    "redis", "elasticsearch", "opensearch", "memcached", "solr",
    # ── Object / Cloud Storage ──
    "s3", "gcs", "azure_blob", "adls_gen2", "minio",
    # ── File Systems ──
    "ftp", "sftp", "hdfs", "local_file",
    # ── Streaming / Messaging ──
    "kafka", "rabbitmq", "pulsar", "kinesis", "eventhub",
    "sqs", "pubsub", "nats",
    # ── APIs ──
    # 2026-05-22 — microsoft_graph slotted here (NOT under
    # Collaboration) because it's a generic API surface that
    # SharePoint / OneDrive / Teams / Planner / Outlook / Users /
    # Groups all federate through. The product-specific SharePoint /
    # OneDrive connectors stay for their file-flavored UX (browse +
    # download); microsoft_graph is the general-purpose REST surface
    # for any Graph endpoint.
    "rest_api", "graphql", "odata", "soap", "microsoft_graph",
    # ── SaaS / Business Apps ──
    # 2026-05-23 (T4 + U1/U2 + V1/V2): the Oracle / SAP product
    # families. `oracle_fusion` and `oracle_bip` replace the vague
    # `oracle_api`; `sap_s4hana` and `sap_successfactors` disambiguate
    # the previous catch-all `sap`. Legacy types stay listed for
    # back-compat — see DEPRECATED_TYPE_ALIASES for the resolution map.
    "oracle_fusion", "oracle_bip", "oracle_api",
    "sap_s4hana", "sap_successfactors",
    "salesforce", "hubspot", "zendesk", "jira", "servicenow",
    "sap", "netsuite", "dynamics365", "workday",
    # 2026-05-23 (W1): promoted from manifest-only to first-class
    # saved connections. Each ships with a backend tester + catalog
    # provider that drives the existing v1 manifest's streams.
    "github", "shopify", "stripe", "notion", "asana",
    # ── Collaboration / Productivity ──
    "sharepoint", "onedrive", "gdrive", "dropbox", "box",
    "gsheet", "slack", "teams",
    # ── Data Integration / ETL ──
    "airbyte", "fivetran", "dbt",
    # ── Notifications ──
    "smtp", "sendgrid", "twilio", "pagerduty",
    # ── Observability ──
    "datadog", "splunk", "newrelic", "prometheus", "grafana",
    # ── Vector / AI ──
    "pinecone", "weaviate", "qdrant", "chroma", "pgvector", "milvus",
    # ── Catch-all ──
    "custom",
]

# Connector categories for UI grouping
CONNECTION_CATEGORIES = {
    "Databases": ["postgresql", "mysql", "mssql", "oracle", "sqlite", "mariadb",
                   "db2", "sap_hana", "teradata", "informix", "cockroachdb"],
    "NoSQL": ["mongodb", "cassandra", "couchbase", "dynamodb", "cosmosdb",
              "neo4j", "arangodb", "firebase"],
    "Data Warehouses": ["snowflake", "bigquery", "redshift", "databricks", "synapse",
                        "clickhouse", "duckdb", "trino", "presto", "athena"],
    "Cloud Storage": ["s3", "gcs", "azure_blob", "adls_gen2", "minio"],
    "File Systems": ["ftp", "sftp", "hdfs", "local_file"],
    "Streaming": ["kafka", "rabbitmq", "pulsar", "kinesis", "eventhub",
                   "sqs", "pubsub", "nats"],
    "APIs": ["rest_api", "graphql", "odata", "soap", "microsoft_graph"],
    "SaaS": ["salesforce", "hubspot", "zendesk", "jira", "servicenow",
             # 2026-05-23: Oracle / SAP product families.
             "oracle_fusion", "oracle_bip",
             "sap_s4hana", "sap_successfactors",
             "sap", "netsuite", "dynamics365", "workday",
             # 2026-05-23 (W1): manifest-promoted SaaS.
             "github", "shopify", "stripe", "notion", "asana"],
    "Collaboration": ["sharepoint", "onedrive", "gdrive", "dropbox", "box",
                      "gsheet", "slack", "teams"],
    "Notifications": ["smtp", "sendgrid", "twilio", "pagerduty"],
    "Search & Cache": ["redis", "elasticsearch", "opensearch", "memcached", "solr"],
    "Observability": ["datadog", "splunk", "newrelic", "prometheus", "grafana"],
    "Vector / AI": ["pinecone", "weaviate", "qdrant", "chroma", "pgvector", "milvus"],
    "Integration": ["airbyte", "fivetran", "dbt"],
    "Other": ["custom"],
}

# ── Deprecated type aliases ──
#
# When a connector gets renamed (vague → specific) we keep the legacy
# name registered so old saved connections continue to load. The
# resolver below maps a legacy type to its canonical replacement on
# read; create paths block legacy types from new connections to nudge
# users to the canonical name.
#
# 2026-05-23 (T4 + V3): first batch — Oracle and SAP umbrella renames.
DEPRECATED_TYPE_ALIASES: dict[str, str] = {
    # Vague "Oracle ERP API" → product-specific Oracle Fusion REST.
    # `oracle_bip` users had to pick `oracle_api` previously even
    # though BI Publisher is a different protocol — they're now
    # encouraged to migrate to the named connector.
    "oracle_api": "oracle_fusion",
    # Vague "SAP (OData)" → product-specific S/4HANA OData. Users
    # wanting SuccessFactors / Ariba should pick the dedicated type.
    "sap": "sap_s4hana",
}


def resolve_connection_type(connection_type: str) -> str:
    """Resolve a legacy connection_type to its canonical name.

    Reads return the canonical name so downstream dispatch (testers,
    catalog providers, runtime nodes) sees one shape regardless of
    which alias the row was created under. Writes should pass through
    unchanged — saving a legacy alias on a brand-new connection is a
    UX miss, but the underlying type still works.
    """
    return DEPRECATED_TYPE_ALIASES.get(connection_type, connection_type)


# Types that support file format selection (CSV, JSON, Parquet, etc.)
STORAGE_TYPES = {"s3", "gcs", "azure_blob", "adls_gen2", "minio",
                 "ftp", "sftp", "hdfs", "local_file",
                 "sharepoint", "onedrive", "gdrive", "dropbox", "box"}

FILE_FORMATS = ["csv", "json", "parquet", "excel", "xml", "avro", "orc", "delta"]


# Connection types that are inherently one-directional. Everything not
# in either set defaults to ["read", "write"] — most DB-like and file-
# storage connectors can do both. Apr 22 2026: added so source nodes
# don't show notification destinations, and sink nodes don't show
# read-only observability endpoints.
WRITE_ONLY_TYPES = {
    # Notification destinations — push messages out only
    "slack", "teams", "smtp", "sendgrid", "twilio", "pagerduty",
}
READ_ONLY_TYPES: set[str] = set()  # placeholder; nothing strictly read-only yet


def default_capabilities(connection_type: str) -> list[str]:
    """Pick a sensible default capability list for a connector type.

    Frontend pre-fills the create form's checkboxes from this; user can
    flip either box. Backend always trusts whatever is sent — this helper
    is purely a UX default, not an authorisation rule.
    """
    if connection_type in WRITE_ONLY_TYPES:
        return ["write"]
    if connection_type in READ_ONLY_TYPES:
        return ["read"]
    return ["read", "write"]


# ── Report Parameter ──

class ReportParameter(BaseModel):
    """A single parameter definition for a report template."""
    name: str
    type: str = "string"  # string, integer, float, date, boolean
    default: Any = None
    required: bool = True


# ── Connection ──

# 2026-05-22 (audit G4): allowed values for the ``environment`` field
# on Connection / Credential / etc. The previous schema accepted any
# string. The audit flagged that the frontend filter then misclassified
# rows whose value didn't match dev/prod/all. Centralise the set here
# so the API layer can validate against it.
VALID_ENVIRONMENTS: frozenset[str] = frozenset({"dev", "prod", "all"})


class Connection(BaseModel):
    """A reusable saved connection (database, API, cloud service, etc.)."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    type: str  # one of CONNECTION_TYPES
    config: dict[str, Any] = Field(default_factory=dict)
    credential_id: str | None = None  # optional ref to credentials store
    project_id: str | None = None  # None = Global, set = project-scoped
    # Tenant boundary — connections are NOT shared across workspaces
    # even when project_id is None ("Global"). "Global within workspace"
    # is the correct read: a DSN that's visible to every project in
    # one workspace must still be invisible to every project in
    # another workspace. Legacy rows back-filled to 'default' by v6.
    workspace_id: str = "default"
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    # Environment visibility — 'dev' / 'prod' isolate the connection to one
    # env, 'all' shows it in both, None = legacy untagged (hidden by the
    # strict filter on the frontend until re-tagged). Matches Credential.
    environment: str | None = None
    # Direction capabilities — which roles this connection can play in a
    # pipeline. ConnectionPicker filters by this so source nodes don't
    # see notification-only endpoints and sink nodes don't see read-only
    # ones. Defaults to both for DB-like connectors (see
    # default_capabilities). Empty list = wildcard, treated as both for
    # backward compat with rows created before this field existed.
    capabilities: list[str] = Field(default_factory=lambda: ["read", "write"])
    # Last connectivity-test result. Populated by /test endpoint; the
    # Connections list reads these to power HEALTHY / STALE / FAILING
    # stat cards and the per-row "last test" column.
    last_test_at: datetime | None = None
    last_test_ok: bool | None = None
    last_test_error: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ConnectionCreate(BaseModel):
    name: str
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    credential_id: str | None = None
    project_id: str | None = None  # None = Global
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    environment: str | None = None
    # None on create → server picks the type-appropriate default via
    # default_capabilities(). Caller may pass an explicit list to override.
    capabilities: list[str] | None = None


class ConnectionUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    config: dict[str, Any] | None = None
    credential_id: str | None = None
    project_id: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    environment: str | None = None
    capabilities: list[str] | None = None


# ── Connection Report ──

class ConnectionReport(BaseModel):
    """A parameterized report/query template attached to a connection."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    connection_id: str
    name: str
    description: str = ""
    query_template: str = ""  # SQL or request template with {{param}} placeholders
    parameters: list[ReportParameter] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReportCreate(BaseModel):
    name: str
    description: str = ""
    query_template: str = ""
    parameters: list[ReportParameter] = Field(default_factory=list)


class ReportUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    query_template: str | None = None
    parameters: list[ReportParameter] | None = None


# ── Run Report ──

class RunReportRequest(BaseModel):
    """Request to run a report with parameter values.

    2026-05-22 (audit G3): ``report_id`` made optional. The URL
    ``POST /connections/{connection_id}/reports/{report_id}/run``
    already carries the id in the path, and the frontend posts
    ``{"params": {...}}`` without echoing it. The old required field
    caused 422 on every UI invocation. URL stays the source of
    truth; ``report_id`` here is informational only (legacy clients
    that still send it are accepted unchanged).
    """
    report_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class RunReportResult(BaseModel):
    """Result of running a report."""
    connection_id: str
    report_id: str
    status: str = "success"  # success | error
    row_count: int = 0
    columns: list[str] = Field(default_factory=list)
    sample_data: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int = 0
    error: str | None = None
