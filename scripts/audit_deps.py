"""Cross-reference imports vs declared deps vs actually-installed packages.

Three sources of truth:
  1. Imports — scanned by audit_imports.py logic, parsed from .py AST
  2. Declared — what's listed in backend/requirements.txt and pyproject.toml
  3. Installed — what `pip list` says is actually in the venv

The "silent gap" bug we hit today (openpyxl, pyodbc) was a case where
something was imported + installed but not declared. So a venv rebuild
dropped it. This script lists every such case.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path


# ── Step 1: collect imports from the codebase ────────────────────────

repo_root = Path(__file__).resolve().parents[1]
backend = repo_root / "backend" / "fpulse"
stdlib = set(sys.stdlib_module_names) | {"typing_extensions"}

# These are local F-Pulse modules that show up as top-level when
# `from .X import Y` is parsed — they're not third-party.
LOCAL_RELATIVE = {
    "activity", "agent", "ai", "ai_config", "ai_cost_rates", "alerts",
    "audit", "auth", "auth_health", "backfills", "backup", "bus",
    "cases", "catalog", "cert_matrix", "collaboration", "connections",
    "connector_authoring", "consent", "contracts", "credentials",
    "dashboard", "database", "deployments", "descriptor", "encryptor",
    "error_intel", "execution", "execution_intel", "execution_manager",
    "executor", "exports", "extraction", "factory", "feature_flags",
    "flatten_engine", "folders", "gateway", "health_memory",
    "in_process", "inproc", "intelligence", "lineage", "logs",
    "manifest_v2", "marketplace", "mcp", "metrics", "models", "monitor",
    "nats_bus", "notifications", "pipeline_health", "planner",
    "plugins", "pool", "pool_allocation", "pre_publish",
    "pre_validator", "preview", "product_knowledge", "projects",
    "recipes", "redis_queue", "registry", "reports", "resolver",
    "rest_framework", "rule_planner", "runner", "schedules", "schema",
    "schema_contract", "schema_detector", "schema_history", "sender",
    "storage", "store", "system", "templates", "topics_autogen",
    "topics_handwritten", "trust", "types_meta", "uploads", "validator",
    "variables", "versioning", "websocket", "windows", "workflows",
    "workspace_settings", "workspaces",
}

# Distribution-name aliases — the import name doesn't always equal the
# pip package name.
IMPORT_TO_DIST = {
    "yaml": "pyyaml",
    "docx": "python-docx",
    "google": "google-api-python-client",   # broad — really many google-* pkgs
    "PIL": "pillow",
    "cv2": "opencv-python",
    "cx_Oracle": "cx-oracle",
    "ibm_db": "ibm-db",
    "ibm_db_dbi": "ibm-db",
    "psycopg2": "psycopg2-binary",
    "qdrant_client": "qdrant-client",
    "redshift_connector": "redshift-connector",
    "sentence_transformers": "sentence-transformers",
    "clickhouse_connect": "clickhouse-connect",
    "clickhouse_driver": "clickhouse-driver",
    "kafka": "kafka-python",
    "prometheus_client": "prometheus-client",
    "confluent_kafka": "confluent-kafka",
    "neo4j": "neo4j",
    "ollama": "ollama",
    "snowflake": "snowflake-connector-python",
    "trino": "trino",
    "weaviate": "weaviate-client",
    "azure": "azure-storage-blob",
    "boto3": "boto3",
    "botocore": "botocore",
    "chromadb": "chromadb",
    "cohere": "cohere",
    "databricks": "databricks-sql-connector",
    "deltalake": "deltalake",
    "elasticsearch": "elasticsearch",
    "hdbcli": "hdbcli",
    "nats": "nats-py",
    "openai": "openai",
    "oracledb": "oracledb",
    "paramiko": "paramiko",
    "pinecone": "pinecone-client",
    "prestodb": "presto-python-client",
    "pymongo": "pymongo",
    "pymysql": "pymysql",
    "redis": "redis",
    "requests": "requests",
    "teradatasql": "teradatasql",
    "zeep": "zeep",
    "cassandra": "cassandra-driver",
    "numpy": "numpy",
    "anyio": "anyio",
}

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

third_party: dict[str, set[str]] = {}
for name, files in imports.items():
    if name in stdlib:
        continue
    if name in LOCAL_RELATIVE:
        continue
    if name.startswith(("fpulse", "tests", "_")):
        continue
    third_party[name] = files


# ── Step 2: declared deps from requirements.txt ──────────────────────

req_path = repo_root / "backend" / "requirements.txt"
declared: set[str] = set()
for line in req_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    # Strip version specifier, extras: e.g. "fastapi>=0.115.0",
    # "uvicorn[standard]>=0.34.0"
    name = re.split(r"[<>=!~\[]", line)[0].strip().lower()
    if name:
        declared.add(name)


# ── Step 3: installed packages from `pip list` ───────────────────────

result = subprocess.run(
    [sys.executable, "-m", "pip", "list", "--format=freeze"],
    capture_output=True,
    text=True,
)
installed: set[str] = set()
for line in result.stdout.splitlines():
    if "==" in line:
        installed.add(line.split("==")[0].strip().lower())


# ── Step 4: cross-reference + report ─────────────────────────────────


def dist_name_for(import_name: str) -> str:
    return IMPORT_TO_DIST.get(import_name, import_name).lower()


print("=" * 78)
print("CROSS-REFERENCE: imports vs declared vs installed")
print("=" * 78)
print()
print(f"  Imports in code:     {len(third_party)}")
print(f"  Declared in reqs:    {len(declared)}")
print(f"  Installed in venv:   {len(installed)}")
print()

# Category A: imported AND installed AND declared — fine
# Category B: imported AND installed AND NOT declared — SILENT GAP RISK
# Category C: imported AND NOT installed — runtime crash
# Category D: declared AND NOT imported — possibly dead

silent_gaps = []
missing = []
sample_rows = []

for imp in sorted(third_party):
    dist = dist_name_for(imp)
    is_declared = (dist in declared) or (imp.lower() in declared)
    is_installed = (dist in installed) or (imp.lower() in installed)
    files = len(third_party[imp])
    if is_installed and not is_declared:
        silent_gaps.append((imp, dist, files))
    elif not is_installed:
        missing.append((imp, dist, files))

if silent_gaps:
    print(f"[GAP] SILENT GAPS - imported + installed + NOT declared ({len(silent_gaps)}):")
    for imp, dist, files in silent_gaps:
        print(f"    {imp:30s}  pip: {dist:30s}  ({files} files import this)")
    print()
    print("    Any of these could disappear on the next venv refresh, exactly")
    print("    like openpyxl + pyodbc did today. ADD them to requirements.txt.")
    print()
else:
    print("[OK] No silent gaps - every imported+installed package is declared")
    print()

if missing:
    print(f"[MISSING] IMPORTED BUT NOT INSTALLED - will crash at runtime ({len(missing)}):")
    for imp, dist, files in missing:
        print(f"    {imp:30s}  pip: {dist:30s}  ({files} files import this)")
    print()
    print("    Most of these are optional-connector imports inside try/except,")
    print("    so the connector silently disables when missing. Review case by case.")
    print()
else:
    print("[OK] Every imported package is installed")
    print()
