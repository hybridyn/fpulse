"""Slowly-Changing Dimension Type 2 (SCD2) node — Sprint 1 / Gate 1.

Tracks historical versions of dimension rows: every change to a tracked
column closes out the previous version (`is_current=false`,
`valid_to=run_time`) and inserts a new one (`is_current=true`,
`valid_from=run_time`, `valid_to=null_high_water`).

Inputs (one or two):
  1. **incoming**  (required) — the source-of-truth feed for this run.
     Must contain the business key + every tracked column. Extra
     columns are kept and treated as additional tracked columns unless
     listed in `passthrough_columns`.
  2. **current_target**  (optional) — the SCD2 dimension table as it
     stands today. Same shape as the node's output. When absent, the
     node behaves as the initial load: every incoming row is a new
     version with `valid_from = run_time` and `is_current = true`.

Output:  the FULL new state of the SCD2 dimension table — historical
versions (untouched), closed-out versions (updated valid_to + is_current),
new versions (just inserted). The downstream sink is expected to do a
truncate-and-replace OR a merge keyed by `surrogate_key_column`. Picking
the multi-stream variant (separate Insert / Update streams routed to one
sink) is deferred to a follow-up — most production tools handle SCD2 as
a single-state-emit and let the sink pick the write strategy.

  IMPORTANT — this node is RELATION-PRODUCING, not write-performing.
  It does NOT issue any UPDATE / DELETE statement against an external
  table. All change tracking is computed in-memory; the sink is
  solely responsible for committing the new state. Reviewers who
  expect SCD2 to "soft-close" rows out-of-band against a live target
  are reading the wrong abstraction — close-out is rendered into the
  emitted relation (is_current=false rows with valid_to=run_time);
  the sink writes those rows back.

Idempotency: surrogate key is `sha256(business_key||valid_from)` so
re-running the same input on the same run timestamp produces an
identical surrogate key set. The hash-based change detection means a
no-op input (every business key already at its current version) is a
zero-write outcome.

Params (see DESIGN_SPRINT1_BULK_LOADERS.md §"SCD Type 2"):

    business_key:           list[str]                # required
    tracked_columns:        list[str]                # required, ≥1
    effective_from_column:  str = "valid_from"
    effective_to_column:    str = "valid_to"
    current_flag_column:    str = "is_current"
    surrogate_key_column:   str = "scd_id"
    null_high_water:        str = "9999-12-31"       # ISO date for "currently active"
    passthrough_columns:    list[str] = []           # carried but NOT hashed
    run_time:               str | None = None        # ISO timestamp; default: now()
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Reusable hash helper
# ─────────────────────────────────────────────────────────────────────


def row_hash(values: list[Any]) -> str:
    """SHA-256 of a stable string concatenation of `values`.

    Public/reusable so SCD3, change-detection-only transforms, and
    eventual streaming-incremental nodes can share the same scheme.

    Stability rules — keep these consistent so re-runs match:
      * None → empty string
      * bool → '1'/'0'
      * everything else → str(...)
      * separator: NUL byte (0x00) — far less likely to appear inside
        a real-world value than '|' or ',' so collisions don't sneak in.
    """
    parts: list[str] = []
    for v in values:
        if v is None:
            parts.append("")
        elif isinstance(v, bool):
            parts.append("1" if v else "0")
        else:
            parts.append(str(v))
    blob = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _surrogate_key(business_key_values: list[Any], valid_from: str) -> str:
    """Deterministic surrogate key from business key + valid_from. Re-running
    the same merge with the same run_time yields identical keys."""
    return row_hash([*business_key_values, valid_from])


# ─────────────────────────────────────────────────────────────────────
# Node
# ─────────────────────────────────────────────────────────────────────


@register(StepType.SCD2)
class SCD2Node(BaseNode):
    """SCD Type 2 — track historical versions per business key."""

    display_name = "SCD Type 2"
    category = "transform"
    description = (
        "Maintain a Type-2 slowly-changing dimension. Tracks historical "
        "versions per business key; emits the full new dimension state."
    )

    # ── UI metadata ──────────────────────────────────────────────────

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "business_key": [],
            "tracked_columns": [],
            "effective_from_column": "valid_from",
            "effective_to_column": "valid_to",
            "current_flag_column": "is_current",
            "surrogate_key_column": "scd_id",
            "null_high_water": "9999-12-31",
            "passthrough_columns": [],
            # 2026-05-18: explicit handling of business keys that vanish
            # from the incoming feed. Default `ignore` preserves prior
            # behavior (orphan rows stay is_current=true). `soft_close`
            # closes them out the same way a tracked-column change does.
            "delete_detection": "ignore",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "business_key",
             "type": "list[str]",
             "required": True,
             "description": "Logical primary key — column(s) identifying a single dimension entity."},
            {"name": "tracked_columns",
             "type": "list[str]",
             "required": True,
             "description": "Columns whose changes trigger a new version."},
            {"name": "effective_from_column",
             "type": "str", "default": "valid_from",
             "description": "Column name for the version-start timestamp."},
            {"name": "effective_to_column",
             "type": "str", "default": "valid_to",
             "description": "Column name for the version-end timestamp."},
            {"name": "current_flag_column",
             "type": "str", "default": "is_current",
             "description": "Boolean column flagging the latest active version."},
            {"name": "surrogate_key_column",
             "type": "str", "default": "scd_id",
             "description": "Deterministic SHA-256 surrogate key column."},
            {"name": "null_high_water",
             "type": "str", "default": "9999-12-31",
             "description": "Sentinel value placed in valid_to for the active version."},
            {"name": "passthrough_columns",
             "type": "list[str]", "default": [],
             "description": "Columns carried through but NOT used for change detection."},
            {"name": "delete_detection",
             "type": "str", "default": "ignore",
             "options": ["ignore", "soft_close"],
             "description": "When a business key in the current dimension is missing from "
                            "the incoming feed: 'ignore' keeps the row as-is (orphan), "
                            "'soft_close' marks it is_current=false + valid_to=run_time."},
        ]

    # ── Param parsing ────────────────────────────────────────────────

    def _params(self) -> dict[str, Any]:
        p = self.params
        business_key = p.get("business_key") or []
        tracked = p.get("tracked_columns") or []
        if not business_key:
            raise ValueError("SCD2: 'business_key' must be a non-empty list of column names")
        if not tracked:
            raise ValueError("SCD2: 'tracked_columns' must be a non-empty list of column names")
        if not isinstance(business_key, list):
            business_key = [business_key]
        if not isinstance(tracked, list):
            tracked = [tracked]
        return {
            "business_key": [str(c) for c in business_key],
            "tracked_columns": [str(c) for c in tracked],
            "effective_from_column": str(p.get("effective_from_column", "valid_from")),
            "effective_to_column": str(p.get("effective_to_column", "valid_to")),
            "current_flag_column": str(p.get("current_flag_column", "is_current")),
            "surrogate_key_column": str(p.get("surrogate_key_column", "scd_id")),
            "null_high_water": str(p.get("null_high_water", "9999-12-31")),
            "passthrough_columns": [str(c) for c in (p.get("passthrough_columns") or [])],
            "delete_detection": str(p.get("delete_detection", "ignore")).lower(),
            "run_time": p.get("run_time"),  # may be None → use now
        }

    # ── Helpers ──────────────────────────────────────────────────────

    def _now_iso(self) -> str:
        # Drop microseconds — the same run executing two SCD2 nodes back-to-back
        # should produce the same valid_from across them so surrogate keys align.
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _as_row_dicts(self, rel) -> tuple[list[str], list[dict[str, Any]]]:
        """Materialize a DuckDB relation as (columns, list-of-row-dicts).

        SCD2 is implemented in Python rather than pure SQL because the
        change-detection logic + multi-version emission is far clearer
        as imperative code. For dimension tables (typically <1M rows in
        practice) this is the right trade-off; if a customer hits a
        scaling cliff, the follow-up version can rewrite hot paths in
        DuckDB SQL.
        """
        cols = list(rel.columns)
        rows = rel.fetchall()
        return cols, [dict(zip(cols, r)) for r in rows]

    # ── Main path ────────────────────────────────────────────────────

    def execute(self, ctx: ExecutionContext) -> "duckdb.DuckDBPyRelation":
        cfg = self._params()
        bk = cfg["business_key"]
        tracked = cfg["tracked_columns"]
        passthrough = cfg["passthrough_columns"]
        col_from = cfg["effective_from_column"]
        col_to = cfg["effective_to_column"]
        col_current = cfg["current_flag_column"]
        col_sk = cfg["surrogate_key_column"]
        null_hi = cfg["null_high_water"]
        run_time = str(cfg["run_time"]) if cfg["run_time"] else self._now_iso()

        input_step_ids = self.params.get("_input_step_ids") or []
        inputs = ctx.get_inputs(input_step_ids)
        if not inputs:
            raise ValueError("SCD2 node has no input data")

        # First input = incoming; second (optional) = current target snapshot.
        incoming_rel = inputs[0]
        target_rel = inputs[1] if len(inputs) >= 2 else None

        in_cols, in_rows = self._as_row_dicts(incoming_rel)

        # Validate that bk + tracked columns are present on incoming.
        missing = [c for c in bk + tracked if c not in in_cols]
        if missing:
            raise ValueError(
                f"SCD2: incoming relation is missing required columns: {missing}"
            )

        target_cols: list[str] = []
        target_rows: list[dict[str, Any]] = []
        if target_rel is not None:
            target_cols, target_rows = self._as_row_dicts(target_rel)

        # Build the full output column list. Order:
        #   surrogate_key, business_key..., tracked..., passthrough...,
        #   valid_from, valid_to, is_current
        # Anything in the target that we don't recognise is dropped from the
        # output (operator should make passthrough explicit).
        output_cols: list[str] = (
            [col_sk] + bk + tracked + passthrough +
            [col_from, col_to, col_current]
        )

        # Index existing target rows by business key, separating "current"
        # from historical so we can update the current ones without
        # rewriting history.
        bk_to_history: dict[tuple, list[dict[str, Any]]] = {}
        bk_to_current: dict[tuple, dict[str, Any]] = {}
        if target_rows:
            for row in target_rows:
                key = tuple(row.get(c) for c in bk)
                if row.get(col_current):
                    if key in bk_to_current:
                        # Defensive: more than one is_current row per BK.
                        # Keep the latest valid_from; demote the rest.
                        prev = bk_to_current[key]
                        if str(row.get(col_from, "")) > str(prev.get(col_from, "")):
                            bk_to_history.setdefault(key, []).append(prev)
                            bk_to_current[key] = row
                        else:
                            bk_to_history.setdefault(key, []).append(row)
                    else:
                        bk_to_current[key] = row
                else:
                    bk_to_history.setdefault(key, []).append(row)

        # Compute the new state. For each incoming row:
        #   - new business key       → insert a fresh current version
        #   - existing, hash matches → keep the current as-is (no write)
        #   - existing, hash differs → close out current + insert new
        emitted: list[dict[str, Any]] = []
        seen_bks: set[tuple] = set()

        for row in in_rows:
            key = tuple(row.get(c) for c in bk)
            seen_bks.add(key)
            new_hash = row_hash([row.get(c) for c in tracked])
            current = bk_to_current.get(key)
            if current is None:
                # Brand-new business key.
                emitted.append(self._make_current_row(
                    row, bk, tracked, passthrough,
                    col_sk, col_from, col_to, col_current,
                    valid_from=run_time, valid_to=null_hi,
                ))
                continue

            current_hash = row_hash([current.get(c) for c in tracked])
            if current_hash == new_hash:
                # Same content — keep the existing current version untouched.
                emitted.append(self._project(current, output_cols))
                continue

            # Change detected: close out the existing current and open a new one.
            closed = dict(current)
            closed[col_current] = False
            closed[col_to] = run_time
            emitted.append(self._project(closed, output_cols))
            emitted.append(self._make_current_row(
                row, bk, tracked, passthrough,
                col_sk, col_from, col_to, col_current,
                valid_from=run_time, valid_to=null_hi,
            ))

        # Business keys that exist in the target but NOT in incoming.
        # Default `ignore` keeps current and historical untouched (orphan
        # rows stay is_current=true). `soft_close` closes them out the
        # same way a tracked-column change does: is_current=false +
        # valid_to=run_time. This matches the "Type 2 with deletes"
        # variant common in finance / regulatory dimensions where a
        # vanishing business key is itself a tracked event.
        delete_detection = cfg["delete_detection"]
        for key, current in bk_to_current.items():
            if key in seen_bks:
                continue
            if delete_detection == "soft_close":
                closed = dict(current)
                closed[col_current] = False
                closed[col_to] = run_time
                emitted.append(self._project(closed, output_cols))
            else:
                # `ignore` (default) — keep the orphan untouched.
                emitted.append(self._project(current, output_cols))

        # Always replay history (closed versions older than the latest current).
        for key, history in bk_to_history.items():
            for h in history:
                emitted.append(self._project(h, output_cols))

        return self._materialize(ctx, output_cols, emitted)

    # ── Construction helpers ─────────────────────────────────────────

    def _make_current_row(
        self,
        incoming_row: dict[str, Any],
        bk: list[str],
        tracked: list[str],
        passthrough: list[str],
        col_sk: str,
        col_from: str,
        col_to: str,
        col_current: str,
        *,
        valid_from: str,
        valid_to: str,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        out[col_sk] = _surrogate_key(
            [incoming_row.get(c) for c in bk], valid_from,
        )
        for c in bk:
            out[c] = incoming_row.get(c)
        for c in tracked:
            out[c] = incoming_row.get(c)
        for c in passthrough:
            if c in incoming_row:
                out[c] = incoming_row.get(c)
        out[col_from] = valid_from
        out[col_to] = valid_to
        out[col_current] = True
        return out

    def _project(self, row: dict[str, Any], output_cols: list[str]) -> dict[str, Any]:
        """Pick exactly the output columns from `row`, filling missing with None."""
        return {c: row.get(c) for c in output_cols}

    # ── Materialization back to DuckDB ───────────────────────────────

    def _materialize(
        self,
        ctx: ExecutionContext,
        output_cols: list[str],
        rows: list[dict[str, Any]],
    ) -> "duckdb.DuckDBPyRelation":
        """Write `rows` back into a DuckDB relation the executor can return.

        We use VALUES (...) for tiny outputs and a temp table for larger
        ones — both produce the same columns + types under DuckDB's
        type-inference. Passing through DuckDB rather than handing
        downstream nodes a Python list-of-dicts keeps the rest of the
        executor unchanged.
        """
        if not rows:
            # Empty result with the right column names so downstream nodes
            # (sinks especially) don't see a schema-less relation.
            empty_select = ", ".join(
                f"CAST(NULL AS VARCHAR) AS {self._sql_ident(c)}" for c in output_cols
            )
            return ctx.conn.sql(f"SELECT {empty_select} WHERE FALSE")

        # Use a temp table built from row dicts via DuckDB's `register` of a
        # PyArrow / pandas-like object would normally be the cleanest path,
        # but we want to avoid hard pyarrow/pandas deps just for this. So we
        # build a CREATE TABLE … AS SELECT … FROM (VALUES …) statement.
        # For determinism, we write each row's values in `output_cols` order.
        col_idents = [self._sql_ident(c) for c in output_cols]
        col_list = ", ".join(col_idents)

        values_clauses: list[str] = []
        for row in rows:
            cells: list[str] = []
            for c in output_cols:
                cells.append(self._sql_literal(row.get(c)))
            values_clauses.append("(" + ", ".join(cells) + ")")

        # DuckDB caps a single VALUES clause at a few thousand entries before
        # parser cost gets noticeable; chunk if needed.
        CHUNK = 1000
        temp_table = "__scd2_out"
        ctx.conn.execute(f"DROP TABLE IF EXISTS {temp_table}")

        first_chunk = values_clauses[:CHUNK]
        sql_first = (
            f"CREATE TEMP TABLE {temp_table} AS "
            f"SELECT * FROM (VALUES {', '.join(first_chunk)}) AS t({col_list})"
        )
        ctx.conn.execute(sql_first)

        for i in range(CHUNK, len(values_clauses), CHUNK):
            chunk = values_clauses[i: i + CHUNK]
            sql_chunk = (
                f"INSERT INTO {temp_table} "
                f"SELECT * FROM (VALUES {', '.join(chunk)}) AS t({col_list})"
            )
            ctx.conn.execute(sql_chunk)

        return ctx.conn.sql(f"SELECT {col_list} FROM {temp_table}")

    # ── SQL escapers ─────────────────────────────────────────────────

    def _sql_ident(self, name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    def _sql_literal(self, v: Any) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, (int, float)):
            return str(v)
        s = str(v).replace("'", "''")
        return f"'{s}'"
