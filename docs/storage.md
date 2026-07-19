# Storage — workspace data home

The **Storage** page (sidebar between Pool and Insights, URL `#storage`) is F-Pulse's data estate. Every uploaded file, every managed Parquet table, every pipeline output is visible there. It closes the **Connect → Store → Transform → Reuse** loop — the gap that previously made pipeline outputs feel ephemeral.

Storage shipped 2026-05-23 in OSS v1.0.

## The three tabs

### Files

Uploaded data files (CSV / TSV / JSON / NDJSON / Parquet / Excel / XML). Each row shows:

* **Name** + optional description
* **Format** — auto-detected from the extension
* **Scope** — `Global` (visible to every project) or the project name pill
* **Size** + last-updated timestamp
* **Used by N pipelines** — clickable pill that opens a drill-down popover (see Usage tracking below)
* Row actions — Preview / Replace bytes / **Clean & promote** / Promote to managed table / Move to trash

Toggle **Show deleted (trash)** in the panel header to surface soft-deleted rows; restore them with the Restore action.

### Managed Tables

Parquet-backed tables addressable as `schema.name` from pipeline nodes. Each row shows:

* **Schema** + **Table** name (monospaced — these are SQL-like identifiers)
* **Rows / Columns / Size / Parts** — refreshed automatically after every write
* **Used by N pipelines** — same pill as Files
* Drop action — hard-deletes the index row AND the on-disk Parquet parts

Reference a managed table from a pipeline via either:

1. **Source / Destination node** with `connector_type='local_table'` and `schema_name + table_name` params, or
2. The dedicated **Managed Table Source** / **Managed Table Sink** nodes (cleaner, recommended).

### Pipeline Outputs

Files written by pipeline runs, grouped by `(pipeline_id, run_id)`. Each group is one dark-header card showing the pipeline + run + file count + total size, with a row per output file. Preview is the only action — outputs are read-only artifacts of past runs.

## Reports auto-persist to Storage (added 2026-05-25)

Every PDF/DOCX you generate from **Insights → Reports** is automatically written into your workspace storage tree (under `uploads/`) and indexed with a `report` tag. The download fires immediately as before; the toast confirms the saved copy ("*Downloaded ... — also saved to Storage*"). Re-downloading or sharing the same snapshot later doesn't require re-running the report — open Storage → Files and grab it from the row.

Persistence is best-effort. If the storage write fails, the download still works and the response surfaces the error in the `X-Storage-Error` header so ops can diagnose without trawling backend logs. Idempotent schema repair lives at `POST /api/storage/_repair_schema` for installs that ended up with a stale `storage_objects` table.

## Upload dialog — scope picker

Clicking **+ Upload file** at the top right opens a dialog asking:

1. **Scope** — radio between *Global* (workspace-wide) and *Project*.
2. **Project** picker (when Project scope is chosen) — defaults to the active project context if one is set.
3. **Folder** picker — optional sub-folder within the chosen project. F-Pulse OSS folders are 1-level deep.
4. **Description** — optional note shown on the row.
5. **File** — drag-and-drop OR click-to-browse. Max 100 MB by default (`FPULSE_UPLOAD_MAX_MB` env var to raise).

The dialog auto-picks a sensible first project when you flip scope to *Project*, so the dropdown lands on a real value instead of a placeholder. Toast on success confirms the landing spot: *"Uploaded orders.csv to Sales / Q1 2026"*.

## Promote a file to a managed table

The "Promote" row action turns any uploaded records-shaped file (CSV / JSON / Parquet / Excel) into a managed table:

1. Click the Promote icon (cylinders icon, green hover) on a Files-tab row.
2. Pick a target **schema** (existing or click *+ New schema*).
3. Set the **table name** — auto-suggested from the filename, lowercase + snake-cased.
4. Optionally add a **description** and **column renames** (comma-separated `old:new` pairs).
5. Click **Promote to table**. Toast: *"Created sales.orders (1,250 rows)"*; the page switches to the Managed Tables tab.

Promotion is **idempotent** on `(workspace_id, schema, name)` — re-promoting overwrites the existing part-000. For append / merge semantics use Managed Table Sink in a pipeline.

## Clean & promote — fixing dirty data before landing it

Plain **Promote** is a verbatim copy: source bytes → Parquet part-000, no transformation. That's fine when the file is already clean. When the file has trailing whitespace, NULLs you want to drop, dates as strings, dupes, mixed case keys — use **Clean & promote** instead.

Click the wand icon (amber hover) on a Files-tab row. F-Pulse scaffolds a 3-node cleanup pipeline in the Editor:

```
[Source: orders.csv] ─► [Data Wrangler: empty] ─► [Managed Table Sink: default.orders]
```

The middle node is empty — you fill in the cleanup recipe:

| Wrangler sub-step | Typical use |
|---|---|
| `filter` | Drop bad rows (e.g. `WHERE status != 'cancelled'`) |
| `select` / `rename` | Drop / rename columns |
| `cast` | Fix types (`birthdate: VARCHAR → DATE`) |
| `derive` | `TRIM`, `COALESCE`, `LOWER`, `REGEXP_REPLACE` … |
| `dedupe` | Remove duplicates by key |
| `group_by` | Aggregate / collapse |
| `sort` | Order rows |
| `sample` | Take first N |
| `flatten` | Unnest JSON / struct columns |

Click **Run**. The Wrangler compiles all sub-steps to a single DuckDB query, the sink writes Parquet, and the new managed table appears on the **Managed Tables** tab. The original file stays in place — Clean & promote never mutates the source.

The same flow works from the **Connections** page: click the wand icon on a saved connection, pick a stream / table from the catalog, and a Generic Source + Wrangler + Sink scaffold opens in the Editor with everything pre-wired. Use this when the dirty data lives in a Postgres / Oracle / Salesforce / SAP / Graph / REST source rather than as an uploaded file.

## Replace bytes in place

The **Replace** action (upload-arrow icon, indigo hover) lets you swap the bytes of an existing file without changing its `object_id`. Downstream pipelines pick up the new bytes on their next run.

* Extension must match the original — the API rejects swapping `.csv` for `.parquet` (would silently break downstream pipelines).
* If the file is referenced by pipelines, a confirm dialog lists them first so you don't swap data under a live pipeline by accident.
* Cached schema-infer columns are cleared so the next preview re-infers from the new bytes.

## Usage tracking — "what would break if I drop this?"

Every Files / Managed Tables row has a **Used by** column. Rows with references show a blue *"Used by N"* pill; rows with zero references show an em-dash.

Click the pill to open a popover listing each referencing pipeline with name + **Open →** link that navigates to `#editor/{workflow_id}`.

The scanner detects four reference shapes:

1. **`local_table_source` / `local_table_sink`** steps matching `schema.name`.
2. **Generic `source` / `destination`** nodes with `connector_type='local_table'` (same params shape, dispatched through SOURCE_MAP / DEST_MAP).
3. **File-path references** — any source/sink node whose `params.file_path` matches a `storage_object.path` value.
4. **Promote-to-table provenance** — a file that seeded a managed table inherits the table's pipeline list (so deleting the original file warns about downstream damage too).

**Destructive actions** (Delete file / Drop table / Replace bytes) automatically surface the usage list in the confirm dialog before proceeding, so a downstream pipeline doesn't break silently. You can still proceed — the gate is informational, not a hard block.

The scanner caches results for 30 seconds keyed on the workspace; the cache is invalidated on workflow save so freshness matters for the operator's own edits.

## Preview panel

Clicking the eye icon on any row opens a **bottom panel** (~440 px tall) — file list stays visible above so you can click another file to swap previews without closing. Two sub-tabs:

* **Preview** — first 100 rows in a tabular view with sticky header, monospace cells, type badge under each column name.
* **Schema** — column name, type, sample value, ordinal. Disabled for non-tabular JSON.

For non-tabular JSON (configs, OpenAPI specs, F-Pulse pipeline exports, etc.) the preview renders a collapsible **JSON tree** instead of crashing with a malformed-JSON error. If the JSON has F-Pulse pipeline shape, an amber *"This looks like a pipeline definition. **Open in Editor**"* banner appears just below the panel header; one click hands the bytes off to the Workflows import flow.

Press **Escape** or click the X to close. The page content above gets `pb-[460px]` while the panel is open so the last row isn't hidden behind it.

## On-disk layout

Storage browses a structured sub-tree of `$FPULSE_DATA_DIR`:

```
$FPULSE_DATA_DIR/
  uploads/{workspace_id}/{stored_filename}        # raw uploads
  outputs/{workspace_id}/{pipeline_id}/{run_id}/  # pipeline-generated
  tables/{workspace_id}/{schema}/{name}/part-*.parquet  # managed tables
  trash/{workspace_id}/{stored_filename}          # soft-deleted
  checkpoints/                                    # execution artifacts (system)
  step_io/                                        # execution artifacts (system)
```

Metadata for files + tables lives in SQLite (`storage_objects` / `storage_tables` / `storage_columns`, schema v25/v26). Bytes live on the filesystem; the index just lets us query without walking the tree.

A **reconciler** runs once per install on first boot after upgrade (sentinel file `$FPULSE_DATA_DIR/.datastore-reconciled`) and back-fills `storage_objects` rows for any pre-existing files under `uploads/` and `outputs/`. Add-only — never deletes a row. Delete the sentinel to force a rescan.

## Cleanup + retention

A **Clean up files older than 30 days** button appears in the footer when there's anything in trash. Click it to:

1. Get a dry-run preview: *"3 files (12 MB) will be deleted permanently."*
2. Confirm → bytes leave disk + metadata rows hard-delete.

There are no enforced workspace quotas in OSS — cleanup is manual. Plus adds retention policies, quotas, and audit logging on storage operations.

## API surface

The Storage page is a thin client over twelve REST endpoints (auth + workspace scope on every request):

| Endpoint | Purpose |
|---|---|
| `GET /api/storage/summary` | Aggregate counts + bytes for the KPI strip. |
| `GET /api/storage/files?include_deleted=&project_id=&folder_id=` | List files (optionally filtered). |
| `GET /api/storage/tables` + `/tables/{id}/columns` | List managed tables + per-table column metadata. |
| `GET /api/storage/outputs` | Pipeline outputs grouped by run. |
| `GET /api/storage/preview/{object_id}` | DuckDB-backed row sample (or JSON tree fallback). |
| `GET /api/storage/schema/{object_id}` | Inferred column list (cached, mtime-invalidated). |
| `GET /api/storage/raw/{object_id}` | Stream raw bytes — used by "Open in Editor". |
| `GET /api/storage/usage` + `/usage/file/{id}` + `/usage/table/{id}` | Pipeline reference index. |
| `POST /api/storage/upload?project_id=&folder_id=&description=` | Multipart upload. |
| `POST /api/storage/file/{id}/replace` | In-place byte replace. |
| `POST /api/storage/promote-to-table` | Promote a file to a managed Parquet table. |
| `POST /api/storage/move` | Trash ↔ uploads recovery. |
| `DELETE /api/storage/file/{id}` | Soft-delete (move to trash). |
| `DELETE /api/storage/tables/{id}` | Hard-delete a managed table (index + bytes). |
| `POST /api/storage/cleanup` | Bulk purge of old trash / outputs (dry-run by default). |

## Plus differences (post v1.0)

Plus v1.0 (Nov 2026 target) adds the operational layer on top of the OSS substrate:

* **Volumes** — registered folders addressable by URI (Unity-Catalog-style); deferred from OSS to avoid UX confusion with Files.
* **Delta Lake managed tables** — time travel, MERGE INTO with statistics-based skip, ACID transactions. Replaces the OSS Parquet implementation for promoted tables.
* **Remote storage backends** — S3 / Azure Blob / GCS via the `backend` field on every metadata row.
* **Storage governance** — workspace quotas, retention policies, RBAC on storage paths, audit log of every operation.

OSS today carries the `backend` column on every row but only the `local` value is implemented; Plus extends the dispatch without a schema migration.

## Common questions

**Can I upload a pipeline JSON?** You can, but you shouldn't. Storage is for data files. When you preview an accidentally-uploaded pipeline JSON, an amber banner offers the **Open in Editor** recovery path (1-click handoff to Workflows → Import).

**Can I version a managed table?** Not in OSS v1.0. Drop + re-promote is the workaround. Time-travel arrives with Plus's Delta Lake managed tables.

**What happens if I delete a file that 3 pipelines reference?** The delete confirm dialog lists the affected pipelines first. You can still proceed — they'll fail on their next run with a "file not found" error. The gate is informational so you can decide whether to fix the upstream pipelines or restore from trash.

**Where do pipeline outputs go?** Pipelines that write to managed tables land them in `tables/{ws}/{schema}/{name}/`. Pipelines that write to file paths land them in whatever path their sink configures — by convention `outputs/{ws}/{pipeline_id}/{run_id}/{filename}` so the Outputs tab can group them. The reconciler picks this up automatically.

**Can the AI Copilot upload files for me?** No — file upload requires a human-driven file picker for security reasons. The AI can suggest *which* file to upload + draft a pipeline that uses it once uploaded.
