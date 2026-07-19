"""Central runtime configuration for F-Pulse.

Single source of truth for mode + runtime limits. All guardrails read from
here rather than sprinkling ``os.environ.get()`` calls across the codebase.

Mode:
  dev  — permissive, limits relaxed, fast iteration (default)
  prod — enforces concurrency cap, memory cap, file-size cap, row cap,
         backup rotation, version retention

Volume positioning (reviewed and corrected):
  F-Pulse is a **single-node, production-safe** orchestrator designed to
  handle **medium data extremely reliably** — not big data.

  DuckDB CAN stream scans and spill to disk, but memory usage depends on
  TRANSFORMATIONS (joins, group-by, sort) not just file size. A 500 MB CSV
  with a 10-way join can eat more RAM than a 5 GB CSV with a simple filter.

  Practical volume tiers (calibrated 2026-05-03 — see external review):
    <10 GB   — Optimal. Smooth, fast, ideal use case.
    10–100 GB — Supported with tuning. SSD spill path required.
    100–500 GB — Careful pipeline design needed. Simple operations only.
    >500 GB  — Beyond single-node design. Use a distributed execution engine.

Override any value via environment variables listed beside the field.
Values are read ONCE at import time — restart the backend to change them.
"""

from __future__ import annotations

import multiprocessing
import os


def _int(name: str, default: int) -> int:
    """Parse int env var with a default, tolerating junk ('' / non-int)."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ── Mode ────────────────────────────────────────────────────────────────
# FPULSE_MODE=prod activates all guardrails. Default 'dev' for local use.
MODE: str = os.environ.get("FPULSE_MODE", "dev").lower()
IS_PROD: bool = MODE == "prod"
IS_DEV: bool = not IS_PROD


# ── Security mode ───────────────────────────────────────────────────────
# Distinct from FPULSE_MODE (which tunes runtime *limits*). SECURITY_MODE
# governs the AUTH posture, and it is the switch the rest of the security
# model keys off:
#   local  — single-user, loopback convenience. Unauthenticated callers
#            fall back to the 'default' workspace so every page works with
#            no login. Safe ONLY while the bind stays on 127.0.0.1. (default)
#   server — self-hosted / shared / LAN-exposed. Authentication is required;
#            there is NO anonymous workspace fallback. Set this the moment
#            the backend is reachable by anyone but you.
#
# Kept permissive-by-default so the `pip install fpulse && fpulse open`
# laptop flow is unchanged; operators opt into the strict posture.
_sec_raw = os.environ.get("FPULSE_SECURITY_MODE", "").strip().lower()
# Exposure implies server mode: if the operator opened the bind to the LAN
# (FPULSE_ALLOW_LAN=1) they are, by definition, reachable by more than the
# local user — so default to the strict posture unless they explicitly said
# 'local'. A loopback install stays 'local'.
_allow_lan = os.environ.get("FPULSE_ALLOW_LAN", "").strip().lower() in ("1", "true", "yes", "on")
if _sec_raw in ("local", "server"):
    SECURITY_MODE: str = _sec_raw
else:
    SECURITY_MODE = "server" if _allow_lan else "local"
IS_SERVER_MODE: bool = SECURITY_MODE == "server"
IS_LOCAL_MODE: bool = not IS_SERVER_MODE


# ── AI action posture ───────────────────────────────────────────────────
# Whether the in-app assistant may fire *execution* actions (run / cancel a
# pipeline, test a connection) on the user's behalf, vs. read/draft only
# (describe, diagnose, propose). Server-side gate — the chip/prompt is never
# trusted on its own; when execution is allowed AND the server is exposed,
# a write role is still required (see api/agent_action.py).
#   default: allowed on a local single-user box (it's your machine);
#            read/draft-only when the server is exposed (server mode).
# Override explicitly with FPULSE_AI_ALLOW_EXECUTE=1|0.
_ai_exec_raw = os.environ.get("FPULSE_AI_ALLOW_EXECUTE", "").strip().lower()
if _ai_exec_raw in ("1", "true", "yes", "on"):
    AI_ALLOW_EXECUTE: bool = True
elif _ai_exec_raw in ("0", "false", "no", "off"):
    AI_ALLOW_EXECUTE = False
else:
    AI_ALLOW_EXECUTE = IS_LOCAL_MODE


# ── Execution authorization ─────────────────────────────────────────────
# When on, pipeline execution requires a fresh one-time code
# (security/execution_codes.py) — so a stolen session/token alone can't fire
# runs. Default OFF: turning it on also requires every run-initiation path
# (API run, gateway, scheduler, backfill) to mint a code, so it's an explicit
# opt-in, not a silent default.
# Default: ON in server mode (exposed), OFF on a local box. Every run path
# already mints a code, so enabling it fleet-wide is safe. Explicit override
# via FPULSE_REQUIRE_EXECUTION_CODE=1|0.
_req_code_raw = os.environ.get("FPULSE_REQUIRE_EXECUTION_CODE", "").strip().lower()
if _req_code_raw in ("1", "true", "yes", "on"):
    REQUIRE_EXECUTION_CODE: bool = True
elif _req_code_raw in ("0", "false", "no", "off"):
    REQUIRE_EXECUTION_CODE = False
else:
    REQUIRE_EXECUTION_CODE = IS_SERVER_MODE


# ── Concurrency ─────────────────────────────────────────────────────────
# How many workflow runs may execute simultaneously on this node.
# 0 means "no cap" (dev default). Prod default is 4 — enough to keep the
# box busy without thrashing. Each run gets its own in-memory DuckDB so
# N runs = N × DUCKDB_MEMORY_LIMIT worst case.
MAX_CONCURRENT_RUNS: int = _int(
    "FPULSE_MAX_CONCURRENT_RUNS",
    4 if IS_PROD else 0,
)


# ── DuckDB engine tuning ───────────────────────────────────────────────
# Memory: explicit ceiling forces DuckDB to spill intermediate hash tables,
# sorts, and aggregations to disk instead of OOM-killing the process.
# Default 4 GB — set lower on small boxes, higher on big ones.
DUCKDB_MEMORY_LIMIT: str = os.environ.get("FPULSE_DUCKDB_MEMORY_LIMIT", "4GB")

# Spill target. MUST be on a fast disk (SSD/NVMe). DuckDB writes large
# temp files during sort/hash/aggregate overflow. Placing this on a slow
# spinner turns a 30-second query into a 30-minute one.
_DATA_DIR = os.environ.get("FPULSE_DATA_DIR", "./data")
DUCKDB_TEMP_DIRECTORY: str = os.environ.get(
    "FPULSE_DUCKDB_TEMP_DIR",
    os.path.join(_DATA_DIR, "duckdb_spill"),
)

# Thread cap: DuckDB defaults to ALL cores. On a shared box running
# FastAPI + scheduler + SQLite, giving DuckDB every core starves the API
# (login, health, WebSocket all freeze). Cap at half the cores so the
# rest of the process stays responsive. In dev mode we leave it at 0
# which means "let DuckDB decide" — fine for a developer laptop.
_cpu_count = multiprocessing.cpu_count() or 4
DUCKDB_THREADS: int = _int(
    "FPULSE_DUCKDB_THREADS",
    max(2, _cpu_count // 2) if IS_PROD else 0,
)

# preserve_insertion_order=false allows DuckDB to use parallel scans on
# Parquet/CSV without maintaining row order, which is significantly faster
# for analytical queries (GROUP BY, DISTINCT, JOIN). Order is irrelevant
# for pipeline transforms — we never guarantee input-row-order anyway.
DUCKDB_PRESERVE_ORDER: bool = os.environ.get(
    "FPULSE_DUCKDB_PRESERVE_ORDER", "false"
).lower() in ("true", "1", "yes")


# ── Source guardrails ───────────────────────────────────────────────────
# Max file size accepted by file/upload sources (MB).
# This is a SOFT WARNING — we don't block the run, but flag it in the
# pre-execution check so the user knows they're pushing limits.
MAX_UPLOAD_MB: int = _int("FPULSE_MAX_UPLOAD_MB", 500)

# Max rows a single source node may emit. Prevents runaway SELECT *.
MAX_SOURCE_ROWS: int = _int("FPULSE_MAX_SOURCE_ROWS", 10_000_000)

# Sample mode: when > 0, source nodes in DEV preview limit to this many
# rows for fast iteration. The full dataset is only processed on explicit
# "Run Full" execution. 0 = disabled (load everything).
DEV_SAMPLE_ROWS: int = _int("FPULSE_DEV_SAMPLE_ROWS", 1_000_000)

# Volume tier thresholds (bytes) — used for UX warnings and scale-up hints.
# Calibrated 2026-05-03: prior thresholds (1/10/50 GB) were too conservative.
VOLUME_TIER_GOOD: int = 10 * 1024 * 1024 * 1024      # 10 GB
VOLUME_TIER_CAUTION: int = 100 * 1024 * 1024 * 1024  # 100 GB
VOLUME_TIER_WARN: int = 500 * 1024 * 1024 * 1024     # 500 GB


# ── Retention ───────────────────────────────────────────────────────────
# Keep the last N versions per workflow. Older versions are pruned when
# a new deploy is recorded. Deployed version is NEVER pruned regardless.
VERSION_RETENTION_COUNT: int = _int("FPULSE_VERSION_RETENTION", 20)

# Keep last N SQLite snapshots from the auto-backup on startup.
BACKUP_RETENTION_COUNT: int = _int("FPULSE_BACKUP_RETENTION", 5)


# ── Introspection helper ────────────────────────────────────────────────
def snapshot() -> dict:
    """Return a JSON-safe dict of the effective config.

    Used by ``/api/health/ready`` and the admin settings page so operators
    can confirm which limits are actually in force without shelling into
    the container to read env vars.
    """
    return {
        "mode": MODE,
        "is_prod": IS_PROD,
        "security_mode": SECURITY_MODE,
        "is_server_mode": IS_SERVER_MODE,
        "ai_allow_execute": AI_ALLOW_EXECUTE,
        "max_concurrent_runs": MAX_CONCURRENT_RUNS,
        "duckdb_memory_limit": DUCKDB_MEMORY_LIMIT,
        "duckdb_temp_directory": DUCKDB_TEMP_DIRECTORY,
        "duckdb_threads": DUCKDB_THREADS,
        "duckdb_preserve_order": DUCKDB_PRESERVE_ORDER,
        "max_upload_mb": MAX_UPLOAD_MB,
        "max_source_rows": MAX_SOURCE_ROWS,
        "dev_sample_rows": DEV_SAMPLE_ROWS,
        "version_retention_count": VERSION_RETENTION_COUNT,
        "backup_retention_count": BACKUP_RETENTION_COUNT,
    }


def volume_tier(file_size_bytes: int) -> dict:
    """Classify a file size into a volume tier for UX display.

    Returns a dict with ``tier``, ``label``, ``color``, and optionally
    ``warning`` suitable for rendering in the frontend source-node panel.
    """
    if file_size_bytes <= VOLUME_TIER_GOOD:
        return {"tier": "good", "label": "Optimal", "color": "green"}
    if file_size_bytes <= VOLUME_TIER_CAUTION:
        return {
            "tier": "caution",
            "label": "Supported with tuning — spill-to-disk active",
            "color": "amber",
            "warning": (
                "DuckDB will spill intermediate results to disk for this "
                "volume. Ensure the spill directory is on a fast SSD/NVMe "
                "and consider raising FPULSE_DUCKDB_MEMORY_LIMIT."
            ),
        }
    if file_size_bytes <= VOLUME_TIER_WARN:
        return {
            "tier": "warn",
            "label": "Careful pipeline design needed",
            "color": "orange",
            "warning": (
                "At this size, simple operations (filter → aggregate → "
                "output) run reliably. Wide joins may be slow. "
                "Multi-worker mode (F-Pulse+) is recommended."
            ),
        }
    return {
        "tier": "exceeds",
        "label": "Beyond single-node design",
        "color": "red",
        "warning": (
            "This volume exceeds F-Pulse's single-node design. "
            "Use a distributed execution engine for this workload."
        ),
    }
