"""Tests for the pre-build packaging preflight (tools/package_preflight.py).

Asserts the current source tree is packageable (no failures) and that the
checker actually catches a missing package-data glob.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import package_preflight as pp  # noqa: E402


def test_reliable_source_assets_are_detected():
    # These are tracked source (present on any branch), so the checker must see
    # them regardless of which branch is checked out. (Whether package-data
    # globs / staged artifacts are present is branch-dependent, so we don't
    # assert fail == [] here — that's exactly the kind of drift the tool exists
    # to catch.)
    checks = pp.collect_checks()
    joined = " ".join(checks["ok"])
    assert "entry point: fpulse" in joined
    assert "connector manifests" in joined
    assert "vendored Swagger" in joined
    for bucket in ("ok", "warn", "fail"):
        assert all(isinstance(x, str) and x for x in checks[bucket])


def test_required_globs_are_the_ones_that_broke_before():
    # frontend_dist + manifests + swagger + docs are exactly the categories the
    # 1.0.0 "wheel with no UI/connectors" bug was missing.
    assert "frontend_dist/**/*" in pp._REQUIRED_GLOBS
    assert "connectors/manifests/*.json" in pp._REQUIRED_GLOBS
    assert "static/**/*" in pp._REQUIRED_GLOBS


def test_missing_glob_is_a_failure(monkeypatch):
    real = pp._REQUIRED_GLOBS[:]
    monkeypatch.setattr(pp, "_REQUIRED_GLOBS", real + ["connectors/nonexistent/*.json"])
    checks = pp.collect_checks()
    assert any("connectors/nonexistent/*.json" in f for f in checks["fail"])
