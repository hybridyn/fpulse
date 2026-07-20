# F-Pulse worker pool, scaling, persistence

Per `edition-matrix.md` line 53-59 (execution & operations) and line 74 (persistence).

## OSS Free includes

### Single-binary local execution

The OSS install runs as a single FastAPI process on uvicorn. The same process owns:
- The HTTP API
- The pipeline executor (DuckDB-backed)
- The agent loop
- The scheduler
- The notification watchdog
- Background tasks (RAG indexer, product knowledge indexer, eval harness CLI)

**Single-node only.** No queue, no worker daemon, no horizontal scaling in OSS. Pipelines run in-process via the worker pool.

### Worker pool (in-process)

Per `edition-matrix.md` line 56. Every install gets the basic worker pool monitoring page:

- **Governor banner** — "currently running N of M workers". M is auto-detected from CPU count + RAM, capped via `FPULSE_MAX_CONCURRENT_RUNS`.
- **Spill-disk health** — when DuckDB spills hash partitions to disk, performance drops 10-100× on HDD vs SSD. The page shows the spill directory + an SSD/HDD badge.
- **Hardware presets** — "small laptop" / "workstation" / "VPS" / "production" — applies a sensible default for `FPULSE_DUCKDB_MEMORY_LIMIT`, threads, concurrent runs.
- **Queue depth** — how many pipelines are waiting for a worker slot.
- **Throughput** — runs/min over the last 5 / 15 / 60 minutes.

### Vertical scaling knobs (Free)

Per `docs/scaling.md`:

- **`FPULSE_DUCKDB_MEMORY_LIMIT`** — per-worker memory cap (default: 80% of host RAM)
- **`FPULSE_DUCKDB_THREADS`** — DuckDB parallelism per query (default: detected CPU cores)
- **`FPULSE_MAX_CONCURRENT_RUNS`** — global concurrent-run governor
- **`FPULSE_DUCKDB_TEMP_DIR`** — spill directory (set to an SSD for performance)

Settings → General → Execution Tuning shows the live values + the env var name beside each (read-only display; restart to apply changes).

### Worker-role guard (anti-footgun)

Per `edition-matrix.md` line 58. F-Pulse refuses to start in worker-only mode (`FPULSE_ROLE=worker`) on the OSS edition because the multi-worker queue isn't shipped — running multiple OSS containers against the same SQLite would corrupt state. Refuses fast, fails loud.

### Persistence: SQLite

OSS uses SQLite as the single source of truth. The database file lives at `<data_dir>/fpulse.db`. Schema currently at v23.

WAL mode is enforced for concurrent reads + 1 writer. `synchronous=NORMAL` for the WAL-safe value (~30% faster than FULL). 64 MB page cache, 256 MB read mmap window.

## F-Pulse+ adds

F-Pulse+ is a paid extension that adds multi-worker horizontal scaling (queue-based coordinator + worker model), richer Pool-page operator dashboards, and optional Postgres persistence for the metadata store on top of OSS. See [hybridyn.com/f-pulse](https://hybridyn.com/f-pulse).

## Scaling decision guide

**< 100 pipelines, single user:** OSS Free on a laptop or small VPS. SQLite + in-process worker pool. No Plus needed.

**100-1000 pipelines, 2-10 users:** OSS Free + a beefy VPS (Hetzner GEX44 ~$200/mo with 32 GB RAM + RTX 4000 SFF Ada is the reference). Vertical scaling via the env knobs above. SQLite remains fine.

**1000+ pipelines OR > 10 users OR strict SLAs:** F-Pulse+ for the queue + worker daemon + workspace RBAC + audit retention + SSO. Postgres for metadata.

**Compliance-bound (SOC 2 / HIPAA / ISO 27001):** F-Pulse+ for the audit retention + sigstore export + DPA. The OSS edition can be brought into compliance scope by the operator's own attestation but doesn't ship the controls a typical auditor expects out of the box.

## Anti-patterns

- ❌ "Just run `docker compose up --scale fpulse=4`" on OSS — the worker-role guard refuses because multi-worker is Plus only. Running multiple OSS containers against the same SQLite would corrupt state.
- ❌ "Switch OSS to Postgres" — there's no migration path in OSS. Postgres is a Plus optimization. Stay on SQLite for OSS.
- ❌ "Use Kubernetes to autoscale OSS" — same problem; multi-pod OSS is not supported. Plus supports k8s deployment.
- ❌ Telling a Free user to set `FPULSE_ROLE=worker` — that mode is for the Plus multi-worker setup, refused in OSS.
