"""Unit tests for the connector cert-matrix API — Gate 3.

Exercises the pure functions in `fpulse.api.cert_matrix` without
needing a running FastAPI app: build a temp manifests dir with mixed
v1/v2 files, point the module at it, and assert the matrix shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fpulse.api import cert_matrix as cm


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def fake_manifests(tmp_path, monkeypatch):
    """Build a temp manifests dir with one v1 manifest + one v2 manifest."""
    d = tmp_path / "manifests"
    d.mkdir()

    # v1 — old format. With auth + streams (no path → no streams_count).
    (d / "legacy_app.json").write_text(json.dumps({
        "display_name": "Legacy App",
        "category": "crm",
        "vendor": "Acme",
        "auth": {"type": "api_key"},
        "streams": [{"name": "things"}],
    }), encoding="utf-8")

    # v1 — fully wired: auth + streams (with path) + pagination → v1-functional.
    (d / "working_v1.json").write_text(json.dumps({
        "display_name": "Working V1",
        "category": "crm",
        "vendor": "Acme",
        "auth": {"type": "bearer"},
        "streams": [{
            "name": "contacts",
            "path": "/contacts",
            "pagination": {"type": "cursor"},
        }],
    }), encoding="utf-8")

    # v2 — full depth-5 candidate
    (d / "good_app.v2.json").write_text(json.dumps({
        "version": 2,
        "connector": {
            "type": "rest",
            "display_name": "Good App",
            "category": "support",
            "vendor": "Acme",
        },
        "auth": {"schemes": [{"type": "api_key", "header": "X-Api"}]},
        "rate_limit": {"strategy": "fixed", "rpm": 60},
        "streams": [
            {
                "name": "tickets",
                "endpoint": "/tickets",
                "method": "GET",
                "primary_key": ["id"],
                "incremental_field": "updated_at",
                "pagination": {"strategy": "page"},
                "schema": {"properties": {"id": {"type": "string"}}},
                "fixtures": [
                    {"name": "ok"},
                    {"name": "empty"},
                    {"name": "rate_limited"},
                    {"name": "auth_error"},
                    {"name": "schema_error"},
                ],
            }
        ],
    }), encoding="utf-8")

    monkeypatch.setattr(cm, "_manifests_dir", lambda: d)
    return d


# ── Endpoints ────────────────────────────────────────────────────────


class TestCertMatrix:
    def test_matrix_returns_one_row_per_manifest(self, fake_manifests):
        result = cm.cert_matrix()
        ids = {row["id"] for row in result["rows"]}
        # legacy_app.json + working_v1.json + good_app.v2.json — all surfaced.
        assert "legacy_app" in ids
        assert "working_v1" in ids
        assert "good_app.v2" in ids or "good_app" in ids
        assert result["total"] == len(result["rows"])

    def test_v1_basic_manifest_marked_uncertified(self, fake_manifests):
        # legacy_app has auth + streams (no path) → v1_capability_score=1 → v1-basic
        result = cm.cert_matrix()
        legacy = next(r for r in result["rows"] if r["id"] == "legacy_app")
        assert legacy["manifest_version"] == 1
        assert legacy["validation_status"] == "uncertified"
        assert legacy["depth_score"] == 0
        assert legacy["depth_label"] in ("v1-basic", "v1-stub")
        assert legacy["issues_count"] == 0
        assert legacy.get("v1_capability_score") in (0, 1)
        assert "migrate" in (legacy.get("migration_hint") or "").lower()

    def test_v1_functional_manifest_distinguished_from_stub(self, fake_manifests):
        # working_v1 has auth + streams (with path) + pagination → score=3 → v1-functional
        result = cm.cert_matrix()
        working = next(r for r in result["rows"] if r["id"] == "working_v1")
        assert working["manifest_version"] == 1
        assert working["validation_status"] == "uncertified"
        assert working["depth_label"] == "v1-functional"
        assert working["v1_capability_score"] == 3

    def test_v2_full_manifest_validates_pass(self, fake_manifests):
        result = cm.cert_matrix()
        good = next(r for r in result["rows"] if r["id"].startswith("good_app"))
        assert good["manifest_version"] == 2
        # Either depth-5 (production) or at least higher than the v1 row.
        assert good["depth_score"] >= 4
        assert good["depth_label"] in ("beta", "production")

    def test_summary_counts(self, fake_manifests):
        result = cm.cert_matrix()
        assert result["total"] == 3
        assert result["v2_total"] == 1
        # All three rows produce one label each.
        assert sum(result["by_label"].values()) == 3

    def test_audited_at_is_iso_timestamp(self, fake_manifests):
        result = cm.cert_matrix()
        # Must parse as a datetime — i.e. proper ISO string.
        from datetime import datetime
        datetime.fromisoformat(result["audited_at"])


class TestCertMatrixDetail:
    def test_unknown_id_returns_404(self, fake_manifests):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            cm.cert_matrix_detail("does_not_exist")
        assert ei.value.status_code == 404

    def test_path_traversal_blocked(self, fake_manifests):
        from fastapi import HTTPException
        for evil in ["../etc/passwd", "..\\windows\\system32", "/etc/shadow"]:
            with pytest.raises(HTTPException) as ei:
                cm.cert_matrix_detail(evil)
            assert ei.value.status_code == 400

    def test_v2_detail_includes_per_stream_breakdown(self, fake_manifests):
        # The v2 file is good_app.v2.json — by convention, the detail endpoint
        # accepts the connector id WITHOUT the .v2 suffix.
        # First confirm what id the matrix surfaced, then ask for detail by it.
        matrix = cm.cert_matrix()
        good = next(r for r in matrix["rows"] if r["id"].startswith("good_app"))
        detail = cm.cert_matrix_detail(good["id"])
        assert "per_stream" in detail
        assert isinstance(detail["per_stream"], list)
        assert len(detail["per_stream"]) == 1
        s = detail["per_stream"][0]
        assert s["name"] == "tickets"
        assert s["has_schema"] is True
        assert s["has_pagination"] is True
        assert s["incremental_field"] == "updated_at"
        assert s["primary_key"] == ["id"]
