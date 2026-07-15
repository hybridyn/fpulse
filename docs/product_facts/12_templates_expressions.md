# F-Pulse templates and expression engine

Per `edition-matrix.md` line 28 (expression engine), line 30-31 (templates).

## Pipeline templates

OSS Free ships **20 pipeline templates** covering the common starter shapes:

- **Simple ETL** — CSV Source → Transform → CSV Sink
- **API to file** — REST/HTTP API → Schema Mapper → JSON Sink
- **Database replication** — Database Source → Bulk Loader (Postgres or Snowflake)
- **Daily aggregation** — schedule + Source + Aggregate + Database Sink
- **Dedup + upsert** — Source + Deduplicate + Upsert
- **Data quality + DLQ** — Source + Data Quality (DLQ split mode) + two Sinks
- **Profile a source** — Source + Data Profile + JSON Sink (one row per column)
- **SCD Type 2 dimension** — Source + SCD Type 2 + Bulk Loader (merge mode)
- **API ingest with pagination** — REST connector configured with pagination + Schema Mapper + Sink
- **Slack alert on failure** — pipeline + Alert rule (ON_FAILURE → Slack channel)

Templates live in `data/templates/` and `frontend/src/templates/`. Open via Pipelines page → "+ New from template".

**F-Pulse+** adds:
- **Private template marketplace** — workspace-scoped templates shared across team members
- **Template parameter discovery** — auto-detect parameters in saved pipelines for templating

## Expression engine

OSS + Plus both ship the same expression engine. Available in any node param that accepts `string` type.

### Top-level expressions

`$json` — the current row's JSON object. Use in Transform / Filter / Schema Mapper.
- Example: `$json.amount * 1.18`
- Example: `$json.email.toLowerCase()`

`$now` — current timestamp (ISO 8601, UTC).
- Example: as a default value in a sink config

`$today` — current date (ISO 8601 date only).

`$run_time` — when the run started (stable across all steps in one execution).

`$run_id` — the run UUID.

### Node references

`$('Node Name')` — refers to another node by its display label. Returns the node's last output as JSON.
- Example: `$('Source CSV').rows[0].email` reads the first row's email from the upstream Source CSV node.

`$('Node Name').row_count` — total rows the upstream node emitted.

### Workspace variables

`$vars.NAME` — references a workspace variable by name. Variables are scoped `global` (workspace-wide) or `pipeline` (single pipeline).
- Example: `$vars.S3_BUCKET` for an environment-specific bucket name.

### Parameters (Plus-leaning, but available in OSS)

`${param.NAME}` — references a pipeline parameter. Parameters are pipeline-level inputs that can be overridden at run time (UI or API).
- Set in Pipeline → Settings → Parameters
- Override at run via `POST /api/execute/workflow/{id}` with `{"parameter_values": {"NAME": "value"}}`

### Template literals

Inside a string param, wrap expressions in `{{ }}`:
- `'orders_{{ $today }}.csv'` resolves to `'orders_2026-05-04.csv'`.
- `'INSERT INTO {{ $vars.TABLE_NAME }} VALUES ...'`

### Where expressions DON'T work

- The pipeline IR's structural fields (node IDs, connection IDs) are static — not expression-resolved.
- Connection credentials are NEVER expression-substituted; they're resolved by ID against the Credentials page.
- Schedule cron expressions are static — they use the underlying cron syntax, not `$now`.

## Anti-patterns

- ❌ Telling a user "use Jinja templates" — F-Pulse uses its own expression engine, not Jinja. Syntax differences matter (`$json` vs `{{ json }}`).
- ❌ "Reference the pipeline by ID `${pipeline.id}`" — there's no `${pipeline}` namespace. Use `$run_id` for the run, and pipelines reference each other by registered name in `ExecutePipeline` nodes.
- ❌ "Use environment variables in expressions" — env vars are resolved by the BACKEND at startup (`FPULSE_*`). To use a value from an env var, set it as a workspace variable first, then reference `$vars.NAME` in the pipeline.
