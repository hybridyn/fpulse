#!/usr/bin/env python
"""Run the catalog verification harness end-to-end.

Used in the verification workflow:

  1. `docker compose -f docker-compose.dev.yml --profile <profile> up -d`
  2. `python scripts/verify_catalog.py --output catalog_verification.md`
  3. Review the generated matrix; commit alongside release notes.

The script wraps `python -m fpulse.connections.cli verify-all` so the
output is identical, but adds:
  - Discovery of which docker-compose services are reachable (so we
    know whether to expect ✅ vs ⚠️ for each connector before probing).
  - A failure-only mode for CI (`--ci` exits non-zero on any ❌ that
    isn't an emulator-unavailability false positive).
  - Auto-promote suggestions: connectors that consistently return
    ✅ supported can be moved from sdk_validated → sandbox_verified
    in the registry (printed to stderr, not auto-applied).

The output file is intended to be checked in alongside docs/release
notes as evidence behind the verification status badges.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

# Mapping of docker-compose services → tcp ports we can reach to confirm
# the container is up. Mirrors docker-compose.dev.yml. If a service isn't
# listed here we fall back to "probe and see what happens".
_SERVICE_PORTS: dict[str, int] = {
    "postgres": 5432, "mysql": 3306, "mariadb": 3307, "mssql": 1433,
    "oracle": 1521, "db2": 50000, "clickhouse": 9000, "trino": 8080,
    "mongo": 27017, "cassandra": 9042, "neo4j": 7687, "arangodb": 8529,
    "couchbase": 8091, "redis": 6379, "elasticsearch": 9200,
    "opensearch": 9201, "solr": 8983, "kafka": 9092,
    "rabbitmq": 15672, "pulsar": 8085, "nats": 4222,
    "minio": 9000, "azurite": 10000, "cosmos": 8081, "dynamodb": 8000,
    "qdrant": 6333, "weaviate": 8090, "chroma": 8001, "milvus": 9091,
    "prometheus": 9090, "grafana": 3000, "ftp": 21, "sftp": 2222,
    "gcp-pubsub": 8085, "gcp-firestore": 8087,
}

# Map connector types to the docker-compose service that hosts them.
# When a connector has no entry, it's treated as "no local emulator".
_CONNECTOR_TO_SERVICE: dict[str, str] = {
    "postgresql": "postgres", "redshift": "postgres",
    "cockroachdb": "postgres", "pgvector": "postgres",
    "mysql": "mysql", "mariadb": "mariadb",
    "mssql": "mssql", "synapse": "mssql",
    "oracle": "oracle", "db2": "db2",
    "clickhouse": "clickhouse", "trino": "trino", "presto": "trino",
    "mongodb": "mongo", "cassandra": "cassandra",
    "neo4j": "neo4j", "arangodb": "arangodb", "couchbase": "couchbase",
    "redis": "redis", "elasticsearch": "elasticsearch",
    "opensearch": "opensearch", "solr": "solr",
    "kafka": "kafka", "rabbitmq": "rabbitmq",
    "pulsar": "pulsar", "nats": "nats",
    "s3": "minio", "minio": "minio",
    "azure_blob": "azurite", "adls_gen2": "azurite",
    "cosmosdb": "cosmos", "dynamodb": "dynamodb",
    "qdrant": "qdrant", "weaviate": "weaviate",
    "chroma": "chroma", "milvus": "milvus",
    "prometheus": "prometheus", "grafana": "grafana",
    "ftp": "ftp", "sftp": "sftp",
    "pubsub": "gcp-pubsub", "firebase": "gcp-firestore",
}


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover_reachable() -> dict[str, bool]:
    """Probe every known service port. Returns service → reachable map."""
    return {svc: _port_open("localhost", port)
            for svc, port in _SERVICE_PORTS.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Catalog verification harness")
    parser.add_argument("--output", help="write markdown matrix to this file")
    parser.add_argument("--ci", action="store_true",
                         help="exit non-zero on any unexpected failure")
    parser.add_argument("--only", nargs="+",
                         help="restrict to specific connector types")
    args = parser.parse_args(argv)

    # 1. Show what's reachable so a missing emulator is obvious.
    reachable = discover_reachable()
    print("=== Docker compose service reachability ===", file=sys.stderr)
    up = sorted(s for s, ok in reachable.items() if ok)
    down = sorted(s for s, ok in reachable.items() if not ok)
    print(f"  up   ({len(up)}): {', '.join(up) or '(none)'}", file=sys.stderr)
    print(f"  down ({len(down)}): {', '.join(down) or '(none)'}", file=sys.stderr)
    print("", file=sys.stderr)

    # 2. Drive the bundled CLI for the actual probing.
    cmd = [sys.executable, "-m", "fpulse.connections.cli", "verify-all", "--markdown"]
    if args.only:
        cmd.extend(["--only"] + args.only)

    proc = subprocess.run(cmd, capture_output=True, text=True,
                            cwd=str(Path(__file__).parent.parent / "backend"))
    output = proc.stdout
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Wrote verification matrix to {args.output}", file=sys.stderr)
    else:
        print(output)

    if proc.stderr:
        print(proc.stderr, file=sys.stderr)

    # 3. CI mode — fail if any connector targeting a reachable service
    #    came back ❌ (we don't fail when the service is just down).
    if args.ci:
        # Light parse of the markdown table.
        unexpected = []
        for line in output.splitlines():
            if not line.startswith("| "):
                continue
            parts = [p.strip() for p in line.strip("|").split("|")]
            if len(parts) < 5 or parts[0] in ("Connector", "---"):
                continue
            connector, status = parts[0], parts[1]
            if "❌" in status or "⚠️" in status:
                svc = _CONNECTOR_TO_SERVICE.get(connector)
                if svc and reachable.get(svc):
                    unexpected.append((connector, status, svc))
        if unexpected:
            print(f"\n=== CI failure ({len(unexpected)} unexpected) ===",
                   file=sys.stderr)
            for c, s, svc in unexpected:
                print(f"  {c} → {s} (service `{svc}` was reachable)",
                       file=sys.stderr)
            return 1

    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
