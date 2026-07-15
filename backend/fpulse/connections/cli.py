"""Catalog verification CLI.

Three commands:

  list                — print the registry status (counts by category /
                        tier / verification, list of registered types).

  check <type>        — probe one connector type with a config (either
                        from --config-file JSON or --config k=v pairs).
                        Prints the catalog response. Use this from the
                        terminal whenever you want to confirm a saved
                        connection works without spinning up the UI.

  verify-all          — run check across every connector listed in
                        verify_configs.json (which mirrors the
                        docker-compose.dev.yml services). Produces a
                        markdown matrix of:
                          ✅ supported, items=N, latency=Xms
                          ❌ supported=false, reason=…
                          ⚠️  exception
                        Plus a summary block ready to paste into a
                        release-readiness report.

Examples:

    # Quick registry summary
    python -m fpulse.connections.cli list

    # Probe one connector inline
    python -m fpulse.connections.cli check mssql --config host=localhost \\
                                                   --config user=siva \\
                                                   --config password=test

    # Run the full sandbox matrix (after `docker compose -f docker-compose.dev.yml --profile all up -d`)
    python -m fpulse.connections.cli verify-all \\
        --configs backend/fpulse/connections/verify_configs.json

The CLI is also used by the verification harness in
scripts/verify_catalog.py, so both produce the same output format.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from fpulse.connections.catalog import get_catalog, registry_status


# ── Output helpers ────────────────────────────────────────────────────

@dataclass
class ProbeResult:
    connector: str
    supported: bool
    items: int
    reason: str
    error: str
    latency_ms: float


def _probe(connector: str, config: dict[str, Any], timeout_s: int = 15) -> ProbeResult:
    start = time.time()
    try:
        catalog = get_catalog(connector, config)
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(connector, False, 0, "", f"{type(exc).__name__}: {exc}",
                            round((time.time() - start) * 1000, 1))
    return ProbeResult(
        connector=connector,
        supported=bool(catalog.supported),
        items=len(catalog.items or []),
        reason=catalog.reason or "",
        error="",
        latency_ms=round((time.time() - start) * 1000, 1),
    )


def _emoji(r: ProbeResult) -> str:
    if r.error:
        return "⚠️"
    if r.supported:
        return "✅"
    return "❌"


def _kv_pairs_to_dict(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--config expected key=value pairs, got: {p!r}")
        k, v = p.split("=", 1)
        # Cheap type coercion — int / float / bool / fall back to str.
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        elif v.isdigit():
            out[k] = int(v)
        else:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


# ── Subcommands ──────────────────────────────────────────────────────

def cmd_list(_args: argparse.Namespace) -> int:
    status = registry_status()
    print(json.dumps(status, indent=2, sort_keys=False))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    config: dict[str, Any] = {}
    if args.config_file:
        with open(args.config_file, encoding="utf-8") as f:
            config.update(json.load(f))
    config.update(_kv_pairs_to_dict(args.config or []))

    catalog = get_catalog(args.type, config)
    body = catalog.model_dump(mode="json")
    if args.json:
        print(json.dumps(body, indent=2, sort_keys=False))
    else:
        # Human-readable summary.
        status = "✅ supported" if body["supported"] else "❌ unsupported"
        print(f"{args.type:20s}  {status}")
        if body.get("verification"):
            print(f"  verification : {body['verification']}")
        if body.get("category"):
            print(f"  category     : {body['category']}")
        if body.get("tier"):
            print(f"  tier         : {body['tier']}")
        if not body["supported"]:
            print(f"  reason       : {body.get('reason', '')}")
        else:
            print(f"  items        : {len(body.get('items') or [])}")
            print(f"  parents      : {body.get('parents') or []}")
            print(f"  kinds        : {body.get('kinds') or []}")
    return 0 if body["supported"] else 2


def cmd_verify_all(args: argparse.Namespace) -> int:
    configs_path = args.configs or os.path.join(
        os.path.dirname(__file__), "verify_configs.json",
    )
    if not os.path.isfile(configs_path):
        print(f"configs file not found: {configs_path}", file=sys.stderr)
        return 1
    with open(configs_path, encoding="utf-8") as f:
        configs = json.load(f)

    no_emulator = set(configs.pop("_no_local_emulator", []) or [])
    configs.pop("_comment", None)

    status = registry_status()
    registered = set(status.get("real", []))

    results: list[ProbeResult] = []
    skipped: list[tuple[str, str]] = []

    targets = sorted(set(configs.keys()) & registered)
    if args.only:
        targets = [t for t in targets if t in set(args.only)]

    for connector in targets:
        results.append(_probe(connector, configs[connector]))

    # Skipped — connectors registered but with no local emulator config.
    for connector in sorted(registered - set(configs.keys()) - no_emulator):
        skipped.append((connector, "no emulator config in verify_configs.json"))
    for connector in sorted(no_emulator & registered):
        skipped.append((connector, "no local emulator (cloud/SaaS only)"))

    # Markdown output
    if args.markdown or not args.json:
        print("# Catalog Verification Run\n")
        print(f"Probed: **{len(results)}**, "
              f"Passed: **{sum(1 for r in results if r.supported)}**, "
              f"Failed: **{sum(1 for r in results if not r.supported and not r.error)}**, "
              f"Errored: **{sum(1 for r in results if r.error)}**, "
              f"Skipped: **{len(skipped)}**\n")
        print("| Connector | Status | Items | Latency | Detail |")
        print("|---|---|---|---|---|")
        for r in results:
            detail = (r.error or r.reason or "").replace("|", "\\|")[:120]
            print(f"| {r.connector} | {_emoji(r)} | {r.items} | {r.latency_ms}ms | {detail} |")
        if skipped:
            print("\n## Skipped\n")
            print("| Connector | Reason |")
            print("|---|---|")
            for c, why in skipped:
                print(f"| {c} | {why} |")

    if args.json:
        print(json.dumps({
            "results": [r.__dict__ for r in results],
            "skipped": [{"connector": c, "reason": w} for c, w in skipped],
        }, indent=2))

    # Non-zero exit if any failed (excluding skipped) — useful for CI.
    failed = [r for r in results if not r.supported]
    return 0 if not failed else 3


# ── Main ─────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="fpulse-catalog",
        description="F-Pulse catalog verification CLI",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="show registry status (counts + types)")
    p_list.set_defaults(func=cmd_list)

    p_check = sub.add_parser("check", help="probe one connector")
    p_check.add_argument("type", help="connector type, e.g. mssql, mongodb, salesforce")
    p_check.add_argument("--config-file", help="path to a JSON config file")
    p_check.add_argument("--config", action="append",
                          help="key=value pair (repeatable) — overrides file")
    p_check.add_argument("--json", action="store_true",
                          help="emit raw catalog JSON instead of human summary")
    p_check.set_defaults(func=cmd_check)

    p_verify = sub.add_parser("verify-all",
                                help="run check across the docker-compose.dev.yml matrix")
    p_verify.add_argument("--configs",
                           help="path to verify_configs.json (default: bundled)")
    p_verify.add_argument("--only", nargs="+",
                           help="restrict to a subset of connector types")
    p_verify.add_argument("--json", action="store_true",
                           help="emit results as JSON instead of markdown")
    p_verify.add_argument("--markdown", action="store_true",
                           help="force markdown output (default unless --json)")
    p_verify.set_defaults(func=cmd_verify_all)

    args = p.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
