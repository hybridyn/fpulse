"""Server-side metadata mirror of the curated frontend template catalog.

The full template definitions (steps + connections) live in
``frontend/src/templates/catalog.ts``. This module mirrors only the
metadata the Copilot needs to recommend a template — id, name, tagline,
description, complexity, category, tags. Step-level config is intentionally
excluded so the LLM context stays small and the agent never sees stale
node params.

Keep this list in sync with the frontend ``TEMPLATE_CATALOG``. The IDs
are the contract; the agent passes them back to the user as a
recommendation, then the user clicks "Use this template" on the
Templates page which loads the real definition from the frontend catalog.
"""

from __future__ import annotations

from typing import Literal, TypedDict


class BuiltinTemplateMeta(TypedDict):
    id: str
    name: str
    tagline: str
    description: str
    complexity: Literal["simple", "complex"]
    category: str
    tags: list[str]
    node_count: int


BUILTIN_TEMPLATES: list[BuiltinTemplateMeta] = [
    # ── Simple ───────────────────────────────────────────────────────────
    {
        "id": "csv_to_postgres",
        "name": "CSV → Postgres",
        "tagline": "File ingestion, the hello-world of ETL",
        "description": (
            "Read a CSV file, clean nulls and add a load timestamp, then "
            "upsert into a Postgres table. The smallest production-grade "
            "pipeline shape."
        ),
        "complexity": "simple",
        "category": "Ingest",
        "tags": ["File", "Transform", "Database"],
        "node_count": 4,
    },
    {
        "id": "api_incremental",
        "name": "API → DB (incremental)",
        "tagline": "SaaS extract with cursor pagination",
        "description": (
            "Pull from a paginated REST API using the last successful run "
            "as a cursor, flatten nested JSON, and append to a Postgres "
            "table. The most common production pattern for SaaS data."
        ),
        "complexity": "simple",
        "category": "Ingest",
        "tags": ["API", "Incremental", "Database"],
        "node_count": 4,
    },
    {
        "id": "db_to_warehouse",
        "name": "DB → Warehouse sync",
        "tagline": "Operational database to analytical store",
        "description": (
            "Query an operational Postgres table, roll up to daily "
            "aggregates, and write to a warehouse (Parquet on S3 by "
            "default). Foundation for any analytics layer."
        ),
        "complexity": "simple",
        "category": "Replicate",
        "tags": ["Database", "Aggregate", "Warehouse"],
        "node_count": 3,
    },
    {
        "id": "scheduled_alert",
        "name": "Scheduled alert",
        "tagline": "Operational monitoring → notify on threshold",
        "description": (
            "Run a KPI query on a schedule, branch when the result crosses "
            "a threshold, and post to Slack with the offending rows. The "
            "starter pattern for pager-style operational alerting."
        ),
        "complexity": "simple",
        "category": "Monitor",
        "tags": ["Database", "Conditional", "Slack"],
        "node_count": 3,
    },
    # ── Complex ─────────────────────────────────────────────────────────
    {
        "id": "multi_source_join",
        "name": "Multi-source join → warehouse",
        "tagline": "Customer 360 from DB + SaaS API",
        "description": (
            "Pull customer records from Postgres, fetch lifetime-value "
            "scores from a billing API, join on customer_id, enrich with "
            "a derived segment, and load into a warehouse table. The "
            "canonical Customer 360 shape."
        ),
        "complexity": "complex",
        "category": "Multi-source",
        "tags": ["Multi-source", "Join", "Enrich", "Warehouse"],
        "node_count": 5,
    },
    {
        "id": "fanout_regional",
        "name": "Fan-out by region + union",
        "tagline": "Parallel processing per region, then merge",
        "description": (
            "Split incoming events by region, run a region-specific "
            "aggregate on each branch in parallel, union the results "
            "back together, and write a single combined report. "
            "Multi-tenant / regional processing pattern."
        ),
        "complexity": "complex",
        "category": "Branching",
        "tags": ["Conditional Split", "Aggregate", "Union"],
        "node_count": 7,
    },
    {
        "id": "ai_classify_route",
        "name": "AI classify + conditional route",
        "tagline": "LLM categorization → switch to right destination",
        "description": (
            "Read incoming items (tickets, leads, alerts), classify each "
            "with an LLM into urgent / standard / spam, then route to "
            "three different destinations. Demonstrates AI-augmented "
            "workflows with deterministic fallback."
        ),
        "complexity": "complex",
        "category": "AI",
        "tags": ["AI / Semantic", "Switch", "Multi-destination"],
        "node_count": 6,
    },
    {
        "id": "data_quality_dlq",
        "name": "Data quality + dead-letter queue",
        "tagline": "Trustworthy data — bad rows quarantined, not dropped",
        "description": (
            "Validate every row against a schema and business rules. "
            "Passing rows write to the clean table; failing rows write to "
            "a dead-letter table with the failure reason."
        ),
        "complexity": "complex",
        "category": "Quality",
        "tags": ["Validate", "Conditional Split", "DLQ"],
        "node_count": 5,
    },
    {
        "id": "reverse_etl",
        "name": "Reverse ETL: warehouse → SaaS",
        "tagline": "Operationalize analytics — push insights to revenue tools",
        "description": (
            "Query the warehouse for the latest customer scores, shape "
            "into the destination API's payload format, and POST in "
            "batches with retry. The mirror of ingestion."
        ),
        "complexity": "complex",
        "category": "Egress",
        "tags": ["Warehouse", "Reshape", "API write"],
        "node_count": 3,
    },
    {
        "id": "webhook_enrich",
        "name": "Webhook → enrich → DB",
        "tagline": "Real-time event ingestion with API-side enrichment",
        "description": (
            "Receive an inbound webhook, validate the payload shape, "
            "enrich each record by looking up additional context from a "
            "reference API, and persist to a database. The push-based "
            "mirror of polling-based API ingestion."
        ),
        "complexity": "complex",
        "category": "Real-time",
        "tags": ["Webhook", "Lookup", "Database"],
        "node_count": 4,
    },
]
