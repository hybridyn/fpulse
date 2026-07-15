"""Shared column-mapping helper for sink nodes.

Applies two ConfigPanel-managed step params to a DuckDB relation before
it leaves the node for the writer:

  - ``column_mappings``: ``{source_name: destination_name}``. Source
    columns are projected with ``AS "destination_name"`` so the writer
    sees the renamed schema.
  - ``skipped_columns``: ``list[str]`` of source names to drop from the
    output. Matching is case-insensitive.

The helper is a no-op when both fields are absent or empty, so existing
pipelines that never touched the Mapping tab keep their original
behaviour bit-for-bit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import duckdb


def apply_column_mapping(
    source: "duckdb.DuckDBPyRelation",
    params: dict[str, Any],
) -> "duckdb.DuckDBPyRelation":
    """Return a relation with ``column_mappings`` + ``skipped_columns`` applied.

    Args:
        source: The upstream DuckDB relation about to be written.
        params: The sink node's params dict.

    Returns:
        A new relation with the mapping applied, or ``source`` unchanged
        when no mapping is configured.

    Raises:
        ValueError: If the user has skipped every source column — that's
        almost certainly a configuration mistake, and silently writing
        an empty-schema table would be worse than failing the run.
    """
    mappings = params.get("column_mappings") or {}
    skipped = params.get("skipped_columns") or []
    if not mappings and not skipped:
        return source

    skipped_lower = {str(s).lower() for s in skipped}
    select_exprs: list[str] = []
    for col in source.columns:
        if col.lower() in skipped_lower:
            continue
        dst = mappings.get(col, col)
        if dst == col:
            select_exprs.append(f'"{col}"')
        else:
            select_exprs.append(f'"{col}" AS "{dst}"')

    if not select_exprs:
        raise ValueError(
            "Column mapping skipped every source column — nothing to write. "
            "Restore at least one column on the Mapping tab."
        )

    return source.project(", ".join(select_exprs))
