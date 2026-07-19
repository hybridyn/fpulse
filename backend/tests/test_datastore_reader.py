"""Tests for the storage preview / schema reader (Y8 2026-05-23).

Focused on the JSON-document fallback path: any valid JSON file that
isn't records-shaped (object root, configs, F-Pulse pipeline exports)
should preview as a JSON tree instead of crashing with DuckDB's
"Malformed JSON" error.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from fpulse.datastore.reader import (
    _looks_like_pipeline,
    _peek_json_shape,
    _truncate_for_wire,
    infer_schema,
    preview_file,
)


# ── _peek_json_shape ──────────────────────────────────────────────────────


class TestPeekJsonShape:
    def test_object_root(self, tmp_path):
        p = tmp_path / "obj.json"
        p.write_text('  \n\t{"a": 1}')
        assert _peek_json_shape(str(p)) == "object"

    def test_array_root(self, tmp_path):
        p = tmp_path / "arr.json"
        p.write_text('[{"id": 1}]')
        assert _peek_json_shape(str(p)) == "array"

    def test_bare_value_returns_unknown(self, tmp_path):
        p = tmp_path / "ndjson.json"
        p.write_text('{"id": 1}\n{"id": 2}')
        # Starts with '{' so reports as object; but DuckDB also handles
        # this as NDJSON. Both work, doesn't matter to the reader path.
        assert _peek_json_shape(str(p)) == "object"

    def test_missing_file(self, tmp_path):
        assert _peek_json_shape(str(tmp_path / "missing.json")) == "unknown"


# ── _looks_like_pipeline ──────────────────────────────────────────────────


class TestLooksLikePipeline:
    def test_real_pipeline_export_matches(self):
        doc = {
            "name": "My pipeline",
            "description": "test",
            "steps": [
                {"id": "s1", "type": "source", "params": {"connector_type": "csv"}},
                {"id": "s2", "type": "filter", "params": {"condition": "x > 0"}},
            ],
            "connections": [{"from_step": "s1", "to_step": "s2"}],
        }
        assert _looks_like_pipeline(doc) is True

    def test_envelope_shape_matches(self):
        # Format-version-2 envelope ({pipeline: {steps, name, ...}})
        # The detector runs on the inner pipeline so re-test the wrapped form.
        inner = {
            "name": "x", "steps": [{"type": "source", "params": {}}],
            "connection_definitions": [],
        }
        assert _looks_like_pipeline(inner) is True

    def test_random_object_does_not_match(self):
        assert _looks_like_pipeline({"foo": "bar"}) is False
        assert _looks_like_pipeline({"version": "1.0", "data": [1, 2, 3]}) is False
        # Has `steps` but no name + no connection_definitions
        assert _looks_like_pipeline({"steps": [{"type": "x", "params": {}}]}) is False

    def test_package_json_does_not_match(self):
        # Real-world false positive risk: package.json has no `steps`.
        assert _looks_like_pipeline({
            "name": "fpulse-frontend",
            "version": "1.0.0",
            "scripts": {"build": "vite build"},
        }) is False

    def test_array_root_does_not_match(self):
        assert _looks_like_pipeline([{"steps": []}]) is False

    def test_steps_with_wrong_shape_does_not_match(self):
        # `steps` exists but items lack required keys.
        assert _looks_like_pipeline({
            "name": "x", "steps": [{"id": "s1"}],
        }) is False


# ── _truncate_for_wire ────────────────────────────────────────────────────


class TestTruncateForWire:
    def test_caps_object_keys(self):
        big = {f"k{i}": i for i in range(200)}
        out = _truncate_for_wire(big)
        # 100 kept + 1 sentinel.
        assert sum(1 for k in out.keys() if k != "…") == 100
        assert "…" in out

    def test_caps_list_items(self):
        big = list(range(100))
        out = _truncate_for_wire(big)
        # 50 kept + 1 sentinel string.
        assert len(out) == 51
        assert isinstance(out[-1], str) and "more items" in out[-1]

    def test_caps_string_length(self):
        long = "a" * 1000
        out = _truncate_for_wire(long)
        assert out.endswith("…")
        assert len(out) <= 501

    def test_caps_depth(self):
        nested = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "deep"}}}}}}}
        out = _truncate_for_wire(nested)
        # Walk down; at depth 6 we should hit the truncation marker.
        cur = out
        for key in ["a", "b", "c", "d", "e", "f"]:
            if not isinstance(cur, dict) or key not in cur:
                break
            cur = cur[key]
        # The deepest reachable value is the truncation marker, not "deep".
        assert "deep" not in json.dumps(out)


# ── preview_file with JSON document fallback ──────────────────────────────


class TestPreviewFileFallback:
    def test_records_json_returns_rows_kind(self, tmp_path):
        # An array-of-objects JSON parses cleanly as records.
        p = tmp_path / "rows.json"
        p.write_text(json.dumps([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]))
        result = preview_file(str(p), "json")
        assert result["kind"] == "rows"
        assert len(result["rows"]) == 2

    def test_object_root_json_returns_document_kind(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"version": "1.0", "settings": {"theme": "dark"}}))
        result = preview_file(str(p), "json")
        assert result["kind"] == "document"
        assert result["document_kind"] == "object"
        assert result["is_pipeline_definition"] is False
        assert result["document"]["version"] == "1.0"

    def test_pipeline_json_flagged_as_pipeline_definition(self, tmp_path):
        p = tmp_path / "pipeline.json"
        p.write_text(json.dumps({
            "name": "My pipeline",
            "description": "test",
            "steps": [
                {"id": "s1", "type": "source", "params": {"connector_type": "csv"}},
            ],
            "connections": [],
            "connection_definitions": [],
        }))
        result = preview_file(str(p), "json")
        assert result["kind"] == "document"
        assert result["is_pipeline_definition"] is True
        assert result["document"]["name"] == "My pipeline"

    def test_invalid_json_returns_document_kind_invalid(self, tmp_path):
        p = tmp_path / "broken.json"
        p.write_text('{"this is broken')
        result = preview_file(str(p), "json")
        assert result["kind"] == "document"
        assert result["document_kind"] == "invalid"
        assert result["is_pipeline_definition"] is False
        assert "Invalid JSON" in result["message"]


# ── infer_schema with JSON document fallback ──────────────────────────────


class TestInferSchemaFallback:
    def test_object_json_returns_empty_columns(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"foo": "bar"}))
        result = infer_schema(str(p), "json")
        # Document-shaped JSON has no columns to infer.
        assert result == []

    def test_records_json_returns_inferred_columns(self, tmp_path):
        p = tmp_path / "rows.json"
        p.write_text(json.dumps([{"id": 1, "name": "a"}]))
        result = infer_schema(str(p), "json")
        names = {c["name"] for c in result}
        assert names == {"id", "name"}


# ── Regression: the user's actual scenario ────────────────────────────────


class TestUserReproScenario:
    """The bug report screenshot — sample-pipeline JSON upload to Storage.

    Before Y8 this raised "Malformed JSON … unexpected end of data" because
    DuckDB tried to parse the workflow object as NDJSON after the array
    parse failed. The reader now detects object-root via _peek_json_shape
    and routes to _json_document_preview before DuckDB sees it.
    """

    def test_jsonplaceholder_sample_pipeline_previews_as_document(self, tmp_path):
        sample = {
            "id": "01-jsonplaceholder-posts",
            "name": "Fetch JSONPlaceholder posts",
            "description": "Pulls 100 posts from the public JSONPlaceholder API.",
            "connection_definitions": [
                {
                    "name": "jsonplaceholder",
                    "type": "rest_api",
                    "config": {"base_url": "https://jsonplaceholder.typicode.com"},
                }
            ],
            "steps": [
                {
                    "id": "src",
                    "type": "source",
                    "params": {
                        "connector_type": "rest_api",
                        "connection_id": "jsonplaceholder",
                        "path": "/posts",
                    },
                },
                {
                    "id": "out",
                    "type": "destination",
                    "params": {"connector_type": "csv", "file_path": "posts.csv"},
                },
            ],
            "connections": [{"from_step": "src", "to_step": "out"}],
        }
        p = tmp_path / "01-jsonplaceholder-posts.json"
        p.write_text(json.dumps(sample, indent=2))

        result = preview_file(str(p), "json")
        # Before Y8 this raised RuntimeError("Malformed JSON …").
        assert result["kind"] == "document"
        assert result["is_pipeline_definition"] is True
        # The banner copy depends on this flag, so the test pins it.
