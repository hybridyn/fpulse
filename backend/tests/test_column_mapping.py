"""apply_column_mapping — sink-side rename + skip contract tests.

Locks the invariants relied on by both DbSinkNode and WarehouseSinkNode:
the helper must be a strict no-op when the Mapping tab is untouched,
must skip case-insensitively, must rename via SELECT-AS, and must fail
loud (not silent) when the user has skipped every column.
"""

from __future__ import annotations

import duckdb
import pytest

from fpulse.nodes._column_mapping import apply_column_mapping


@pytest.fixture
def relation():
    """A 3-column relation we can re-use across tests."""
    conn = duckdb.connect(":memory:")
    return conn.sql(
        "SELECT 1 AS id, 'Alice' AS name, 30 AS age "
        "UNION ALL SELECT 2, 'Bob', 25"
    )


class TestNoOp:
    def test_no_params_returns_source_unchanged(self, relation):
        out = apply_column_mapping(relation, {})
        assert out is relation
        assert out.columns == ['id', 'name', 'age']

    def test_empty_mapping_and_skip_returns_source_unchanged(self, relation):
        out = apply_column_mapping(relation, {"column_mappings": {}, "skipped_columns": []})
        assert out is relation


class TestSkip:
    def test_skipped_column_dropped(self, relation):
        out = apply_column_mapping(relation, {"skipped_columns": ["age"]})
        assert out.columns == ['id', 'name']

    def test_skip_is_case_insensitive(self, relation):
        out = apply_column_mapping(relation, {"skipped_columns": ["AGE", "Name"]})
        assert out.columns == ['id']

    def test_skip_unknown_column_is_silent(self, relation):
        # Skipping a column that doesn't exist is a no-op for that entry —
        # other columns should still flow through untouched.
        out = apply_column_mapping(relation, {"skipped_columns": ["nonexistent"]})
        assert out.columns == ['id', 'name', 'age']

    def test_skipping_every_column_raises(self, relation):
        with pytest.raises(ValueError, match="every source column"):
            apply_column_mapping(relation, {"skipped_columns": ["id", "name", "age"]})


class TestRename:
    def test_mapping_renames_column(self, relation):
        out = apply_column_mapping(relation, {"column_mappings": {"name": "full_name"}})
        assert out.columns == ['id', 'full_name', 'age']

    def test_mapping_to_same_name_is_unchanged(self, relation):
        # User typed the same name back — equivalent to no rename for that column.
        out = apply_column_mapping(relation, {"column_mappings": {"name": "name"}})
        assert out.columns == ['id', 'name', 'age']

    def test_mapping_unknown_source_is_ignored(self, relation):
        # The user may have configured a mapping for a column that no
        # longer exists upstream (schema drift). Don't crash.
        out = apply_column_mapping(relation, {"column_mappings": {"phantom": "ghost"}})
        assert out.columns == ['id', 'name', 'age']

    def test_rename_preserves_row_data(self, relation):
        out = apply_column_mapping(relation, {"column_mappings": {"name": "full_name"}})
        rows = out.fetchall()
        assert rows == [(1, 'Alice', 30), (2, 'Bob', 25)]


class TestCombined:
    def test_skip_and_rename_together(self, relation):
        out = apply_column_mapping(relation, {
            "column_mappings": {"name": "full_name"},
            "skipped_columns": ["age"],
        })
        assert out.columns == ['id', 'full_name']
        assert out.fetchall() == [(1, 'Alice'), (2, 'Bob')]

    def test_skip_takes_precedence_over_rename(self, relation):
        # If the user both renamed AND skipped a column, the skip wins —
        # the column doesn't appear in the output regardless of the rename.
        out = apply_column_mapping(relation, {
            "column_mappings": {"name": "renamed"},
            "skipped_columns": ["name"],
        })
        assert out.columns == ['id', 'age']
