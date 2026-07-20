# Sprint B — Postgres CDC integration test runbook

The decoder + LSN persistence are unit-tested against synthetic byte
streams (`backend/tests/test_pgoutput_decoder.py`). This runbook covers
the end-to-end gate: a real Postgres in Docker, an `ALTER TABLE`
mid-stream, and the decoder picking up the schema change without
operator intervention.

## One-time setup

```bash
docker compose -f docker-compose.test.yml up -d
# Wait ~5 seconds for the healthcheck to pass.
docker compose -f docker-compose.test.yml ps   # postgres-cdc should be (healthy)
```

The container exposes Postgres on **localhost:5433** (not 5432, to
avoid clobbering an existing local Postgres). User/password/db are all
`fpulse_test`.

Override via environment variables if you need a different host:

```bash
FPULSE_TEST_PG_HOST=10.0.0.5 FPULSE_TEST_PG_PORT=5432 pytest ...
```

## Run the test

```bash
pytest backend/tests/integration/test_cdc_postgres.py -v
```

Two tests run:

1. **`test_insert_decodes_via_real_postgres`** — INSERT a row, consume
   events from the slot, decode through `PgoutputDecoder`, assert the
   columns map correctly. Proves the byte-format reader matches what
   real Postgres emits (no synthetic fixture mismatch).

2. **`test_schema_change_bumps_schema_version`** — INSERT, ALTER TABLE
   ADD COLUMN, INSERT again. Decode all events. Assert the post-ALTER
   Insert carries the new column AND `schema_version >= 2`. **This is
   the Sprint B exit gate.**

If both pass, the CDC primitive is ready for the launch demo.

## Tear down

```bash
docker compose -f docker-compose.test.yml down -v
```

The `-v` flag drops the volume so the next run starts fresh —
important because replication slots otherwise survive container
restarts and the test would race against stale state.

## When the test fails

| Symptom | Cause | Fix |
|---|---|---|
| `connection refused` / skip message | Container isn't up or healthcheck hasn't passed | `docker compose ... ps` then wait or restart |
| `Replication slot already exists` | Previous run didn't clean up | `docker compose ... down -v` then restart |
| `wal_level must be logical` | The compose file's `command:` got overridden | Diff against `docker-compose.test.yml` |
| Decoder raises `unknown tuple value tag` | psycopg2 delivered an unsupported tag (binary, perhaps) | Add a case to `_read_tuple_data` in `pgoutput.py` |
| `schema_version did not advance past 1` | Decoder isn't seeing the second Relation message before the post-ALTER Insert | Increase `max_events` in `_consume_events` |

## What's NOT in scope here

- **Bulk-loader handoff.** The contract (initial-snapshot LSN handoff
  via `pg_export_snapshot`) is documented in
  `DESIGN_SPRINT1_BULK_LOADERS.md` but isn't wired into a node yet.
  Closing this is the next CDC milestone.
- **MySQL / SQL Server CDC.** Different protocol entirely. Defer.
- **Streaming-mode messages** (`Stream Start/Stop/Commit/Abort`).
  Requires `streaming=on` which we don't enable.

## What good looks like

```
backend/tests/integration/test_cdc_postgres.py::test_insert_decodes_via_real_postgres PASSED
backend/tests/integration/test_cdc_postgres.py::test_schema_change_bumps_schema_version PASSED

============== 2 passed in 4.2s ==============
```

Sub-30s end-to-end on a laptop is the launch-criterion target. Both
tests should comfortably finish in well under that.
