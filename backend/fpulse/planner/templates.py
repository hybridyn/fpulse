"""Pre-built pipeline templates for common patterns."""

from __future__ import annotations

from fpulse.ir.schema import Workflow, Step, StepConnection, StepType, NodePosition


TEMPLATES: dict[str, dict] = {
    "simple_etl": {
        "name": "Simple ETL",
        "description": "Load File → Transform → Output Parquet",
        "icon": "arrow-right",
        "tags": ["beginner", "etl"],
    },
    "dedup_pipeline": {
        "name": "Deduplication Pipeline",
        "description": "Load data → Remove duplicates → Clean output",
        "icon": "copy-minus",
        "tags": ["cleaning", "dedup"],
    },
    "aggregation": {
        "name": "Aggregation Report",
        "description": "Load data → Filter → Group & Aggregate → Output",
        "icon": "bar-chart",
        "tags": ["analytics", "report"],
    },
    "data_quality": {
        "name": "Data Quality Check",
        "description": "Load data → Filter nulls → Deduplicate → Validate → Output",
        "icon": "shield-check",
        "tags": ["quality", "validation"],
    },
    # ── Enterprise templates (added May 17 2026, Phase 2C) ─────────────
    # Each codifies a real-world pattern the clarifying-questions flow
    # surfaces. Placeholders (`<your-...>`) make it obvious which fields
    # the user must fill in before the pipeline can run.
    "oracle_bip_to_sql_server": {
        "name": "Oracle BIP → SQL Server (with MERGE)",
        "description": "Fetch Oracle BIP report (XML) → flatten → clean → upsert via staging table + MERGE",
        "icon": "database",
        "tags": ["enterprise", "oracle", "sql-server", "merge"],
    },
    "sql_server_upsert": {
        "name": "SQL Server Upsert (Staging + MERGE)",
        "description": "Source → staging table → MERGE statement (insert new, update changed)",
        "icon": "git-merge",
        "tags": ["enterprise", "sql-server", "upsert", "merge"],
    },
    "scd2_dimension": {
        "name": "SCD2 Dimension Load",
        "description": "Source → SCD2 node (effective_from / effective_to / is_current) → DB sink — historical tracking per business key",
        "icon": "layers",
        "tags": ["enterprise", "scd2", "dimension", "warehouse"],
    },
    "cdc_incremental": {
        "name": "CDC Incremental Replication",
        "description": "CDC source (Debezium-style) → minimal transform → target sink, only changed rows",
        "icon": "refresh-cw",
        "tags": ["enterprise", "cdc", "incremental", "replication"],
    },
}


def create_from_template(template_key: str) -> Workflow | None:
    """Create a workflow from a template."""

    if template_key == "simple_etl":
        s1 = Step(type=StepType.SOURCE, label="Source",
                  params={"connector_type": "csv", "file_path": "orders.csv"},
                  position=NodePosition(x=0, y=100))
        s2 = Step(type=StepType.TRANSFORM, label="Transform",
                  params={"expression": "SELECT *, CURRENT_TIMESTAMP AS loaded_at FROM source_table"},
                  position=NodePosition(x=350, y=100))
        s3 = Step(type=StepType.DESTINATION, label="Destination",
                  params={"connector_type": "parquet", "file_path": "output/etl_result.parquet"},
                  position=NodePosition(x=700, y=100))

        return Workflow(
            name="Simple ETL Pipeline",
            description="Load CSV, transform, output to Parquet",
            steps=[s1, s2, s3],
            connections=[
                StepConnection(from_step=s1.id, to_step=s2.id),
                StepConnection(from_step=s2.id, to_step=s3.id),
            ],
        )

    elif template_key == "dedup_pipeline":
        s1 = Step(type=StepType.SOURCE, label="Source",
                  params={"connector_type": "csv", "file_path": "orders.csv"},
                  position=NodePosition(x=0, y=100))
        s2 = Step(type=StepType.DEDUPLICATE, label="Deduplicate",
                  params={"key": ["order_id"], "strategy": "keep_first"},
                  position=NodePosition(x=350, y=100))
        s3 = Step(type=StepType.DESTINATION, label="Destination",
                  params={"connector_type": "parquet", "file_path": "output/dedup_result.parquet"},
                  position=NodePosition(x=700, y=100))

        return Workflow(
            name="Deduplication Pipeline",
            description="Load data, remove duplicates, output clean data",
            steps=[s1, s2, s3],
            connections=[
                StepConnection(from_step=s1.id, to_step=s2.id),
                StepConnection(from_step=s2.id, to_step=s3.id),
            ],
        )

    elif template_key == "aggregation":
        s1 = Step(type=StepType.SOURCE, label="Source",
                  params={"connector_type": "csv", "file_path": "orders.csv"},
                  position=NodePosition(x=0, y=100))
        s2 = Step(type=StepType.FILTER, label="Filter Valid Rows",
                  params={"condition": "amount IS NOT NULL AND amount > 0"},
                  position=NodePosition(x=350, y=100))
        s3 = Step(type=StepType.AGGREGATE, label="Aggregate",
                  params={
                      "group_by": ["category"],
                      "functions": [
                          {"column": "amount", "function": "SUM", "alias": "total_amount"},
                          {"column": "*", "function": "COUNT", "alias": "count"},
                      ],
                  },
                  position=NodePosition(x=700, y=100))
        s4 = Step(type=StepType.DESTINATION, label="Destination",
                  params={"connector_type": "csv", "file_path": "output/aggregation_report.csv"},
                  position=NodePosition(x=1050, y=100))

        return Workflow(
            name="Aggregation Report",
            description="Load, filter, aggregate, and output report",
            steps=[s1, s2, s3, s4],
            connections=[
                StepConnection(from_step=s1.id, to_step=s2.id),
                StepConnection(from_step=s2.id, to_step=s3.id),
                StepConnection(from_step=s3.id, to_step=s4.id),
            ],
        )

    elif template_key == "data_quality":
        s1 = Step(type=StepType.SOURCE, label="Source",
                  params={"connector_type": "csv", "file_path": "orders.csv"},
                  position=NodePosition(x=0, y=100))
        s2 = Step(type=StepType.FILTER, label="Remove Nulls",
                  params={"condition": "order_id IS NOT NULL"},
                  position=NodePosition(x=350, y=100))
        s3 = Step(type=StepType.DEDUPLICATE, label="Deduplicate",
                  params={"key": ["order_id"], "strategy": "keep_first"},
                  position=NodePosition(x=700, y=100))
        s4 = Step(type=StepType.TRANSFORM, label="Validate & Tag",
                  params={"expression": "SELECT *, CASE WHEN order_id IS NOT NULL THEN 'valid' ELSE 'invalid' END AS quality_flag FROM source_table"},
                  position=NodePosition(x=1050, y=100))
        s5 = Step(type=StepType.DESTINATION, label="Destination",
                  params={"connector_type": "parquet", "file_path": "output/quality_result.parquet"},
                  position=NodePosition(x=1400, y=100))

        return Workflow(
            name="Data Quality Pipeline",
            description="Load, clean nulls, deduplicate, validate, output",
            steps=[s1, s2, s3, s4, s5],
            connections=[
                StepConnection(from_step=s1.id, to_step=s2.id),
                StepConnection(from_step=s2.id, to_step=s3.id),
                StepConnection(from_step=s3.id, to_step=s4.id),
                StepConnection(from_step=s4.id, to_step=s5.id),
            ],
        )

    # ── Enterprise templates (Phase 2C, May 17 2026) ────────────────────
    # These complement the clarifying-questions flow in clarify_draft.py:
    # when a user says "fetch Oracle BIP into SQL Server", they get the
    # questions card, answer it, and the resulting draft starts from one
    # of these hardened scaffolds instead of generic source → sink guesses.
    #
    # Placeholder fields use `<your-...>` syntax so it's obvious what the
    # user must fill in. The pipeline won't validate until those are
    # replaced — that's intentional (forces real config, prevents silent
    # placeholder runs against production targets).

    elif template_key == "oracle_bip_to_sql_server":
        # Oracle BIP report fetch (two-step REST: schedule + fetch result)
        # → XML flatten → optional cleaning → SQL Server staging + MERGE.
        s1 = Step(
            type=StepType.API_SOURCE,
            label="Oracle BIP Report",
            params={
                "url": "<your-oracle-bip-base-url>/xmlpserver/services/v2/ReportService",
                "method": "POST",
                "auth_type": "basic",  # change to oauth2 / cookie as needed
                "username": "<your-bip-username>",
                "password_secret_name": "<your-bip-password-secret>",
                "body_template": "<soap-envelope-or-rest-body-for-runReport>",
                "response_format": "xml",  # most common; can be csv / json
                "retry_max": 3,
                "retry_backoff_seconds": [2, 8, 30],
            },
            position=NodePosition(x=0, y=100),
        )
        s2 = Step(
            type=StepType.TRANSFORM,
            label="Flatten BIP XML",
            params={
                "expression": (
                    "-- BIP returns nested XML under <DATA_DS>/<G_*> rows. "
                    "Flatten to one row per record.\n"
                    "SELECT\n"
                    "  xpath_string(xml_payload, '/G_1/EMPLOYEE_ID') AS employee_id,\n"
                    "  xpath_string(xml_payload, '/G_1/FULL_NAME')   AS full_name,\n"
                    "  xpath_string(xml_payload, '/G_1/HIRE_DATE')   AS hire_date,\n"
                    "  -- add more fields per the BIP report data model\n"
                    "  CURRENT_TIMESTAMP AS extracted_at\n"
                    "FROM source_table"
                ),
            },
            position=NodePosition(x=350, y=100),
        )
        s3 = Step(
            type=StepType.DATA_QUALITY,
            label="Validate + DLQ Split",
            params={
                "rules": [
                    {"column": "employee_id", "rule": "not_null"},
                    {"column": "employee_id", "rule": "unique"},
                    {"column": "hire_date", "rule": "date_not_in_future"},
                ],
                "on_failure": "split_to_dlq",  # passing rows continue, failing → DLQ table
            },
            position=NodePosition(x=700, y=100),
        )
        s4 = Step(
            type=StepType.DB_SINK,
            label="Stage in SQL Server",
            params={
                "connector_type": "sql_server",
                "connection_name": "<your-sql-server-connection>",
                "schema": "stg",
                "table": "<your-staging-table>",  # e.g. stg_employees
                "write_mode": "truncate_load",  # staging is rebuilt each run
                "bulk_load": True,
                "batch_size": 5000,
            },
            position=NodePosition(x=1050, y=100),
        )
        s5 = Step(
            type=StepType.EXECUTE_SQL_TASK,
            label="MERGE into Target",
            params={
                "connection_name": "<your-sql-server-connection>",
                "sql": (
                    "-- Idempotent MERGE — insert new, update changed, no duplicates.\n"
                    "MERGE INTO dbo.<your-target-table> AS T\n"
                    "USING stg.<your-staging-table> AS S\n"
                    "  ON T.employee_id = S.employee_id\n"
                    "WHEN MATCHED AND (\n"
                    "  T.full_name <> S.full_name OR T.hire_date <> S.hire_date\n"
                    ") THEN UPDATE SET\n"
                    "  T.full_name = S.full_name,\n"
                    "  T.hire_date = S.hire_date,\n"
                    "  T.updated_at = SYSUTCDATETIME()\n"
                    "WHEN NOT MATCHED BY TARGET THEN INSERT (employee_id, full_name, hire_date, created_at)\n"
                    "  VALUES (S.employee_id, S.full_name, S.hire_date, SYSUTCDATETIME());"
                ),
            },
            position=NodePosition(x=1400, y=100),
        )
        return Workflow(
            name="Oracle BIP → SQL Server",
            description=(
                "Fetch Oracle BIP report (XML) → flatten → validate (DLQ split) → "
                "stage in SQL Server (truncate-load) → MERGE into target table. "
                "Replace every <your-...> placeholder before running."
            ),
            steps=[s1, s2, s3, s4, s5],
            connections=[
                StepConnection(from_step=s1.id, to_step=s2.id),
                StepConnection(from_step=s2.id, to_step=s3.id),
                StepConnection(from_step=s3.id, to_step=s4.id),
                StepConnection(from_step=s4.id, to_step=s5.id),
            ],
        )

    elif template_key == "sql_server_upsert":
        # Generic upsert pattern: source → staging → MERGE.
        # Use this whenever the target system needs row-level deduplication
        # by business key with insert-or-update semantics.
        s1 = Step(
            type=StepType.SOURCE,
            label="Source",
            params={"connector_type": "csv", "file_path": "<your-source-file>"},
            position=NodePosition(x=0, y=100),
        )
        s2 = Step(
            type=StepType.DB_SINK,
            label="Stage (truncate + load)",
            params={
                "connector_type": "sql_server",
                "connection_name": "<your-sql-server-connection>",
                "schema": "stg",
                "table": "<your-staging-table>",
                "write_mode": "truncate_load",
                "bulk_load": True,
                "batch_size": 10000,
            },
            position=NodePosition(x=350, y=100),
        )
        s3 = Step(
            type=StepType.EXECUTE_SQL_TASK,
            label="MERGE into target",
            params={
                "connection_name": "<your-sql-server-connection>",
                "sql": (
                    "-- Replace business_key + columns to match your target schema.\n"
                    "MERGE INTO dbo.<your-target-table> AS T\n"
                    "USING stg.<your-staging-table> AS S\n"
                    "  ON T.<business_key> = S.<business_key>\n"
                    "WHEN MATCHED THEN UPDATE SET\n"
                    "  T.<col1> = S.<col1>,\n"
                    "  T.<col2> = S.<col2>,\n"
                    "  T.updated_at = SYSUTCDATETIME()\n"
                    "WHEN NOT MATCHED BY TARGET THEN INSERT (<business_key>, <col1>, <col2>, created_at)\n"
                    "  VALUES (S.<business_key>, S.<col1>, S.<col2>, SYSUTCDATETIME());"
                ),
            },
            position=NodePosition(x=700, y=100),
        )
        return Workflow(
            name="SQL Server Upsert (Staging + MERGE)",
            description=(
                "Idempotent upsert via staging table + MERGE statement. Survives "
                "re-runs without duplicates. Replace placeholders before running."
            ),
            steps=[s1, s2, s3],
            connections=[
                StepConnection(from_step=s1.id, to_step=s2.id),
                StepConnection(from_step=s2.id, to_step=s3.id),
            ],
        )

    elif template_key == "scd2_dimension":
        # SCD2 — Slowly-Changing Dimension Type 2: tracks every historical
        # version per business key, with effective_from / effective_to /
        # is_current. The SCD2 node does the heavy lifting; this scaffolds
        # a complete pipeline around it.
        s1 = Step(
            type=StepType.SOURCE,
            label="Source",
            params={"connector_type": "csv", "file_path": "<your-source-file>"},
            position=NodePosition(x=0, y=100),
        )
        s2 = Step(
            type=StepType.SCD2,
            label="SCD2 Dimension Load",
            params={
                "business_key": ["<your-business-key-column>"],
                "tracked_columns": ["<col1>", "<col2>"],  # cols that trigger a new version
                "effective_from_col": "effective_from",
                "effective_to_col": "effective_to",
                "is_current_col": "is_current",
                "high_date": "9999-12-31",  # open-ended sentinel for current row
            },
            position=NodePosition(x=350, y=100),
        )
        s3 = Step(
            type=StepType.DB_SINK,
            label="Target Dimension Table",
            params={
                "connector_type": "sql_server",
                "connection_name": "<your-sql-server-connection>",
                "schema": "dim",
                "table": "<your-dimension-table>",  # e.g. dim_customer
                "write_mode": "merge",
                "bulk_load": True,
            },
            position=NodePosition(x=700, y=100),
        )
        return Workflow(
            name="SCD2 Dimension Load",
            description=(
                "Slowly-Changing Dimension Type 2 load — tracks every "
                "historical version per business key. Auto-populates "
                "effective_from / effective_to / is_current columns."
            ),
            steps=[s1, s2, s3],
            connections=[
                StepConnection(from_step=s1.id, to_step=s2.id),
                StepConnection(from_step=s2.id, to_step=s3.id),
            ],
        )

    elif template_key == "cdc_incremental":
        # CDC incremental replication — uses the CDC source which emits
        # only changed rows since the last checkpoint. Pair with an upsert
        # sink to make this idempotent.
        s1 = Step(
            type=StepType.CDC_SOURCE,
            label="CDC Source",
            params={
                "connector_type": "<your-cdc-connector>",  # debezium-postgres / sql-server-cdc
                "connection_name": "<your-source-db-connection>",
                "tables": ["<your-source-table>"],
                "format": "change_event",  # emits {op: c|u|d, before, after}
                "checkpoint_strategy": "last_lsn",
            },
            position=NodePosition(x=0, y=100),
        )
        s2 = Step(
            type=StepType.TRANSFORM,
            label="Normalize Change Events",
            params={
                "expression": (
                    "-- Pick the 'after' image for inserts/updates; 'before' for deletes.\n"
                    "-- Adds an `_op` column so downstream can handle deletes explicitly.\n"
                    "SELECT\n"
                    "  CASE WHEN op = 'd' THEN before ELSE after END AS row_data,\n"
                    "  op AS _op,\n"
                    "  ts_ms AS _change_ts\n"
                    "FROM source_table"
                ),
            },
            position=NodePosition(x=350, y=100),
        )
        s3 = Step(
            type=StepType.UPSERT,
            label="Upsert into Target",
            params={
                "connector_type": "<your-target-connector>",
                "connection_name": "<your-target-connection>",
                "table": "<your-target-table>",
                "key_columns": ["<business_key>"],
                "handle_deletes": "soft_delete",  # or "hard_delete" / "ignore"
                "deleted_at_column": "deleted_at",
            },
            position=NodePosition(x=700, y=100),
        )
        return Workflow(
            name="CDC Incremental Replication",
            description=(
                "Stream change events (inserts / updates / deletes) from a "
                "CDC source into a target table with idempotent upsert + "
                "configurable delete handling. Survives re-runs cleanly."
            ),
            steps=[s1, s2, s3],
            connections=[
                StepConnection(from_step=s1.id, to_step=s2.id),
                StepConnection(from_step=s2.id, to_step=s3.id),
            ],
        )

    return None
