"""CSV Source node — reads a CSV file into the pipeline."""

from __future__ import annotations

import os
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only needed for the return-type annotation.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register
from fpulse.nodes.guardrails import check_file_size, cap_rows


@register(StepType.CSV_SOURCE)
class CsvSourceNode(BaseNode):
    display_name = "CSV Source"
    category = "source"
    description = "Read data from a CSV file"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        file_path = self.params["file_path"]
        # Resolve relative paths against data_dir, with fallback to
        # project-CWD for sample-pack pipelines (2026-05-26 fix).
        from fpulse.nodes._path_utils import resolve_input_path
        file_path = resolve_input_path(file_path, ctx.data_dir)

        check_file_size(file_path)

        delimiter = self.params.get("delimiter", ",")
        header = self.params.get("header", True)

        rel = ctx.conn.read_csv(
            file_path,
            delimiter=delimiter,
            header=header,
        )
        return cap_rows(rel, label="CSV Source", full_run=ctx.full_run)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"file_path": "", "delimiter": ",", "header": True}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "file_path", "type": "file", "label": "File Path", "required": True},
            {"name": "delimiter", "type": "select", "label": "Delimiter", "options": [",", ";", "\\t", "|"], "default": ","},
            {"name": "header", "type": "boolean", "label": "Has Header", "default": True},
        ]
