"""Guard the wheel's contents.

F-Pulse 1.0.0's wheel contained 445 .py files and nothing else — no UI, no
Swagger assets, no connector manifests — because `pyproject.toml` declared no
`[tool.setuptools.package-data]` and setuptools packages only .py by default.
`pip install fpulse && fpulse open` booted the API and served a 404 at /.

Nothing caught it because every path the team exercised — source checkout,
`pip install -e .`, Docker, the desktop installers — reads files from the repo
tree, where they obviously exist. The wheel was the one artifact nobody opened.

These tests open it.

`test_frontend_dist_resolution` and the package-data declaration checks are
cheap and always run. The full build test is marked `slow` because it shells
out to `python -m build` (~30s); it is the one that would actually have caught
the original bug, so run it before releasing:

    pytest backend/tests/test_packaging.py -m slow
"""
from __future__ import annotations

import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PYPROJECT = REPO / "pyproject.toml"


def _package_data() -> list[str]:
    with open(PYPROJECT, "rb") as fh:
        cfg = tomllib.load(fh)
    return cfg["tool"]["setuptools"]["package-data"]["fpulse"]


def test_package_data_is_declared():
    """Without this section the wheel silently drops every non-.py file."""
    patterns = _package_data()
    assert patterns, "no package-data declared — the wheel will ship .py only"


def test_package_data_covers_every_non_py_file():
    """Every non-.py file in the package must match a package-data pattern.

    Catches the class of bug directly: someone adds a data dir (a new
    manifests/ tree, a template, a seed file), it works from a checkout, and
    silently vanishes from the wheel.
    """
    import fnmatch

    pkg = REPO / "backend" / "fpulse"
    patterns = _package_data()

    uncovered = []
    for path in pkg.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(pkg).as_posix()
        if rel.endswith(".py") or "__pycache__" in rel:
            continue
        # frontend_dist is a staged build artifact; absent in a clean checkout.
        if rel.startswith("frontend_dist/"):
            continue
        if not any(fnmatch.fnmatch(rel, pat) for pat in patterns):
            uncovered.append(rel)

    assert not uncovered, (
        "these files live in the package but match no package-data pattern, "
        f"so they will NOT ship in the wheel: {uncovered}"
    )


def test_connector_manifests_are_covered():
    """The connectors ARE the product — an explicit check, not just the sweep."""
    import fnmatch

    patterns = _package_data()
    assert any(
        fnmatch.fnmatch("connectors/manifests/github.v2.json", p) for p in patterns
    ), "connector manifests are not packaged — a pip install would ship zero connectors"


def test_frontend_dist_resolution_prefers_packaged_copy():
    """main.py must look inside the package first, then the repo tree.

    The original bug was that only the repo-relative path existed: it resolves
    from a checkout and points outside site-packages when installed.
    """
    from fpulse.main import _PACKAGED_DIST, _SOURCE_DIST

    pkg_dir = Path(_PACKAGED_DIST).resolve()
    fpulse_pkg = Path(__import__("fpulse").__file__).resolve().parent

    assert pkg_dir.parent == fpulse_pkg, (
        "packaged frontend must resolve INSIDE the fpulse package "
        f"(got {pkg_dir}); otherwise a pip install serves no UI"
    )
    assert "frontend" in Path(_SOURCE_DIST).as_posix()


@pytest.mark.slow
def test_built_wheel_contains_ui_and_connectors(tmp_path):
    """Build the wheel and assert what's inside it.

    This is the test that would have caught the shipped bug.
    """
    staged = REPO / "backend" / "fpulse" / "frontend_dist"
    if not (staged / "index.html").is_file():
        pytest.skip(
            "frontend not staged — run `npm run build` then "
            "`python scripts/stage_frontend.py` first"
        )

    out = tmp_path / "dist"
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"`python -m build` unavailable/failed: {proc.stderr[-400:]}")

    wheels = list(out.glob("*.whl"))
    assert wheels, "no wheel produced"
    names = zipfile.ZipFile(wheels[0]).namelist()

    assert any(n.endswith("fpulse/frontend_dist/index.html") for n in names), (
        "wheel has no frontend_dist/index.html — `fpulse open` would 404"
    )
    assert any("/frontend_dist/assets/" in n for n in names), (
        "wheel has no frontend asset bundles"
    )
    manifests = [n for n in names if "/connectors/manifests/" in n and n.endswith(".json")]
    assert len(manifests) >= 40, f"wheel carries only {len(manifests)} connector manifests"
    assert any("/static/swagger-ui/" in n for n in names), "wheel has no Swagger assets — /docs breaks"
