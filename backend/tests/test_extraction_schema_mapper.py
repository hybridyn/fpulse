"""Tests for the JSON path navigator + SchemaMapper + coercions.

The schema mapper is the keystone for handling deeply nested IT-asset
APIs. Anything that breaks here cascades into every connector that
projects nested JSON into flat rows, so the contract is locked down
fairly aggressively.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fpulse.extraction.profile import SchemaProfile
from fpulse.extraction.schema_mapper import (
    SchemaMapper,
    coerce_value,
    get_json_path,
)


# ── JSON path navigation ─────────────────────────────────────────────

def test_simple_dotted_path():
    assert get_json_path({"a": {"b": {"c": 7}}}, "a.b.c") == 7


def test_missing_segment_returns_none():
    assert get_json_path({"a": {"b": {}}}, "a.b.c") is None
    assert get_json_path({"a": {}}, "a.b.c") is None
    assert get_json_path({}, "a") is None


def test_array_index():
    record = {"items": [{"name": "x"}, {"name": "y"}, {"name": "z"}]}
    assert get_json_path(record, "items[0].name") == "x"
    assert get_json_path(record, "items[2].name") == "z"


def test_array_index_out_of_range():
    record = {"items": [{"name": "x"}]}
    assert get_json_path(record, "items[5].name") is None


def test_wildcard_returns_list():
    record = {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}
    assert get_json_path(record, "items[*].id") == [1, 2, 3]


def test_wildcard_with_missing_field_yields_none_in_list():
    """A wildcard expansion produces a list; missing fields per-element
    surface as None in the list rather than dropping the slot."""
    record = {"items": [{"id": 1}, {}, {"id": 3}]}
    assert get_json_path(record, "items[*].id") == [1, None, 3]


def test_default_null():
    assert get_json_path({"a": {}}, "a.b|default=null") is None


def test_default_literal_when_missing():
    assert get_json_path({"a": {}}, "a.b|default=foo") == "foo"


def test_default_int_when_missing():
    assert get_json_path({}, "x.y|default=0") == 0


def test_default_bool_when_missing():
    assert get_json_path({}, "x.y|default=true") is True


def test_default_does_not_apply_when_value_present():
    assert get_json_path({"a": "real"}, "a|default=fallback") == "real"


def test_path_against_non_dict_intermediate():
    """Walking through a string or int (not a dict) must return None,
    not raise — vendor APIs occasionally return inconsistent shapes."""
    assert get_json_path({"a": "string-not-dict"}, "a.b") is None
    assert get_json_path({"a": 42}, "a.b") is None


# ── Coercions ───────────────────────────────────────────────────────

def test_coerce_int():
    assert coerce_value("42", "int") == 42
    assert coerce_value(3.7, "int") == 3
    assert coerce_value(None, "int") is None


def test_coerce_float():
    assert coerce_value("3.14", "float") == 3.14
    assert coerce_value(7, "float") == 7.0


def test_coerce_bool_string_truthy_falsy():
    for v in ("true", "True", "1", "yes", "y", "t"):
        assert coerce_value(v, "bool") is True
    for v in ("false", "False", "0", "no", "n", "f", ""):
        assert coerce_value(v, "bool") is False


def test_coerce_bool_passthrough():
    assert coerce_value(True, "bool") is True
    assert coerce_value(0, "bool") is False
    assert coerce_value(1, "bool") is True


def test_coerce_iso_datetime_with_z():
    """Trailing-Z form is the most common ISO output; must work even
    on Python <3.11 where stdlib fromisoformat rejects it."""
    out = coerce_value("2026-05-09T14:30:00Z", "iso_datetime")
    assert isinstance(out, datetime)
    assert out.tzinfo is not None
    assert out.year == 2026 and out.month == 5 and out.day == 9


def test_coerce_iso_datetime_without_tz_assumes_utc():
    out = coerce_value("2026-05-09T14:30:00", "iso_datetime")
    assert isinstance(out, datetime)
    assert out.tzinfo == timezone.utc


def test_coerce_iso_datetime_unparseable_returns_original():
    """Bad timestamps don't drop rows — the engine logs and continues."""
    assert coerce_value("not-a-date", "iso_datetime") == "not-a-date"


def test_coerce_lower_upper():
    assert coerce_value("HELLO", "lower") == "hello"
    assert coerce_value("hello", "upper") == "HELLO"


def test_coerce_unknown_kind_passes_through():
    assert coerce_value("x", "made_up_kind") == "x"


def test_coerce_invalid_value_passes_through_not_raises():
    """Bad coercion input returns the original value; never raises.
    Whole-row drops would lose more than the engine can afford."""
    assert coerce_value("not_a_number", "int") == "not_a_number"
    assert coerce_value("not_a_number", "float") == "not_a_number"


# ── SchemaMapper end-to-end ─────────────────────────────────────────

def test_schema_mapper_flattens_deeply_nested_record():
    """Mirrors the actual shape we'd see from an IT-asset API —
    several levels of nesting + array indexing + a wildcard list."""
    record = {
        "resource_id": "r-12345",
        "computer_name": "lab-host-01",
        "os_info": {
            "platform_name": "Windows",
            "version": "11",
            "build_number": "22631",
        },
        "hardware": {
            "manufacturer": "Dell",
            "model": "Latitude 7440",
            "memory": {"total_gb": "16"},
            "cpu": {"core_count": "8"},
        },
        "network": {
            "interfaces": [
                {"ip_address": "10.0.0.5"},
                {"ip_address": "192.168.1.5"},
            ],
        },
        "scan_history": [
            {"timestamp": "2026-05-09T14:30:00Z"},
            {"timestamp": "2026-05-08T14:30:00Z"},
        ],
    }
    profile = SchemaProfile(
        field_paths={
            "id":           "resource_id",
            "name":         "computer_name",
            "os":           "os_info.platform_name",
            "memory_gb":    "hardware.memory.total_gb",
            "cpu_cores":    "hardware.cpu.core_count",
            "primary_ip":   "network.interfaces[0].ip_address",
            "all_ips":      "network.interfaces[*].ip_address",
            "last_scan":    "scan_history[0].timestamp",
            "missing_field": "does.not.exist|default=unknown",
        },
        coercions={
            "memory_gb":  "float",
            "cpu_cores":  "int",
            "last_scan":  "iso_datetime",
        },
    )
    mapper = SchemaMapper(profile)
    flat = mapper.flatten(record)

    assert flat["id"] == "r-12345"
    assert flat["name"] == "lab-host-01"
    assert flat["os"] == "Windows"
    assert flat["memory_gb"] == 16.0
    assert flat["cpu_cores"] == 8
    assert flat["primary_ip"] == "10.0.0.5"
    assert flat["all_ips"] == ["10.0.0.5", "192.168.1.5"]
    assert isinstance(flat["last_scan"], datetime)
    assert flat["missing_field"] == "unknown"


def test_schema_mapper_partial_record_yields_nones_with_defaults():
    """Real API responses are inconsistent — some fields missing on
    some records. The mapper must produce a row of the right shape
    every time, with None / default values where data is absent."""
    record = {"resource_id": "r-empty"}
    profile = SchemaProfile(
        field_paths={
            "id":      "resource_id",
            "name":    "computer_name",
            "memory":  "hardware.memory.total_gb|default=0",
        },
        coercions={"memory": "float"},
    )
    flat = SchemaMapper(profile).flatten(record)
    assert flat == {"id": "r-empty", "name": None, "memory": 0.0}


def test_flatten_many_returns_list():
    profile = SchemaProfile(field_paths={"id": "id"})
    mapper = SchemaMapper(profile)
    out = mapper.flatten_many([{"id": 1}, {"id": 2}, {"id": 3}])
    assert out == [{"id": 1}, {"id": 2}, {"id": 3}]
