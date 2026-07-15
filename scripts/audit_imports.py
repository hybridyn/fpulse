"""Audit script — scan backend/fpulse for every third-party import.

Walks backend/fpulse/ recursively, parses each .py file with ast,
extracts top-level imports, filters to third-party (not stdlib, not
local fpulse.*), and prints a sorted table with import counts and an
example file.

Output is used to cross-reference against requirements.txt + pyproject.toml
to catch "silent gap" deps like openpyxl / pyodbc that aren't declared
but are imported at runtime.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


repo_root = Path(__file__).resolve().parents[1]
backend = repo_root / "backend" / "fpulse"

# Stdlib detection — sys.stdlib_module_names lists every stdlib top-level
# module. Plus typing_extensions, which behaves like stdlib for users
# (it's a backport rather than a real third-party dep).
stdlib = set(sys.stdlib_module_names) | {"typing_extensions"}

# First-party prefixes — anything starting with these is internal to F-Pulse.
local_prefixes = ("fpulse", "tests")

imports: dict[str, set[str]] = {}


def visit(path: Path) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                imports.setdefault(top, set()).add(
                    str(path.relative_to(backend.parent))
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            imports.setdefault(top, set()).add(
                str(path.relative_to(backend.parent))
            )


for path in backend.rglob("*.py"):
    if "__pycache__" in path.parts:
        continue
    visit(path)

# Filter to third-party only.
third_party: dict[str, set[str]] = {}
for name, files in imports.items():
    if name in stdlib:
        continue
    if name.startswith(local_prefixes):
        continue
    if name.startswith("_"):
        continue
    third_party[name] = files

print("THIRD-PARTY IMPORTS IN backend/fpulse/")
print("=" * 78)
for name in sorted(third_party):
    count = len(third_party[name])
    sample = sorted(third_party[name])[0]
    print(f"  {name:30s}  {count:4d} files  e.g. {sample}")
print()
print(f"TOTAL: {len(third_party)} distinct third-party modules imported")
