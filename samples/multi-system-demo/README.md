# Five cross-system F-Pulse OSS demos

Five additional pipelines, numbered 19–23, with **seven nodes each** and
five distinct source systems and destination systems. They use real database,
HTTP and S3 connectors. The data is synthetic; no production data is needed.

| Pipeline | Source | Destination | Output |
|---|---|---|---|
| 19 Sales summary | SQL Server | PostgreSQL | `public.fpulse_demo_19_summary` |
| 20 API order report | REST API `GET /orders` | SQL Server | `dbo.fpulse_demo_20_summary` |
| 21 Subscription totals | PostgreSQL | MySQL | `fpulse_demo.fpulse_demo_21_summary` |
| 22 Inventory export | MySQL | S3-compatible MinIO | `fpulse-demo/output/inventory-summary.parquet` |
| 23 Shipment API delivery | S3-compatible MinIO | REST API `POST /results/shipments` | API receipt persisted to `runtime/api-shipments.json` |

All five follow the same teachable flow:

**Source → filter → remove duplicates (SQL DISTINCT) → calculate amount →
aggregate by region → sort → destination.**

The six fixture rows intentionally include an exact duplicate, a cancelled
record and a negative quantity. Per-node row counts are `6, 4, 3, 3, 2, 2, 2`.
Every destination should contain North = **275** and South = **150**.
Database outputs, the S3 output object and the API receipt are replaced on
reruns, so totals do not accumulate. These are separate demo outputs.

## Current machine

The five pipelines and five `Demo ...` connections have been installed in the
**default workspace** of `data/samples/fpulse.db`. Refresh the Pipelines page
and look for pipelines **19–23**. The existing SQL Server connection is reused
with the user's approval: only `fpulse_test.dbo.fpulse_demo_input` and
`fpulse_test.dbo.fpulse_demo_20_summary` are demo tables there.

All five saved connections are tagged **DEV**, available to all projects in
the default workspace, and support both reads and writes. Refresh
**DEV → Connections** to see `Demo sqlserver`, `Demo postgres`, `Demo mysql`,
`Demo api`, and `Demo s3`. Create missing connections, repair visibility, and
refresh their actual connection-test status with:

```powershell
.venv/Scripts/python.exe samples/multi-system-demo/demo.py connections --local-db D:/Siva/hybridyn-f-pulse-oss/data/samples/fpulse.db
```

PostgreSQL, MySQL and MinIO run in a dedicated Compose project; the sample
API runs locally on port 28080. The SQL Server container is optional because
the current Docker VM has insufficient memory for SQL Server. No Docker
memory settings were changed.

All five pipelines were executed twice with the F-Pulse executor, and each
destination was independently read back after every run. The detailed result
is in `runtime/verification.json`. Six regression tests cover the S3 source
download-lifetime fix across CSV/JSON/Parquet and boto3/HTTP transports.

**Before running from the existing UI:** restart the backend to load the S3
source fix. Start the local demo backend with
`FPULSE_API_SOURCE_ALLOW_PRIVATE=1`, which is required for the loopback REST
source. The verification runner sets this only in its own process; it does
not change the running app's security configuration. For the repository
launcher, close the existing instance normally, then launch from a PowerShell
session with:

```powershell
$env:FPULSE_API_SOURCE_ALLOW_PRIVATE = '1'
.\start.ps1
```

This opt-in permits private-network API sources in that backend process;
use it for this trusted local demo session. The CLI verification command below
can be used immediately without restarting the current app.

## Reproduce on a fresh machine

Run commands from the repository root, using its Python environment with
F-Pulse dependencies (`pyodbc`, `psycopg2`, `pymysql`, `boto3`, and `pyarrow`).
Windows SQL Server access also needs Microsoft ODBC Driver 18.
Docker needs at least 2 GB available to SQL Server **in addition to** the other
services. All published container ports bind to `127.0.0.1`.

```powershell
.venv/Scripts/python.exe samples/multi-system-demo/demo.py prepare
docker compose -f samples/multi-system-demo/compose.yaml --profile sqlserver up -d
```

`prepare` writes a random demo password to ignored `.env` and regenerates the
fixture and pipeline JSON files. Keep `.env` when restarting existing volumes.
The MinIO image comes from its [documented Quay registry](https://github.com/minio/minio/blob/master/docs/docker/README.md).
MySQL is configured with `ANSI_QUOTES` because the current warehouse writer
uses ANSI double-quoted identifiers.

Start the sample API in another terminal:

```powershell
.venv/Scripts/python.exe samples/multi-system-demo/sample_api.py
```

After the databases are ready:

```powershell
.venv/Scripts/python.exe samples/multi-system-demo/demo.py seed
.venv/Scripts/python.exe samples/multi-system-demo/demo.py run
```

`seed` creates a dedicated `fpulse_demo` database in SQL Server. It creates
missing fixture tables and refuses to overwrite different existing fixture
data. `run` executes all five workflows twice through the actual executor,
checks every node's row count, then reads all five destinations back.
A failed check exits nonzero. It does not silently fall back to mock connectors.

## Reuse an existing SQL Server

Put the connection configuration in ignored `runtime/sqlserver-config.json`
using the usual F-Pulse fields: `host`, `port`, `database`, `user` or `username`,
`password`, and optionally `odbc_driver`. Never commit this file.
For an explicitly approved existing demo database:

```powershell
docker compose -f samples/multi-system-demo/compose.yaml up -d
.venv/Scripts/python.exe samples/multi-system-demo/demo.py seed --existing-sqlserver-database
.venv/Scripts/python.exe samples/multi-system-demo/demo.py run
```

Without SQL Server, `seed --skip-sqlserver` and `run --skip-sqlserver` exercise
pipelines 21–23. This partial mode does not count as verification of all five.

## Import

### Import from the Pipelines page

Use the **`.fpulse` files in `exports/`** with **Pipelines → Import**.
These use the application's version-2 export envelope and preserve the
`multidemo_*` connection IDs installed by `import-local`. On the current
machine, in the default workspace, leave the connection mappings at
**Keep original**. Each preview should show **7 steps and 6 edges**.
Import creates another copy; the previously installed pipelines 19–23 are
also available directly in the list.

The flat `.json` files in `pipelines/` are inputs for the setup scripts;
they are not `.fpulse` exports and the Pipelines-page importer rejects them.
Regenerate the UI files with:

```powershell
.venv/Scripts/python.exe samples/multi-system-demo/demo.py export
```

For a different installation/workspace, first install the demo connections
there using the setup importer or configure and remap them separately. The
`.fpulse` exports contain connection references, never passwords.

### Script import

The tracked pipeline files refer to connection **names** and contain no
credentials. Use one of these importers to bind them to saved connection IDs.

For a locally owned installation, use the normal F-Pulse persistence stores:

```powershell
.venv/Scripts/python.exe samples/multi-system-demo/demo.py import-local --local-db D:/path/to/data/fpulse.db --workspace default
```

This adds five connections and five workflows. Repeating it skips existing
matching sample assets and refuses conflicting connection IDs/configurations.
It does not update an existing workflow or delete any user pipeline.

Alternatively, set `FPULSE_TOKEN` to a valid session token and use the
authenticated app API:

```powershell
.venv/Scripts/python.exe samples/multi-system-demo/demo.py import --base-url http://127.0.0.1:8002 --workspace default
```

The API importer reuses matching connection names/types and creates new
pipeline copies on each invocation. Both import paths require the actual
services to remain running.

## Inspect and stop

Open `http://127.0.0.1:28080/orders` to see API input and
`http://127.0.0.1:28080/results/shipments` to see received output.
The MinIO console is at `http://127.0.0.1:29001` (user `fpulse_demo`, password
from local `.env`). Other ports: PostgreSQL 25432, MySQL 23306, optional
container SQL Server 21433.

```powershell
docker compose -f samples/multi-system-demo/compose.yaml --profile sqlserver stop
```

This preserves the demo volumes. Stop the sample API with Ctrl+C in its
terminal, or stop the specific process recorded in `runtime/api.pid` after
verifying that it is still the sample API process. The existing SQL Server
service and F-Pulse app are not stopped by Compose.
