"""Data preview — always LIMIT 50, summarized, never raw dumps."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for the preview_relation parameter
# annotation. The relation object itself is passed in from callers.
if TYPE_CHECKING:
    import duckdb


def preview_relation(relation: duckdb.DuckDBPyRelation, limit: int = 50) -> dict[str, Any]:
    """Generate a preview of a DuckDB relation.

    Returns:
        {
            "columns": ["col1", "col2", ...],
            "schema_info": [{"name": "col1", "type": "VARCHAR", "nullable": true}, ...],
            "sample_data": [{"col1": "val", "col2": 123}, ...],
            "total_rows": 12345,
            "total_columns": 5,
            "preview_rows": 50,
            "truncated": true
        }
    """
    columns = relation.columns
    types = relation.types

    # Get total row count
    try:
        total_rows = relation.aggregate("count(*)").fetchone()[0]
    except Exception:
        total_rows = -1

    # Fetch sample rows
    try:
        sample = relation.limit(limit).fetchall()
    except Exception:
        sample = []

    sample_data = []
    for row in sample:
        record = {}
        for i, col in enumerate(columns):
            val = row[i]
            # Ensure JSON-serializable
            if isinstance(val, (bytes, bytearray)):
                record[col] = val.hex()
            elif val is None:
                record[col] = None
            else:
                try:
                    record[col] = val if isinstance(val, (str, int, float, bool)) else str(val)
                except Exception:
                    record[col] = str(val)
        sample_data.append(record)

    schema_info = []
    for i, col in enumerate(columns):
        type_str = str(types[i]) if i < len(types) else "UNKNOWN"
        schema_info.append({
            "name": col,
            "type": type_str,
            "nullable": True,  # DuckDB doesn't expose this easily
        })

    return {
        "columns": columns,
        "schema_info": schema_info,
        "sample_data": sample_data,
        "total_rows": total_rows,
        "total_columns": len(columns),
        "preview_rows": len(sample_data),
        "truncated": total_rows > limit if total_rows >= 0 else False,
    }
