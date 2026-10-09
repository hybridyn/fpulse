"""The demo files must round-trip through the Pipelines-page importer."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fpulse.api import workflows as api

EXPORTS = Path(__file__).resolve().parents[2] / "samples" / "multi-system-demo" / "exports"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", [
    "19-sqlserver-to-postgres.fpulse",
    "20-api-to-sqlserver.fpulse",
    "21-postgres-to-mysql.fpulse",
    "22-mysql-to-s3.fpulse",
    "23-s3-to-api.fpulse",
])
async def test_demo_export_import(filename, monkeypatch):
    envelope = json.loads((EXPORTS / filename).read_text(encoding="utf-8"))
    # These are the shape checks made by the Pipelines page before showing
    # its import modal; the endpoint below exercises actual IR reconstruction.
    assert envelope["format_version"] == 2
    assert envelope["export_type"] == "pipeline"
    pipeline = envelope["pipeline"]
    assert len(pipeline["steps"]) == 7
    assert len(pipeline["connections"]) == 6
    captured = []

    def save(workflow, **kwargs):
        captured.append(workflow)
        return SimpleNamespace(version=1)

    monkeypatch.setattr(api, "get_store", lambda: SimpleNamespace(save=save))
    monkeypatch.setattr(api, "get_lifecycle_store", lambda: SimpleNamespace(add_event=lambda *a, **kw: None))
    result = await api.import_pipeline(api.PipelineImportRequest(pipeline=pipeline), workspace_id="default")
    assert result["steps_imported"] == 7
    assert result["connections_imported"] == 6
    imported = captured[0]
    assert imported.id != pipeline["id"]
    assert imported.name == pipeline["name"]
    ids = {step.id for step in imported.steps}
    assert all(edge.from_step in ids and edge.to_step in ids for edge in imported.connections)
    references = [step.params["connection_id"] for step in imported.steps if "connection_id" in step.params]
    assert len(references) == 2
    assert all(ref.startswith("multidemo_") for ref in references)
