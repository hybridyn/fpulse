# SQL Server — F-Pulse connector deep-dive

P3 of the reviewer audit (2026-06-08): second of the five "deepen
serious connectors" passes. Everything an operator needs to use
F-Pulse's SQL Server connector confidently in production, including
a runnable smoke-test.

> **Status: Beta** (per the cert-matrix at `GET /api/connectors/cert-matrix`).
> Live-DB smoke test is in the repo; promotion to **Verified** requires
> a 7-day green streak across the smoke-test in CI plus a named owner,
> per the OSS connector certification rubric in `docs/connectors.md`.

## What works in OSS today

| Capability | Node | How |
|---|---|---|
| **Source** — read from a SQL Server table or arbitrary T-SQL query | `db_source` | Pick a connection of type `mssql`/`sqlserver`, supply schema + table OR a free T-SQL query |
| **Sink** — write to a SQL Server table | `db_sink` | Connection + target table; insert/upsert/merge modes per `sync_mode` |
| **Schema discovery** — `INFORMATION_SCHEMA.COLUMNS` walk | `describe_table_columns()` | Mapping tab → *Import destination schema*; works against any saved SQL Server connection |
| **Test connection** — protocol-level ping | `POST /api/connections/{id}/test` | Eye-icon button in Connections page; auto-records to Steward's `connector_health` sidecar |
| **Sample preview** — `SELECT TOP N *` | per-node preview | Right-click any source node → Preview |
| **Incremental sync** — cursor + watermark column | `db_source.sync_mode` | `incremental` mode reads only rows where `watermark_column > last_watermark` |
| **Backfill** — re-read full history with safety preflight | preflight modal in canvas | Estimates row count, confirms before any read |
| **Windows Authentication** | connection config | Set `trusted_connection: yes` and leave username/password blank |

## Driver installation

F-Pulse uses [pyodbc](https://pypi.org/project/pyodbc/) — the standard
Python ODBC driver. You need BOTH the Python wrapper and the system
ODBC driver:

```powershell
# Python wrapper (any platform)
pip install pyodbc
```

System ODBC driver — pick one per OS:

| OS | Recommended driver | Install |
|---|---|---|
| **Windows 10/11/Server** | ODBC Driver 18 for SQL Server | [Download from Microsoft](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server). MSI installer; no reboot needed. |
| **Ubuntu / Debian** | `msodbcsql18` | `curl https://packages.microsoft.com/keys/microsoft.asc \| sudo apt-key add -` then `apt install msodbcsql18` (see Microsoft's Linux quickstart for the full apt source setup) |
| **RHEL / Fedora** | `msodbcsql18` | `yum install msodbcsql18` after adding the Microsoft RPM repo |
| **macOS** | `msodbcsql18` | `brew tap microsoft/mssql-release && brew install msodbcsql18` |

ODBC Driver **17** still works if your platform doesn't have 18 yet;
just change the driver name in your connection config. Driver 18
enables TLS-by-default which Driver 17 does not — preferred for new
deployments.

## Credential model

Two F-Pulse stores are involved, same shape as PostgreSQL:

| Store | What it holds for SQL Server |
|---|---|
| **ConnectionStore** | server, port (default 1433), database, driver name, encrypt (`yes`/`no`/`strict`), trust_server_certificate, trusted_connection (Windows Auth flag) |
| **CredentialStore** | username, password (encrypted at rest via the master key) — empty when using Windows Authentication |

For **Windows Authentication** (Active Directory) — set
`trusted_connection: yes` in the connection config; leave username +
password empty. The F-Pulse process must be running under a Windows
identity that has the right SQL Server permissions.

For **Azure AD authentication** (Azure SQL / managed identity) — set
`authentication: ActiveDirectoryServicePrincipal` (or
`ActiveDirectoryMSI` for managed identity), supply `client_id` /
`client_secret` in the credential. Production hardening guidance is in
the cloud-specific section below.

## TLS / encryption — important defaults

Driver 18 changed the defaults vs Driver 17:

| Setting | Driver 17 default | Driver 18 default | Recommendation |
|---|---|---|---|
| `Encrypt` | `no` | `yes` | **Always `yes`** for any non-loopback target. Set explicitly. |
| `TrustServerCertificate` | `no` | `no` | Leave `no` in production; set `yes` only against a dev SQL Server with self-signed cert |

If you're getting "SSL Provider: The certificate chain was issued by
an authority that is not trusted" errors, the server's TLS cert isn't
in your machine's trust store. Either install the cert chain, switch
to `TrustServerCertificate=yes` (dev only), or set `Encrypt=no` (test
only — never in production).

## Incremental sync — how the cursor works

Same model as PostgreSQL (see `docs/connectors/postgres.md` for the
full description). When `db_source.sync_mode = "incremental"`:

1. F-Pulse reads the last watermark for `(connection_id, table,
   watermark_column)` from the incremental sync state store.
2. Issues `SELECT * FROM <schema>.<table> WHERE <watermark_column> >
   ? ORDER BY <watermark_column>` (parameterised — pyodbc handles the
   binding safely).
3. After the read succeeds, writes the new max watermark back to the
   state store.

Watermark column must be monotonically non-decreasing — typically a
`datetime2` / `datetimeoffset` `LastModified` column, or a `bigint`
identity. SQL Server's `ROWVERSION` (`timestamp`) datatype is also a
good fit if you have one — it's monotonically increasing per row and
bumps automatically on UPDATE.

For exact CDC (capture rows DELETED, capture column-level diffs), use
SQL Server's native CDC feature exposed via the `cdc.*` schema; F-Pulse
can read those CDC tables via the standard `db_source` node, but the
F-Pulse-side CDC adapter for SQL Server is on the roadmap (not in
1.1).

## Smoke test (run this before relying on the connector in production)

```powershell
# Against a saved F-Pulse connection (uses connection_store + credentials):
python "<repo>\backend\scripts\sqlserver_smoke_test.py" `
    --connection-id conn-1234

# Against a raw ODBC connection string (no F-Pulse state needed):
python "<repo>\backend\scripts\sqlserver_smoke_test.py" `
    --connection-string "DRIVER={ODBC Driver 18 for SQL Server};SERVER=db.example.com,1433;DATABASE=orders;UID=app;PWD=pass;Encrypt=yes;TrustServerCertificate=no"

# Focus on one specific table:
python "...sqlserver_smoke_test.py" --connection-string ... `
    --probe-schema dbo --probe-table Customers
```

The script runs six checks (connect, version, list_schemas,
list_tables, describe_table, read_sample), reports PASS/FAIL with
timing for each, and exits 0 on all-pass / 1 on any failure. **It
performs only reads** — safe to run against production.

If `FPULSE_URL` + `FPULSE_TOKEN` are set in the environment AND
`--connection-id` was used, the final pass/fail is POSTed to
`/api/steward/connector-health` — the result flows into Steward's
connector-health detector, giving you "this SQL Server has been red
for 2 days" alerting without a separate monitoring stack.

## Common pitfalls + how F-Pulse handles them

| Pitfall | F-Pulse behaviour |
|---|---|
| Driver name in connection config doesn't match what's installed | The connect step fails clearly: `IM002: Data source name not found and no default driver specified`. Check `Get-OdbcDriver` (Windows) or `odbcinst -q -d` (Linux). |
| Password contains `;` (breaks ODBC string parser) | Use the saved-connection path (where F-Pulse builds the string from typed fields) rather than passing `--connection-string` by hand. |
| Windows Auth fails under a service account | The F-Pulse service must run as a domain user with the right SQL Server permissions. Check the service-config user, not the operator's own user. |
| Azure SQL connection timeouts | Default `LoginTimeout` is 30s; under cold-start Azure SQL serverless that's often too short. Add `Login Timeout=60` to the connection string. |
| `INSERT EXEC` deadlocks during sink writes | Set isolation level explicitly via `SET TRANSACTION ISOLATION LEVEL READ COMMITTED SNAPSHOT` at session start — covered by a future built-in option; today add it manually via a pre-query step. |
| Identity-column inserts blocked by `IDENTITY_INSERT` | `db_sink` sets `SET IDENTITY_INSERT <table> ON` automatically when the target table has an identity column being explicitly written. Use the standard sink config; don't try to bypass with a custom T-SQL pre-step. |
| AlwaysOn / read-replica routing | Connection string `ApplicationIntent=ReadOnly` directs reads to a secondary replica when AlwaysOn is configured. Add it to the saved connection's `extra_options`. |

## What F-Pulse Plus adds on top

| Capability | OSS | Plus |
|---|---|---|
| All of the above | ✓ | ✓ |
| Visual incremental-sync configurator (no manual watermark column entry) | — | ✓ |
| Native CDC adapter (reads `cdc.*` change tables with row-level granularity) | — | ✓ |
| Scheduled connection-health probes (separate from user Test clicks) | — | ✓ |
| Connection pool sharing across workflows | — | ✓ |
| Bulk-load via `BULK INSERT` / SQL Server bulk-copy API | basic via `INSERT` | bulk-copy path with per-batch tuning |
| Audit log of every read/write per connection | — | ✓ |

## Reference

- `backend/fpulse/nodes/db_source.py` — read implementation (shared with PostgreSQL / MySQL / DuckDB)
- `backend/fpulse/nodes/db_sink.py` — write implementation
- `backend/fpulse/connectors/jdbc_dialects.py` — dialect-specific quoting / type mapping
- `backend/fpulse/engine/bulk_load/dialects/` — bulk-load dialect entry point (SQL Server bulk path on roadmap)
- `backend/scripts/sqlserver_smoke_test.py` — the script described above

## See also

- [`docs/connectors.md`](../connectors.md) — full first-party catalog + cert matrix
- [`docs/connectors/postgres.md`](postgres.md) — companion deep-dive for PostgreSQL (same shape)
- [`docs/steward/connector-health.md`](../steward/connector-health.md) — how smoke-test results feed Steward
- [`docs/extend/build-a-connector.md`](../extend/build-a-connector.md) — write your own connector if F-Pulse doesn't ship the one you need
