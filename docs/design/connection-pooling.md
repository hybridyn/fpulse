# Connection Pooling — Design

> Critical #5 from `NODE_AUDIT_MATRIX.md`. Deferred 6 sessions; designing properly here so the implementation is safe rather than rushed.
>
> Per `DESIGN_DECISIONS.md` D-002: **5 concurrent per `connection_id`**, env-var override, keyed by `(connection_id, run_id)`, lifecycle hooks for run-end cleanup.

---

## Why this matters

Today every step opens its own database connection via `_get_connection_config()` → fresh `psycopg2.connect(...)` / `pymysql.connect(...)` / etc. A 12-step workflow against the same Postgres opens 12 connections. With parallel branches that grows multiplicatively. On a Postgres with `max_connections=100`, a few concurrent runs can exhaust the database.

**Fixed cost per connection** (typical Postgres):
- TCP handshake + TLS: 30-100 ms
- Auth + role check: 20-50 ms
- Total: **50-150 ms per step**, paid every time

**With pooling**, a 12-step workflow pays the cost once, not 12 times. Multi-step pipelines see 500-1500 ms shaved off wall-clock time — significant on hot paths.

---

## Constraints

1. **No regression**: existing `_get_connection_config()` callers must keep working unchanged.
2. **Additive integration**: pool wraps the connect logic; if pool is unavailable / disabled, the legacy path runs.
3. **Credential rotation safety**: when a user updates a connection's password, the pool must invalidate stale entries on next access — never silently keep using the old credentials.
4. **No cross-run leakage**: a connection borrowed by run A must NOT be reusable by run B. Pool is keyed by `(connection_id, run_id)`.
5. **Bounded**: per-`connection_id` cap is 5 (env-overridable via `FPULSE_CONNECTION_POOL_SIZE`).
6. **Lifecycle**: when a run finishes (success / failure / cancellation), all its borrowed connections are returned + closed, not held.

---

## API surface (proposed)

```python
# backend/fpulse/engine/connection_pool.py

class ConnectionPool:
    """Per-run connection cache. Reuses live driver connections across
    steps in a single run; closes them at run end. Not a long-lived
    pool (so credential rotation works correctly) — more like a
    'session cache' that survives only the current execute_workflow call.
    """

    def __init__(self, max_per_connection: int = 5):
        self._cache: dict[tuple[str, str], list[Any]] = {}  # (conn_id, run_id) → [drivers]
        self._max = max_per_connection
        self._lock = threading.RLock()

    def acquire(
        self, *, connection_id: str, run_id: str, conn_type: str,
        config: dict[str, Any], factory: Callable[[str, dict], Any],
    ) -> Any:
        """Borrow a driver connection. If the pool has a free one for
        this (conn_id, run_id), return it. Otherwise call factory()
        to create a new one (subject to the per-connection cap)."""
        ...

    def release_run(self, run_id: str) -> None:
        """Close every connection borrowed by this run. Called from
        WorkflowExecutor.execute_workflow finally-block."""
        ...

    def invalidate_connection(self, connection_id: str) -> None:
        """Drop all cached entries for this connection_id (every run).
        Called when a credential is rotated so the next acquire creates
        a fresh connection with the new password."""
        ...
```

**Wired into `app_state`**:
```python
app_state["connection_pool"] = ConnectionPool(
    max_per_connection=int(os.environ.get("FPULSE_CONNECTION_POOL_SIZE", "5"))
)
```

**Used by `_get_connection_config` callers**:
```python
# Old:
conn = psycopg2.connect(host=cfg["host"], ...)
# ... use conn ...
conn.close()

# New (opt-in via run context):
pool = ctx.app_state.get("connection_pool")
run_id = ctx.run_id
if pool and run_id:
    conn = pool.acquire(
        connection_id=connection_id, run_id=run_id, conn_type="postgresql",
        config=cfg, factory=lambda ct, c: psycopg2.connect(host=c["host"], ...)
    )
    # NO close() — pool owns it; release_run() closes at run end
else:
    # legacy path
    conn = psycopg2.connect(host=cfg["host"], ...)
    try:
        # ... use conn ...
    finally:
        conn.close()
```

---

## Lifecycle integration in `WorkflowExecutor.execute_workflow`

```python
def execute_workflow(self, workflow, ..., run_id=None):
    run_id = run_id or uuid.uuid4().hex
    pool = self.app_state.get("connection_pool")
    try:
        # ... existing execution ...
    finally:
        if pool and run_id:
            pool.release_run(run_id)
```

This is the **load-bearing safety net**: every connection borrowed during the run is guaranteed to be closed at run-end, even if the run threw an exception or was killed by the wall-clock cap.

---

## Credential rotation hook

When `PUT /api/connections/{id}` updates credentials, the connections API endpoint calls:
```python
pool = app_state.get("connection_pool")
if pool:
    pool.invalidate_connection(connection_id)
```

The next time a step asks for a connection, it gets a fresh driver connection with the new password. Open connections held by other in-flight runs are NOT killed — they're allowed to finish their current step, and the next step that needs the same `connection_id` will get a new one.

---

## Per-dialect drivers

The pool is dialect-agnostic — `factory` is a closure provided by the caller. Each call site writes its own factory. Dialects supported by the existing F-Pulse codebase:

| conn_type | Driver | Status |
|---|---|---|
| `postgresql` | `psycopg2` | Phase 1 — implement first |
| `mysql` | `pymysql` | Phase 2 |
| `sqlite` | `sqlite3` | Phase 2 — note: SQLite connections are not safe to share across threads; pool should set thread-local for SQLite or skip pooling |
| `mssql` | `pyodbc` / `pymssql` | Phase 3 |
| `snowflake` | `snowflake.connector` | Phase 3 |
| `bigquery` | `google.cloud.bigquery` | Phase 3 — note: BQ uses HTTP, not persistent conn — pool benefit is auth-token reuse only |
| `redshift` | uses `psycopg2` | Phase 1 (same as Postgres) |

---

## Test plan

1. **Pool warm-up**: first acquire creates a new connection; second acquire (same run, same conn_id) returns the same instance.
2. **Per-connection cap**: 6th concurrent acquire on the same `connection_id` blocks or errors with a clear message.
3. **Run-end cleanup**: after `release_run(run_id)`, all that run's connections are closed; `pool._cache` has no entries for that run_id.
4. **Cross-run isolation**: run A's connection is NOT reusable by run B even if same conn_id (verify by integration test).
5. **Credential rotation**: rotate password → `invalidate_connection()` → next acquire returns a NEW connection that uses the new password (verify by `psycopg2.OperationalError` if the cached one were used with the old password).
6. **Exception safety**: if the workflow throws, `release_run()` still runs (try/finally test).
7. **Wall-clock-cap safety**: if the run is killed by `WorkflowExecutor`'s timeout, `release_run()` still fires.

---

## Implementation phases

| Phase | Scope | Effort |
|---|---|---|
| **1. Skeleton** | `ConnectionPool` class + tests + docs (no wiring yet) | 1 day |
| **2. Postgres wiring** | `db_source.py` + `db_sink.py` + `flow_control.py` Copy Data path | 1 day |
| **3. Lifecycle** | `WorkflowExecutor.execute_workflow` try/finally + run_id propagation | 0.5 day |
| **4. Credential rotation** | `PUT /api/connections/{id}` invalidation hook | 0.5 day |
| **5. Other dialects** | MySQL, SQLite, MSSQL, Snowflake, BigQuery, Redshift | 1.5 days |
| **6. Hardening** | Concurrency tests, integration tests, docs | 1 day |
| **Total** | | **~5.5 days** |

---

## What NOT to build (explicitly out of scope)

- **Long-lived pool (across runs)**: this would defeat credential rotation and create surprising "why is my old password still working?" bugs. The pool is a per-run cache, not a long-lived pool.
- **Dynamic pool resizing**: 5 is the hardcoded default; env var overrides at startup. No runtime resizing.
- **Pool metrics page**: out of scope — observability of pool size + hit rate is a Plus feature in the Pool monitoring page.
- **Async pool**: F-Pulse's executor is sync (DuckDB is sync). When it goes async, redesign.
- **Connection probing/health-check**: just-in-time error handling is simpler. If a cached connection is dead (network blip, server restart), the next query throws `OperationalError`, the pool catches it, drops the cached entry, and retries with a fresh connection. No "ping every N seconds" logic.

---

## Skeleton — see [`backend/fpulse/engine/connection_pool.py`](backend/fpulse/engine/connection_pool.py)

Phase 1 deliverable. Additive — does NOT modify any existing call site. The next phase wires it into `db_source._get_connection_config()`.
