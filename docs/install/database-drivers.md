# Database driver installation

F-Pulse ships the **engine + framework**; the actual database client
libraries are *optional installs* keyed by extras. This lets a fresh
`pip install fpulse` stay small (~80 MB) instead of pulling 600 MB of
client libraries you may never use.

The trade-off: the first time you create a connection to (say) Oracle,
you'll see a friendly "oracledb not installed" message in the
Connections page with the exact install command. This page is the
master reference for every install command and any OS-level driver
that needs to live alongside the Python wheel.

> **At launch, `fpulse` is not on PyPI yet.** Until it's published, install
> from a source checkout — replace `pip install fpulse[x]` anywhere on this
> page with `pip install -e ".[x]"`, run from the repo root (e.g.
> `pip install -e ".[postgres]"`). The `pip install fpulse[x]` shorthand
> shown below works unchanged once F-Pulse is published to PyPI.

## TL;DR

| Database | Install command | OS-level driver also required? |
|---|---|---|
| PostgreSQL | `pip install fpulse[postgres]` | No |
| MySQL / MariaDB | `pip install fpulse[mysql]` | No |
| SQLite | (builtin, nothing to install) | No |
| MS SQL Server / Azure SQL / Synapse / Fabric SQL | `pip install fpulse[mssql]` | **Yes** — see below |
| Oracle (thin mode) | `pip install fpulse[oracle]` | No |
| Oracle (thick mode) | `pip install fpulse[oracle]` | **Yes** — Oracle Instant Client |
| IBM Db2 / Informix | `pip install fpulse[db2]` | **Yes** — IBM CLI Driver |
| SAP HANA | `pip install fpulse[hana]` | Sometimes (Linux glibc-old) |
| Teradata | `pip install fpulse[teradata]` | No |
| Snowflake | `pip install fpulse[snowflake]` | No |
| Google BigQuery | `pip install fpulse[bigquery]` | No |
| Databricks SQL | `pip install fpulse[databricks]` | No |
| ClickHouse (native) | `pip install fpulse[clickhouse]` | No |
| MongoDB | `pip install fpulse[mongodb]` | No |
| Cassandra | `pip install fpulse[cassandra]` | No |
| Neo4j | `pip install fpulse[neo4j]` | No |
| Redis | `pip install fpulse[redis]` | No |
| Elasticsearch | `pip install fpulse[elasticsearch]` | No |
| Trino / Starburst | `pip install fpulse[trino]` | No |

Convenience bundles:

```bash
# Every database where the Python wheel works without OS-level drivers
pip install fpulse[all-databases-no-os-deps]

# Everything (incl. AWS / Azure / Google cloud SDKs)
pip install fpulse[all]
```

## What "OS-level driver" means

Some database client libraries are thin Python wrappers around a
native shared library. The Python wheel installs the wrapper; the
shared library has to be installed at the OS level, usually via the
system package manager.

If you skip the OS install, `pip install` succeeds but the connection
fails at runtime with an error like `Can't open lib 'ODBC Driver 17
for SQL Server' : file not found`. This page tells you the OS install
to pair with each `pip install`.

---

## MS SQL Server / Azure SQL / Synapse / Fabric SQL endpoint

Python install:
```bash
pip install fpulse[mssql]
```

OS-level driver — **Microsoft ODBC Driver 17 or 18 for SQL Server**:

### Windows
Download + install: <https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server>

Quick check:
```powershell
odbcad32.exe   # GUI confirms the driver appears under "Drivers" tab
```

### Linux (Ubuntu / Debian)
```bash
curl https://packages.microsoft.com/keys/microsoft.asc | sudo tee /etc/apt/trusted.gpg.d/microsoft.asc
curl https://packages.microsoft.com/config/ubuntu/22.04/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev
```

### macOS
```bash
brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release
brew update
HOMEBREW_ACCEPT_EULA=Y brew install msodbcsql18
```

### Verify
```bash
python -c "import pyodbc; print([d for d in pyodbc.drivers() if 'SQL Server' in d])"
```
Expected output includes `ODBC Driver 17 for SQL Server` or
`ODBC Driver 18 for SQL Server`.

---

## Oracle Database

Python install:
```bash
pip install fpulse[oracle]
```

The `oracledb` library has **two modes**:

### Thin mode (default, no extra install)
Pure Python — no Oracle Instant Client required. Supports the common
case: read/write rows, parameterised queries, basic types. **This is
what F-Pulse uses unless you explicitly switch to thick.** No further
action needed.

### Thick mode (for AQ, sharding, advanced types)
Requires **Oracle Instant Client** installed at the OS level + a
config flag in the connection.

#### Windows
1. Download Oracle Instant Client Basic Lite (smallest, ~50 MB) from
   <https://www.oracle.com/database/technologies/instant-client/winx64-64-downloads.html>
2. Unzip to e.g. `C:\oracle\instantclient_21_13`
3. Add that folder to `PATH`:
   ```powershell
   [Environment]::SetEnvironmentVariable("PATH", "C:\oracle\instantclient_21_13;$env:PATH", "User")
   ```

#### Linux
```bash
# Ubuntu/Debian
sudo apt-get install libaio1   # transitive C dep
# Download Instant Client Basic Lite RPM/ZIP from Oracle
sudo unzip instantclient-basiclite-linux.x64-21.13.0.0.0dbru.zip -d /opt/oracle
echo /opt/oracle/instantclient_21_13 | sudo tee /etc/ld.so.conf.d/oracle.conf
sudo ldconfig
```

#### Tell `oracledb` to use thick mode
In your F-Pulse connection JSON or via env var before starting the
backend:
```bash
export ORACLE_THICK_MODE=1
```
(F-Pulse's `connections/catalog_extensions.py` honours this and calls
`oracledb.init_oracle_client()` once at startup.)

### Verify
```bash
python -c "import oracledb; oracledb.connect('user/pwd@localhost:1521/free')"
```

---

## IBM Db2 / Informix

Python install:
```bash
pip install fpulse[db2]
```

OS-level driver — bundled with `ibm_db` on most platforms; **manual
install required on Linux ARM64 and recent Apple Silicon macOS**.

### Windows / Linux x86_64
`pip install` is usually sufficient — the wheel bundles the CLI driver.

### macOS (Apple Silicon) / Linux ARM64
Download IBM Data Server Driver Package (DSDriver):
<https://www.ibm.com/support/pages/download-initial-version-11510-clients-and-drivers>

Set environment:
```bash
export IBM_DB_HOME=/opt/ibm/dsdriver
export DYLD_LIBRARY_PATH=$IBM_DB_HOME/lib:$DYLD_LIBRARY_PATH   # macOS
export LD_LIBRARY_PATH=$IBM_DB_HOME/lib:$LD_LIBRARY_PATH       # Linux
```

---

## SAP HANA

Python install:
```bash
pip install fpulse[hana]
```

The `hdbcli` wheel is self-contained on Windows + modern Linux +
macOS. On glibc < 2.27 systems (older CentOS / RHEL 7), you'll need
to install the SAP HANA Client manually — see SAP Note 2399566.

---

## Snowflake

Python install:
```bash
pip install fpulse[snowflake]
```

No OS-level driver. The wheel includes pure-Python + C extensions.

Optional perf bonus: install Apache Arrow extras for faster fetch:
```bash
pip install "snowflake-connector-python[pandas]"
```

---

## Google BigQuery

Python install:
```bash
pip install fpulse[bigquery]
```

No OS-level driver. Authentication is via a service-account JSON key
file (or Application Default Credentials when running on GCE / Cloud
Run / GKE).

---

## Databricks SQL

Python install:
```bash
pip install fpulse[databricks]
```

No OS-level driver. Authentication is via Personal Access Token (PAT)
or OAuth M2M, both passed in the F-Pulse connection config.

---

## ClickHouse (native protocol)

Python install:
```bash
pip install fpulse[clickhouse]
```

The framework also speaks ClickHouse via the **HTTP interface** with
zero install (the generic REST connector handles it). Use the native
driver path only when you need binary transport or specific session
settings.

---

## Convenience: install everything at once

For a dev laptop where you want every connector to "just work":

```bash
# All databases where pip-only works (no OS drivers needed)
pip install fpulse[all-databases-no-os-deps]

# All databases + cloud SDKs (AWS, Azure, GCP)
pip install fpulse[all]
```

Then add MSSQL / Oracle (thick) / Db2 separately if you need them —
those each require the OS-level driver install documented above.

---

## Troubleshooting

| Error | Likely cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'oracledb'` | Extra not installed | `pip install fpulse[oracle]` |
| `Can't open lib 'ODBC Driver 17 for SQL Server'` | Python wheel installed; OS driver missing | Install Microsoft ODBC Driver (see above) |
| `DPI-1047: Cannot locate a 64-bit Oracle Client library` | `ORACLE_THICK_MODE=1` set but Instant Client missing | Install Instant Client + add to PATH/`LD_LIBRARY_PATH` |
| `cffi.api.CDefError: ...` on Linux ARM64 (db2) | ARM64 wheel needs DSDriver | Install DSDriver, set `IBM_DB_HOME` |
| `RuntimeError: snowflake-connector-python ... requires Python 3.8+` | Older Python on host | Upgrade to Python 3.11 (F-Pulse's recommended runtime) |

If the connection still fails after the right install, open an issue
with the full traceback + your OS / Python version — the team will
add the case to this page.
