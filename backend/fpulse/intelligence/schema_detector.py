"""
Schema Detector — infers schema from CSV, JSON, and XML data.

Uses DuckDB for CSV detection (fast, handles edge cases well).
Custom logic for JSON/XML nested structure analysis.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, TYPE_CHECKING
from xml.etree import ElementTree

# Stage 2.5b: duckdb is RUNTIME-USED in _detect_csv_file (calls
# duckdb.connect). The runtime import lives inside that method.
# Annotations on _analyze_duckdb_relation reference duckdb types but
# are evaluated as strings under `from __future__ import annotations`.
if TYPE_CHECKING:
    import duckdb  # noqa: F401
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ColumnInfo(BaseModel):
    """Detected information about a single column."""
    name: str
    detected_type: str  # string, integer, float, date, boolean, nested_object, nested_array
    nullable: bool = False
    unique_ratio: float = 0.0  # 0.0-1.0
    sample_values: list[Any] = Field(default_factory=list)
    date_format: str | None = None


class RepeatingGroup(BaseModel):
    """A detected repeating group pattern (e.g., G_1, G_2 or items[*])."""
    pattern: str
    count: int
    fields: list[str]


class DetectedSchema(BaseModel):
    """Full schema detection result."""
    source_type: str  # csv, json, xml
    total_rows: int
    total_columns: int
    columns: list[ColumnInfo]
    repeating_groups: list[RepeatingGroup]
    suggested_primary_keys: list[str]
    nested_depth: int = 0  # 0 for flat, 1+ for nested
    flatten_recommended: bool = False
    detected_tables: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Date format detection
# ---------------------------------------------------------------------------

_DATE_FORMATS = [
    ("%Y-%m-%d", r"^\d{4}-\d{2}-\d{2}$"),
    ("%Y-%m-%dT%H:%M:%S", r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$"),
    ("%Y-%m-%dT%H:%M:%SZ", r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"),
    ("%Y-%m-%dT%H:%M:%S.%f", r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+$"),
    ("%Y-%m-%dT%H:%M:%S.%fZ", r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z$"),
    ("%Y-%m-%d %H:%M:%S", r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"),
    ("%m/%d/%Y", r"^\d{2}/\d{2}/\d{4}$"),
    ("%d/%m/%Y", r"^\d{2}/\d{2}/\d{4}$"),
    ("%m-%d-%Y", r"^\d{2}-\d{2}-\d{4}$"),
    ("%d-%m-%Y", r"^\d{2}-\d{2}-\d{4}$"),
    ("%Y%m%d", r"^\d{8}$"),
    ("%B %d, %Y", r"^[A-Z][a-z]+ \d{1,2}, \d{4}$"),
    ("%b %d, %Y", r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$"),
]


def _detect_date_format(values: list[str]) -> str | None:
    """Try to match sample string values against known date formats."""
    non_empty = [v for v in values if v and str(v).strip()]
    if len(non_empty) < 2:
        return None

    for fmt, regex in _DATE_FORMATS:
        matches = 0
        for v in non_empty[:20]:
            s = str(v).strip()
            if re.match(regex, s):
                try:
                    datetime.strptime(s, fmt)
                    matches += 1
                except ValueError:
                    pass
        if matches >= len(non_empty[:20]) * 0.8:
            return fmt
    return None


# ---------------------------------------------------------------------------
# Type inference helpers
# ---------------------------------------------------------------------------

def _infer_type_from_values(values: list[Any]) -> tuple[str, str | None]:
    """Infer column type and optional date format from a list of Python values."""
    non_null = [v for v in values if v is not None]
    if not non_null:
        return "string", None

    type_counts: Counter = Counter()
    str_samples: list[str] = []

    for v in non_null:
        if isinstance(v, bool):
            type_counts["boolean"] += 1
        elif isinstance(v, int):
            type_counts["integer"] += 1
        elif isinstance(v, float):
            type_counts["float"] += 1
        elif isinstance(v, dict):
            type_counts["nested_object"] += 1
        elif isinstance(v, list):
            type_counts["nested_array"] += 1
        elif isinstance(v, str):
            str_samples.append(v)
            # Try to narrow string type
            s = v.strip()
            if s.lower() in ("true", "false", "yes", "no", "1", "0"):
                type_counts["boolean"] += 1
            elif _is_integer(s):
                type_counts["integer"] += 1
            elif _is_float(s):
                type_counts["float"] += 1
            else:
                type_counts["string"] += 1
        else:
            type_counts["string"] += 1

    # Check for date format on string samples
    date_fmt = _detect_date_format(str_samples) if str_samples else None
    if date_fmt and len(str_samples) >= 2:
        return "date", date_fmt

    if not type_counts:
        return "string", None

    # Most common type wins
    dominant = type_counts.most_common(1)[0]
    dominant_type = dominant[0]

    # If integers and floats are mixed, call it float
    if "integer" in type_counts and "float" in type_counts:
        dominant_type = "float"

    return dominant_type, date_fmt if dominant_type == "date" else None


def _is_integer(s: str) -> bool:
    try:
        int(s)
        return True
    except ValueError:
        return False


def _is_float(s: str) -> bool:
    try:
        float(s)
        return "." in s or "e" in s.lower()
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Repeating group detection
# ---------------------------------------------------------------------------

_REPEATING_PATTERNS = [
    # Numbered suffixes: item_1, item_2, ... or G_1, G_2
    re.compile(r"^(.+?)_(\d+)$"),
    # Numbered prefixes (less common): 1_name, 2_name
    re.compile(r"^(\d+)_(.+)$"),
    # Dot-separated: item.1, item.2
    re.compile(r"^(.+?)\.(\d+)$"),
]


def _detect_repeating_groups_flat(column_names: list[str]) -> list[RepeatingGroup]:
    """Detect numbered repeating groups in flat column names."""
    groups: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    for col in column_names:
        for pattern in _REPEATING_PATTERNS:
            m = pattern.match(col)
            if m:
                prefix, idx = m.group(1), m.group(2)
                groups[prefix][idx].append(col)
                break

    results = []
    for prefix, indexed in groups.items():
        if len(indexed) < 2:
            continue
        all_fields = []
        for idx_fields in indexed.values():
            all_fields.extend(idx_fields)
        results.append(RepeatingGroup(
            pattern=f"{prefix}_*",
            count=len(indexed),
            fields=sorted(all_fields),
        ))

    return results


def _detect_repeating_groups_nested(data: dict | list, path: str = "") -> list[RepeatingGroup]:
    """Detect array-based repeating groups in nested JSON."""
    results = []

    if isinstance(data, list) and len(data) > 0:
        if isinstance(data[0], dict):
            fields = list(data[0].keys())
            pattern_name = path if path else "root[*]"
            results.append(RepeatingGroup(
                pattern=f"{pattern_name}[*]",
                count=len(data),
                fields=fields,
            ))
            # Recurse into first element for deeper nesting
            for key, val in data[0].items():
                child_path = f"{path}[*].{key}" if path else f"root[*].{key}"
                results.extend(_detect_repeating_groups_nested(val, child_path))
    elif isinstance(data, dict):
        for key, val in data.items():
            child_path = f"{path}.{key}" if path else key
            if isinstance(val, list):
                results.extend(_detect_repeating_groups_nested(val, child_path))
            elif isinstance(val, dict):
                results.extend(_detect_repeating_groups_nested(val, child_path))

    return results


# ---------------------------------------------------------------------------
# Nesting depth
# ---------------------------------------------------------------------------

def _compute_depth(obj: Any, current: int = 0) -> int:
    """Compute maximum nesting depth of a Python object."""
    if isinstance(obj, dict):
        if not obj:
            return current + 1
        return max(_compute_depth(v, current + 1) for v in obj.values())
    elif isinstance(obj, list):
        if not obj:
            return current + 1
        return max(_compute_depth(item, current + 1) for item in obj[:10])
    return current


# ---------------------------------------------------------------------------
# Detected tables from nested data
# ---------------------------------------------------------------------------

def _extract_tables_from_nested(data: Any, name: str = "root") -> list[dict]:
    """Walk nested JSON and identify table-like structures."""
    tables = []

    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
        columns = list(data[0].keys())
        tables.append({
            "name": name,
            "columns": columns,
            "row_count": len(data),
        })
        # Check nested arrays inside first record
        for key, val in data[0].items():
            if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                child_tables = _extract_tables_from_nested(val, f"{name}_{key}")
                tables.extend(child_tables)
    elif isinstance(data, dict):
        # Single record with nested arrays
        for key, val in data.items():
            if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                child_tables = _extract_tables_from_nested(val, key)
                tables.extend(child_tables)

    return tables


# ---------------------------------------------------------------------------
# SchemaDetector
# ---------------------------------------------------------------------------

class SchemaDetector:
    """Detect schema from CSV, JSON, or XML data."""

    def __init__(self, data_dir: str = "."):
        self.data_dir = data_dir

    def detect(
        self,
        *,
        file_path: str | None = None,
        raw_data: str | None = None,
        source_type: str | None = None,
    ) -> DetectedSchema:
        """Detect schema from a file path or raw data string.

        Args:
            file_path: Path to a CSV/JSON/XML file (relative to data_dir or absolute).
            raw_data: Raw string content (CSV, JSON, or XML).
            source_type: Force source type (csv, json, xml). Auto-detected if not given.

        Returns:
            DetectedSchema with full analysis.
        """
        if file_path and not raw_data:
            resolved = file_path if os.path.isabs(file_path) else os.path.join(self.data_dir, file_path)
            if not os.path.exists(resolved):
                raise FileNotFoundError(f"File not found: {resolved}")

            if not source_type:
                source_type = self._detect_source_type(resolved)

            if source_type == "csv":
                return self._detect_csv_file(resolved)
            elif source_type == "json":
                with open(resolved, "r", encoding="utf-8") as f:
                    raw_data = f.read()
                return self._detect_json(raw_data)
            elif source_type == "xml":
                with open(resolved, "r", encoding="utf-8") as f:
                    raw_data = f.read()
                return self._detect_xml(raw_data)
            else:
                raise ValueError(f"Unsupported source type: {source_type}")

        if raw_data:
            if not source_type:
                source_type = self._detect_source_type_from_content(raw_data)
            if source_type == "csv":
                return self._detect_csv_raw(raw_data)
            elif source_type == "json":
                return self._detect_json(raw_data)
            elif source_type == "xml":
                return self._detect_xml(raw_data)
            else:
                raise ValueError(f"Unsupported source type: {source_type}")

        raise ValueError("Either file_path or raw_data must be provided")

    # ------------------------------------------------------------------
    # CSV detection (via DuckDB)
    # ------------------------------------------------------------------

    def _detect_csv_file(self, file_path: str) -> DetectedSchema:
        """Use DuckDB to sniff and detect CSV schema."""
        import duckdb  # method-scoped (Stage 2.5b)
        conn = duckdb.connect(":memory:")
        try:
            rel = conn.read_csv(file_path)
            return self._analyze_duckdb_relation(conn, rel, "csv")
        finally:
            conn.close()

    def _detect_csv_raw(self, raw_data: str) -> DetectedSchema:
        """Detect schema from raw CSV content using DuckDB."""
        import tempfile
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8")
        try:
            tmp.write(raw_data)
            tmp.close()
            return self._detect_csv_file(tmp.name)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def _analyze_duckdb_relation(
        self,
        conn: duckdb.DuckDBPyConnection,
        rel: duckdb.DuckDBPyRelation,
        source_type: str,
    ) -> DetectedSchema:
        """Analyze a DuckDB relation and produce DetectedSchema."""
        columns_list = rel.columns
        types_list = rel.types

        try:
            total_rows = rel.aggregate("count(*)").fetchone()[0]
        except Exception:
            total_rows = 0

        # Fetch sample for deeper analysis
        try:
            sample_rows = rel.limit(200).fetchall()
        except Exception:
            sample_rows = []

        column_infos: list[ColumnInfo] = []
        for i, col_name in enumerate(columns_list):
            duck_type = str(types_list[i]) if i < len(types_list) else "VARCHAR"

            # Extract column values from sample
            col_values = [row[i] for row in sample_rows]
            non_null = [v for v in col_values if v is not None]
            nullable = len(non_null) < len(col_values)

            # Unique ratio
            unique_count = len(set(str(v) for v in non_null)) if non_null else 0
            unique_ratio = unique_count / max(len(non_null), 1)

            # Map DuckDB type to our types
            detected_type = self._map_duckdb_type(duck_type)

            # Try date format detection on string columns
            date_fmt = None
            if detected_type == "string" and non_null:
                str_vals = [str(v) for v in non_null[:30]]
                date_fmt = _detect_date_format(str_vals)
                if date_fmt:
                    detected_type = "date"

            # Sample values (up to 5)
            samples = []
            seen = set()
            for v in non_null:
                key = str(v)
                if key not in seen:
                    seen.add(key)
                    samples.append(self._safe_value(v))
                    if len(samples) >= 5:
                        break

            column_infos.append(ColumnInfo(
                name=col_name,
                detected_type=detected_type,
                nullable=nullable,
                unique_ratio=round(unique_ratio, 4),
                sample_values=samples,
                date_format=date_fmt,
            ))

        # Detect repeating groups from column names
        repeating = _detect_repeating_groups_flat(columns_list)

        # Suggest primary keys: columns with high uniqueness
        pk_candidates = [
            c.name for c in column_infos
            if c.unique_ratio > 0.95 and c.detected_type in ("string", "integer")
            and total_rows > 1
        ]

        return DetectedSchema(
            source_type=source_type,
            total_rows=total_rows,
            total_columns=len(columns_list),
            columns=column_infos,
            repeating_groups=repeating,
            suggested_primary_keys=pk_candidates,
            nested_depth=0,
            flatten_recommended=len(repeating) > 0,
            detected_tables=[{
                "name": "main",
                "columns": columns_list,
                "row_count": total_rows,
            }],
        )

    # ------------------------------------------------------------------
    # JSON detection (custom)
    # ------------------------------------------------------------------

    def _detect_json(self, raw_data: str) -> DetectedSchema:
        """Detect schema from JSON string."""
        data = json.loads(raw_data)

        # Normalize: if it's a single object, wrap it
        records: list[dict]
        is_array = isinstance(data, list)

        if is_array:
            records = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            # Check for a common wrapper pattern: {"data": [...], "results": [...]}
            array_key = self._find_main_array_key(data)
            if array_key:
                records = data[array_key]
                if not isinstance(records, list):
                    records = [data]
            else:
                records = [data]
        else:
            raise ValueError("JSON must be an object or array of objects")

        if not records:
            return DetectedSchema(
                source_type="json",
                total_rows=0,
                total_columns=0,
                columns=[],
                repeating_groups=[],
                suggested_primary_keys=[],
                nested_depth=0,
                flatten_recommended=False,
            )

        # Collect all keys across all records
        all_keys: dict[str, list[Any]] = defaultdict(list)
        for rec in records[:500]:  # Cap analysis at 500 records
            if not isinstance(rec, dict):
                continue
            for key, val in rec.items():
                all_keys[key].append(val)

        # Build column infos
        column_infos: list[ColumnInfo] = []
        for key, values in all_keys.items():
            detected_type, date_fmt = _infer_type_from_values(values)
            non_null = [v for v in values if v is not None]
            nullable = len(non_null) < len(values)
            unique_count = len(set(str(v) for v in non_null)) if non_null else 0
            unique_ratio = unique_count / max(len(non_null), 1)

            samples = []
            seen = set()
            for v in non_null:
                sv = self._safe_value(v)
                key_str = str(sv)
                if key_str not in seen:
                    seen.add(key_str)
                    samples.append(sv)
                    if len(samples) >= 5:
                        break

            column_infos.append(ColumnInfo(
                name=key,
                detected_type=detected_type,
                nullable=nullable,
                unique_ratio=round(unique_ratio, 4),
                sample_values=samples,
                date_format=date_fmt,
            ))

        # Nesting analysis
        depth = _compute_depth(data)
        repeating = _detect_repeating_groups_nested(data)
        detected_tables = _extract_tables_from_nested(data)

        # Primary key candidates
        total_rows = len(records)
        pk_candidates = [
            c.name for c in column_infos
            if c.unique_ratio > 0.95
            and c.detected_type in ("string", "integer")
            and total_rows > 1
        ]

        # Also detect flat repeating groups from column names
        flat_repeating = _detect_repeating_groups_flat(list(all_keys.keys()))
        all_repeating = repeating + flat_repeating

        flatten_recommended = depth > 1 or any(
            c.detected_type in ("nested_object", "nested_array") for c in column_infos
        )

        return DetectedSchema(
            source_type="json",
            total_rows=total_rows,
            total_columns=len(column_infos),
            columns=column_infos,
            repeating_groups=all_repeating,
            suggested_primary_keys=pk_candidates,
            nested_depth=max(depth - 1, 0),
            flatten_recommended=flatten_recommended,
            detected_tables=detected_tables if detected_tables else [{
                "name": "main",
                "columns": [c.name for c in column_infos],
                "row_count": total_rows,
            }],
        )

    def _find_main_array_key(self, data: dict) -> str | None:
        """Find the key in a JSON object that contains the main data array."""
        # Common patterns: data, results, items, records, rows, entries
        priority_keys = ["data", "results", "items", "records", "rows", "entries", "values"]

        for pk in priority_keys:
            if pk in data and isinstance(data[pk], list) and len(data[pk]) > 0:
                return pk

        # Fallback: any key with a list of dicts
        for key, val in data.items():
            if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                return key

        return None

    # ------------------------------------------------------------------
    # XML detection (custom)
    # ------------------------------------------------------------------

    def _detect_xml(self, raw_data: str) -> DetectedSchema:
        """Detect schema from XML string."""
        root = ElementTree.fromstring(raw_data)

        # Walk XML tree to extract records
        records, record_tag = self._extract_xml_records(root)

        if not records:
            return DetectedSchema(
                source_type="xml",
                total_rows=0,
                total_columns=0,
                columns=[],
                repeating_groups=[],
                suggested_primary_keys=[],
                nested_depth=0,
                flatten_recommended=False,
            )

        # Collect fields from records
        all_keys: dict[str, list[Any]] = defaultdict(list)
        for rec_elem in records[:500]:
            for child in rec_elem:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if len(child) > 0:
                    # Has sub-elements — nested
                    all_keys[tag].append({"_nested": True, "_children": len(child)})
                else:
                    all_keys[tag].append(child.text)

        # Build column infos
        column_infos: list[ColumnInfo] = []
        for key, values in all_keys.items():
            # Check if nested
            if values and isinstance(values[0], dict) and values[0].get("_nested"):
                column_infos.append(ColumnInfo(
                    name=key,
                    detected_type="nested_object",
                    nullable=False,
                    unique_ratio=0.0,
                    sample_values=[],
                ))
                continue

            text_values = [v for v in values if v is not None]
            detected_type, date_fmt = _infer_type_from_values(text_values)
            non_null = [v for v in values if v is not None]
            nullable = len(non_null) < len(values)
            unique_count = len(set(str(v) for v in non_null)) if non_null else 0
            unique_ratio = unique_count / max(len(non_null), 1)

            samples = []
            seen = set()
            for v in non_null[:20]:
                sv = self._safe_value(v)
                key_str = str(sv)
                if key_str not in seen:
                    seen.add(key_str)
                    samples.append(sv)
                    if len(samples) >= 5:
                        break

            column_infos.append(ColumnInfo(
                name=key,
                detected_type=detected_type,
                nullable=nullable,
                unique_ratio=round(unique_ratio, 4),
                sample_values=samples,
                date_format=date_fmt,
            ))

        # Nesting and repeating groups
        depth = self._xml_depth(root)
        repeating = self._detect_xml_repeating_groups(root)
        detected_tables = self._extract_xml_tables(root)
        total_rows = len(records)

        pk_candidates = [
            c.name for c in column_infos
            if c.unique_ratio > 0.95
            and c.detected_type in ("string", "integer")
            and total_rows > 1
        ]

        flatten_recommended = depth > 2 or any(
            c.detected_type in ("nested_object", "nested_array") for c in column_infos
        )

        return DetectedSchema(
            source_type="xml",
            total_rows=total_rows,
            total_columns=len(column_infos),
            columns=column_infos,
            repeating_groups=repeating,
            suggested_primary_keys=pk_candidates,
            nested_depth=max(depth - 2, 0),  # root + record level = not nesting
            flatten_recommended=flatten_recommended,
            detected_tables=detected_tables if detected_tables else [{
                "name": record_tag or "record",
                "columns": [c.name for c in column_infos],
                "row_count": total_rows,
            }],
        )

    def _extract_xml_records(
        self, root: ElementTree.Element
    ) -> tuple[list[ElementTree.Element], str | None]:
        """Find repeating record elements in XML.

        Heuristic: the most frequent direct child tag of root is the record element.
        """
        child_tags: Counter = Counter()
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            child_tags[tag] += 1

        if not child_tags:
            return [], None

        record_tag, count = child_tags.most_common(1)[0]
        if count < 1:
            return [], None

        records = []
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == record_tag:
                records.append(child)

        return records, record_tag

    def _xml_depth(self, elem: ElementTree.Element, current: int = 0) -> int:
        """Compute max depth of an XML tree."""
        if len(elem) == 0:
            return current
        return max(self._xml_depth(child, current + 1) for child in elem)

    def _detect_xml_repeating_groups(self, root: ElementTree.Element) -> list[RepeatingGroup]:
        """Detect repeating groups in XML by looking for repeated child tags."""
        results = []
        self._walk_xml_for_repeating(root, "", results)
        return results

    def _walk_xml_for_repeating(
        self,
        elem: ElementTree.Element,
        path: str,
        results: list[RepeatingGroup],
    ):
        """Recursively walk XML to find array-like structures."""
        child_tags: Counter = Counter()
        for child in elem:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            child_tags[tag] += 1

        for tag, count in child_tags.items():
            if count >= 2:
                child_path = f"{path}/{tag}" if path else tag
                # Gather fields of the first occurrence
                first_child = None
                for child in elem:
                    ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if ctag == tag:
                        first_child = child
                        break
                fields = []
                if first_child is not None:
                    for sub in first_child:
                        stag = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
                        fields.append(stag)
                results.append(RepeatingGroup(
                    pattern=f"{child_path}[*]",
                    count=count,
                    fields=fields if fields else [tag],
                ))

        # Recurse into children
        for child in elem:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            child_path = f"{path}/{tag}" if path else tag
            self._walk_xml_for_repeating(child, child_path, results)

    def _extract_xml_tables(self, root: ElementTree.Element) -> list[dict]:
        """Extract table-like structures from XML."""
        tables = []
        records, record_tag = self._extract_xml_records(root)
        if records:
            first = records[0]
            columns = []
            for child in first:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                columns.append(tag)
            tables.append({
                "name": record_tag or "record",
                "columns": columns,
                "row_count": len(records),
            })

            # Check for nested arrays in the first record
            for child in first:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                sub_count = Counter()
                for sub in child:
                    stag = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
                    sub_count[stag] += 1
                for stag, cnt in sub_count.items():
                    if cnt >= 2:
                        sub_elem = None
                        for s in child:
                            st = s.tag.split("}")[-1] if "}" in s.tag else s.tag
                            if st == stag:
                                sub_elem = s
                                break
                        sub_cols = []
                        if sub_elem is not None:
                            for sc in sub_elem:
                                sctag = sc.tag.split("}")[-1] if "}" in sc.tag else sc.tag
                                sub_cols.append(sctag)
                        tables.append({
                            "name": f"{record_tag}_{tag}_{stag}",
                            "columns": sub_cols if sub_cols else [stag],
                            "row_count": cnt,
                        })

        return tables

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_source_type(file_path: str) -> str:
        ext = os.path.splitext(file_path)[1].lower()
        ext_map = {
            ".csv": "csv", ".tsv": "csv", ".txt": "csv",
            ".json": "json", ".jsonl": "json",
            ".xml": "xml",
        }
        if ext in ext_map:
            return ext_map[ext]
        raise ValueError(f"Cannot determine source type for extension: {ext}")

    @staticmethod
    def _detect_source_type_from_content(raw_data: str) -> str:
        stripped = raw_data.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return "json"
        if stripped.startswith("<?xml") or stripped.startswith("<"):
            return "xml"
        return "csv"

    @staticmethod
    def _map_duckdb_type(duck_type: str) -> str:
        t = duck_type.upper()
        if "INT" in t or t == "BIGINT" or t == "SMALLINT" or t == "TINYINT" or t == "HUGEINT":
            return "integer"
        if "FLOAT" in t or "DOUBLE" in t or "DECIMAL" in t or "NUMERIC" in t:
            return "float"
        if "BOOL" in t:
            return "boolean"
        if "DATE" in t or "TIMESTAMP" in t or "TIME" in t:
            return "date"
        if "STRUCT" in t or "MAP" in t:
            return "nested_object"
        if "LIST" in t or t.endswith("[]"):
            return "nested_array"
        return "string"

    @staticmethod
    def _safe_value(v: Any) -> Any:
        """Convert a value to a JSON-safe representation."""
        if v is None:
            return None
        if isinstance(v, (str, int, float, bool)):
            return v
        if isinstance(v, (dict, list)):
            # Truncate for sample display
            try:
                s = json.dumps(v, default=str)
                if len(s) > 200:
                    return s[:200] + "..."
                return v
            except Exception:
                return str(v)
        return str(v)
