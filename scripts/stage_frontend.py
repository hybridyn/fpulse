"""Stage the built frontend into the Python package so the wheel carries it.

Run this AFTER `npm run build` and BEFORE `python -m build`:

    cd frontend && npm ci && npm run build
    cd .. && python scripts/stage_frontend.py
    python -m build

Why this exists
---------------
`fpulse` is one pip package that serves its own UI. The React app builds to
`frontend/dist/`, which lives outside the Python package, so setuptools never
sees it. Until this was added, the wheel shipped 445 .py files and nothing
else: no UI, no Swagger assets, no connector manifests. `pip install fpulse &&
fpulse open` — the README's headline command — booted the API and opened a
browser onto a 404.

It survived because every path the team exercised (source checkout, `pip
install -e .`, Docker, the desktop installers) reads `frontend/dist` directly
and works. Only someone installing from PyPI hit it.

This script copies `frontend/dist` -> `backend/fpulse/frontend_dist` so
`[tool.setuptools.package-data]` can pick it up. The staged copy is a build
artifact: gitignored, and rewritten from scratch on every run so a stale UI
can never ship.

It FAILS LOUDLY (exit 1) when the frontend isn't built. That is the point —
the original bug was silence, so the only acceptable failure here is one you
cannot miss. Never make this a warning.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "frontend" / "dist"
DEST = REPO / "backend" / "fpulse" / "frontend_dist"


def main() -> int:
    if not (SRC / "index.html").is_file():
        print(
            f"ERROR: no frontend build at {SRC}\n"
            f"       (looked for index.html)\n\n"
            f"Build it first:\n"
            f"    cd frontend && npm ci && npm run build\n\n"
            f"Refusing to stage. Building the wheel now would produce a package "
            f"whose UI 404s for every user — silently.",
            file=sys.stderr,
        )
        return 1

    # Rebuild from scratch: a partial overlay could leave stale asset bundles
    # next to a fresh index.html, which the browser resolves to 404s.
    if DEST.exists():
        shutil.rmtree(DEST)
    shutil.copytree(SRC, DEST)

    files = sum(1 for p in DEST.rglob("*") if p.is_file())
    size_mb = sum(p.stat().st_size for p in DEST.rglob("*") if p.is_file()) / 1_048_576
    print(f"Staged {files} files ({size_mb:.2f} MB) -> {DEST.relative_to(REPO)}")

    # index.html is the file main.py probes for; assets/ is what it references.
    if not (DEST / "index.html").is_file():
        print("ERROR: index.html missing after copy", file=sys.stderr)
        return 1
    print("Ready: python -m build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
