# Code generation training — F-Pulse-specific code prompts and answers

Each entry is a user prompt + the correct code sample targeting F-Pulse's
actual APIs, node types, and conventions. The Copilot uses these to
generate accurate code when the user asks for help.

**Rules:**
- DuckDB SQL (not PySpark, not Spark SQL, not Hive)
- F-Pulse expression engine (`$json`, `$now`, `$vars`, `$('Node')`)
- F0.1 manifest v2 JSON (not v1)
- BaseNode subclass pattern from `backend/fpulse/nodes/base.py`
- BulkLoaderProtocol from `backend/fpulse/engine/bulk_load/types.py`
- EvalCase from `backend/fpulse/eval/cases.py`

---

## 1. DuckDB SQL: daily aggregation in a Transform node

**User prompt:** "Write a Transform node SQL that aggregates daily
revenue by region for the last 30 days."

**Answer:**

```sql
SELECT
    CAST(order_date AS DATE)        AS order_day,
    region,
    SUM(amount)                     AS total_revenue,
    COUNT(*)                        AS order_count,
    AVG(amount)                     AS avg_order_value
FROM input
WHERE order_date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY CAST(order_date AS DATE), region
ORDER BY order_day DESC, total_revenue DESC
```

**Context:** The Transform node receives its upstream data as a virtual
table named `input`. DuckDB SQL — supports `INTERVAL`, `CAST`, and
standard aggregate functions. No PySpark, no Spark context.

---

## 2. DuckDB SQL: window function — running total

**User prompt:** "Add a running total of revenue partitioned by customer,
ordered by order date."

**Answer:**

```sql
SELECT
    *,
    SUM(amount) OVER (
        PARTITION BY customer_id
        ORDER BY order_date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS running_total
FROM input
```

---

## 3. DuckDB SQL: dedup with row_number

**User prompt:** "Keep only the latest row per customer_id based on
updated_at."

**Answer:**

```sql
WITH ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY customer_id
               ORDER BY updated_at DESC
           ) AS rn
    FROM input
)
SELECT * EXCLUDE (rn)
FROM ranked
WHERE rn = 1
```

**Context:** DuckDB supports the `EXCLUDE` clause to drop columns from
`SELECT *`. The Deduplicate node does this natively, but a Transform
node with this SQL gives full control over the dedup logic.

---

## 4. DuckDB SQL: JSON extraction

**User prompt:** "Extract nested fields from a JSON column called
`payload` — I need `payload.customer.name` and `payload.items[0].sku`."

**Answer:**

```sql
SELECT
    payload->>'$.customer.name'     AS customer_name,
    payload->>'$.items[0].sku'      AS first_item_sku,
    CAST(payload->>'$.total' AS DOUBLE) AS total_amount
FROM input
WHERE payload IS NOT NULL
```

**Context:** DuckDB uses `->>'$.path'` for JSON path extraction
returning VARCHAR. Cast to the target type explicitly.

---

## 5. Expression engine: common patterns

**User prompt:** "Show me the expression engine syntax for common
operations."

**Answer:**

```
# Current row field access
$json.amount * 1.18

# String manipulation on current row
$json.email.toLowerCase()

# Current timestamp (ISO 8601 UTC)
$now

# Today's date only
$today

# Run UUID (stable across all steps in one execution)
$run_id

# Reference another node's output
$('Source CSV').row_count

# Workspace variable
$vars.S3_BUCKET

# Pipeline parameter (overridable at runtime)
${param.TARGET_SCHEMA}

# Template literal in a string param
'orders_{{ $today }}.csv'

# Template literal with variable
'INSERT INTO {{ $vars.TABLE_NAME }} VALUES ...'
```

**Where expressions work:** Any node param that accepts `string` type.

**Where they DON'T work:**
- Pipeline IR structural fields (node IDs, connection IDs)
- Connection credentials (resolved by ID, never expression-substituted)
- Schedule cron expressions (static cron syntax)

---

## 6. F0.1 manifest v2: minimal connector with one stream

**User prompt:** "Write a minimal F0.1 v2 manifest for a hypothetical
'acme-crm' connector with a Contacts stream."

**Answer:**

```json
{
  "manifest_version": "2.0",
  "connector_id": "acme-crm",
  "display_name": "Acme CRM",
  "vendor": "Acme Corp",
  "category": "crm",
  "description": "Pull contacts from the Acme CRM API.",
  "auth": {
    "type": "api_key",
    "header": "X-Api-Key",
    "env_var": "ACME_CRM_API_KEY"
  },
  "base_url": "https://api.acmecrm.example.com/v1",
  "retry": {
    "max_attempts": 3,
    "backoff_factor": 2.0,
    "retryable_status_codes": [429, 500, 502, 503]
  },
  "streams": {
    "contacts": {
      "path": "/contacts",
      "method": "GET",
      "primary_key": ["id"],
      "incremental_field": "updated_at",
      "pagination": {
        "type": "cursor",
        "cursor_field": "next_cursor",
        "cursor_param": "cursor",
        "page_size": 100,
        "page_size_param": "limit"
      },
      "schema": {
        "type": "object",
        "properties": {
          "id": {"type": "string"},
          "email": {"type": "string"},
          "name": {"type": "string"},
          "company": {"type": "string"},
          "updated_at": {"type": "string", "format": "date-time"}
        },
        "required": ["id", "email"]
      },
      "response_path": "$.data"
    }
  }
}
```

**Validate:** `python -m fpulse.connectors.certify acme-crm`

**Depth score this achieves:** 5 (schema + pagination + incremental +
primary key). To reach certified depth-5, also add fixture files under
`manifests/acme-crm/fixtures/`.

---

## 7. F0.1 manifest: fixture file shapes

**User prompt:** "What fixture files do I need for a depth-5 connector?"

**Answer:**

Five fixture types under `manifests/<connector>/fixtures/`:

```
manifests/acme-crm/fixtures/
  contacts_list.json       # sample response from the list endpoint
  contacts_single.json     # sample response for a single record
  contacts_incremental.json # response with incremental cursor
  contacts_schema.json     # expected JSON Schema for validation
  contacts_error.json      # sample error response (4xx/5xx)
```

Each fixture is a raw HTTP response body. The certify validator checks:
1. `contacts_list.json` parses correctly via `response_path`
2. `contacts_single.json` matches the declared schema
3. `contacts_incremental.json` contains the `incremental_field`
4. `contacts_schema.json` validates against the stream's JSON Schema
5. `contacts_error.json` contains a retryable status code from the
   `retry.retryable_status_codes` list

---

## 8. Custom node: subclass BaseNode

**User prompt:** "Write a custom node that uppercases all VARCHAR
columns."

**Answer:**

```python
from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


@register(StepType.TRANSFORM)  # or a custom StepType if adding to the enum
class UppercaseNode(BaseNode):
    """Uppercase all VARCHAR columns in the input relation."""

    display_name = "Uppercase All Text"
    category = "transform"
    description = "Converts every VARCHAR column to uppercase."

    def execute(self, ctx: ExecutionContext):
        upstream = ctx.get_input(self.params.get("input_node", ""))
        cols = upstream.columns
        types = upstream.types

        projections = []
        for col, dtype in zip(cols, types):
            if "VARCHAR" in str(dtype).upper():
                projections.append(f'UPPER("{col}") AS "{col}"')
            else:
                projections.append(f'"{col}"')

        sql = f"SELECT {', '.join(projections)} FROM upstream"
        return ctx.conn.sql(sql)

    @staticmethod
    def default_params():
        return {"input_node": ""}

    @staticmethod
    def param_schema():
        return [
            {
                "name": "input_node",
                "type": "string",
                "label": "Input node",
                "description": "Display label of the upstream node.",
            }
        ]
```

**Key points:**
- Subclass `BaseNode` from `fpulse.nodes.base`
- Decorate with `@register(StepType.X)` from `fpulse.nodes.registry`
- `execute()` returns a `DuckDBPyRelation`
- Access upstream data via `ctx.get_input(label)`
- Run SQL against `ctx.conn` (a DuckDB connection)

---

## 9. Bulk-load dialect: implement BulkLoaderProtocol

**User prompt:** "Write a skeleton bulk-load dialect for MySQL."

**Answer:**

```python
"""MySQL bulk-load via LOAD DATA LOCAL INFILE.

Optional dependency: mysql-connector-python.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from typing import Any

from ..registry import register as register_plugin
from ..types import BulkLoadRequest, BulkLoadResult, BulkLoaderProtocol

logger = logging.getLogger(__name__)


def _try_import():
    try:
        import mysql.connector  # type: ignore[import-not-found]
        return mysql.connector
    except ImportError:
        return None


class MySQLBulkLoader:
    """LOAD DATA LOCAL INFILE for MySQL."""

    dialect = "mysql"
    method = "LOAD DATA LOCAL INFILE"

    def is_available(self) -> bool:
        return _try_import() is not None

    def load(self, request: BulkLoadRequest) -> BulkLoadResult:
        mysql_mod = _try_import()
        if mysql_mod is None:
            from ..types import BulkLoaderNotAvailable
            raise BulkLoaderNotAvailable(
                "mysql dialect requires mysql-connector-python"
            )

        start = time.monotonic()

        # Export DuckDB relation to CSV in memory
        buf = io.StringIO()
        writer = csv.writer(buf)
        columns = request.columns or request.relation.columns
        for row in request.relation.fetchall():
            writer.writerow(row)
        csv_data = buf.getvalue().encode("utf-8")

        # Connect and load
        cfg = request.config
        conn = mysql_mod.connect(
            host=cfg["host"],
            port=cfg.get("port", 3306),
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            allow_local_infile=True,
        )
        try:
            cursor = conn.cursor()
            table = f"{request.schema_name}.{request.table}"

            if request.mode == "truncate":
                cursor.execute(f"TRUNCATE TABLE {table}")

            # LOAD DATA from the in-memory CSV
            cursor.execute(
                f"LOAD DATA LOCAL INFILE '/dev/stdin' "
                f"INTO TABLE {table} "
                f"FIELDS TERMINATED BY ',' "
                f"ENCLOSED BY '\"' "
                f"LINES TERMINATED BY '\\n' "
                f"({', '.join(columns)})",
                data=csv_data,
            )
            rows_loaded = cursor.rowcount
            conn.commit()
        finally:
            conn.close()

        elapsed = int((time.monotonic() - start) * 1000)
        return BulkLoadResult(
            rows_loaded=rows_loaded,
            duration_ms=elapsed,
            dialect=self.dialect,
            method=self.method,
            bytes_written=len(csv_data),
        )


# Register at module bottom — triggers on import
register_plugin(MySQLBulkLoader())
```

**Key points:**
- Implement `BulkLoaderProtocol` (dialect, method, is_available, load)
- Guard optional driver import inside `is_available()` and `load()`
- Call `register(YourPlugin())` at module bottom
- Add the import to `dialects/__init__.py`
- Modes: create, append, truncate, merge

---

## 10. Eval harness: add a new test case

**User prompt:** "Add an eval case that tests whether the agent picks
the right tool for 'show me my connections'."

**Answer:**

Append to `AGENT_TOOL_CASES` in
`backend/fpulse/eval/cases.py`:

```python
EvalCase(
    category="agent_tools",
    name="list_connections_intent",
    prompt="Show me my connections",
    expected={"tools_called": ["inspect_connections"]},
),
```

**Available categories:**
- `planner_intent` — natural-language → pipeline intent
- `sql_helper` — describe-the-transform → DuckDB SQL
- `agent_tools` — agent picks the right tool
- `agent_safety` — agent refuses malicious requests
- `sanitization` — PII/credentials redacted before LLM
- `gate1_core_etl` — code-presence probes

**Run:** `python -m fpulse.eval.run`
**Results:** `data/eval/latest.json` + Trust page pass rate.

---

## 11. DuckDB SQL: pivot monthly revenue by product

**User prompt:** "Pivot monthly revenue — rows are months, columns are
product categories."

**Answer:**

```sql
PIVOT (
    SELECT
        DATE_TRUNC('month', order_date) AS month,
        product_category,
        SUM(amount) AS revenue
    FROM input
    GROUP BY month, product_category
)
ON product_category
USING SUM(revenue)
ORDER BY month
```

**Context:** DuckDB supports native `PIVOT` syntax. Alternatively use
`crosstab`-style conditional aggregation:

```sql
SELECT
    DATE_TRUNC('month', order_date) AS month,
    SUM(CASE WHEN product_category = 'Electronics' THEN amount ELSE 0 END)
        AS electronics,
    SUM(CASE WHEN product_category = 'Clothing' THEN amount ELSE 0 END)
        AS clothing
FROM input
GROUP BY month
ORDER BY month
```

---

## 12. DuckDB SQL: flatten nested JSON array

**User prompt:** "Explode a JSON array column `items` into one row per
item."

**Answer:**

```sql
SELECT
    order_id,
    UNNEST(from_json(items, '["json"]')) AS item
FROM input
```

Or if `items` is already a `LIST` type:

```sql
SELECT
    order_id,
    UNNEST(items) AS item
FROM input
```

**Context:** DuckDB `UNNEST` explodes arrays into rows. The
Flatten/Explode node does this visually, but a Transform node with
UNNEST gives SQL-level control.

---

## 13. Certify CLI: validate and migrate a connector

**User prompt:** "How do I validate my connector manifest and migrate
from v1 to v2?"

**Answer:**

```bash
# Validate a v2 manifest (reports depth score + specific errors)
python -m fpulse.connectors.certify acme-crm

# Validate ALL manifests at once
python -m fpulse.connectors.certify --all

# Migrate a v1 manifest to v2 skeleton
python -m fpulse.connectors.certify --migrate acme-crm
```

**Exit codes:**
- 0 — pass (all validations green)
- 1 — validation errors found
- 2 — connector not found or bad arguments

**What the validator checks:**
- Manifest schema compliance (v2 required fields)
- Auth section validity
- Retry codes are valid HTTP status codes
- Per-stream: primary key declared, incremental field present,
  pagination configured, JSON Schema valid, no circular `$ref`
  in schema

---

## 14. DuckDB SQL: data quality check with CASE

**User prompt:** "Flag rows where email is null or amount is negative,
but keep all rows in the output."

**Answer:**

```sql
SELECT
    *,
    CASE
        WHEN email IS NULL THEN 'missing_email'
        WHEN amount < 0    THEN 'negative_amount'
        ELSE 'valid'
    END AS quality_flag
FROM input
```

**Context:** For drop/DLQ behavior, use the Data Quality node instead
of SQL. But for flagging without dropping, a Transform node with a
CASE expression is simpler.

---

## 15. Expression engine: dynamic file naming

**User prompt:** "Name my output file with today's date and the
pipeline run ID."

**Answer:**

In the CSV Sink / JSON Sink node's `file_path` parameter:

```
exports/orders_{{ $today }}_{{ $run_id }}.csv
```

This resolves at runtime to something like:
`exports/orders_2026-05-04_a1b2c3d4-e5f6-7890-abcd-ef1234567890.csv`

**Template literal syntax:** wrap expressions in `{{ }}` inside any
string-type node parameter.
