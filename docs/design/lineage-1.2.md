# Lineage — 1.2 design

## What we shipped to reviewers as "shallow"

The 2026-06-07 reviewer audit said: *"Lineage is still shallow… needs
pipeline-level lineage, node-level lineage, column-level lineage for
transforms, output-to-consumer lineage, OpenLineage export later."*

That critique was directionally fair but factually imprecise. Three of
those five items already exist.

## What actually ships today

| Item | Status | Implementation |
|---|---|---|
| **Pipeline-level lineage** (workflow → workflow via shared sources/sinks) | ✓ | Implicit via Archeologist's duplicate-source / duplicate-pipeline detection |
| **Node-level lineage** (step → step within a workflow) | ✓ | `LineageStore.record_step()` + `record_edge()` |
| **Column-level lineage for transforms** (column → column across joins / renames / derived columns) | ✓ | `lineage_edges` table with `source_column` + `target_column` + `transform_type` + `expression`; auto-inferred by `LineageStore.build_from_workflow()` |
| **Auto-inference from workflow IR** | ✓ | `_infer_columns()` walks step params (renames, derived_columns, select_columns, aggregate functions) |
| **Trace single column upstream + downstream** | ✓ | `get_column_lineage(workflow_id, column_name)` |
| **React Flow render format** | ✓ | `get_graph()` returns `{nodes, edges}` shaped for the canvas |
| **Output-to-consumer lineage** ("which downstream Snowflake table reads from this output?") | ✗ | Nothing tracks consumption beyond the F-Pulse workspace |
| **OpenLineage export** (Marquez / Airflow / dbt compat) | ✗ | No emission of the standardized event format |
| **Runtime emission** (lineage events at execution time, not just at save time) | ✗ | `build_from_workflow()` runs on workflow save; nothing tied to actual run completion |

So the real gap is **three specific items**, not "everything." Saying
"lineage is shallow" miscommunicated the actual scope.

## Why those three matter

| Gap | Who feels it |
|---|---|
| Output-to-consumer lineage | Data eng teams trying to assess "if we change this F-Pulse pipeline's output schema, what downstream queries break?" |
| OpenLineage export | Teams already running Marquez / Datahub / Airflow's lineage UI who want F-Pulse pipelines to appear in the same graph |
| Runtime emission | Anyone debugging a specific failed run wants to see what lineage was actually produced **that run**, not just the design-time inference |

## Proposed architecture

### Track 1 — Runtime lineage events (foundation)

Today: `build_from_workflow(wf)` rebuilds the entire graph on workflow
save by inferring from IR. That's design-time lineage — it doesn't
know whether a specific run actually executed those steps.

Proposal: every step's `execute()` calls
`ctx.lineage.emit_step_run(run_id, step_id, columns_in, columns_out,
rows_in, rows_out, started_at, completed_at)`. A new `lineage_runs`
table records per-run lineage facts independently of the design-time
graph.

```sql
CREATE TABLE lineage_step_runs (
    id            TEXT PRIMARY KEY,
    workflow_id   TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    step_id       TEXT NOT NULL,
    columns_in    TEXT,    -- JSON list
    columns_out   TEXT,    -- JSON list
    rows_in       INTEGER,
    rows_out      INTEGER,
    started_at    REAL,
    completed_at  REAL
);
CREATE INDEX idx_lineage_step_runs_run  ON lineage_step_runs(run_id);
CREATE INDEX idx_lineage_step_runs_wf   ON lineage_step_runs(workflow_id);
```

Same shape as `lineage_nodes` / `lineage_edges` but keyed by `run_id`.
Lets the UI render a "this specific execution" view distinct from the
"design-time intent" view.

### Track 2 — OpenLineage export (interop)

OpenLineage is the de-facto standard event format for lineage between
data tools. Schema reference: https://openlineage.io/docs/spec/run-events

New module: `backend/fpulse/lineage/openlineage.py`

```python
def to_openlineage_run_event(
    *,
    workflow_id: str,
    run_id: str,
    step_id: str,
    event_type: Literal["START", "COMPLETE", "ABORT", "FAIL"],
    inputs: list[OpenLineageDataset],
    outputs: list[OpenLineageDataset],
    facets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produces an OpenLineage RunEvent JSON dict per the v0.x spec."""
```

Two export modes:

| Mode | Activated by | Behaviour |
|---|---|---|
| **Console / file** | `FPULSE_LINEAGE_EXPORT=file:///path/to/lineage.jsonl` | Append RunEvents to a JSONL file per run; useful for dev + audit |
| **HTTP POST** | `FPULSE_LINEAGE_OPENLINEAGE_URL=https://marquez.example.com/api/v1/lineage` | POST each RunEvent to a Marquez / DataHub endpoint |

Both are opt-in env-var configurable. Default = no emission.

### Track 3 — Output-to-consumer lineage

This is the hardest of the three because "consumer" is whatever lives
downstream of an F-Pulse output — could be another F-Pulse pipeline,
a Snowflake VIEW, a Tableau dashboard, a Python notebook. F-Pulse
doesn't own that downstream.

Three options ranked from cheap to expensive:

1. **Self-attestation API** — consumers POST `{output_id, consumer_id,
   consumer_type, last_read_at}` to `/api/lineage/consumers`. F-Pulse
   surfaces "this output is read by 7 known consumers; you'll
   impact them if you change the schema." Cheap to ship; relies on
   consumers being polite. Recommended for OSS 1.2.
2. **OpenLineage import** — if the downstream ALSO emits OpenLineage,
   F-Pulse can scrape a shared Marquez/DataHub for "who reads dataset
   X" queries. Free-ish if the customer already runs Marquez.
3. **Warehouse query log scrape** — F-Pulse reads Snowflake's
   ACCOUNT_USAGE.QUERY_HISTORY (or BigQuery's INFORMATION_SCHEMA.JOBS)
   to find SELECTs referencing F-Pulse-produced tables. Powerful,
   needs read access on the warehouse, customer-tier feature.

OSS ships #1. Plus ships #2 + #3 with the Cost Steward / Optimizer.

## Phased milestones

| Milestone | Scope | Effort |
|---|---|---|
| **L1** | Track 1: runtime lineage events table + `ctx.lineage.emit_step_run()` + wire into 3 node types (db_source, db_sink, transform) as the proof | 3-4 days |
| **L2** | Track 2: OpenLineage formatter + file/HTTP export modes + 1 worked example (POST to local Marquez via docker-compose) | 3-4 days |
| **L3** | Track 3 option 1: self-attestation API + UI panel "consumers of this output" + a doc on how to instrument downstreams | 4-5 days |
| **L4** (Plus) | Track 3 option 3: Snowflake query-log scraper that auto-discovers consumers | 1-2 weeks |

L1 → L3 is **~2 weeks total for OSS 1.2**. L4 is Plus territory.

## Open questions for human review

1. **OpenLineage version pin** — the spec is at 1.x but Marquez and
   DataHub still accept 0.x payloads. Do we emit 1.x (modern) or 0.x
   (broader interop)?
2. **Output ID stability** — what's the canonical ID for an F-Pulse
   output? Today the lineage uses `step_id`, but for cross-tool
   correlation we need a deterministic URI like
   `fpulse://workspace/<ws>/pipeline/<pl>/sink/<step>`. Lock the
   URI scheme before shipping L2.
3. **Privacy** — OpenLineage events include column names. For
   organizations where column names are themselves sensitive (e.g.
   `ssn_pii_2023`), do we redact? Probably yes, gated by a
   `FPULSE_LINEAGE_REDACT_COLUMN_NAMES` flag.
4. **Pull vs push for runtime events** — emit live (push to URL on
   every step), or batch (write to JSONL, ship periodically)? Push
   is closer to real-time but blocks on the downstream's
   availability. Batch is what production tracing usually does.
   Recommendation: batch with periodic flush (every 10s + on run
   complete), and a `--immediate` flag for debugging.

## What this design explicitly does NOT do

- **Replace the Steward layer.** Lineage is *what flows where*; the
  Steward is *what's wrong about how it flows*. The two surfaces compose.
- **Column-level lineage across F-Pulse → external systems.** Tracking
  that a SELECT from Snowflake renamed `customer_id` to `cust_id` is
  the downstream's lineage tool to solve, not ours.
- **Capture data values.** Lineage is metadata. We do NOT log row
  contents anywhere.

## Decision log (what I considered and rejected)

| Considered | Rejected because |
|---|---|
| Rip out `LineageStore` and rewrite from scratch around OpenLineage | The existing code works; the gap is *emission* + *interop*, not the storage shape. Refactor for refactor's sake is the wrong overnight move. |
| Build a custom lineage format then convert to OpenLineage on export | Two formats = two truth sources to drift. Use OpenLineage as the source of truth in flight; persist it to our tables on receive. |
| Use Marquez as the storage backend (replace our SQLite tables) | Plus tier could; OSS users shouldn't need to run a separate Java service for lineage to work. |
| Auto-discover downstream consumers via DNS / SSH magic | Magic = bugs. Self-attestation API is honest about the relationship. |
