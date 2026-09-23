#!/usr/bin/env python3
"""Pre-build packaging preflight for F-Pulse (T1: install friction).

Catches the recurring "shipped a broken wheel" class of bug BEFORE
`python -m build`: a wheel that installs but serves a 404 (no UI), or is missing
connector manifests / vendored Swagger / docs — because a `package-data` glob
didn't match or a build artifact wasn't staged. Every other CI check tests the
SOURCE TREE (where these obviously exist); the wheel is the one artifact that
historically shipped without them.

    python tools/package_preflight.py

Exit 0 = safe to build. Non-zero = a required source asset or packaging config
is missing (a real wheel bug). Build artifacts that just need staging
(frontend_dist, in-package docs) are WARNINGS with the exact fix command — they
are produced by the release workflow's stage steps, not committed.
"""

from __future__ import annotations

import sys
import argparse
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "backend" / "fpulse"

# package-data globs that MUST be present or the wheel ships .py files only.
_REQUIRED_GLOBS = [
    "frontend_dist/**/*",           # the built SPA — served at /
    "connectors/manifests/*.json",  # the connectors ARE the product
    "static/**/*",                  # vendored Swagger for /docs
    "docs/**/*.md",                 # bundled Help -> Documentation
]


def collect_checks(strict: bool = False) -> dict[str, list[str]]:
    """Return {'ok': [...], 'warn': [...], 'fail': [...]}."""
    ok: list[str] = []
    warn: list[str] = []
    fail: list[str] = []

    try:
        with open(REPO / "pyproject.toml", "rb") as fh:
            py = tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001
        return {"ok": [], "warn": [], "fail": [f"cannot read pyproject.toml: {exc}"]}

    project = py.get("project", {})

    # 1) console entry point — `fpulse` must launch the app
    if project.get("scripts", {}).get("fpulse"):
        ok.append(f"entry point: fpulse = {project['scripts']['fpulse']}")
    else:
        fail.append("no [project.scripts].fpulse console entry point — `fpulse` won't exist")

    # 2) version declared
    if project.get("version"):
        ok.append(f"version: {project['version']}")
    else:
        fail.append("no [project].version")

    # 3) package-data globs present (non-.py assets ship only if listed)
    pkg_data = py.get("tool", {}).get("setuptools", {}).get("package-data", {}).get("fpulse", [])
    for glob in _REQUIRED_GLOBS:
        if glob in pkg_data:
            ok.append(f"package-data glob: {glob}")
        else:
            fail.append(f"package-data missing glob '{glob}' — those assets won't ship in the wheel")

    # 4) required SOURCE assets exist on disk
    manifests = list((PKG / "connectors" / "manifests").glob("*.json"))
    if manifests:
        ok.append(f"connector manifests: {len(manifests)} present")
    else:
        fail.append("no connector manifests under fpulse/connectors/manifests — the wheel would ship no connectors")

    if (PKG / "static" / "swagger-ui" / "swagger-ui-bundle.js").is_file():
        ok.append("vendored Swagger bundle present")
    else:
        fail.append("missing static/swagger-ui/swagger-ui-bundle.js — /docs will 404 (breaks the air-gap promise)")

    seeds = list((PKG / "seed_data").glob("*.csv"))
    if seeds:
        ok.append(f"seed data: {len(seeds)} csv")
    else:
        warn.append("no seed_data/*.csv — first-run sample dataset absent")

    # 5) build artifacts — WARN (staged at release time, not committed)
    if (PKG / "frontend_dist" / "index.html").is_file():
        ok.append("frontend_dist staged (UI will ship)")
    else:
        warn.append("frontend_dist NOT staged — run `python scripts/stage_frontend.py` "
                    "before build, or the wheel serves a blank UI")

    docs_dir = PKG / "docs"
    if docs_dir.is_dir() and any(docs_dir.glob("*.md")):
        ok.append("in-package docs staged")
    else:
        warn.append("in-package docs NOT staged — run `python scripts/stage_docs.py`, "
                    "or Help -> Documentation ships empty")

    if strict:
        fail.extend(warn)
        warn.clear()
        if not any((PKG / "frontend_dist" / "assets").glob("*.js")):
            fail.append("frontend_dist/assets contains no JavaScript bundle")
    return {"ok": ok, "warn": warn, "fail": fail}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="Fail on unstaged release assets")
    args = parser.parse_args()
    checks = collect_checks(strict=args.strict)
    for item in checks["ok"]:
        print(f"  ok   {item}")
    for item in checks["warn"]:
        print(f"  WARN {item}")
    for item in checks["fail"]:
        print(f"  FAIL {item}")
    print(f"\npackage-preflight: {len(checks['ok'])} ok, "
          f"{len(checks['warn'])} warning(s), {len(checks['fail'])} failure(s)")
    if checks["fail"]:
        print("NOT safe to build — fix the failures above.")
        return 1
    print("Safe to build (stage any warned build artifacts first).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
