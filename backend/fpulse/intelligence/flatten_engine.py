"""
Flatten Engine — transforms nested JSON/XML into tabular DataFrames.

Uses DuckDB unnest() and JSON functions where possible.
Falls back to recursive Python flattening for complex structures.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from typing import Any, TYPE_CHECKING
from xml.etree import ElementTree

# Stage 2.5b: duckdb is RUNTIME-USED in _flatten_json and _flatten_csv
# (both call duckdb.connect). The runtime imports live inside those
# methods so module import stays cheap for callers that don't flatten.
if TYPE_CHECKING:
    import duckdb  # noqa: F401
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class FlattenedTable(BaseModel):
    """A single table extracted from nested data."""
    name: str
    columns: list[str]
    row_count: int
    sample_data: list[dict] = Field(default_factory=list)
    parent_table: str | None = None
    join_key: str | None = None


class FlattenResult(BaseModel):
    """Result of flattening nested data into multiple tables."""
    tables: list[FlattenedTable]


# ---------------------------------------------------------------------------
# FlattenEngine
# ---------------------------------------------------------------------------

class FlattenEngine:
    """Flatten nested JSON/XML into tabular form."""

    def __init__(self, data_dir: str = ".", sample_limit: int = 100):
        self.data_dir = data_dir
        self.sample_limit = sample_limit

    def flatten(
        self,
        *,
        file_path: str | None = None,
        raw_data: str | None = None,
        source_type: str | None = None,
    ) -> FlattenResult:
        """Flatten nested data from file or raw string.

        Args:
            file_path: Path to JSON/XML file (relative to data_dir or absolute).
            raw_data: Raw string data.
            source_type: Force type (json, xml, csv). Auto-detected if not given.

        Returns:
            FlattenResult with one or more tables.
        """
        if file_path and not raw_data:
            resolved = file_path if os.path.isabs(file_path) else os.path.join(self.data_dir, file_path)
            if not os.path.exists(resolved):
                raise FileNotFoundError(f"File not found: {resolved}")

            if not source_type:
                source_type = self._detect_type(resolved)

            with open(resolved, "r", encoding="utf-8") as f:
                raw_data = f.read()

        if not raw_data:
            raise ValueError("Either file_path or raw_data must be provided")

        if not source_type:
            source_type = self._detect_type_from_content(raw_data)

        if source_type == "json":
            return self._flatten_json(raw_data)
        elif source_type == "xml":
            return self._flatten_xml(raw_data)
        elif source_type == "csv":
            return self._flatten_csv(raw_data, file_path)
        else:
            raise ValueError(f"Unsupported source type: {source_type}")

    # ------------------------------------------------------------------
    # JSON flattening
    # ------------------------------------------------------------------

    def _flatten_json(self, raw_data: str) -> FlattenResult:
        """Flatten nested JSON into multiple tables."""
        data = json.loads(raw_data)

        # Normalize input
        if isinstance(data, dict):
            # Check for wrapper: {"data": [...], "results": [...]}
            main_key = self._find_main_array_key(data)
            if main_key:
                records = data[main_key]
                if not isinstance(records, list):
                    records = [data]
            else:
                records = [data]
        elif isinstance(data, list):
            records = data
        else:
            raise ValueError("JSON must be an object or array")

        # Try DuckDB-based unnest first for simple structures
        duckdb_result = self._try_duckdb_json_flatten(raw_data, records)
        if duckdb_result:
            return duckdb_result

        # Fall back to recursive Python flattening
        return self._recursive_json_flatten(records)

    def _try_duckdb_json_flatten(
        self, raw_data: str, records: list
    ) -> FlattenResult | None:
        """Try to flatten JSON using DuckDB's native JSON support."""
        if not records or not isinstance(records[0], dict):
            return None

        # Check if any top-level fields are arrays of objects (need multi-table)
        has_nested_arrays = False
        for key, val in records[0].items():
            if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                has_nested_arrays = True
                break

        if has_nested_arrays:
            # DuckDB single-table unnest won't capture the multi-table structure well
            return None

        # Simple case: flat or single-level nesting, DuckDB can handle it
        import duckdb  # method-scoped (Stage 2.5b)
        conn = duckdb.connect(":memory:")
        try:
            # Write records as JSON for DuckDB
            import tempfile
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            )
            json.dump(records if isinstance(records, list) else [records], tmp)
            tmp.close()

            try:
                rel = conn.read_json(tmp.name)
                columns = rel.columns
                total = rel.aggregate("count(*)").fetchone()[0]
                sample_rows = rel.limit(self.sample_limit).fetchall()

                sample_data = []
                for row in sample_rows:
                    record = {}
                    for i, col in enumerate(columns):
                        record[col] = _safe_json(row[i])
                    sample_data.append(record)

                return FlattenResult(tables=[
                    FlattenedTable(
                        name="main",
                        columns=columns,
                        row_count=total,
                        sample_data=sample_data,
                    )
                ])
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        except Exception:
            return None
        finally:
            conn.close()

    def _recursive_json_flatten(self, records: list) -> FlattenResult:
        """Recursively flatten JSON records into multiple tables."""
        tables: dict[str, list[dict]] = {}
        self._extract_tables_recursive(records, "main", None, None, tables)

        result_tables = []
        for tbl_name, rows in tables.items():
            if not rows:
                continue
            columns = list(rows[0].keys()) if rows else []
            # Deduplicate columns across all rows
            all_cols = set()
            for row in rows:
                all_cols.update(row.keys())
            columns = sorted(all_cols)

            sample = rows[:self.sample_limit]
            # Determine parent info
            parent = None
            join_key = None
            if tbl_name != "main" and "_parent_id" in all_cols:
                parent = "main"
                join_key = "_parent_id"
                # Check if this is a child of a child
                parts = tbl_name.split("_", 1)
                if len(parts) > 1:
                    potential_parent = parts[0]
                    if potential_parent in tables and potential_parent != tbl_name:
                        parent = potential_parent

            result_tables.append(FlattenedTable(
                name=tbl_name,
                columns=columns,
                row_count=len(rows),
                sample_data=sample,
                parent_table=parent,
                join_key=join_key,
            ))

        return FlattenResult(tables=result_tables)

    def _extract_tables_recursive(
        self,
        records: list,
        table_name: str,
        parent_table: str | None,
        parent_id_field: str | None,
        tables: dict[str, list[dict]],
        parent_id_value: Any = None,
    ):
        """Recursively extract tables from nested JSON records."""
        if table_name not in tables:
            tables[table_name] = []

        for idx, record in enumerate(records):
            if not isinstance(record, dict):
                continue

            flat_row: dict[str, Any] = {}
            row_id = f"{table_name}_{idx}"

            # Add parent foreign key
            if parent_id_field and parent_id_value is not None:
                flat_row["_parent_id"] = parent_id_value

            flat_row["_row_id"] = row_id

            for key, val in record.items():
                if isinstance(val, dict):
                    # Flatten nested object: prefix with parent key
                    for sub_key, sub_val in val.items():
                        if isinstance(sub_val, (dict, list)):
                            flat_row[f"{key}_{sub_key}"] = json.dumps(sub_val, default=str)[:200]
                        else:
                            flat_row[f"{key}_{sub_key}"] = _safe_json(sub_val)
                elif isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                    # Nested array of objects -> separate table
                    child_table = f"{table_name}_{key}"
                    self._extract_tables_recursive(
                        val, child_table, table_name, "_parent_id", tables,
                        parent_id_value=row_id,
                    )
                elif isinstance(val, list):
                    # Simple array -> comma-joined string
                    flat_row[key] = ", ".join(str(v) for v in val)
                else:
                    flat_row[key] = _safe_json(val)

            tables[table_name].append(flat_row)

    # ------------------------------------------------------------------
    # XML flattening
    # ------------------------------------------------------------------

    def _flatten_xml(self, raw_data: str) -> FlattenResult:
        """Flatten XML into tabular form."""
        root = ElementTree.fromstring(raw_data)

        # Find record elements (most frequent child of root)
        child_tags = defaultdict(list)
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            child_tags[tag].append(child)

        if not child_tags:
            return FlattenResult(tables=[])

        # Pick the most frequent child as the record tag
        record_tag = max(child_tags, key=lambda t: len(child_tags[t]))
        record_elements = child_tags[record_tag]

        tables: dict[str, list[dict]] = {}
        tables[record_tag] = []

        for idx, elem in enumerate(record_elements):
            row_id = f"{record_tag}_{idx}"
            flat_row: dict[str, Any] = {"_row_id": row_id}

            for child in elem:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

                if len(child) > 0:
                    # Has sub-elements -> check if repeating (array-like)
                    sub_tags = defaultdict(list)
                    for sub in child:
                        stag = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
                        sub_tags[stag].append(sub)

                    for stag, sub_elems in sub_tags.items():
                        if len(sub_elems) >= 2:
                            # Repeating -> separate table
                            child_table = f"{record_tag}_{tag}_{stag}"
                            if child_table not in tables:
                                tables[child_table] = []
                            for si, sub_elem in enumerate(sub_elems):
                                sub_row: dict[str, Any] = {
                                    "_parent_id": row_id,
                                    "_row_id": f"{child_table}_{idx}_{si}",
                                }
                                for leaf in sub_elem:
                                    ltag = leaf.tag.split("}")[-1] if "}" in leaf.tag else leaf.tag
                                    sub_row[ltag] = leaf.text
                                if not any(k for k in sub_row if not k.startswith("_")):
                                    sub_row["value"] = sub_elem.text
                                tables[child_table].append(sub_row)
                        else:
                            # Single nested object -> flatten with prefix
                            for sub_elem in sub_elems:
                                for leaf in sub_elem:
                                    ltag = leaf.tag.split("}")[-1] if "}" in leaf.tag else leaf.tag
                                    flat_row[f"{tag}_{stag}_{ltag}"] = leaf.text
                                if not list(sub_elem):
                                    flat_row[f"{tag}_{stag}"] = sub_elem.text
                else:
                    # Leaf node
                    flat_row[tag] = child.text

                # Also capture attributes
                for attr_name, attr_val in child.attrib.items():
                    flat_row[f"{tag}@{attr_name}"] = attr_val

            tables[record_tag].append(flat_row)

        # Build result
        result_tables = []
        for tbl_name, rows in tables.items():
            if not rows:
                continue
            all_cols = set()
            for row in rows:
                all_cols.update(row.keys())
            columns = sorted(all_cols)

            parent = None
            join_key = None
            if tbl_name != record_tag and "_parent_id" in all_cols:
                parent = record_tag
                join_key = "_parent_id"

            result_tables.append(FlattenedTable(
                name=tbl_name,
                columns=columns,
                row_count=len(rows),
                sample_data=rows[:self.sample_limit],
                parent_table=parent,
                join_key=join_key,
            ))

        return FlattenResult(tables=result_tables)

    # ------------------------------------------------------------------
    # CSV flattening (for columns with embedded JSON or repeating groups)
    # ------------------------------------------------------------------

    def _flatten_csv(self, raw_data: str, file_path: str | None = None) -> FlattenResult:
        """Flatten CSV — mainly handles embedded JSON columns and repeating groups."""
        import duckdb  # method-scoped (Stage 2.5b)
        conn = duckdb.connect(":memory:")
        try:
            import tempfile
            if file_path and os.path.exists(file_path):
                rel = conn.read_csv(file_path)
            else:
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".csv", delete=False, encoding="utf-8"
                )
                tmp.write(raw_data)
                tmp.close()
                try:
                    rel = conn.read_csv(tmp.name)
                finally:
                    try:
                        os.unlink(tmp.name)
                    except OSError:
                        pass

            columns = rel.columns
            total = rel.aggregate("count(*)").fetchone()[0]
            rows = rel.limit(self.sample_limit).fetchall()

            sample_data = []
            for row in rows:
                record = {}
                for i, col in enumerate(columns):
                    record[col] = _safe_json(row[i])
                sample_data.append(record)

            # Detect repeating column groups and split them
            tables = [FlattenedTable(
                name="main",
                columns=columns,
                row_count=total,
                sample_data=sample_data,
            )]

            # Check for repeating groups in column names (e.g., item_1_name, item_1_qty, item_2_name, ...)
            groups = self._detect_csv_repeating_columns(columns)
            if groups:
                for group_name, group_info in groups.items():
                    group_rows = []
                    for row_data in sample_data:
                        for i in group_info["indices"]:
                            group_row = {"_parent_row": sample_data.index(row_data)}
                            for field in group_info["fields"]:
                                col_name = f"{group_name}_{i}_{field}"
                                if col_name in row_data:
                                    group_row[field] = row_data[col_name]
                            if any(v is not None for k, v in group_row.items() if k != "_parent_row"):
                                group_rows.append(group_row)

                    if group_rows:
                        all_cols = set()
                        for r in group_rows:
                            all_cols.update(r.keys())
                        tables.append(FlattenedTable(
                            name=f"main_{group_name}",
                            columns=sorted(all_cols),
                            row_count=len(group_rows),
                            sample_data=group_rows[:self.sample_limit],
                            parent_table="main",
                            join_key="_parent_row",
                        ))

            return FlattenResult(tables=tables)
        finally:
            conn.close()

    def _detect_csv_repeating_columns(self, columns: list[str]) -> dict[str, dict]:
        """Detect repeating column groups like item_1_name, item_1_qty, item_2_name, item_2_qty."""
        pattern = re.compile(r"^(.+?)_(\d+)_(.+)$")
        groups: dict[str, dict[str, set]] = defaultdict(lambda: {"indices": set(), "fields": set()})

        for col in columns:
            m = pattern.match(col)
            if m:
                prefix, idx, field = m.group(1), m.group(2), m.group(3)
                groups[prefix]["indices"].add(idx)
                groups[prefix]["fields"].add(field)

        # Only keep groups with 2+ indices
        return {
            k: {"indices": sorted(v["indices"]), "fields": sorted(v["fields"])}
            for k, v in groups.items()
            if len(v["indices"]) >= 2
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_main_array_key(self, data: dict) -> str | None:
        priority = ["data", "results", "items", "records", "rows", "entries", "values"]
        for pk in priority:
            if pk in data and isinstance(data[pk], list) and len(data[pk]) > 0:
                return pk
        for key, val in data.items():
            if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                return key
        return None

    @staticmethod
    def _detect_type(file_path: str) -> str:
        ext = os.path.splitext(file_path)[1].lower()
        return {"csv": "csv", ".csv": "csv", ".tsv": "csv", ".json": "json", ".xml": "xml"}.get(
            ext, "csv"
        )

    @staticmethod
    def _detect_type_from_content(raw_data: str) -> str:
        stripped = raw_data.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return "json"
        if stripped.startswith("<?xml") or stripped.startswith("<"):
            return "xml"
        return "csv"


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _safe_json(v: Any) -> Any:
    """Make a value JSON-serializable."""
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    try:
        json.dumps(v, default=str)
        return v
    except (TypeError, ValueError):
        return str(v)
