"""CLI: validate connector manifests against the F0.1 v2 schema.

Usage:
    python -m fpulse.connectors.certify <connector_id>
    python -m fpulse.connectors.certify --all
    python -m fpulse.connectors.certify --migrate <connector_id>   # dry-run

Exit codes:
    0 — all validated manifests pass
    1 — at least one manifest has errors
    2 — connector_id not found / bad CLI args
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .manifest_v2 import (
    validate_manifest_file,
    migrate_v1_to_v2,
    ValidationResult,
)

MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"


def _print_result(result: ValidationResult) -> None:
    head = f"Connector: {result.connector_id}"
    sep = "─" * max(len(head), 40)
    print(f"\n{sep}\n{head}\n{sep}")
    print(f"  Manifest valid:           {'✓' if result.valid else '✗'}")
    print(f"  Declared depth score:     {result.declared_depth_score}")
    print(f"  Computed depth score:     {result.computed_depth_score}")
    print(f"  Effective depth score:    {result.effective_depth_score}")
    if result.streams_evaluated:
        print(f"  Streams evaluated:        {', '.join(result.streams_evaluated)}")
    if result.errors:
        print(f"\n  Errors ({len(result.errors)}):")
        for e in result.errors:
            print(f"    ✗ {e}")
    if result.warnings:
        print(f"\n  Warnings ({len(result.warnings)}):")
        for w in result.warnings:
            print(f"    ⚠ {w}")


def _load_v1(connector_id: str) -> dict | None:
    path = MANIFEST_DIR / f"{connector_id}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _certify_one(connector_id: str) -> int:
    """Return 0 on pass, 1 on fail, 2 on missing manifest."""
    path = MANIFEST_DIR / f"{connector_id}.json"
    if not path.exists():
        print(f"error: manifest not found: {path}", file=sys.stderr)
        return 2
    # Try v2 first; fall back to "v1, depth 0" if version != 2.
    with open(path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    if manifest.get("version") != 2:
        print(f"\nConnector: {connector_id}")
        print(f"  Manifest version: 1 (legacy) — depth_score: 0")
        print(f"  Run with --migrate to generate a v2 skeleton.")
        return 1
    result = validate_manifest_file(path)
    _print_result(result)
    return 0 if result.valid else 1


def _certify_all() -> int:
    rc = 0
    for f in sorted(MANIFEST_DIR.glob("*.json")):
        connector_id = f.stem
        result_rc = _certify_one(connector_id)
        if result_rc != 0:
            rc = 1
    return rc


def _migrate(connector_id: str) -> int:
    v1 = _load_v1(connector_id)
    if v1 is None:
        print(f"error: manifest not found: {connector_id}", file=sys.stderr)
        return 2
    v2 = migrate_v1_to_v2(v1)
    print(json.dumps(v2, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Validate F-Pulse connector manifests against the F0.1 schema")
    p.add_argument("connector_id", nargs="?", help="Connector to validate (or use --all)")
    p.add_argument("--all", action="store_true", help="Validate every manifest in the manifests/ directory")
    p.add_argument("--migrate", action="store_true", help="Print a v1→v2 migration skeleton for the named connector")
    args = p.parse_args(argv)

    if args.all and args.connector_id:
        print("error: pass either --all or a connector_id, not both", file=sys.stderr)
        return 2

    if args.all:
        return _certify_all()

    if not args.connector_id:
        print("error: provide a connector_id or --all", file=sys.stderr)
        return 2

    if args.migrate:
        return _migrate(args.connector_id)

    return _certify_one(args.connector_id)


if __name__ == "__main__":
    sys.exit(main())
