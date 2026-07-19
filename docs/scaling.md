# Vertical scaling

F-Pulse OSS is **single-node by design**. With proper tuning it handles pipelines up to ~500 GB on a tuned VPS. For multi-worker horizontal scaling, see [F-Pulse+](editions.md).

> This guide is about **capacity**. For install, upgrade, backup, and
> disaster recovery, see the canonical operator runbook in the
> [Deployment guide](deployment.md).

## Volume tiers

| Size | Status | What you need |
|---|---|---|
| **<10 GB** | Optimal | Default config, any laptop |
| **10–100 GB** | Supported with tuning | SSD spill path, raised memory cap |
| **100–500 GB** | Careful pipeline design | Avoid wide joins; consider F-Pulse+ multi-worker |
| **>500 GB** | Beyond single-node design | Use a distributed-compute platform |

These tiers are calibrated against the 2026 audit — older copy ("50 GB ceiling") was too conservative.

## The 4 vertical-scaling knobs

All tunable via environment variables. Restart the backend to apply changes — values are read once at import time.

| Variable | Default (prod) | Effect |
|---|---|---|
| `FPULSE_MAX_CONCURRENT_RUNS` | `4` | Number of pipelines that can execute simultaneously. Each gets its own DuckDB connection. `0` = unlimited. |
| `FPULSE_DUCKDB_MEMORY_LIMIT` | `4GB` | Per-worker memory ceiling. Forces spill-to-disk instead of OOM. |
| `FPULSE_DUCKDB_THREADS` | half of CPUs | Caps DuckDB so it doesn't starve FastAPI/scheduler/WebSocket. |
| `FPULSE_DUCKDB_TEMP_DIR` | `./data/duckdb_spill` | Spill target. **Must be SSD/NVMe** — wrong disk turns 30s into 30min. |

## Reference configurations

### Laptop (8 GB RAM, 4 CPU cores)
```bash
FPULSE_MODE=prod
FPULSE_MAX_CONCURRENT_RUNS=2
FPULSE_DUCKDB_MEMORY_LIMIT=2GB
FPULSE_DUCKDB_THREADS=2
```

### VPS (16 GB RAM, 8 CPU cores)
```bash
FPULSE_MODE=prod
FPULSE_MAX_CONCURRENT_RUNS=4
FPULSE_DUCKDB_MEMORY_LIMIT=2GB
FPULSE_DUCKDB_THREADS=2
FPULSE_DUCKDB_TEMP_DIR=/mnt/nvme/duckdb_spill
```

### Production (64 GB RAM, 16 CPU cores — Hetzner GEX44 reference)
```bash
FPULSE_MODE=prod
FPULSE_MAX_CONCURRENT_RUNS=8
FPULSE_DUCKDB_MEMORY_LIMIT=6GB
FPULSE_DUCKDB_THREADS=2
FPULSE_DUCKDB_TEMP_DIR=/mnt/nvme/duckdb_spill
FPULSE_MAX_UPLOAD_MB=2000
FPULSE_MAX_SOURCE_ROWS=50000000
```

## The Global Resource Governor

F-Pulse has a built-in **admission controller** that samples system memory + CPU every few seconds and refuses new work when pressure crosses tier thresholds:

| Tier | Entry | Behavior |
|---|---|---|
| GREEN | mem < 70% AND cpu < 85% | Accept all spawns |
| YELLOW | 70–80% mem OR cpu ≥ 85% | Queue pipelines, reject non-queueable spawns |
| ORANGE | 80–90% mem | Same as YELLOW + emit slow-signal to reducers |
| RED | mem ≥ 90% | Reject everything until pressure relieves |

Hysteresis (5 percentage points) prevents flapping. The current tier is shown live on the **Pool** admin page.

If you see jobs being queued or rejected unexpectedly, check the Pool page banner — it tells you why ("Throttling — memory at 78% (yellow tier)").

## Spill-disk health check

The Pool → Configuration tab shows a **Spill-Disk Health** card with three signals:

- **Disk type** — SSD ✓ / HDD ⚠ / Unknown (Linux: read from `/sys/block/*/queue/rotational`)
- **IO-wait %** — healthy (<10%) / elevated (10–25%) / saturated (>25%)
- **Spill directory path**

DuckDB performance drops 10–100× on HDD spill. If you see HDD ⚠, move the spill directory to SSD/NVMe before running anything serious.

## Scaling beyond a single node

When vertical scaling stops being enough:

| You need | Use |
|---|---|
| Multi-worker on a queue with a shared database backend | **F-Pulse+** — see [editions](editions.md) |
| Distributed compute over Iceberg/Delta tables | A dedicated distributed-compute platform |

## See also

- [Deployment](deployment.md) — production deployment, reverse proxy, TLS, backups
- [Security deployment guide](security-deployment.md) — rate limits, hardening checklist
- [Performance budgets](performance.md) — per-tool latency targets
