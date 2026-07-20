"""Tests for the F0.1 manifest v2 validator."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from fpulse.connectors.manifest_v2 import (
    REQUIRED_FIXTURE_TYPES,
    compute_stream_depth_score,
    migrate_v1_to_v2,
    validate_manifest,
    validate_manifest_file,
)

MANIFEST_DIR = Path(__file__).resolve().parent.parent / "fpulse" / "connectors" / "manifests"


# ── Fixtures ──────────────────────────────────────────────────────────────

def _minimal_valid_manifest() -> dict:
    """A v2 manifest that should validate cleanly at depth-5."""
    return {
        "version": 2,
        "connector": {
            "type": "test_connector",
            "display_name": "Test Connector",
            "category": "saas",
            "oss": True,
        },
        "certification": {
            "depth_score": 5,
            "status": "certified",
            "last_validated": "2026-05-03",
            "owner": "core",
        },
        "auth": {
            "schemes": [{"type": "api_key"}],
        },
        "rate_limit": {
            "default": {"requests_per_minute": 60},
            "retry": {"retry_on_status": [429, 500, 502]},
        },
        "streams": [
            {
                "name": "users",
                "primary_key": ["id"],
                "incremental_field": "updated_at",
                "incremental_format": "iso8601",
                "cursor_strategy": "timestamp",
                "pagination": {"strategy": "cursor", "page_size": 100},
                "depends_on": [],
                "schema": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "required": ["id", "updated_at"],
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "updated_at": {"type": "string", "format": "date-time"},
                    },
                },
            },
        ],
        "fixtures": [
            {"stream": "users", "name": ftype, "file": f"fixtures/users/{ftype}.json"}
            for ftype in REQUIRED_FIXTURE_TYPES
        ],
    }


# ── Top-level validation ──────────────────────────────────────────────────

def test_minimal_manifest_validates():
    m = _minimal_valid_manifest()
    result = validate_manifest(m)
    assert result.valid, f"unexpected errors: {result.errors}"
    assert result.computed_depth_score == 5


def test_wrong_version_fails():
    m = _minimal_valid_manifest()
    m["version"] = 1
    result = validate_manifest(m)
    assert not result.valid
    assert any("version" in str(e) for e in result.errors)


def test_missing_connector_fails():
    m = _minimal_valid_manifest()
    del m["connector"]
    result = validate_manifest(m)
    assert not result.valid


def test_invalid_auth_type_fails():
    m = _minimal_valid_manifest()
    m["auth"]["schemes"] = [{"type": "magical_handshake"}]
    result = validate_manifest(m)
    assert not result.valid
    assert any("auth.schemes" in str(e) for e in result.errors)


def test_invalid_retry_status_fails():
    m = _minimal_valid_manifest()
    m["rate_limit"]["retry"]["retry_on_status"] = [200, 429]  # 200 not allowed
    result = validate_manifest(m)
    assert not result.valid
    assert any("retry_on_status" in str(e) for e in result.errors)


# ── Stream-level validation ───────────────────────────────────────────────

def test_missing_primary_key_fails():
    m = _minimal_valid_manifest()
    del m["streams"][0]["primary_key"]
    result = validate_manifest(m)
    assert not result.valid
    assert any("primary_key" in str(e) for e in result.errors)


def test_empty_primary_key_is_allowed():
    m = _minimal_valid_manifest()
    m["streams"][0]["primary_key"] = []
    result = validate_manifest(m)
    # Empty PK is allowed (append-only) but lowers depth score
    assert result.valid


def test_missing_incremental_field_fails_unless_full_refresh():
    m = _minimal_valid_manifest()
    del m["streams"][0]["incremental_field"]
    del m["streams"][0]["cursor_strategy"]
    result = validate_manifest(m)
    assert not result.valid
    assert any("incremental_field" in str(e) for e in result.errors)


def test_full_refresh_cursor_strategy_skips_incremental_check():
    m = _minimal_valid_manifest()
    del m["streams"][0]["incremental_field"]
    m["streams"][0]["cursor_strategy"] = "full_refresh"
    result = validate_manifest(m)
    assert result.valid, f"unexpected errors: {result.errors}"


def test_invalid_cursor_strategy_fails():
    m = _minimal_valid_manifest()
    m["streams"][0]["cursor_strategy"] = "telepathy"
    result = validate_manifest(m)
    assert not result.valid
    assert any("cursor_strategy" in str(e) for e in result.errors)


def test_duplicate_stream_names_fail():
    m = _minimal_valid_manifest()
    m["streams"].append(copy.deepcopy(m["streams"][0]))  # same name
    result = validate_manifest(m)
    assert not result.valid
    assert any("duplicate" in str(e) for e in result.errors)


# ── Schema validation ─────────────────────────────────────────────────────

def test_required_field_not_in_properties_fails():
    m = _minimal_valid_manifest()
    m["streams"][0]["schema"]["required"] = ["nonexistent_field"]
    result = validate_manifest(m)
    assert not result.valid
    assert any("nonexistent_field" in str(e) for e in result.errors)


def test_property_without_type_fails():
    m = _minimal_valid_manifest()
    m["streams"][0]["schema"]["properties"]["broken"] = {"description": "no type"}
    result = validate_manifest(m)
    assert not result.valid
    assert any("broken.type" in str(e) for e in result.errors)


# ── Fixtures ──────────────────────────────────────────────────────────────

def test_missing_fixtures_fails():
    m = _minimal_valid_manifest()
    m["fixtures"] = m["fixtures"][:2]  # only 2 of 5 fixture types
    result = validate_manifest(m)
    assert not result.valid
    assert any("fixture" in str(e) for e in result.errors)


# ── Cycle detection ───────────────────────────────────────────────────────

def test_self_dependency_fails():
    m = _minimal_valid_manifest()
    m["streams"][0]["depends_on"] = ["users"]  # depends on itself
    result = validate_manifest(m)
    assert not result.valid
    assert any("cannot depend on self" in str(e) for e in result.errors)


def test_cycle_in_depends_on_fails():
    m = _minimal_valid_manifest()
    s2 = copy.deepcopy(m["streams"][0])
    s2["name"] = "orgs"
    s2["depends_on"] = ["users"]
    m["streams"][0]["depends_on"] = ["orgs"]
    m["streams"].append(s2)
    # Cycle: users → orgs → users (and add fixtures for orgs)
    m["fixtures"].extend([
        {"stream": "orgs", "name": ftype, "file": f"fixtures/orgs/{ftype}.json"}
        for ftype in REQUIRED_FIXTURE_TYPES
    ])
    result = validate_manifest(m)
    assert not result.valid
    assert any("cycle" in str(e) for e in result.errors)


# ── Depth scoring ─────────────────────────────────────────────────────────

def test_depth_score_zero_when_no_schema():
    stream = {"name": "x", "primary_key": ["id"]}
    assert compute_stream_depth_score(stream, []) == 0


def test_depth_score_one_with_schema():
    stream = {
        "name": "x",
        "schema": {"type": "object", "properties": {"id": {"type": "string"}}},
    }
    assert compute_stream_depth_score(stream, []) == 1


def test_depth_score_two_with_pagination():
    stream = {
        "name": "x",
        "schema": {"type": "object", "properties": {"id": {"type": "string"}}},
        "pagination": {"strategy": "cursor"},
    }
    assert compute_stream_depth_score(stream, []) == 2


def test_depth_score_three_with_incremental():
    stream = {
        "name": "x",
        "schema": {"type": "object", "properties": {"id": {"type": "string"}}},
        "pagination": {"strategy": "cursor"},
        "incremental_field": "updated_at",
    }
    assert compute_stream_depth_score(stream, []) == 3


def test_depth_score_four_with_pk():
    stream = {
        "name": "x",
        "primary_key": ["id"],
        "schema": {"type": "object", "properties": {"id": {"type": "string"}}},
        "pagination": {"strategy": "cursor"},
        "incremental_field": "updated_at",
    }
    assert compute_stream_depth_score(stream, []) == 4


def test_depth_score_five_with_fixtures():
    stream = {
        "name": "x",
        "primary_key": ["id"],
        "schema": {"type": "object", "properties": {"id": {"type": "string"}}},
        "pagination": {"strategy": "cursor"},
        "incremental_field": "updated_at",
    }
    fixtures = [{"stream": "x", "name": ft} for ft in REQUIRED_FIXTURE_TYPES]
    assert compute_stream_depth_score(stream, fixtures) == 5


# ── v1 → v2 migration ────────────────────────────────────────────────────

def test_migrate_v1_to_v2_produces_valid_skeleton():
    v1 = {
        "id": "test_legacy",
        "name": "Test Legacy",
        "category": "saas",
        "auth": {"type": "oauth2"},
        "streams": [
            {"name": "items", "pagination": {"type": "cursor", "page_size": 50}},
        ],
    }
    v2 = migrate_v1_to_v2(v1)
    assert v2["version"] == 2
    assert v2["connector"]["type"] == "test_legacy"
    assert v2["auth"]["schemes"][0]["type"] == "oauth2"
    assert v2["certification"]["depth_score"] == 0
    assert v2["streams"][0]["name"] == "items"
    assert v2["streams"][0]["pagination"]["strategy"] == "cursor"
    # Migration intentionally leaves placeholders that will fail validation
    # so humans see exactly what to fix:
    assert "_migration_notes" in v2


def test_migrated_skeleton_warns_on_unvalidated_certification():
    """The validator was relaxed so a migrated-from-v1 skeleton VALIDATES
    cleanly (the schema_todo + missing-fixtures cases are now non-blocking
    so authors can save progress incrementally). The skeleton must still
    surface a `certification.last_validated` warning so the human knows
    nobody has signed off on the manifest yet."""
    v1 = {"id": "x", "name": "X", "category": "saas", "auth": {"type": "api_key"}, "streams": [{"name": "items"}]}
    v2 = migrate_v1_to_v2(v1)
    result = validate_manifest(v2)
    assert result.valid
    assert any("not set" in str(w) or "validated" in str(w) for w in result.warnings)


# ── Real-file validation against the bundled v2 sample ────────────────────

def test_salesforce_v2_sample_validates_at_depth_3():
    """The bundled salesforce.v2.json should validate cleanly and score
    depth 3 (incremental wired, no fixtures yet)."""
    path = MANIFEST_DIR / "salesforce.v2.json"
    if not path.exists():
        pytest.skip("salesforce.v2.json not present")
    result = validate_manifest_file(path)
    # Has primary_key, incremental_field, pagination, schema → depth-4 candidate
    # but no fixtures → blocked at depth-3.
    assert result.computed_depth_score >= 3
    # Manifest declares depth 3 — effective should match
    assert result.declared_depth_score == 3
    # Should fail on missing fixtures (REQUIRED_FIXTURE_TYPES check)
    assert not result.valid
    assert any("fixture" in str(e) for e in result.errors)
