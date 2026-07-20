"""Deduplicate node — remove duplicate rows based on key columns."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for the execute() return-type annotation.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


@register(StepType.DEDUPLICATE)
class DeduplicateNode(BaseNode):
    display_name = "Deduplicate"
    category = "transform"
    description = "Remove duplicate rows. Pick which columns make a row unique."

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Deduplicate node has no input data")

        source = inputs[0]
        # Accept 'key' (canonical / UI) or 'columns' (natural alias).
        # Use .get() so a missing param yields a clear ValueError rather
        # than a raw KeyError.
        keys = self.params.get("key")
        if not keys:
            keys = self.params.get("columns")
        if isinstance(keys, str):
            keys = [keys]
        if not keys:
            raise ValueError("Deduplicate requires 'key' (or 'columns') parameter")

        strategy = self.params.get("strategy", "keep_first")
        order_by = self.params.get("order_by", "")

        # 2026-06-11 (node-audit fix): keep_last previously fell into
        # `SELECT DISTINCT ON (keys) *`, which keeps an ARBITRARY row and
        # ignores order_by entirely — it was keep_first's evil twin, not
        # keep_last. Both strategies now run through ROW_NUMBER with the
        # parsed order; keep_last simply reverses each direction so
        # "last by created_at ASC" == "first by created_at DESC".
        # Without an order_by, which row survives is engine-arbitrary —
        # the frontend validator warns about that.
        if strategy not in ("keep_first", "keep_last"):
            raise ValueError(
                f"Deduplicate: invalid strategy '{strategy}'. Allowed: keep_first, keep_last."
            )

        available = list(source.columns)
        missing = [k for k in keys if k not in available]
        if missing:
            raise ValueError(
                f"Deduplicate: key column(s) not found: {', '.join(missing)}. "
                f"Available: {', '.join(available)}"
            )

        # Parse "created_at DESC, id" into validated (column, direction)
        # pairs — the raw string used to be spliced into the SQL verbatim.
        order_rules: list[tuple[str, str]] = []
        for tok in str(order_by or "").split(","):
            tok = tok.strip()
            if not tok:
                continue
            parts = tok.split()
            col = parts[0]
            dirn = (parts[1].upper() if len(parts) > 1 else "ASC")
            if len(parts) > 2 or dirn not in ("ASC", "DESC"):
                raise ValueError(
                    f"Deduplicate: invalid order-by entry '{tok}'. Expected '<column> [ASC|DESC]'."
                )
            if col not in available:
                raise ValueError(
                    f"Deduplicate: order-by column '{col}' not found. "
                    f"Available: {', '.join(available)}"
                )
            order_rules.append((col, dirn))

        if strategy == "keep_last":
            order_rules = [
                (col, "ASC" if dirn == "DESC" else "DESC") for col, dirn in order_rules
            ]

        emit_duplicates = bool(self.params.get("emit_duplicates", False))

        dedup_input = ctx.register_scoped("__dedup_input", source)
        key_cols = ", ".join(f'"{k}"' for k in keys)
        order_clause = (
            "ORDER BY " + ", ".join(f'"{c}" {d}' for c, d in order_rules)
            if order_rules
            else ""
        )
        numbered = f"""
            SELECT *, ROW_NUMBER() OVER (PARTITION BY {key_cols} {order_clause}) AS __rn
            FROM {dedup_input}
        """

        # 2026-06-11 (multi-output): dual-output mode. Instead of dropping
        # duplicates, tag every row — the surviving row per key as 'unique',
        # the rest as 'duplicate' — via _split_output, exposing two streams.
        # Downstream wired to the "Unique" handle gets the deduped set; the
        # "Duplicate" handle gets the removed rows (audit / stewardship).
        if emit_duplicates:
            dedup_numbered = ctx.register_scoped("__dedup_numbered", ctx.conn.sql(numbered))
            keep_cols = ", ".join(f'"{c}"' for c in source.columns)
            return ctx.conn.sql(
                f"SELECT {keep_cols}, "
                f"CASE WHEN __rn = 1 THEN 'unique' ELSE 'duplicate' END AS _split_output "
                f"FROM {dedup_numbered}"
            )

        result = ctx.conn.sql(f"SELECT * FROM ({numbered}) WHERE __rn = 1")
        # Remove the helper column
        cols = [c for c in result.columns if c != "__rn"]
        col_list = ", ".join(f'"{c}"' for c in cols)
        dedup_result = ctx.register_scoped("__dedup_result", result)
        return ctx.conn.sql(f"SELECT {col_list} FROM {dedup_result}")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"key": [], "strategy": "keep_first", "order_by": "", "emit_duplicates": False}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "key", "type": "column_list", "label": "Dedup Key Columns", "required": True},
            {"name": "strategy", "type": "select", "label": "Strategy",
             "options": ["keep_first", "keep_last"], "default": "keep_first"},
            {"name": "order_by", "type": "text", "label": "Order By (optional)", "placeholder": "created_at DESC"},
            {"name": "emit_duplicates", "type": "boolean", "label": "Output duplicates separately",
             "default": False,
             "description": "Expose two outputs — Unique (deduped) and Duplicate (removed rows) — instead of dropping duplicates."},
        ]
