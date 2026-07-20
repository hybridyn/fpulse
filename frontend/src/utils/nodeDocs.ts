/**
 * Node documentation catalog — #4 of the F-Pulse product vision.
 *
 * Each node card on the canvas should answer four questions:
 *   1. What comes in?    (inputs)
 *   2. What happens here? (does)
 *   3. What goes out?    (outputs)
 *   4. What can fail?    (failsWhen)
 *
 * This catalog is the static source of truth for those answers per
 * step type. FPulseNode reads from it to render the per-node tooltip
 * + (later) the expandable details panel. Keys match the StepType
 * enum values in backend/fpulse/ir/schema.py.
 *
 * Coverage policy: start with the most-used 15-20 node types; add
 * more as we see them appear in real pipelines. Missing types fall
 * back to the generic placeholder in `getNodeDocs()` below.
 *
 * Style rule: keep each field to one short sentence. Long content
 * doesn't read well in a tooltip; the user can open the full Help
 * page for deeper docs.
 */

export interface NodeDocs {
  /** One short sentence — the verb-first summary of what this node does. */
  does: string;
  /** What schema/data shape is expected from upstream. "None" for sources. */
  inputs: string;
  /** What schema/data shape this node emits to downstream nodes. */
  outputs: string;
  /** Common failure modes — what the user should check first if it fails. */
  failsWhen: string;
}

const DOCS: Record<string, NodeDocs> = {
  // ── Sources ────────────────────────────────────────────────────────────
  source: {
    does: 'Read rows from an external source (configured by connector_type).',
    inputs: 'None — sources are the starting point of the pipeline.',
    outputs: 'Rows in the source schema, with column types inferred or declared.',
    failsWhen: 'The connection is unreachable, credentials are invalid, or the file/table is missing.',
  },
  csv_source: {
    does: 'Read rows from a local CSV file.',
    inputs: 'None — reads from disk.',
    outputs: 'Rows with one column per CSV header, types inferred.',
    failsWhen: 'file_path is missing or unreadable, or the file is malformed.',
  },
  db_source: {
    does: 'Run a SQL query against a configured database connection.',
    inputs: 'None — reads from the database.',
    outputs: 'Rows in the query result, with column types from the database.',
    failsWhen: 'connection_id is missing, the database is unreachable, or the query has a syntax error.',
  },
  api_source: {
    does: 'Fetch rows from a REST endpoint, with auth and pagination handled.',
    inputs: 'None — calls the API on each run.',
    outputs: 'Rows parsed from the JSON response, flattened by Schema Mapper if configured.',
    failsWhen: 'The endpoint is unreachable, auth fails, or the response shape changes.',
  },
  local_table_source: {
    does: 'Read from a Managed Table (Parquet) by schema.name.',
    inputs: 'None — reads from the workspace data home.',
    outputs: 'Rows in the table\'s declared schema.',
    failsWhen: 'The schema.name does not exist, or the Parquet files are corrupt.',
  },

  // ── Transforms ─────────────────────────────────────────────────────────
  filter: {
    does: 'Drop rows that do not match the SQL condition.',
    inputs: 'Any row shape.',
    outputs: 'A subset of the input rows (same schema, fewer rows).',
    failsWhen: 'The condition references a column that does not exist, or has a SQL syntax error.',
  },
  transform: {
    does: 'Apply a SQL expression to compute new column values.',
    inputs: 'Any row shape with the columns the expression references.',
    outputs: 'Rows with the new/modified columns produced by the expression.',
    failsWhen: 'The expression references a missing column, or has a SQL syntax error.',
  },
  derived_column: {
    does: 'Add one or more new columns computed from a SQL expression.',
    inputs: 'Any row shape with the source columns the expression uses.',
    outputs: 'Original rows plus the new derived columns appended.',
    failsWhen: 'The expression references a missing column, has a type mismatch, or has a SQL syntax error.',
  },
  aggregate: {
    does: 'GROUP BY zero or more columns and compute SUM / COUNT / AVG / MIN / MAX / median.',
    inputs: 'Rows containing the group_by columns and the columns the functions aggregate.',
    outputs: 'One row per group, with the function results as new columns.',
    failsWhen: 'A function targets a missing or non-numeric column, or functions[] is empty.',
  },
  join: {
    does: 'Combine two upstream inputs on a key (inner / left / right / full / semi / anti / cross).',
    inputs: 'Two upstream tables — keyed by same name, mapped name pairs, or a custom ON clause.',
    outputs: 'Rows with columns from both inputs; a shared non-key column name is suffixed (e.g. name_right).',
    failsWhen: 'Fewer (or more) than 2 inputs are wired, or a key column does not exist on its side.',
  },
  deduplicate: {
    does: 'Collapse rows by key columns, keeping the first or last per Order By.',
    inputs: 'Rows containing the key columns (and the Order By column, if set).',
    outputs: 'One row per distinct key. With "emit duplicates" on, a second output carries the removed rows.',
    failsWhen: 'A key column is missing, or Order By points to a column that does not exist.',
  },
  flatten_explode: {
    does: 'Flatten nested JSON or explode array columns into one row per element.',
    inputs: 'Rows containing the nested/array column(s) named in `columns` (dot-paths allowed).',
    outputs: 'Rows where each array element becomes its own row (optionally with an index column).',
    failsWhen: 'A target column is missing, or — in explode mode — the values are not array-shaped.',
  },
  schema_mapper: {
    does: 'Map source columns to target field names with type coercion and defaults.',
    inputs: 'Rows with the source column names listed in the mapping grid.',
    outputs: 'Rows with the target field names and types as declared.',
    failsWhen: 'A source column is missing, or a coercion (e.g. text → number) fails.',
  },
  data_quality: {
    does: 'Check rows against rules (not null / unique / range / accepted values / regex).',
    inputs: 'Any row shape with the columns the rules target.',
    outputs: 'On fail: drop the bad rows, fail the run, tag them, or split them out a "reject" branch.',
    failsWhen: 'A rule fires and the failure mode is "fail" (vs drop / tag / reject / split).',
  },
  upsert: {
    does: 'Collapse duplicate keys to one row — latest wins by Order By (stream-level, not a table merge).',
    inputs: 'Rows containing the key columns and the Order By column.',
    outputs: 'One row per distinct key — the most recent by Order By. (Use Deduplicate instead — this is folded into it.)',
    failsWhen: 'A key column is missing, or Order By references a column that does not exist.',
  },

  // ── Sinks ──────────────────────────────────────────────────────────────
  destination: {
    does: 'Write rows to an external destination (configured by connector_type).',
    inputs: 'Rows in the schema the destination expects.',
    outputs: 'No further downstream output — sinks are terminal nodes.',
    failsWhen: 'The connection is unreachable, credentials are invalid, or the target schema does not match.',
  },
  csv_sink: {
    does: 'Write rows to a local CSV file.',
    inputs: 'Any row shape — column order becomes header order.',
    outputs: 'No downstream output — writes to disk.',
    failsWhen: 'The output path is unwritable, or the parent directory does not exist.',
  },
  db_sink: {
    does: 'Insert rows into a database table via INSERT (or upsert if key_columns set).',
    inputs: 'Rows whose columns match the target table schema.',
    outputs: 'No downstream output — writes to the database.',
    failsWhen: 'A column type mismatches, the table does not exist, or key_columns are missing for upsert mode.',
  },
  warehouse_sink: {
    does: 'Write to a warehouse with schema evolution (auto-adds new columns).',
    inputs: 'Rows in any schema — new columns are added to the target.',
    outputs: 'No downstream output — writes to the warehouse.',
    failsWhen: 'The warehouse connection is unreachable, or the bulk-load path (PUT/COPY) fails.',
  },
  local_table_sink: {
    does: 'Write to a Managed Table (Parquet) addressable by schema.name.',
    inputs: 'Any row shape — column types persist in the Parquet schema.',
    outputs: 'No downstream output — writes to the workspace data home.',
    failsWhen: 'mode=merge and merge_on columns are missing, or the data directory is unwritable.',
  },

  // ── Data movement (extra) ───────────────────────────────────────────────
  copy_data: {
    does: 'Bulk-copy rows from a source to a destination in one step, no transform.',
    inputs: 'None — reads from the configured source and writes to the configured destination.',
    outputs: 'No downstream output — this is a terminal copy activity.',
    failsWhen: 'Either side is unreachable/misconfigured, or the schemas are incompatible.',
  },

  // ── Transforms (extra) ──────────────────────────────────────────────────
  data_wrangler: {
    does: 'Run an ordered list of inline sub-steps (filter / rename / cast / derive / group) with per-step preview.',
    inputs: 'Any row shape — each sub-step transforms the output of the previous one.',
    outputs: 'Rows after all sub-steps have applied, in order.',
    failsWhen: 'A sub-step references a missing column or has an invalid expression.',
  },
  sort: {
    does: 'Order rows by one or more columns, each ascending or descending, with NULLS first/last.',
    inputs: 'Rows containing the sort columns.',
    outputs: 'The same rows, reordered (schema unchanged).',
    failsWhen: 'A sort column does not exist.',
  },
  sample: {
    does: 'Take a subset of rows — first N, a percentage, or a random sample (seed for repeatable).',
    inputs: 'Any row shape.',
    outputs: 'A subset of the input rows (same schema, fewer rows).',
    failsWhen: 'The row count / percentage is out of range.',
  },
  split_out: {
    does: 'Explode one array column into one row per element (a preset of Flatten / Explode).',
    inputs: 'Rows containing the array column to split out.',
    outputs: 'Rows where each array element becomes its own row.',
    failsWhen: 'The target column is missing or its values are not array-shaped.',
  },
  scd2: {
    does: 'Maintain a Type-2 slowly-changing dimension — version history with effective_from / effective_to.',
    inputs: 'Incoming rows keyed by the business key, plus the tracked columns to watch for changes.',
    outputs: 'Versioned rows: existing versions closed off and new versions opened when tracked columns change.',
    failsWhen: 'The business key or a tracked column is missing.',
  },

  // ── Combine ─────────────────────────────────────────────────────────────
  lookup: {
    does: 'Enrich the main input with columns from a reference input matched by key.',
    inputs: 'Two inputs — the main stream and a reference table (pick which is the reference).',
    outputs: 'Main rows plus the chosen reference columns; no-match rows kept or dropped per setting.',
    failsWhen: 'A key column is missing on either side.',
  },
  union: {
    does: 'Stack rows from multiple inputs (union all, distinct, or align by column name).',
    inputs: 'Two or more inputs with compatible columns.',
    outputs: 'All input rows combined into one stream.',
    failsWhen: 'Column counts/types are incompatible (by-position modes) — use "by name" to align.',
  },
  pivot: {
    does: 'Long → wide: spread a column\'s values into new columns, aggregated per group.',
    inputs: 'Rows with the group columns, the column to spread, and a value to aggregate.',
    outputs: 'One row per group, with a column per distinct spread value.',
    failsWhen: 'The pivot/value column is missing, or the aggregate is invalid.',
  },
  unpivot: {
    does: 'Wide → long: collapse value columns into name/value pairs, keeping the id columns.',
    inputs: 'Rows with the id columns to keep and the value columns to fold.',
    outputs: 'Rows with a name column and a value column (nulls optionally included).',
    failsWhen: 'A named id/value column does not exist.',
  },
  window: {
    does: 'Compute windowed columns (ROW_NUMBER / RANK / LAG / LEAD / running SUM / AVG) over partitions.',
    inputs: 'Rows containing the partition and order columns the functions need.',
    outputs: 'Original rows plus the computed window columns.',
    failsWhen: 'A ranking/navigation function has no Order By, or a referenced column is missing.',
  },

  // ── Control flow ───────────────────────────────────────────────────────
  if_condition: {
    does: 'Route each row to a True or False output by a condition.',
    inputs: 'Any row shape with the columns the condition references.',
    outputs: 'Two output handles — True (condition holds) and False (the rest). Legacy single-output edges map to True.',
    failsWhen: 'The condition has a syntax error or references a missing column.',
  },
  // switch_case is retired from the palette ("Switch" is conditional_split now);
  // kept for back-compat with old pipelines.
  switch_case: {
    does: 'Retired single-case filter — use Switch (conditional_split) for multi-output routing.',
    inputs: 'Any row shape with the columns the case expressions use.',
    outputs: 'Rows for the active case only.',
    failsWhen: 'A case expression errors, or cases are empty/duplicated.',
  },
  conditional_split: {
    does: 'Switch — route each row to a named output branch by condition (multi-output).',
    inputs: 'Any row shape with the columns the branch conditions reference.',
    outputs: 'Each row goes to the first branch whose condition matches (or the default handle).',
    failsWhen: 'A branch condition has a syntax error, or branch names are duplicated.',
  },
  foreach_loop: {
    does: 'Batch Rows — split rows into fixed-size batches, tagging each with its batch index.',
    inputs: 'Any row shape — batched by the configured size.',
    outputs: 'All rows plus _batch_index / _batch_total (recombined after batching).',
    failsWhen: 'The batch size is invalid.',
  },
  foreach_pipeline: {
    does: 'Run a saved sub-pipeline once per input row, injecting the row\'s columns as parameters.',
    inputs: 'Rows whose columns map to the child pipeline\'s declared parameters.',
    outputs: 'The input rows pass through unchanged (control-flow semantics).',
    failsWhen: 'The child pipeline is missing, the row count exceeds the max, or a run fails (per on-error).',
  },
  wait_delay: {
    does: 'Pause the run for a fixed duration before continuing.',
    inputs: 'Any row shape — passed through after the delay.',
    outputs: 'The input rows, unchanged.',
    failsWhen: 'The duration is negative or above the cap.',
  },
  set_variable: {
    does: 'Set runtime variables ({{ $vars.NAME }}) from a constant or SQL expression.',
    inputs: 'Optional — an input lets the expression reference its columns (use an aggregate for one value).',
    outputs: 'Input rows pass through unchanged; the variables are read by later steps via {{ $vars.NAME }}.',
    failsWhen: 'An expression cannot be evaluated.',
  },
  lookup_activity: {
    does: 'Fetch a value or reference row into a variable for control flow (watermarks, gates).',
    inputs: 'Self-contained (Source = connection + query), or read a wired upstream relation.',
    outputs: 'Captures {{ $vars.X }} = {firstRow, rows, value, count, isEmpty}; the rows also pass through.',
    failsWhen: 'Connection mode without a query, or no input wired in upstream mode, or on_empty="fail" with no rows.',
  },
  execute_pipeline: {
    does: 'Run another saved pipeline as a child step, optionally passing parameters.',
    inputs: 'Depends on the child pipeline.',
    outputs: 'Depends on the child pipeline.',
    failsWhen: 'The child pipeline is missing or its run fails.',
  },
  retry_handler: {
    does: 'Retry the wrapped operation on failure, with a delay and a max attempt count.',
    inputs: 'Any row shape.',
    outputs: 'The result of the wrapped operation once it succeeds (or the final failure).',
    failsWhen: 'All retry attempts are exhausted.',
  },
  fail: {
    does: 'Stop the run with an error — optionally only when a condition holds.',
    inputs: 'Any row shape with the columns the condition references.',
    outputs: 'None — halts the pipeline when it fires.',
    failsWhen: 'By design, when triggered (this node\'s purpose is to fail the run).',
  },

  // ── Action ──────────────────────────────────────────────────────────────
  http_request: {
    does: 'Call an HTTP endpoint (GET/POST/…) and capture the response.',
    inputs: 'Optional — request fields can be templated from input rows / variables.',
    outputs: 'Rows parsed from the response (or the input passed through, per config).',
    failsWhen: 'The endpoint is unreachable, auth fails, or a non-2xx status is returned.',
  },
  send_email: {
    does: 'Send an email via the configured SMTP connection.',
    inputs: 'Optional — subject/body can be templated from input rows / variables.',
    outputs: 'No data output — sends the message as a side effect.',
    failsWhen: 'SMTP credentials are wrong, or the recipient/host is invalid.',
  },
  slack_notify: {
    does: 'Post a message to a Slack or Teams channel via webhook.',
    inputs: 'Optional — the message can be templated from input rows / variables.',
    outputs: 'No data output — posts the message as a side effect.',
    failsWhen: 'The webhook URL is missing or rejected.',
  },
  code_script: {
    does: 'Run a custom Python/SQL snippet against the data.',
    inputs: 'Any row shape — exposed to the script.',
    outputs: 'Whatever the script returns as its result set.',
    failsWhen: 'The script raises an error or returns an unexpected shape.',
  },
  get_metadata: {
    does: 'Return metadata about the input — row count, column names, and types.',
    inputs: 'Any row shape.',
    outputs: 'A small result describing the input (used for gates / logging).',
    failsWhen: 'No input is wired.',
  },

  // ── AI / Semantic ───────────────────────────────────────────────────────
  embedder: {
    does: 'Compute vector embeddings for a text column using the configured model.',
    inputs: 'Rows containing the text column to embed.',
    outputs: 'Rows plus an embedding (vector) column.',
    failsWhen: 'The AI provider is unconfigured, rate-limited, or the text column is missing.',
  },
  llm_guardrail: {
    does: 'Check rows against a guardrail (PII, toxicity, schema, policy) and act on violations.',
    inputs: 'Rows with the column(s) to inspect.',
    outputs: 'Rows that pass; violations are dropped, tagged, or fail the run per config.',
    failsWhen: 'A violation fires and the mode is "fail", or the provider is unavailable.',
  },
  semantic_router: {
    does: 'Classify rows by semantic similarity to defined categories and route accordingly.',
    inputs: 'Rows with the text column to classify.',
    outputs: 'Rows tagged with the matched category (and routable downstream).',
    failsWhen: 'The AI provider is unconfigured, or the text column is missing.',
  },
};

/**
 * Generic placeholder for step types not in the catalog yet. Returns a
 * minimally useful tooltip so the user still sees the four-W structure.
 */
function placeholder(stepType: string): NodeDocs {
  const pretty = stepType.replace(/_/g, ' ');
  return {
    does: `Run a ${pretty} operation. See the Help page for details.`,
    inputs: 'Depends on configuration — see the Inputs section of the config panel.',
    outputs: 'Depends on configuration — see the schema preview after running.',
    failsWhen: 'Configuration is incomplete, or upstream data does not match what this node expects.',
  };
}

/**
 * Lookup helper — returns the catalog entry if present, or a generic
 * placeholder so callers never get `undefined`.
 */
export function getNodeDocs(stepType: string): NodeDocs {
  return DOCS[stepType] ?? placeholder(stepType);
}

/**
 * Render the four-W structure as a single multi-line string suitable
 * for a `title` HTML attribute (newlines render as line breaks in
 * browser tooltips). Used by FPulseNode on the step-type subtitle.
 */
export function formatNodeDocsTooltip(stepType: string): string {
  const d = getNodeDocs(stepType);
  return `${d.does}\n\nInputs: ${d.inputs}\nOutputs: ${d.outputs}\nFails when: ${d.failsWhen}`;
}

export { DOCS as NODE_DOCS };
