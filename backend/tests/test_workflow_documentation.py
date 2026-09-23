"""Self-documenting pipelines — first-class documentation primitives (OSS).

Covers the four documentation additions built on top of the existing
versioning / change-notes / owner fields:

  * ``business_purpose`` field + required-before-publish gate
  * first-class ``readme``
  * first-class ``tags`` (+ legacy ``metadata['tags']`` hoist)
  * Markdown doc-export endpoint ``GET /api/workflows/{id}/docs``
"""
from __future__ import annotations

from fpulse.ir.schema import Workflow, PipelineStatus
from fpulse.ir.docs import render_workflow_markdown, _node_role

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


# ── Schema-level: fields + tags hoist (no app needed) ─────────────────

class TestSchemaFields:
    def test_defaults_are_empty(self):
        wf = Workflow(name="x")
        assert wf.business_purpose == ""
        assert wf.readme == ""
        assert wf.tags == []

    def test_roundtrip_through_json(self):
        wf = Workflow(name="x", business_purpose="why", readme="# hi", tags=["a", "b"])
        again = Workflow(**wf.model_dump(mode="json"))
        assert again.business_purpose == "why"
        assert again.readme == "# hi"
        assert again.tags == ["a", "b"]

    def test_legacy_tags_hoisted_from_metadata(self):
        # A blob that stashed tags in the freeform metadata dict surfaces
        # them as first-class on load — blanks dropped.
        wf = Workflow(name="x", metadata={"tags": ["sales", "daily", "  "]})
        assert wf.tags == ["sales", "daily"]

    def test_explicit_tags_win_over_metadata(self):
        wf = Workflow(name="x", tags=["x"], metadata={"tags": ["a"]})
        assert wf.tags == ["x"]


# ── Renderer unit test (pure function, deterministic) ─────────────────

class TestMarkdownRenderer:
    def test_node_role_classification(self):
        assert _node_role("csv_source") == "Source"
        assert _node_role("parquet_sink") == "Sink"
        assert _node_role("webhook_trigger") == "Trigger"
        assert _node_role("filter") == "Transform"

    def test_contains_all_sections(self):
        wf = Workflow(
            name="Orders ETL",
            business_purpose="Load daily orders into the warehouse",
            description="Nightly batch",
            readme="## Runbook\nRun after midnight.",
            tags=["orders", "nightly"],
            owner_name="Jane",
            steps=[
                {"id": "src", "type": "csv_source", "label": "Load orders"},
                {"id": "flt", "type": "filter", "label": "Keep big"},
            ],
        )
        versions = [
            {"version": 1, "created_at": "2026-07-01T09:00:00",
             "created_by": "user", "change_summary": "Initial creation"},
            {"version": 2, "created_at": "2026-07-02T10:30:00",
             "created_by": "jane", "change_summary": "Added filter"},
        ]
        md = render_workflow_markdown(wf, versions)
        assert "# Orders ETL" in md
        assert "Load daily orders into the warehouse" in md   # purpose
        assert "## Runbook" in md                             # readme verbatim
        assert "`orders`" in md                               # tags rendered
        assert "Load orders" in md                            # node label
        assert "Source" in md and "Transform" in md           # node roles
        assert "Change log" in md and "Added filter" in md    # change log

    def test_empty_pipeline_still_renders(self):
        md = render_workflow_markdown(Workflow(name="Empty"), [])
        assert "# Empty" in md
        assert "no steps yet" in md.lower()


# ── API: create round-trip + docs endpoint ────────────────────────────

class TestDocsApi:
    def _create(self, authed_client, name):
        r = authed_client.post("/api/workflows", json={
            "name": name,
            "business_purpose": "Prove docs primitives",
            "readme": "## Runbook\nStep-by-step.",
            "tags": ["demo", "docs"],
            "steps": [
                {"id": "src", "type": "csv_source", "label": "Load orders"},
                {"id": "flt", "type": "filter", "label": "Keep big"},
            ],
        })
        assert r.status_code in (200, 201), r.text[:300]
        return r.json()["id"]

    def test_create_persists_doc_fields(self, authed_client):
        wid = self._create(authed_client, "docs-persist")
        got = authed_client.get(f"/api/workflows/{wid}").json()
        # GET nests the IR under a "workflow" key.
        wf = got.get("workflow", got)
        assert wf["business_purpose"] == "Prove docs primitives"
        assert wf["readme"].startswith("## Runbook")
        assert set(wf["tags"]) == {"demo", "docs"}

    def test_docs_markdown(self, authed_client):
        wid = self._create(authed_client, "docs-md")
        r = authed_client.get(f"/api/workflows/{wid}/docs")
        assert r.status_code == 200
        assert "text/markdown" in r.headers["content-type"]
        body = r.text
        assert "Prove docs primitives" in body
        assert "## Runbook" in body
        assert "`demo`" in body
        assert "Load orders" in body
        assert "Change log" in body

    def test_docs_json_and_download(self, authed_client):
        wid = self._create(authed_client, "docs-json")
        rj = authed_client.get(f"/api/workflows/{wid}/docs?format=json")
        assert rj.status_code == 200
        j = rj.json()
        assert j["filename"].endswith(".md")
        assert "Prove docs primitives" in j["markdown"]

        rd = authed_client.get(f"/api/workflows/{wid}/docs?download=1")
        assert "attachment" in rd.headers.get("content-disposition", "")

    def test_docs_404_for_unknown(self, authed_client):
        assert authed_client.get("/api/workflows/does-not-exist/docs").status_code == 404


# ── API: the required-before-publish gate ─────────────────────────────

class TestPublishGate:
    def test_publish_blocked_without_purpose(self, authed_client):
        r = authed_client.post("/api/workflows", json={
            "name": "gate-no-purpose",
            "steps": [{"id": "src", "type": "csv_source", "label": "L"}],
        })
        wid = r.json()["id"]
        pr = authed_client.post(f"/api/workflows/{wid}/publish")
        assert pr.status_code == 400
        assert "purpose" in pr.text.lower()

    def test_purpose_gate_precedes_test_gate(self, authed_client):
        # With a purpose set but no passing test, the NEXT gate (test)
        # fires — proving the purpose gate let it through rather than
        # silently blocking.
        r = authed_client.post("/api/workflows", json={
            "name": "gate-with-purpose",
            "business_purpose": "Because it matters",
            "steps": [{"id": "src", "type": "csv_source", "label": "L"}],
        })
        wid = r.json()["id"]
        pr = authed_client.post(f"/api/workflows/{wid}/publish")
        assert pr.status_code == 400
        assert "test" in pr.text.lower()

    def test_publish_succeeds_with_purpose_and_passing_test(self, authed_client):
        r = authed_client.post("/api/workflows", json={
            "name": "gate-publish-ok",
            "business_purpose": "Because it ships value",
            "steps": [{"id": "src", "type": "csv_source", "label": "L"}],
        })
        wid = r.json()["id"]
        # Simulate a passing test result via the store (mirrors /test).
        from fpulse.state import get_workflow_store
        store = get_workflow_store()
        store.update_status(wid, PipelineStatus.TESTING,
                            test_results={"status": "success"})
        pr = authed_client.post(f"/api/workflows/{wid}/publish")
        assert pr.status_code == 200, pr.text[:300]
        assert pr.json()["status"] == "published"


# ── The admin override policy (single instance-level escape hatch) ────

class TestPublishPolicy:
    """The business-purpose gate is on by default, but an operator can
    relax it org-wide (never per-pipeline). Exercised at the resolver
    level so it runs without the login-gated client.

    Each test takes the unauthenticated ``client`` fixture (not
    ``authed_client``) purely to boot the app: ``require_pipeline_purpose``
    reads ``app_state['db']``, which is only populated when the lifespan
    runs (``_populate_state``). Depending on ``db_fixture`` alone left the
    tests passing only when co-located with an app-booting test — they
    failed standalone or when an xdist worker ran them first. Booting the
    app makes them self-sufficient in any ordering."""

    def _set(self, db, value):
        import json
        from fpulse.api.publish_policy import SETTING_REQUIRE_PURPOSE
        db.execute(
            "INSERT OR REPLACE INTO settings (id, data, created_at) VALUES ('admin_settings', ?, ?)",
            (json.dumps({SETTING_REQUIRE_PURPOSE: value}), "2026-07-31T00:00:00Z"),
        )
        db.commit()

    def _clear(self, db):
        db.execute("DELETE FROM settings WHERE id = 'admin_settings'")
        db.commit()

    def test_default_is_required(self, client):
        from fpulse.main import app_state
        from fpulse.api.publish_policy import require_pipeline_purpose
        db = app_state["db"]
        self._clear(db)
        try:
            assert require_pipeline_purpose() is True   # default ON
        finally:
            self._clear(db)

    def test_admin_can_disable(self, client):
        from fpulse.main import app_state
        from fpulse.api.publish_policy import require_pipeline_purpose
        db = app_state["db"]
        try:
            self._set(db, False)
            assert require_pipeline_purpose() is False
            self._set(db, True)
            assert require_pipeline_purpose() is True
        finally:
            self._clear(db)

    def test_env_override_wins(self, client, monkeypatch):
        from fpulse.main import app_state
        from fpulse.api.publish_policy import require_pipeline_purpose, REQUIRE_PURPOSE_ENV
        db = app_state["db"]
        try:
            self._set(db, True)          # setting says required...
            monkeypatch.setenv(REQUIRE_PURPOSE_ENV, "0")   # ...env forces off
            assert require_pipeline_purpose() is False
        finally:
            self._clear(db)
