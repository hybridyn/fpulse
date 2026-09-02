"""Stage bundled markdown docs into the Python package for wheel builds.

Run this BEFORE `python -m build`, alongside `scripts/stage_frontend.py`.

The Help -> Documentation tab reads markdown through backend API endpoints.
In a source checkout those files live at repo-root `docs/`, but a PyPI wheel
does not carry repo-root folders unless we copy them under the import package.
This script stages a read-only copy at `backend/fpulse/docs`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "docs"
DEST = REPO / "backend" / "fpulse" / "docs"
PACKAGE_ROOT = REPO / "backend" / "fpulse"

INCLUDE_SUFFIXES = {".md", ".txt", ".jsonl"}
EXCLUDE_NAMES = {
    "connector-validation-report.md",
    "connector-validation-live-report.md",
}


def _should_copy(path: Path) -> bool:
    if path.name in EXCLUDE_NAMES:
        return False
    return path.suffix.lower() in INCLUDE_SUFFIXES


def main() -> int:
    if not SRC.is_dir():
        raise SystemExit(f"Missing docs source directory: {SRC}")

    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True, exist_ok=True)

    copied = 0
    for src in sorted(SRC.rglob("*")):
        if not src.is_file() or not _should_copy(src):
            continue
        rel = src.relative_to(SRC)
        dst = DEST / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    if copied == 0:
        raise SystemExit("No docs staged; refusing to build docs-less wheel.")

    changelog = REPO / "CHANGELOG.md"
    if changelog.is_file():
        shutil.copy2(changelog, PACKAGE_ROOT / "CHANGELOG.md")
    else:
        raise SystemExit("Missing CHANGELOG.md; refusing to build incomplete docs bundle.")

    print(f"Staged {copied} documentation files into {DEST}")
    print(f"Staged CHANGELOG.md into {PACKAGE_ROOT}")
    print("Ready: python -m build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
