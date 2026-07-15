# PostgreSQL — F-Pulse connector deep-dive

P3 of the reviewer audit (2026-06-07): "pick 5 serious connectors and
make them excellent." This is the PostgreSQL deepening pass —
everything an operator needs to use F-Pulse's PostgreSQL connector
confidently in production, including a runnable smoke-test.

> **Status: Beta** (per the cert-matrix at `GET /api/connectors/cert-matrix`).
> Live-DB smoke test is in the repo; promotion to **Verified** requires
> a 7-day green streak across the smoke-test in CI plus a named owner,
> per the OSS connector certification rubric in `docs/connectors.md`.

## What works in OSS today

| Capability | Node | How |
|---|---|---|
| **Source** — read from a Postgres table or arbitrary query | `db_source` | Pick a connection of type `postgres`, supply schema + table OR a free SQL query |
| **Sink** — write to a Postgres table | `db_sink` | Connection + target table; insert/upsert/merge modes per `sync_mode` |
| **Schema discovery** — list columns + types for a table | `describe_table_columns()` helper | Mapping tab → *Import destination schema*; works against any saved Postgres connection |
| **Test connection** — protocol-level ping | `POST /api/connections/{id}/test` | Eye-icon button in Connections page; also auto-records to Steward's `connector_health` sidecar |
| **Sample preview** — fetch first N rows | per-node preview | Right-click any source node → Preview |
| **Incremental sync** — cursor + watermark column | `db_source.sync_mode` | `incremental` mode reads only rows where `watermark_column > last_watermark` |
| **Backfill** — re-read full history with safety preflight | preflight modal in canvas | Estimates row count, confirms before any read |

## Driver installation

F-Pulse uses [psycopg2](https://pypi.org/project/psycopg2/) (the
mature Python Postgres driver) to connect. Pick **one** of:

```bash
# Easiest — bundled binary, no system deps:
pip install psycopg2-binary

# Production — requires system libpq-dev / postgres-devel:
pip install psycopg2
```

The bundled `psycopg2-binary` is fine for almost everyone. The
non-binary build is recommended only if you want the driver to link
against a libpq version your DBA team has pinned (e.g. for FIPS or
custom SSL config).

## Credential model

Two F-Pulse stores are involved:

| Store | What it holds for Postgres |
|---|---|
| **ConnectionStore** | host, port, database, schema, ssl_mode (visible to all workspace users with the Connections page enabled) |
| **CredentialStore** | username, password, ssl_cert, ssl_key (encrypted at rest via the master key; access gated by RBAC) |

The recommended pattern: create one Credential per `(database role,
environment)` and let multiple Connections reuse it via
`connection.credential_id`. Rotating a password updates the Credential
once and every Connection picks it up at the next test.

## Incremental sync — how the cursor works

When `db_source.sync_mode = "incremental"`, F-Pulse:

1. Looks up the last watermark for `(connection_id, table, watermark_column)`
   from the **incremental sync state store** (see task #45 in `CHANGELOG.md`).
2. Issues `SELECT * FROM <table> WHERE <watermark_column> > <last_watermark>
   ORDER BY <watermark_column>`.
3. After the read succeeds, writes the new max watermark back to the state store.

The watermark column must be **monotonically non-decreasing** —
typically `updated_at`, `created_at`, or a serial/identity column.
Mutations to old rows that don't bump the watermark will be missed;
if you need exact CDC, use the Postgres `pgoutput` logical replication
adapter at `backend/fpulse/connectors/pgoutput.py` (currently
experimental).

## Smoke test (run this before relying on the connector in production)

```powershell
# Against a saved F-Pulse connection (uses connection_store + credentials):
python -m fpulse.scripts.postgres_smoke_test --connection-id conn-1234

# Against a raw DSN (no F-Pulse state needed):
python -m fpulse.scripts.postgres_smoke_test --dsn `
    "postgresql://app:pass@db.example.com:5432/orders"

# Focus on one specific table:
python -m fpulse.scripts.postgres_smoke_test --dsn ... `
    --probe-schema public --probe-table customers
```

The script runs six checks (connect, version, list_schemas, list_tables,
describe_table, read_sample), reports PASS/FAIL with timing for each,
and exits 0 on all-pass / 1 on any failure. It performs **only reads**
— safe to run against production.

If `FPULSE_URL` + `FPULSE_TOKEN` are set in the environment AND
`--connection-id` was used, the script also POSTs the final pass/fail
to `/api/steward/connector-health` so the result flows into Steward's
connector-health detector. That gives you a real "connector X has
been red for 2 days" alert path without needing a separate monitoring
stack.

## Common pitfalls + how F-Pulse handles them

| Pitfall | F-Pulse behaviour |
|---|---|
| Password contains `@` / `:` / `/` (would break a naive DSN) | The smoke-test script `urllib.parse.quote`s creds before building the DSN. The runtime path uses keyword args, not DSN, so this isn't an issue there. |
| SSL required by the server, client doesn't request it | Connection config exposes `sslmode` (`disable` / `prefer` / `require` / `verify-ca` / `verify-full`); leave it set to `require` for any non-loopback target. |
| Connection pool exhaustion under high concurrency | `FPULSE_CONNECTION_POOL_SIZE` (env, default 5) caps per-connection pool size. Combined with the scheduler's `--concurrent-jobs` limit, exhaustion is rare on single-machine workloads. |
| Long-running queries blocking shutdown | The executor signals a query cancel before tearing down; psycopg2 respects it within ~1s. |
| Timezone confusion (server in UTC, client interprets as local) | Connection config has `timezone` override; defaults to UTC. Cast columns explicitly in your transforms if you need a specific tz. |
| Schema discovery returns 0 columns | Usually means the connection user lacks `USAGE` on the schema or `SELECT` on the table. Test with `SELECT * FROM information_schema.tables WHERE table_schema='public' LIMIT 1` in psql under the same role. |

## What F-Pulse Plus adds on top

| Capability | OSS | Plus |
|---|---|---|
| All of the above | ✓ | ✓ |
| Visual incremental-sync configurator (no manual watermark column entry) | — | ✓ |
| pgoutput CDC adapter (logical replication) — production-tier | experimental in `connectors/pgoutput.py` | hardened + supported |
| Scheduled connection-health probes (separate from user Test clicks) | — | ✓ |
| Connection pool sharing across workflows | — | ✓ |
| Audit log of every read/write per connection | — | ✓ |

## Reference

- `backend/fpulse/nodes/db_source.py` — read implementation
- `backend/fpulse/nodes/db_sink.py` — write implementation
- `backend/fpulse/connectors/jdbc_dialects.py` — dialect specifics
- `backend/fpulse/connectors/pgoutput.py` — experimental CDC
- `backend/fpulse/engine/bulk_load/dialects/postgres.py` — bulk-load path
- `backend/scripts/postgres_smoke_test.py` — the script described above

## See also

- [`docs/connectors.md`](../connectors.md) — full first-party catalog + cert matrix
- [`docs/steward/connector-health.md`](../steward/connector-health.md) — how smoke-test results feed Steward
- [`docs/extend/build-a-connector.md`](../extend/build-a-connector.md) — write your own connector if F-Pulse doesn't ship the one you need
