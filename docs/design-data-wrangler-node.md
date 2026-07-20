# DESIGN — Data Wrangler Node (Stepwise Visible Transform)

**Status:** v1 design — locked 2026-05-17
**Scope owner:** F-Pulse OSS
**Effort:** ~10 dev-days (backend ~3, frontend ~5, tests + polish ~2)

---

## 1. Summary

The Data Wrangler Node is a single canvas node that hosts an **ordered list of small transformation sub-steps** with **per-step preview** and **inline schema evolution**. It collapses what is today a long linear chain of canvas nodes (Rename → Cast → Filter → Derive → Group) into one tile, while keeping each sub-step individually visible, reorderable, and inspectable.

The point is not to add operations F-Pulse can already do — it can do all of them. The point is **authoring UX**: a "wash my CSV" pipeline that today takes 6 canvas nodes should take one Data Wrangler Node with 6 visible sub-steps.

## 2. Goals

- **G1.** One node hosts an ordered list of up to ~20 sub-steps without canvas clutter.
- **G2.** Per-step preview: row count delta, sample rows after each sub-step, columns added/removed/retyped.
- **G3.** Generated SQL visible at any time (transparency for power users; debugging path).
- **G4.** Drag-reorder, disable, duplicate, delete sub-steps.
- **G5.** v1 ships with **6 sub-step ops** — see §5.
- **G6.** Convert wrangler → N canvas nodes (escape hatch); no nesting.

## 3. Non-goals

- **NG1.** No expression-language equivalent (no custom DSL parser). Every sub-step compiles to SQL via templated fragments.
- **NG2.** No nested wranglers. A Data Wrangler cannot contain a Data Wrangler.
- **NG3.** No AI-generated authoring in v1. Adding sub-steps via natural language is v2.
- **NG4.** No reusable / shared wrangler library. Each wrangler is local to one node's `params`.
- **NG5.** No spreadsheet-style cell editing. The grid is read-only preview, not authoring.
- **NG6.** No live-as-you-type preview. Preview re-runs on explicit step add / edit / reorder / toggle.
- **NG7.** No per-sub-step error routing / DLQ. Wrangler-level failures fail the whole node (DLQ remains a separate `DATA_QUALITY` node).
- **NG8.** No connector-aware sub-steps. Sub-steps operate on already-loaded relations only.

## 4. UX

### 4.1 Canvas

The Data Wrangler Node looks like any other F-Pulse node on the canvas (single tile, one input port, one output port, optional badge with the count of sub-steps — e.g. `Data Wrangler · 5 steps`).

### 4.2 Config panel

When opened, the ConfigPanel renders a `DataWranglerConfig` component with three regions:

```
┌─────────────────────────────────────────────────────────────────┐
│ Data Wrangler                                   [+ Add step ▾]  │
├──────────────────┬──────────────────────────────────────────────┤
│ ① Rename cols    │ Step config: Rename                          │
│ ② Filter         │  rename_map:                                 │
│ ③ Cast dates     │    cust_id   → customer_id                   │
│ ④ Derive month   │    ord_dt    → order_date                    │
│ ⑤ Group by cust  │                                              │
│                  │                                              │
│ [drag to reorder]│                                              │
├──────────────────┴──────────────────────────────────────────────┤
│ Preview (after step ②):  120 rows → 95 rows                     │
│ Schema delta:  + (nothing)   − (nothing)   ~ (nothing)          │
│ [grid with first 50 rows of post-step-② sample]                 │
└─────────────────────────────────────────────────────────────────┘
[ Run preview ]  [ Open generated SQL ]  [ Convert to canvas nodes ]
```

**Left column** — ordered step list. Each item: index, op label, one-line summary (auto-derived from params, e.g. `Filter: amount > 100`), disable toggle, delete, duplicate. Drag-handle on left for reorder (v2; v1 uses up/down arrows).

**Right column** — config form for the selected sub-step. Reuses existing rule-builder UX from FilterConfig / AggregateConfig where applicable.

**Bottom strip** — preview after the *currently selected* sub-step. Shows row-count delta, schema delta (`+col`, `-col`, `~type→type`), and a read-only data grid of the capped sample. Refreshes on "Run preview" click (not auto).

### 4.3 Schema evolution display

For each sub-step, the bottom strip shows column changes vs. the prior sub-step's output schema:

```
+ fiscal_year (INTEGER)              # added
- raw_date                           # dropped
~ amount  VARCHAR → DECIMAL(18,2)    # retyped
```

When no schema changes, render `Schema delta: no changes`.

### 4.4 "Open Generated SQL"

Opens a read-only panel with the SQL that the entire wrangler (or the wrangler up to the selected step) compiles to. Power users can copy this and paste it into a `TRANSFORM` node if they ever want to break out of the wrangler.

## 5. Sub-step DSL (v1: 6 ops)

The wrangler is stored as `params.steps: list[SubStep]` where each `SubStep` is a JSON object:

```json
{
  "op": "filter",
  "id": "s_x7k9",
  "enabled": true,
  "label": "Filter active",
  "config": { /* op-specific */ }
}
```

### 5.1 `filter` — drop rows

```json
{ "op": "filter",
  "config": { "mode": "rules" | "expression",
              "rules":     [ { "column": "...", "op": "=", "value": "..." } ],
              "combinator": "AND" | "OR",
              "expression": "amount > 100 AND status = 'active'" } }
```
Compiles to: `WHERE <condition>`. Reuses `filter_node._rules_to_condition`.

### 5.2 `select` — keep only N columns

```json
{ "op": "select",
  "config": { "columns": ["customer_id", "order_date", "amount"] } }
```
Compiles to: `SELECT "customer_id", "order_date", "amount"`.

### 5.3 `rename` — rename columns

```json
{ "op": "rename",
  "config": { "rename_map": { "cust_id": "customer_id", "ord_dt": "order_date" } } }
```
Compiles to: `SELECT * RENAME ("cust_id" AS "customer_id", "ord_dt" AS "order_date") FROM ...`.

### 5.4 `cast` — change column types

```json
{ "op": "cast",
  "config": { "casts": [ { "column": "amount", "to_type": "DECIMAL(18,2)" },
                         { "column": "order_date", "to_type": "DATE" } ] } }
```
Compiles to: `SELECT * REPLACE (CAST("amount" AS DECIMAL(18,2)) AS "amount", CAST("order_date" AS DATE) AS "order_date")`.

Allowed `to_type` values: `INTEGER | BIGINT | DOUBLE | DECIMAL(p,s) | VARCHAR | BOOLEAN | DATE | TIMESTAMP`. Validated server-side.

### 5.5 `derive` — compute new column(s)

```json
{ "op": "derive",
  "config": { "derived": [ { "name": "fiscal_year",
                             "expression": "year(order_date) + (CASE WHEN month(order_date) >= 4 THEN 1 ELSE 0 END)" },
                           { "name": "domain",
                             "expression": "split_part(email, '@', 2)" } ] } }
```
Compiles to: `SELECT *, <expr1> AS "<name1>", <expr2> AS "<name2>"`.

### 5.6 `group_by` — aggregate

```json
{ "op": "group_by",
  "config": { "keys": ["customer_id", "fiscal_year"],
              "aggregations": [
                { "func": "SUM",   "column": "amount",  "alias": "total_amount" },
                { "func": "COUNT", "column": "*",       "alias": "n_orders" }
              ] } }
```
Compiles to: `SELECT key1, key2, SUM("amount") AS "total_amount", COUNT(*) AS "n_orders" FROM <prev> GROUP BY key1, key2`.

Allowed `func` values: `SUM | COUNT | AVG | MIN | MAX | COUNT_DISTINCT`.

## 6. Backend architecture — dual execution model

### 6.1 Edit-time preview (sample materialize)

When the frontend calls `POST /api/workflows/{wid}/nodes/{nid}/data-wrangler/preview` with `{ "steps": [...], "sample_rows": 100 }`:

1. Resolve the upstream input relation for this Data Wrangler node (via the executor's existing context).
2. Cap to N rows (default 100; max 1000 — guardrail).
3. Register that capped sample as `__wrangler_input` in DuckDB.
4. For each enabled sub-step `i ∈ [0, upto_step_index]`:
   - Compile the sub-step's SQL fragment.
   - Wrap as a logical subquery aliased `_w{i}`.
   - Capture `{ columns, types, row_count, sample_data }` from the running SQL.
5. Return `{ steps: [{ index, columns, types, row_count, schema_delta, sample_data }, ...], generated_sql: "..." }`.

This is bounded — capped sample × ~20 sub-steps × cheap SQL fragments → millisecond latency. No OOM risk because data volume is bounded to the cap.

### 6.2 Run-time execution (compile to single SQL)

When the Data Wrangler Node is executed as part of a real pipeline run (full data, not preview):

1. Build the full SQL by chaining sub-step compilations into one expression:
   ```sql
   SELECT key1, SUM(amount) AS total
   FROM (
     SELECT *, year(order_date) AS fiscal_year
     FROM (
       SELECT * REPLACE (CAST(amount AS DECIMAL(18,2)) AS amount)
       FROM (
         SELECT *, "cust_id" AS customer_id
         FROM __wrangler_input WHERE status = 'active'
       )
     )
   )
   GROUP BY key1
   ```
2. Submit as **one SQL query** to DuckDB. The optimizer fuses the layered subselects.
3. Return the resulting `DuckDBPyRelation`.

No temp views are materialized at runtime. The wrangler is logically one chain. DuckDB handles vectorization end-to-end.

### 6.3 The compiler

`fpulse.nodes.data_wrangler.compile_wrangle(steps: list[dict], input_table: str) -> str`

Returns a single `SELECT` statement. Implementation: walk the step list, generating a SQL fragment per sub-step that wraps the previous fragment as a subquery aliased `_w{i}`. Disabled sub-steps are skipped. Empty step list returns `SELECT * FROM {input_table}`.

### 6.4 Identifier quoting & safety

All column references go through a `_q(col)` helper that double-quotes and escapes embedded quotes (`"` → `""`). Expressions in `derive` and `filter.expression` mode are passed through verbatim (these are SQL expressions by contract — same as the existing `TRANSFORM` node) but **never** wrap user-provided text in a way that would allow it to escape its position in the generated query.

This is consistent with the existing `TRANSFORM` node's contract: the user is writing SQL, and we trust the SQL they wrote.

## 7. Frontend architecture

### 7.1 Files

- `frontend/src/components/data-wrangler/DataWranglerConfig.tsx` — single file holding `DataWranglerConfig` component plus step list, step config dispatcher, per-op forms, preview strip, and generated-SQL panel.
- `frontend/src/components/ConfigPanel.tsx` — routes `stepType === 'data_wrangler'` to `<DataWranglerConfig />`.
- `frontend/src/components/ModulesPanel.tsx` — palette entry `{ type: 'data_wrangler', label: 'Data Wrangler' }`.
- `frontend/src/components/nodes/FPulseNode.tsx` — `data_wrangler` icon (six stacked horizontal lines) + emerald gradient + param-preview label `"3 of 5 steps"`.

### 7.2 State shape

```ts
interface SubStep {
  id: string;
  op: 'filter' | 'select' | 'rename' | 'cast' | 'derive' | 'group_by';
  enabled: boolean;
  label?: string;
  config: Record<string, unknown>;  // op-specific
}

interface DataWranglerParams {
  steps: SubStep[];
  selectedStepId?: string;          // UI-only — last-focused sub-step
}
```

Selection (`selectedStepId`) is currently UI state only; persisting it across config-panel open/close is a v2 polish item.

### 7.3 Reuse

`FilterForm` shares the SQL expression model with the standalone `FilterConfig`. The rules-mode builder is not reused inside the Wrangler in v1 (kept simple — SQL expression box only). v2 may lift the existing `RuleRow` component into `frontend/src/components/recipe/ops/`.

## 8. Escape hatches

### 8.1 "Convert to canvas nodes" (v2)

A button at the bottom of `DataWranglerConfig` that:
1. For each enabled sub-step, creates a canvas node of the equivalent existing StepType (`filter` → `FILTER`, `select`/`rename` → `RENAME`, `cast` → `TYPECAST`, `derive` → `DERIVED_COLUMN`, `group_by` → `AGGREGATE`).
2. Wires them in series, slotted in place of the original Data Wrangler node.
3. Removes the Data Wrangler node.

No round-trip. One-way operation. The user can undo via the canvas undo stack. (Deferred from v1.)

### 8.2 "Open generated SQL"

Renders the compiled SQL from §6.3 in a read-only panel. Copyable. If the user wants to fully escape the wrangler abstraction, they can paste it into a `TRANSFORM` node and delete the Data Wrangler.

## 9. v2 backlog (locked out of v1)

Tracked here so they don't leak into v1 scope:

- `regex_split` / `regex_extract` sub-ops.
- `conditional_case` sub-op (CASE WHEN with row counts per branch).
- `dedup` sub-op (exact only — fuzzy dedup is a v3 separate-node story).
- `lookup` sub-op (join with reference table).
- AI-generated sub-step authoring ("add a filter for active customers").
- Reusable / shared wranglers (templates).
- "Promote sub-step to canvas node" (single-step extraction, not whole wrangler).
- Drag-drop reorder (v1 uses up/down arrow buttons).
- "Convert to canvas nodes" escape hatch.

## 10. Risks

- **R1. Schema drift between preview and runtime.** Preview uses a 100-row sample; runtime hits the full dataset. A `cast` sub-step that succeeds on the sample may fail on full data (e.g. cell value `"N/A"` not in sample). Mitigation: surface a clear warning in the preview ("This is a 100-row sample — full-run results may differ"). v2 may add `cast` failure routing.
- **R2. Large wrangler (>20 steps) renders slowly in config panel.** Mitigation: virtualize the step list at >50 steps.
- **R3. Generated SQL is hard to read for deeply nested wranglers.** Mitigation: emit with consistent indentation; the user mostly wants to copy it, not edit.
- **R4. Users build complex SQL inside `derive.expression` that the wrangler abstraction can't reason about (schema delta won't catch a column drop done via REPLACE).** Accepted: the contract says `derive` adds columns; if the user does something exotic, the schema-delta view degrades to "best effort."

## 11. Test plan

Backend (`backend/tests/test_data_wrangler_node.py`):

- `test_compile_empty_wrangler_is_identity` — empty step list → `SELECT * FROM <input>`.
- `test_compile_filter_*` — one filter sub-step compiles to `WHERE`.
- `test_compile_all_disabled_is_identity`.
- `test_compile_chain_all_six_ops_wraps_subqueries` — one of each op in sequence.
- `test_quote_identifier_escapes_embedded_quotes` — column names with spaces / quotes.
- `test_preview_returns_one_entry_per_enabled_step` — preview endpoint shape.
- `test_preview_schema_delta_reports_renames_as_add_remove`.
- `test_execute_*` — end-to-end against a real DuckDB relation.
- `test_data_wrangler_registered_in_node_registry` — sanity.

Frontend (manual + 1 Vitest):

- Reorder sub-steps via up/down → params update in correct order.
- Disable toggle → step skipped in compiled SQL.
- "Convert to canvas nodes" produces the expected node chain (v2).

## 12. Open questions

1. **Q1.** Should `derive.expression` accept references to other derived columns within the same sub-step? **Default: yes** (DuckDB allows it). Need to verify and document.
2. **Q2.** Where does `selectedStepId` belong — node params (persisted) or local component state (lost on close)? **Default: local component state** (cleaner).
3. **Q3.** Should "Open generated SQL" show SQL up to the selected step, or the full wrangler? **Default: full wrangler**, with a toggle for "up to selected."

---

## Decision log

| Date | Decision | Source |
|---|---|---|
| 2026-05-17 | v1 sub-step DSL frozen at 6 ops: filter, select, rename, cast, derive, group_by | three external reviews + memory inventory |
| 2026-05-17 | Dual execution model (sample-materialize for preview, compiled-SQL-once for runtime) — resolves OOM concern about single-node DuckDB | Review 3 |
| 2026-05-17 | No nested wranglers; no AI authoring in v1; no shared library | Review 1 + 2 ("keep v1 narrow") |
| 2026-05-17 | "Convert to canvas nodes" required as escape hatch | Review 2 |
| 2026-05-17 | "Open generated SQL" required for trust | Review 1 |
| 2026-05-17 | Renamed "Recipe Node" → **Data Wrangler Node** | User directive |
