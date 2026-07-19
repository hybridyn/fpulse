"""Managed local-table source + sink nodes (2026-05-23, Y3).

A *managed table* is a workspace-scoped Parquet table at
``{DATA_DIR}/tables/{workspace_id}/{schema}/{name}/part-*.parquet``,
indexed by the ``storage_tables`` / ``storage_columns`` rows. Two
node classes:

  * ``LocalTableSourceNode``  — reads a managed table by ``schema.name``.
  * ``LocalTableSinkNode``    — writes to a managed table in one of
                                 three modes: replace | append | merge.

Why this isn't just a Parquet source pointing at a path:

  * The user thinks in ``default.customers``, not in
    ``data/tables/default/default/customers/part-000.parquet``.
  * The Storage page needs to track row count + size after each
    sink write so the table list stays honest.
  * The merge mode requires upserting on a key set — that's
    workflow-level logic, not a connector option.

Workspace resolution: the node executes inside ``ExecutionContext``
which already carries ``workspace_id``. If a future runtime path
drops ``workspace_id`` on the context the node falls back to
``"default"`` so OSS single-tenant installs keep working.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

from fpulse.datastore.models import (
    SINK_MODE_APPEND,
    SINK_MODE_MERGE,
    SINK_MODE_REPLACE,
    StorageColumn,
    StorageTable,
)
from fpulse.datastore.paths import (
    safe_schema_or_table_name,
    workspace_paths,
)
from fpulse.datastore.store import DataStore
from fpulse.intelligence.schema_policy import (
    DEFAULT_POLICY,
    PolicyDecision,
    SchemaPolicy,
    evaluate_policy,
    schema_policy_param,
)
from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register

logger = logging.getLogger(__name__)


def _columns_from_storage_rows(rows) -> list[dict[str, Any]]:
    """Convert StorageColumn rows into the dict shape evaluate_policy expects."""
    return [
        {"name": r.name, "type": r.type, "nullable": bool(r.nullable)}
        for r in rows
    ]


def _columns_from_relation(rel) -> list[dict[str, Any]]:
    """Pull (name, type) tuples off a DuckDBPyRelation into our dict shape.

    DuckDB doesn't expose per-column nullability on relations (you'd
    have to scan), so we default to ``True``. The policy treats this
    as "could be NULL" — the safer assumption for drift detection.
    """
    try:
        columns = list(rel.columns)
        types = list(rel.types)
    except Exception:
        return []
    return [
        {"name": col, "type": str(typ), "nullable": True}
        for col, typ in zip(columns, types)
    ]


def _publish_drift_event(
    ctx: ExecutionContext,
    *,
    workspace_id: str,
    table_id: str,
    table_display_name: str,
    step_id: str,
    decision: PolicyDecision,
    applied: bool,
    schema_version: int,
    rejection_reason: str = "",
) -> None:
    """Fire a SchemaDriftDetected event on the bus.

    Best-effort by design: a missing/closed bus must NOT take a sink
    write down. The event is the audit signal; the data already landed
    (or was correctly rejected) before this runs.
    """
    try:
        from fpulse.events import SchemaDriftDetected, get_event_bus
        bus = get_event_bus()
        if bus is None:
            return
        summary = decision.to_summary()
        bus.publish(SchemaDriftDetected(
            run_id=getattr(ctx, "run_id", "") or "",
            step_id=step_id or "",
            workspace_id=workspace_id,
            table_id=table_id,
            table_name=table_display_name,
            policy=decision.policy.value,
            severity=decision.severity if applied else "critical",
            applied=applied,
            schema_version=schema_version,
            added_columns=summary.get("added") or [],
            dropped_columns=summary.get("dropped") or [],
            type_changes=summary.get("type_changed") or [],
            rejection_reason=rejection_reason,
        ))
    except Exception as exc:
        # Bus offline / not wired / closed — drift is still in
        # schema_history; the event is the live signal. Don't crash.
        logger.debug("schema drift event publish skipped: %s", exc)


def _record_history(
    ctx: ExecutionContext,
    *,
    workspace_id: str,
    table_id: str,
    columns: list[dict[str, Any]],
    decision: PolicyDecision,
) -> int:
    """Append a row to schema_history; return the new version (0 on no-op)."""
    try:
        store = (getattr(ctx, "app_state", None) or {}).get("schema_history_store")
        if store is None:
            return 0
        row = store.record(
            workspace_id=workspace_id,
            table_id=table_id,
            columns=columns,
            change_summary=decision.to_summary(),
            applied_by_run_id=getattr(ctx, "run_id", "") or "",
            policy=decision.policy.value,
        )
        return int(row.get("version") or 0)
    except Exception as exc:
        # Same robustness contract as the event publish: history is an
        # audit trail, not a guard. Log + continue.
        logger.warning("schema_history.record failed: %s", exc)
        return 0


def _ctx_workspace_id(ctx: ExecutionContext) -> str:
    """Resolve the workspace_id from ExecutionContext, falling back to default.

    ExecutionContext has carried workspace_id since the multi-tenant
    work shipped, but the attribute is optional on older test fixtures
    so we tolerate its absence.
    """
    return getattr(ctx, "workspace_id", None) or "default"


def _datastore_for(ctx: ExecutionContext) -> DataStore:
    """Pull the live DataStore off app_state with a clear error if missing."""
    state = getattr(ctx, "app_state", None) or {}
    store = state.get("datastore")
    if store is None:
        raise RuntimeError(
            "local_table node: app_state['datastore'] is not initialised. "
            "Check fpulse.main._populate_state."
        )
    return store


def _data_dir(ctx: ExecutionContext) -> str:
    state = getattr(ctx, "app_state", None) or {}
    data_dir = state.get("data_dir")
    if not data_dir:
        # ExecutionContext may carry data_dir directly too.
        data_dir = getattr(ctx, "data_dir", None)
    if not data_dir:
        raise RuntimeError(
            "local_table node: data_dir is unavailable; "
            "ExecutionContext must carry data_dir or app_state['data_dir']."
        )
    return data_dir


# ─── Source ──────────────────────────────────────────────────────────────


@register(StepType.LOCAL_TABLE_SOURCE)
class LocalTableSourceNode(BaseNode):
    """Read a managed Parquet table by ``schema.name``.

    Params:
      schema_name (default: ``"default"``)
      table_name  (required)

    Output: a DuckDB relation over all part-*.parquet files in the
    table directory. Empty table → DuckDB empty relation, NOT an
    error — downstream nodes are responsible for handling zero rows.
    """

    display_name = "Managed Table Source"
    category = "source"
    description = "Read a managed Parquet table from the workspace datastore"

    def execute(self, ctx: ExecutionContext) -> "duckdb.DuckDBPyRelation":
        schema_name = safe_schema_or_table_name(self.params.get("schema_name") or "default")
        table_name = safe_schema_or_table_name(self.params.get("table_name") or "")
        if not table_name:
            raise ValueError("local_table_source: table_name is required")

        workspace_id = _ctx_workspace_id(ctx)
        store = _datastore_for(ctx)

        table = store.find_table_by_name(workspace_id, schema_name, table_name)
        if not table:
            raise ValueError(
                f"local_table_source: no such managed table '{schema_name}.{table_name}' "
                f"in workspace {workspace_id!r}. Use the Storage page → Promote, "
                f"or write to it from a local_table_sink first."
            )

        paths = workspace_paths(_data_dir(ctx), workspace_id)
        table_dir = paths.table_dir(schema_name, table_name)
        glob = os.path.join(table_dir, "part-*.parquet")
        # Read every part file as one relation. read_parquet accepts a
        # glob; if no files match it returns an error, so probe first.
        if not any(
            fname.startswith("part-") and fname.endswith(".parquet")
            for fname in (os.listdir(table_dir) if os.path.isdir(table_dir) else [])
        ):
            # Empty table — synthesise a zero-row relation with the
            # cached column shape so downstream nodes still see a
            # well-typed schema.
            cols = store.list_columns(table_id=table.id)
            if not cols:
                return ctx.conn.sql("SELECT NULL AS empty WHERE false")
            select_clauses = ", ".join(
                f"CAST(NULL AS {c.type}) AS \"{c.name}\"" for c in cols
            )
            return ctx.conn.sql(f"SELECT {select_clauses} WHERE false")
        # union_by_name=True so part files written under different
        # schema versions (e.g. one before, one after an add_columns
        # evolution) read as a single coherent table — missing columns
        # NULL-fill, extra columns are kept. Mirrors the metadata
        # refresh path in LocalTableSinkNode._refresh_metadata.
        return ctx.conn.sql(
            f"SELECT * FROM read_parquet('{glob}', union_by_name=true)"
        )

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"schema_name": "default", "table_name": ""}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "schema_name", "type": "string", "label": "Schema", "default": "default"},
            {"name": "table_name", "type": "string", "label": "Table", "required": True},
        ]


# ─── Sink ─────────────────────────────────────────────────────────────────


@register(StepType.LOCAL_TABLE_SINK)
class LocalTableSinkNode(BaseNode):
    """Write the upstream relation to a managed Parquet table.

    Three modes mirror the standard sink contract:

      replace — drop existing part-*.parquet, write a fresh part-000.
      append  — write a new part-{timestamp}.parquet alongside existing.
      merge   — read existing rows, MERGE INTO logic on ``merge_on``
                keys, rewrite as a single part-000. Last-writer-wins on
                key collision: rows with matching keys are replaced by
                incoming, non-matching rows are kept.

    On every write the storage_tables row is upserted with the new
    row count + column count + size + part count.
    """

    display_name = "Managed Table Sink"
    category = "destination"
    description = "Write to a managed Parquet table in the workspace datastore"

    def execute(self, ctx: ExecutionContext) -> "duckdb.DuckDBPyRelation":
        schema_name = safe_schema_or_table_name(self.params.get("schema_name") or "default")
        table_name = safe_schema_or_table_name(self.params.get("table_name") or "")
        if not table_name:
            raise ValueError("local_table_sink: table_name is required")
        mode = (self.params.get("mode") or SINK_MODE_REPLACE).lower()
        if mode not in (SINK_MODE_REPLACE, SINK_MODE_APPEND, SINK_MODE_MERGE):
            raise ValueError(
                f"local_table_sink: unknown mode {mode!r}; "
                f"expected one of replace | append | merge."
            )
        merge_on: list[str] = self.params.get("merge_on") or []
        if mode == SINK_MODE_MERGE and not merge_on:
            raise ValueError("local_table_sink: merge mode requires `merge_on` key list.")

        # Resolve the upstream relation via the canonical sinks-API
        # pattern (matches sinks.py:_get_input). The executor injects
        # `_input_step_ids` into params from the workflow's connection
        # graph; ctx.get_inputs looks them up against ctx._results.
        #
        # Z21 (2026-05-23) — fixes "local_table_sink: no upstream relation"
        # that fired for every Test Node / Run on this sink. The previous
        # code read `ctx.input` / `ctx.inputs` which don't exist on
        # ExecutionContext (see backend/fpulse/nodes/base.py:43).
        input_step_ids = self.params.get("_input_step_ids") or []
        inputs = ctx.get_inputs(input_step_ids) if input_step_ids else []
        if not inputs:
            # Last-resort fallback: if a caller bypassed the injection
            # path (some tests do), take the first registered result.
            # Production runs always reach the input_step_ids branch.
            inputs = list(ctx._results.values())
        if not inputs:
            raise ValueError("local_table_sink: no upstream relation.")
        upstream = inputs[0]

        workspace_id = _ctx_workspace_id(ctx)
        store = _datastore_for(ctx)
        paths = workspace_paths(_data_dir(ctx), workspace_id).ensure()
        table_dir = paths.table_dir(schema_name, table_name)

        # ── Schema-policy enforcement (2026-05-27) ────────────────────
        # The policy decision is computed BEFORE we touch the filesystem
        # so a "strict" rejection produces zero side-effects — no part
        # files, no metadata churn, no event bus noise. The decision
        # object travels with us into the post-write history record
        # and event publish.
        policy_value = self.params.get("schema_policy") or DEFAULT_POLICY.value
        existing_table = store.find_table_by_name(workspace_id, schema_name, table_name)
        existing_cols: list[dict[str, Any]] = []
        if existing_table is not None:
            existing_cols = _columns_from_storage_rows(
                store.list_columns(table_id=existing_table.id)
            )
        incoming_cols = _columns_from_relation(upstream)
        decision = evaluate_policy(existing_cols, incoming_cols, policy_value)
        if not decision.ok:
            # Publish a rejected-drift event before raising so the audit
            # log captures the refusal even though no bytes moved.
            _publish_drift_event(
                ctx,
                workspace_id=workspace_id,
                table_id=existing_table.id if existing_table else "",
                table_display_name=f"{schema_name}.{table_name}",
                step_id=self.params.get("_step_id", ""),
                decision=decision,
                applied=False,
                schema_version=0,
                rejection_reason=decision.rejection_reason or "",
            )
            decision.raise_if_rejected()

        os.makedirs(table_dir, exist_ok=True)

        # Register upstream so DuckDB can address it in subsequent SQL.
        ctx.conn.register("_lt_sink_input", upstream)
        try:
            if mode == SINK_MODE_REPLACE:
                self._mode_replace(ctx, table_dir)
            elif mode == SINK_MODE_APPEND:
                self._mode_append(ctx, table_dir)
            else:
                self._mode_merge(ctx, table_dir, merge_on)
        finally:
            try:
                ctx.conn.unregister("_lt_sink_input")
            except Exception:
                pass

        # Refresh metadata.
        self._refresh_metadata(
            store, ctx, workspace_id, schema_name, table_name, table_dir, paths,
        )

        # ── Post-write audit: history + event ─────────────────────────
        # When the policy accepted a drifted schema, we now have:
        #   * The new bytes on disk
        #   * Refreshed storage_columns reflecting the post-write shape
        # So this is the right moment to append a history row and emit
        # the bus event. ``has_drift=False`` means the incoming schema
        # matched existing exactly — no audit row needed.
        if decision.has_drift and decision.ok:
            refreshed_table = store.find_table_by_name(
                workspace_id, schema_name, table_name,
            )
            if refreshed_table is not None:
                refreshed_cols = _columns_from_storage_rows(
                    store.list_columns(table_id=refreshed_table.id)
                )
                version = _record_history(
                    ctx,
                    workspace_id=workspace_id,
                    table_id=refreshed_table.id,
                    columns=refreshed_cols,
                    decision=decision,
                )
                _publish_drift_event(
                    ctx,
                    workspace_id=workspace_id,
                    table_id=refreshed_table.id,
                    table_display_name=f"{schema_name}.{table_name}",
                    step_id=self.params.get("_step_id", ""),
                    decision=decision,
                    applied=True,
                    schema_version=version,
                )

        # Sinks are passthrough by convention — return the same relation
        # so downstream nodes can chain off the side-effect.
        return upstream

    # ── Mode implementations ──────────────────────────────────────────────

    def _mode_replace(self, ctx: ExecutionContext, table_dir: str) -> None:
        # Drop all existing part-*.parquet, then write part-000.
        for fname in os.listdir(table_dir):
            if fname.startswith("part-") and fname.endswith(".parquet"):
                try:
                    os.remove(os.path.join(table_dir, fname))
                except OSError as exc:
                    logger.warning("replace mode: could not remove %s: %s", fname, exc)
        out = os.path.join(table_dir, "part-000.parquet").replace("\\", "/")
        ctx.conn.sql(f"COPY (SELECT * FROM _lt_sink_input) TO '{out}' (FORMAT PARQUET)")

    def _mode_append(self, ctx: ExecutionContext, table_dir: str) -> None:
        # New part-{utc-timestamp}.parquet alongside existing.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        out = os.path.join(table_dir, f"part-{stamp}.parquet").replace("\\", "/")
        ctx.conn.sql(f"COPY (SELECT * FROM _lt_sink_input) TO '{out}' (FORMAT PARQUET)")

    def _mode_merge(
        self, ctx: ExecutionContext, table_dir: str, merge_on: list[str],
    ) -> None:
        """Last-writer-wins merge on the given key columns.

        Strategy:
          1. Read existing rows (if any) into a temp view.
          2. SELECT existing rows whose keys are NOT in the incoming set
             UNION ALL incoming rows.
          3. COPY that union to part-000.parquet (replacing all parts).

        Cost is O(existing + incoming) — fine for medium workspaces.
        A Plus-tier sink will swap this for a Delta MERGE INTO with
        time travel and statistics-based skip; that's intentionally
        out of scope for OSS v1.0.
        """
        existing_parts = [
            os.path.join(table_dir, f).replace("\\", "/")
            for f in os.listdir(table_dir)
            if f.startswith("part-") and f.endswith(".parquet")
        ]
        if not existing_parts:
            # First write — fall through to replace semantics.
            self._mode_replace(ctx, table_dir)
            return

        # Build the WHERE clause that excludes rows the incoming batch
        # is overwriting. Quoted identifiers + named-tuple subquery for
        # the IN-list — safe against bizarre column names.
        glob = os.path.join(table_dir, "part-*.parquet").replace("\\", "/")
        ctx.conn.execute(
            f"CREATE OR REPLACE TEMP VIEW _lt_existing AS SELECT * FROM read_parquet('{glob}')"
        )
        key_cols = ", ".join(f'"{c}"' for c in merge_on)
        merged = (
            f"SELECT * FROM _lt_existing "
            f"WHERE ({key_cols}) NOT IN (SELECT {key_cols} FROM _lt_sink_input) "
            f"UNION ALL SELECT * FROM _lt_sink_input"
        )
        # Write to a staging part, then rotate.
        staging = os.path.join(table_dir, "part-merge-staging.parquet").replace("\\", "/")
        ctx.conn.sql(f"COPY ({merged}) TO '{staging}' (FORMAT PARQUET)")
        # Drop existing parts; rename staging → part-000.
        for fp in existing_parts:
            try:
                os.remove(fp)
            except OSError as exc:
                logger.warning("merge mode: could not remove %s: %s", fp, exc)
        final = os.path.join(table_dir, "part-000.parquet")
        os.replace(staging, final)
        try:
            ctx.conn.execute("DROP VIEW IF EXISTS _lt_existing")
        except Exception:
            pass

    # ── Metadata refresh ──────────────────────────────────────────────────

    def _refresh_metadata(
        self,
        store: DataStore,
        ctx: ExecutionContext,
        workspace_id: str,
        schema_name: str,
        table_name: str,
        table_dir: str,
        paths,
    ) -> None:
        """Recompute row/column/size counts after the write and upsert
        the storage_tables row. Cheap (Parquet footer scan)."""
        parts = sorted(
            f for f in os.listdir(table_dir)
            if f.startswith("part-") and f.endswith(".parquet")
        )
        if not parts:
            return
        glob = os.path.join(table_dir, "part-*.parquet").replace("\\", "/")

        try:
            row_count = ctx.conn.sql(
                f"SELECT COUNT(*) FROM read_parquet('{glob}', union_by_name=true)"
            ).fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("metadata refresh: row count failed for %s: %s", table_dir, exc)
            row_count = 0
        size_bytes = sum(
            os.path.getsize(os.path.join(table_dir, f)) for f in parts
        )
        try:
            # union_by_name=True so a column added in a later part file
            # (e.g. ``email`` written under schema_policy=add_columns)
            # contributes to the metadata refresh rather than getting
            # dropped to the first-part schema. Tests:
            # test_local_table_schema_policy.py::test_add_columns_applies_*
            rel = ctx.conn.sql(
                f"SELECT * FROM read_parquet('{glob}', union_by_name=true)"
            )
            columns = list(zip(rel.columns, rel.types))
        except Exception as exc:  # noqa: BLE001
            logger.warning("metadata refresh: column scan failed for %s: %s", table_dir, exc)
            columns = []

        rel_dir = paths.relative_to_data_dir(table_dir)

        # Z33 (2026-05-23) — capture Pipeline-Data-Prep provenance.
        # The sink looks at its single upstream step via `_input_step_ids`.
        # If that upstream step is a `data_wrangler`, its `params.steps`
        # list is the prep recipe. The workflow_id + source_object_id
        # come from ExecutionContext (populated by the executor from
        # the workflow IR + scaffold metadata). Three guard rails:
        #   * Falls back to None on every read so a sink invoked outside
        #     a Z1 pipeline (manual promote, ad-hoc write) doesn't crash
        #     or stamp bogus provenance.
        #   * Only records the recipe when the upstream is actually a
        #     Wrangler — a sink fed by a Source directly is not a
        #     prep pipeline, leave the fields as-is.
        #   * Re-write paths preserve a previously-stamped recipe when
        #     the current run doesn't carry one (so a one-off ad-hoc
        #     re-run from the Editor doesn't clobber the prep linkage).
        prep_recipe: list | None = None
        prep_source_object_id: str | None = None
        prep_workflow_id: str | None = None
        try:
            upstream_ids = self.params.get("_input_step_ids") or []
            step_params_map = getattr(ctx, "step_params", {}) or {}
            if upstream_ids:
                up_params = step_params_map.get(upstream_ids[0]) or {}
                up_steps = up_params.get("steps")
                # Heuristic: if the upstream params expose a list under
                # `steps`, treat it as a Wrangler recipe. We avoid a
                # rigid type check because the IR may evolve and a
                # forgiving read keeps the provenance feature
                # forward-compatible.
                if isinstance(up_steps, list):
                    prep_recipe = list(up_steps)
            wf_meta = getattr(ctx, "workflow_metadata", {}) or {}
            src_obj = wf_meta.get("source_object_id")
            if isinstance(src_obj, str) and src_obj:
                prep_source_object_id = src_obj
            wf_id = getattr(ctx, "workflow_id", None)
            if isinstance(wf_id, str) and wf_id:
                prep_workflow_id = wf_id
        except Exception as exc:  # noqa: BLE001
            logger.warning("prep provenance capture skipped: %s", exc)

        existing = store.find_table_by_name(workspace_id, schema_name, table_name)
        if existing:
            existing.row_count = int(row_count)
            existing.column_count = len(columns)
            existing.size_bytes = int(size_bytes)
            existing.part_count = len(parts)
            existing.path = rel_dir
            # Only overwrite prep fields when the current run carries
            # them — otherwise preserve whatever was previously stamped.
            if prep_recipe is not None:
                existing.prep_recipe = prep_recipe
            if prep_source_object_id is not None:
                existing.prep_source_object_id = prep_source_object_id
            if prep_workflow_id is not None:
                existing.prep_workflow_id = prep_workflow_id
            store.save_table(existing)
            table_id = existing.id
        else:
            table = StorageTable(
                workspace_id=workspace_id,
                schema_name=schema_name,
                name=table_name,
                path=rel_dir,
                row_count=int(row_count),
                column_count=len(columns),
                size_bytes=int(size_bytes),
                part_count=len(parts),
                prep_recipe=prep_recipe,
                prep_source_object_id=prep_source_object_id,
                prep_workflow_id=prep_workflow_id,
            )
            store.save_table(table)
            table_id = table.id

        store.save_columns(
            [
                StorageColumn(
                    workspace_id=workspace_id,
                    table_id=table_id,
                    name=name,
                    type=str(typ),
                    nullable=True,
                    ordinal=idx,
                )
                for idx, (name, typ) in enumerate(columns)
            ],
            table_id=table_id,
        )

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "schema_name": "default",
            "table_name": "",
            "mode": SINK_MODE_REPLACE,
            "merge_on": [],
            # Drift-handling default. See fpulse/intelligence/schema_policy.py
            # for the four-value enum and what each value does. Honor user
            # overrides via the Schema tab in the node config panel.
            "schema_policy": DEFAULT_POLICY.value,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "schema_name", "type": "string", "label": "Schema", "default": "default"},
            {"name": "table_name", "type": "string", "label": "Table", "required": True},
            {
                "name": "mode", "type": "string", "label": "Write Mode",
                "default": SINK_MODE_REPLACE,
                "options": [
                    {"value": SINK_MODE_REPLACE, "label": "Replace (drop existing parts)"},
                    {"value": SINK_MODE_APPEND, "label": "Append (new part file)"},
                    {"value": SINK_MODE_MERGE, "label": "Merge (upsert on key)"},
                ],
            },
            {
                "name": "merge_on", "type": "array", "label": "Merge Keys",
                "help": "Required when mode = merge. Column names to match on.",
            },
            schema_policy_param(),
        ]


__all__ = ["LocalTableSinkNode", "LocalTableSourceNode"]
