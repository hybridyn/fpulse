"""Pinned tests for the tombstone partition helper (B4, 2026-06-08).

Third backfill milestone from docs/design/backfill-ux-1.2.md.
Foundation only - the per-dialect sink wire-in (B4.1) needs its own
focused session because each dialect has its own MERGE / DELETE
semantics.

Contracts pinned:
  * is_tombstoned handles bool, int, ISO timestamp, string variants
  * Missing / None / empty values → not tombstoned (safe default)
  * partition_tombstones preserves input order in both partitions
  * partition_tombstones short-circuits when column is empty
  * extract_tombstone_keys returns just the key fields
"""
from __future__ import annotations

import pytest

from fpulse.sinks.tombstone import (
    build_delete_sql,
    extract_tombstone_keys,
    flatten_delete_params,
    is_tombstoned,
    partition_tombstones,
)


# ── is_tombstoned predicate ────────────────────────────────────────


class TestIsTombstoned:
    def test_boolean_true(self):
        assert is_tombstoned({"id": 1, "is_deleted": True}, "is_deleted") is True

    def test_boolean_false(self):
        assert is_tombstoned({"id": 1, "is_deleted": False}, "is_deleted") is False

    def test_integer_one(self):
        # postgres int / mssql BIT
        assert is_tombstoned({"id": 1, "is_deleted": 1}, "is_deleted") is True

    def test_integer_zero(self):
        assert is_tombstoned({"id": 1, "is_deleted": 0}, "is_deleted") is False

    def test_iso_timestamp_present(self):
        # deleted_at convention - any timestamp = tombstoned
        row = {"id": 1, "deleted_at": "2026-06-08T10:00:00+00:00"}
        assert is_tombstoned(row, "deleted_at") is True

    def test_string_true_variants(self):
        for v in ("true", "TRUE", "True", "t", "T", "yes", "Yes", "1"):
            assert is_tombstoned({"is_deleted": v}, "is_deleted") is True, v

    def test_string_false_variants(self):
        for v in ("false", "FALSE", "f", "F", "no", "No", "0", ""):
            assert is_tombstoned({"is_deleted": v}, "is_deleted") is False, v

    def test_none_is_not_tombstoned(self):
        # NULL deleted_at = row is alive
        assert is_tombstoned({"deleted_at": None}, "deleted_at") is False

    def test_missing_column_is_not_tombstoned(self):
        # Source doesn't even have the column = treat as alive
        assert is_tombstoned({"id": 1}, "is_deleted") is False

    def test_empty_column_name_short_circuits(self):
        # No tombstone column configured - predicate should return False
        # regardless of row contents
        assert is_tombstoned({"is_deleted": True}, "") is False

    def test_unknown_type_present_is_tombstoned(self):
        # bytes / datetime / arbitrary - presence = tombstoned
        from datetime import datetime
        row = {"deleted_at": datetime(2026, 6, 8)}
        assert is_tombstoned(row, "deleted_at") is True


# ── partition_tombstones split ──────────────────────────────────────


class TestPartitionTombstones:
    def test_empty_input(self):
        live, dead = partition_tombstones([], "is_deleted")
        assert live == [] and dead == []

    def test_no_tombstones(self):
        rows = [{"id": 1, "is_deleted": False}, {"id": 2, "is_deleted": False}]
        live, dead = partition_tombstones(rows, "is_deleted")
        assert len(live) == 2 and len(dead) == 0

    def test_all_tombstones(self):
        rows = [{"id": 1, "is_deleted": True}, {"id": 2, "is_deleted": True}]
        live, dead = partition_tombstones(rows, "is_deleted")
        assert len(live) == 0 and len(dead) == 2

    def test_mixed_preserves_order(self):
        rows = [
            {"id": 1, "is_deleted": False},
            {"id": 2, "is_deleted": True},
            {"id": 3, "is_deleted": False},
            {"id": 4, "is_deleted": True},
            {"id": 5, "is_deleted": False},
        ]
        live, dead = partition_tombstones(rows, "is_deleted")
        assert [r["id"] for r in live] == [1, 3, 5]
        assert [r["id"] for r in dead] == [2, 4]

    def test_empty_column_returns_everything_live(self):
        # Most sources have no tombstone column - fast path
        rows = [{"id": 1, "x": "anything"}, {"id": 2}]
        live, dead = partition_tombstones(rows, "")
        assert live == rows
        assert dead == []

    def test_mixed_types_in_column(self):
        # Source might serialise the flag inconsistently row-to-row
        rows = [
            {"id": 1, "del": True},
            {"id": 2, "del": "false"},
            {"id": 3, "del": 1},
            {"id": 4, "del": 0},
            {"id": 5, "del": "2026-06-08"},
            {"id": 6, "del": None},
        ]
        live, dead = partition_tombstones(rows, "del")
        assert [r["id"] for r in live] == [2, 4, 6]
        assert [r["id"] for r in dead] == [1, 3, 5]

    def test_accepts_generator(self):
        def gen():
            yield {"id": 1, "x": True}
            yield {"id": 2, "x": False}
        live, dead = partition_tombstones(gen(), "x")
        assert len(live) == 1 and len(dead) == 1


# ── extract_tombstone_keys ──────────────────────────────────────────


class TestExtractKeys:
    def test_extracts_single_key(self):
        rows = [
            {"id": 1, "name": "a", "is_deleted": True},
            {"id": 2, "name": "b", "is_deleted": True},
        ]
        keys = extract_tombstone_keys(rows, ["id"])
        assert keys == [{"id": 1}, {"id": 2}]

    def test_extracts_composite_key(self):
        rows = [
            {"customer_id": 10, "order_date": "2026-06-01", "amount": 99},
            {"customer_id": 11, "order_date": "2026-06-02", "amount": 50},
        ]
        keys = extract_tombstone_keys(rows, ["customer_id", "order_date"])
        assert keys == [
            {"customer_id": 10, "order_date": "2026-06-01"},
            {"customer_id": 11, "order_date": "2026-06-02"},
        ]

    def test_empty_key_columns_returns_empty(self):
        # Without a merge key, we can't construct a DELETE WHERE - skip
        assert extract_tombstone_keys([{"id": 1}], []) == []

    def test_empty_rows_returns_empty(self):
        assert extract_tombstone_keys([], ["id"]) == []

    def test_skips_rows_missing_key_columns(self):
        # Operator config error: row doesn't have the declared merge
        # key. The sink wire-in will surface this separately; here we
        # just skip so we don't crash with a KeyError.
        rows = [
            {"id": 1, "is_deleted": True},      # has key
            {"name": "no-id", "is_deleted": True},  # missing 'id'
            {"id": 3, "is_deleted": True},
        ]
        keys = extract_tombstone_keys(rows, ["id"])
        assert keys == [{"id": 1}, {"id": 3}]


# ── Realistic integration scenarios ─────────────────────────────────


class TestBuildDeleteSql:
    def test_single_key_postgres(self):
        sql = build_delete_sql("postgres", "public.orders", ["id"], n_rows=3)
        assert sql == 'DELETE FROM "public"."orders" WHERE "id" IN (%s, %s, %s)'

    def test_single_key_mssql_uses_brackets_and_qmark(self):
        sql = build_delete_sql("mssql", "dbo.orders", ["id"], n_rows=2)
        assert sql == "DELETE FROM [dbo].[orders] WHERE [id] IN (?, ?)"

    def test_single_key_duckdb_qmark(self):
        sql = build_delete_sql("duckdb", "orders", ["id"], n_rows=1)
        assert sql == 'DELETE FROM "orders" WHERE "id" IN (?)'

    def test_single_key_snowflake_pyformat(self):
        sql = build_delete_sql("snowflake", "orders", ["id"], n_rows=1)
        assert sql == 'DELETE FROM "orders" WHERE "id" IN (%s)'

    def test_composite_key_or_of_ands_postgres(self):
        sql = build_delete_sql("postgres", "t", ["a", "b"], n_rows=2)
        assert sql == (
            'DELETE FROM "t" WHERE ("a" = %s AND "b" = %s) '
            'OR ("a" = %s AND "b" = %s)'
        )

    def test_composite_key_mssql_brackets(self):
        sql = build_delete_sql("mssql", "t", ["customer_id", "order_date"], n_rows=1)
        assert sql == (
            "DELETE FROM [t] WHERE ([customer_id] = ? AND [order_date] = ?)"
        )

    def test_identifier_quoting_escapes_delimiter(self):
        # A column named with an embedded quote/bracket must be escaped
        assert '"a""b"' in build_delete_sql("postgres", "t", ['a"b'], n_rows=1)
        assert "[a]]b]" in build_delete_sql("mssql", "t", ["a]b"], n_rows=1)

    def test_unknown_dialect_raises(self):
        with pytest.raises(ValueError):
            build_delete_sql("oracle", "t", ["id"], n_rows=1)

    def test_empty_key_columns_raises(self):
        with pytest.raises(ValueError):
            build_delete_sql("postgres", "t", [], n_rows=1)

    def test_zero_rows_raises(self):
        with pytest.raises(ValueError):
            build_delete_sql("postgres", "t", ["id"], n_rows=0)


class TestFlattenDeleteParams:
    def test_single_key_flatten(self):
        keys = [{"id": 1}, {"id": 2}, {"id": 3}]
        assert flatten_delete_params(keys, ["id"]) == [1, 2, 3]

    def test_composite_key_flatten_row_major(self):
        keys = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
        assert flatten_delete_params(keys, ["a", "b"]) == [1, "x", 2, "y"]

    def test_param_count_matches_placeholders(self):
        # The whole point: flattened params line up 1:1 with the
        # placeholders build_delete_sql emits.
        keys = [{"a": 1, "b": 2}, {"a": 3, "b": 4}, {"a": 5, "b": 6}]
        sql = build_delete_sql("postgres", "t", ["a", "b"], n_rows=len(keys))
        params = flatten_delete_params(keys, ["a", "b"])
        assert sql.count("%s") == len(params) == 6


class TestRealisticScenarios:
    def test_postgres_soft_delete_with_is_deleted(self):
        rows = [
            {"id": 1, "name": "alice", "is_deleted": False},
            {"id": 2, "name": "bob",   "is_deleted": True},
            {"id": 3, "name": "carol", "is_deleted": False},
        ]
        live, dead = partition_tombstones(rows, "is_deleted")
        assert [r["id"] for r in live] == [1, 3]
        assert extract_tombstone_keys(dead, ["id"]) == [{"id": 2}]

    def test_rails_style_deleted_at_timestamp(self):
        rows = [
            {"id": 1, "deleted_at": None},
            {"id": 2, "deleted_at": "2026-06-08T10:00:00Z"},
            {"id": 3, "deleted_at": None},
            {"id": 4, "deleted_at": "2026-06-08T11:00:00Z"},
        ]
        live, dead = partition_tombstones(rows, "deleted_at")
        assert [r["id"] for r in live] == [1, 3]
        assert [r["id"] for r in dead] == [2, 4]

    def test_no_tombstone_configured_is_a_no_op(self):
        # The vast majority of pipelines won't configure a tombstone
        # column. Pin that the partition helper doesn't even inspect
        # row contents in that case.
        rows = [
            {"id": 1, "is_deleted": True},   # would be deleted IF configured
            {"id": 2, "is_deleted": False},
        ]
        live, dead = partition_tombstones(rows, "")
        assert len(live) == 2
        assert len(dead) == 0
