"""Output node — write results to file (Parquet, CSV, or JSON)."""

from __future__ import annotations

import os
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for the execute() return-type annotation.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


# Generic OutputNode dropped from palette — superseded by format-specific
# sinks (csv_sink, json_sink, parquet via warehouse_sink, etc.). The class is
# kept (unregistered) so existing pipelines that still reference StepType.OUTPUT
# can be loaded if needed.
class OutputNode(BaseNode):
    display_name = "Output"
    category = "output"
    description = "Write data to Parquet, CSV, or JSON"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Output node has no input data")

        source = inputs[0]
        fmt = self.params.get("format", "parquet").lower()
        file_path = self.params.get("file_path", "")

        if not file_path:
            file_path = os.path.join(ctx.data_dir, f"output.{fmt}")
        elif not os.path.isabs(file_path):
            file_path = os.path.join(ctx.data_dir, file_path)

        # Ensure output directory exists
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)

        ctx.conn.register("__output_data", source)

        if fmt == "parquet":
            ctx.conn.sql(f"COPY __output_data TO '{file_path}' (FORMAT PARQUET)")
        elif fmt == "csv":
            ctx.conn.sql(f"COPY __output_data TO '{file_path}' (FORMAT CSV, HEADER)")
        elif fmt == "json":
            ctx.conn.sql(f"COPY __output_data TO '{file_path}' (FORMAT JSON)")
        else:
            raise ValueError(f"Unsupported output format: {fmt}")

        # P0 Day 4 (2026-05-23) — register the output in the storage
        # index immediately so the Storage → Pipeline Outputs tab shows
        # this row without waiting for the next-boot reconciler sweep.
        # Best-effort; reconciler stays as fallback.
        try:
            from fpulse.nodes.sinks import _register_output_in_storage_index
            _register_output_in_storage_index(ctx, file_path, fmt)
        except Exception:
            import logging
            logging.getLogger(__name__).debug(
                "output node: storage-index registration failed", exc_info=True,
            )

        # Return the data as-is for preview
        return source

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"format": "parquet", "file_path": ""}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "format", "type": "select", "label": "Output Format",
             "options": ["parquet", "csv", "json"], "default": "parquet"},
            {"name": "file_path", "type": "text", "label": "File Path (optional)",
             "placeholder": "output/results.parquet"},
        ]
