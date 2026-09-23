"""dbt project importer — compiled manifest.json → F-Pulse pipeline.

Covers the pure converter (``fpulse.importers.dbt.manifest_to_pipeline``) and
the ``POST /api/workflows/import-dbt`` endpoint. The converter tests need no
app; the endpoint test uses the authed client.
"""
from __future__ import annotations

from fpulse.importers.dbt import manifest_to_pipeline
from fpulse.ir.schema import Workflow, Step, StepConnection, NodePosition

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


# A small but realistic compiled manifest: a source → staging model → mart
# model chain, plus a dbt test node (which must be ignored) and an
# incremental model (which must warn).
MANIFEST = {
    "metadata": {
        "project_name": "jaffle_shop",
        "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v11.json",
    },
    "nodes": {
        "model.jaffle_shop.stg_orders": {
            "resource_type": "model",
            "name": "stg_orders",
            "schema": "analytics",
            "compiled_code": "SELECT id, amount FROM raw.orders",
            "config": {"materialized": "view"},
            "depends_on": {"nodes": ["source.jaffle_shop.raw.orders"]},
        },
        "model.jaffle_shop.orders": {
            "resource_type": "model",
            "name": "orders",
            "schema": "analytics",
            "compiled_code": "SELECT * FROM stg_orders WHERE amount > 0",
            "config": {"materialized": "table"},
            "depends_on": {"nodes": ["model.jaffle_shop.stg_orders"]},
        },
        "model.jaffle_shop.orders_daily": {
            "resource_type": "model",
            "name": "orders_daily",
            "schema": "analytics",
            "compiled_code": "SELECT date_trunc('day', ts) d, count(*) FROM orders GROUP BY 1",
            "config": {"materialized": "incremental"},
            "depends_on": {"nodes": ["model.jaffle_shop.orders"]},
        },
        # Non-model resource types must be ignored entirely.
        "test.jaffle_shop.not_null_orders_id": {
            "resource_type": "test",
            "name": "not_null_orders_id",
        },
    },
    "sources": {
        "source.jaffle_shop.raw.orders": {
            "resource_type": "source",
            "name": "orders",
            "source_name": "raw",
        },
        # An UNREFERENCED source must not become a dangling node.
        "source.jaffle_shop.raw.customers": {
            "resource_type": "source",
            "name": "customers",
            "source_name": "raw",
        },
    },
}


class TestConverter:
    def test_shapes_and_counts(self):
        pipeline, report = manifest_to_pipeline(MANIFEST)
        assert pipeline["name"] == "jaffle_shop (dbt import)"
        # 3 models + 1 referenced source; the dbt test node and the
        # unreferenced 'customers' source are dropped.
        assert report["models"] == 3
        assert report["sources"] == 1
        types = sorted(s["type"] for s in pipeline["steps"])
        assert types == ["source", "transform", "transform", "transform"]

    def test_source_is_placeholder(self):
        pipeline, _ = manifest_to_pipeline(MANIFEST)
        src = next(s for s in pipeline["steps"] if s["type"] == "source")
        assert src["params"]["_needs_connection"] is True
        assert src["params"]["table"] == "orders"
        assert src["label"] == "raw.orders"

    def test_edges_carry_ref_aliases(self):
        pipeline, _ = manifest_to_pipeline(MANIFEST)
        id_by_label = {s["label"]: s["id"] for s in pipeline["steps"]}
        edges = {
            (c["from_step"], c["to_step"]): c.get("alias")
            for c in pipeline["connections"]
        }
        # stg_orders → orders, aliased 'stg_orders' so `FROM stg_orders` resolves.
        assert edges[(id_by_label["stg_orders"], id_by_label["orders"])] == "stg_orders"
        # source → stg_orders, aliased with the source table name.
        assert edges[(id_by_label["raw.orders"], id_by_label["stg_orders"])] == "orders"

    def test_incremental_and_dialect_warnings(self):
        _, report = manifest_to_pipeline(MANIFEST)
        assert report["incremental_models"] == ["orders_daily"]
        joined = " ".join(report["warnings"]).lower()
        assert "incremental" in joined
        assert "duckdb" in joined
        assert "connection" in joined  # unbound-source warning

    def test_layout_is_left_to_right_by_depth(self):
        pipeline, _ = manifest_to_pipeline(MANIFEST)
        x = {s["label"]: s["position"]["x"] for s in pipeline["steps"]}
        # raw.orders(0) < stg_orders(1) < orders(2) < orders_daily(3)
        assert x["raw.orders"] < x["stg_orders"] < x["orders"] < x["orders_daily"]

    def test_output_is_importable_ir(self):
        # The dict must round-trip through the same IR the import endpoint builds.
        pipeline, _ = manifest_to_pipeline(MANIFEST)
        wf = Workflow(name="rt", project_id="default", workspace_id="default")
        for s in pipeline["steps"]:
            wf.steps.append(Step(
                id=s["id"], type=s["type"], label=s.get("label", ""),
                params=s.get("params", {}),
                position=NodePosition(**s.get("position", {})),
            ))
        for c in pipeline["connections"]:
            wf.connections.append(StepConnection(**c))
        assert len(wf.steps) == 4
        assert len(wf.connections) == 3

    def test_rejects_non_dict(self):
        import pytest
        with pytest.raises(ValueError):
            manifest_to_pipeline("not a manifest")  # type: ignore[arg-type]

    def test_empty_manifest_yields_no_steps(self):
        pipeline, report = manifest_to_pipeline({"nodes": {}, "sources": {}})
        assert pipeline["steps"] == []
        assert report["models"] == 0


class TestEndpoint:
    """Exercise the import-dbt handler's persist path deterministically.

    We call the async handler directly with ``workspace_id`` supplied rather
    than going over HTTP: auth is enforced by middleware above the route (so a
    dependency override can't bypass it) and the login-seed fixture skips on
    some workers — both would leave this new route unexercised. The ``client``
    fixture boots the app so ``app_state['db']`` (hence ``get_store()``) is
    wired; the handler then converts + saves exactly as a real request would.
    """

    async def test_import_creates_pipeline(self, client):
        from fpulse.api.workflows import import_dbt_project, DbtImportRequest
        result = await import_dbt_project(
            DbtImportRequest(manifest=MANIFEST), workspace_id="default",
        )
        assert result["steps_imported"] == 4      # 3 models + 1 source
        assert result["connections_imported"] == 3
        assert result["report"]["models"] == 3
        assert result["report"]["incremental_models"] == ["orders_daily"]

    async def test_empty_manifest_400(self, client):
        from fpulse.api.workflows import import_dbt_project, DbtImportRequest
        from fastapi import HTTPException
        import pytest
        with pytest.raises(HTTPException) as exc:
            await import_dbt_project(
                DbtImportRequest(manifest={"nodes": {}}), workspace_id="default",
            )
        assert exc.value.status_code == 400
        assert "dbt" in str(exc.value.detail).lower()
