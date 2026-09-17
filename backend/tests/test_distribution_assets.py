"""Regression tests for incomplete release artifacts."""
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import package_preflight as pp
from verify_wheel import verify


@pytest.fixture
def assets():
    return {
        "fpulse/frontend_dist/index.html": '<script src="/assets/app.js"></script><link href="/assets/app.css" rel="stylesheet">',
        "fpulse/frontend_dist/assets/app.js": "console.log('ready')",
        "fpulse/frontend_dist/assets/app.css": "body{}",
        "fpulse/seed_data/orders.csv": "id\n1",
        "fpulse/static/swagger-ui/swagger-ui-bundle.js": "swagger",
        "fpulse/CHANGELOG.md": "changes",
        "fpulse/connectors/manifests/csv.json": "{}",
        "fpulse/docs/install.md": "install",
    }


def wheel(tmp_path, assets):
    path = tmp_path / "test.whl"
    with ZipFile(path, "w") as archive:
        for name, content in assets.items():
            archive.writestr(name, content)
    return path


def test_complete_wheel(tmp_path, assets):
    verify(wheel(tmp_path, assets))


@pytest.mark.parametrize("missing", [
    "fpulse/frontend_dist/index.html", "fpulse/frontend_dist/assets/app.js",
    "fpulse/frontend_dist/assets/app.css", "fpulse/docs/install.md",
    "fpulse/connectors/manifests/csv.json", "fpulse/seed_data/orders.csv",
    "fpulse/static/swagger-ui/swagger-ui-bundle.js", "fpulse/CHANGELOG.md",
])
def test_incomplete_wheel_rejected(tmp_path, assets, missing):
    del assets[missing]
    with pytest.raises(ValueError):
        verify(wheel(tmp_path, assets))


def test_no_javascript_entry_rejected(tmp_path, assets):
    assets["fpulse/frontend_dist/index.html"] = "<html>placeholder</html>"
    with pytest.raises(ValueError, match="JavaScript"):
        verify(wheel(tmp_path, assets))


def test_strict_requires_staging(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "PKG", tmp_path)
    normal = pp.collect_checks()
    strict = pp.collect_checks(strict=True)
    assert normal["warn"]
    assert not strict["warn"]
    assert all(item in strict["fail"] for item in normal["warn"])
    assert any("JavaScript" in item for item in strict["fail"])
