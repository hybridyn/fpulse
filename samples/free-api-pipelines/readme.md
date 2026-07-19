# Free-API sample pipelines (F-Pulse OSS)

Eighteen pipelines that pull from public, free, no-auth (or DEMO_KEY) APIs — plus local CSV / XML files — and land into an existing SQL Server connection. Covers a broad slice of OSS node types: pagination, nested flatten, raw-SQL transform, typecast, derived columns, dedup, aggregate, filter (both expression and visual rules mode), dual-sink fan-out, plus the 2026-05-26 demo lineup that adds lookup, SCD2, pivot/unpivot, validate, conditional_split, sample, code_script, if_condition, slack_notify, send_email, set_variable, http_request, validation (file-wait), xml_source, execute_sql_task, foreach_loop, and file_system.

## Self-contained import (2026-05-22)

Each sample pipeline JSON ships with its own `connection_definitions` block at the top level. The Toolbar's **Import pipeline** action reads that block, idempotently creates each connection (skipped if a connection with the same name + type already exists), and remaps the workflow's `connection_id` references from connection NAMES to the actual backend ids before loading the canvas.

That means you can:

1. Open the **Editor** → **Import pipeline**
2. Pick one of the JSON files from `pipelines/`
3. Hit **Run**

…and the sample's REST connection is auto-provisioned on first run. No "create connection first" pre-flight.

### Why connection_definitions instead of inline URLs?

The locked source-node contract is **file OR saved connection** — inline URLs are no longer accepted on the canvas. This keeps every network-side credential under the Connections page's audit/RBAC/environment scoping, so a fresh install with these samples behaves the same as a production deployment with its own connections.

Sample 13 is the file-input exception — it uses a local CSV via `file_path` instead of a connection, demonstrating the other half of the contract.

## What's here

```
samples/free-api-pipelines/
├── connections/
│   └── mssql.json                  ← SQL Server target connection (edit creds first)
├── data/
│   ├── orders.csv                  ← used by 13, 14
│   ├── customers.csv               ← used by 14 (also expected as dbo.customers source)
│   ├── daily_sales.csv             ← used by 15
│   ├── baseline_sales.csv          ← used by 15
│   ├── signups.csv                 ← used by 16
│   └── products.xml                ← used by 18
├── pipelines/
│   ├── 01-jsonplaceholder-posts.json
│   ├── 02-jsonplaceholder-users-flatten.json
│   ├── 03-open-meteo-hourly.json
│   ├── 04-coingecko-markets.json
│   ├── 05-github-issues.json
│   ├── 06-rest-countries.json
│   ├── 07-open-library-search.json
│   ├── 08-frankfurter-fx.json
│   ├── 09-nasa-apod.json
│   ├── 10-usgs-earthquakes.json
│   ├── 11-crypto-fx-cross-join.json
│   ├── 12-data-profile.json
│   ├── 13-local-csv-materialize.json
│   ├── 14-customer-360-enrichment.json     ← 2026-05-26 (lookup×2 + SCD2)
│   ├── 15-sales-pivot-trend.json           ← 2026-05-26 (pivot + unpivot)
│   ├── 16-conditional-routing.json         ← 2026-05-26 (validate + conditional_split)
│   ├── 17-event-driven-alerts.json         ← 2026-05-26 (code_script + Slack/Email)
│   └── 18-xml-ingest-foreach-archive.json  ← 2026-05-26 (xml_source + file_system)
├── import.ps1                      ← PowerShell importer
└── readme.md                       ← this file
```

## Pipeline matrix

| # | Source | What it exercises | Sink(s) | Mode |
|---|---|---|---|---|
| 01 | JSONPlaceholder `/posts` | Baseline smoke + `rename` | `dbo.raw_posts` | create |
| 02 | JSONPlaceholder `/users` | `flatten_explode` × 2 (struct fields) + `rename` | `dbo.dim_users` | create |
| 03 | Open-Meteo `/forecast` | UNNEST + `typecast` + **`data_quality`** (drop+score+profile) + **`upsert`** (idempotent on observed_at) | `dbo.weather_hourly` | append |
| 04 | CoinGecko `/coins/markets` | Filter + typecast + derived + **`sort`** + **`window`** (DENSE_RANK / SUM / LAG / PERCENT_RANK) | `dbo.crypto_markets` | truncate |
| 05 | GitHub `/repos/duckdb/duckdb/issues` | Offset pagination + visual-rules `filter` + derived | `dbo.gh_duckdb_issues` | create |
| 06 | REST Countries `/all` | `flatten_explode` + raw-SQL with `map_keys`/`map_values`/array indexing | `dbo.dim_countries` | create |
| **07** | **Public Google Sheet** | **`gsheet_source`** (CSV export, no API key, no OAuth) | `dbo.gsheet_import` | create |
| 08 | Frankfurter `/latest` | Pivot map → rows + derived + **dual sink** | `dbo.fx_rates_daily` + `data/fx_backup.csv` | append + file |
| 09 | NASA APOD | Visual rules `filter` + **dual sink** | `dbo.nasa_apod` + `data/nasa_apod.xlsx` | append + file |
| 10 | USGS earthquakes (GeoJSON) | `data_key=features` + struct flatten + `aggregate` (5 functions, GROUP BY) | `dbo.quakes_daily_agg` | create |
| **11** | **CoinGecko + Frankfurter** | **`join` CROSS** between two parallel API sources → multi-currency price fact | `dbo.crypto_prices_by_currency` | truncate |
| **12** | **REST Countries** | `flatten_explode` + **`data_profile`** (null%/distinct/min/max/top per column) | `dbo.countries_profile` | create |
| **13** | **Local CSV (`data/orders.csv`)** | **`file_source`** + **`materialize`** (diamond DAG) + branches: filter → MSSQL, aggregate → CSV. **Offline-capable.** | `dbo.orders_high_value` + `orders_by_country.csv` | create + file |
| **14** | **Orders CSV + dbo.customers + REST Countries** | **`lookup`** ×2 (customer master + country region) → derived tier → **`scd2`** (track tier/region changes) | `dbo.customer_360` | create |
| **15** | **Local CSV (`data/daily_sales.csv` + `baseline_sales.csv`)** | aggregate → **`pivot`** (region→cols) → window-based growth_pct → `union` baseline → `sort` → **dual sink**: Excel report AND **`unpivot`** → MSSQL fact | `dbo.fact_sales` + `data/sales_report.xlsx` | create + file |
| **16** | **Local CSV (`data/signups.csv`)** | **`validate`** (regex + required) → **`conditional_split`** (4 region branches) → per-region warehouse + quarantine CSV + **`sample`** 10% QA CSV | `dbo.signups_eu` / `_us` / `_asia` + 2× CSV | create + file |
| **17** | **GitHub `/repos/duckdb/duckdb/issues`** | filter 24h → **`code_script`** (Python severity classifier) → **`if_condition`** → **`slack_notify`** + main branch → aggregate → **`send_email`** digest + parallel **`set_variable`** → **`http_request`** monitoring metric | (no sink — notifications only) | logs |
| **18** | **Local XML (`data/products.xml`)** | **`validation`** (wait for file) → **`xml_source`** → **`execute_sql_task`** (TRUNCATE) → typecast + data_quality → warehouse + parallel `db_source` → transform → **`foreach_loop`** → CSV + **`file_system`** (move to archive/) | `dbo.products_staging` + `data/exports/partner_exports.csv` + archived XML | append + file |

### Coverage diff from the v1 set
- **DROPPED** ~~07 Open Library~~ (polymorphic-shape demo was confusing; left a misleading filename slot — rename `07-open-library-search.json` to `07-gsheet-public.json` at your convenience).
- **ENHANCED** #03 (now demonstrates `data_quality` + `upsert`) and #04 (now demonstrates `sort` + `window`).
- **ADDED v2** #11 (`join`), #12 (`data_profile`), #13 (`file_source` + `materialize`, offline-capable).
- **ADDED v3 (2026-05-26)** #14 (`lookup` + `scd2`), #15 (`pivot` + `unpivot`), #16 (`validate` + `conditional_split` + `sample`), #17 (`code_script` + `if_condition` + `slack_notify` + `send_email` + `set_variable` + `http_request`), #18 (`xml_source` + `execute_sql_task` + `validation` + `foreach_loop` + `file_system`).
- **STILL NOT COVERED in OSS**: `data_wrangler`, `embedder` / vector nodes, `until_loop`, `switch_case`, `delete_data`, `webhook_trigger` (deprecated from palette), `parquet_source`/`parquet_sink`, the SaaS / cloud-files connectors (need real creds), `cdc_source`, `delta_sink`.

## Demo lineup (added 2026-05-26)

The five new pipelines round out OSS node coverage with the types previously left for a follow-up. They share a "complex but offline-capable where possible" goal — only #17 actually needs network (GitHub API) and even its alerting hops (Slack / SMTP / monitoring webhook) are wired to **placeholder URLs/hosts that fail silently** so the pipeline still completes end-to-end on a fresh install.

| # | One-line story | New node types exercised |
|---|---|---|
| **14** — **Customer 360 Enrichment** | Orders CSV joins customer master from SQL Server, then enriches with REST Countries region; derives a tier from lifetime value; SCD2 tracks tier/region history. | `lookup` ×2, `scd2` |
| **15** — **Sales Pivot + Trend** | Daily sales aggregated by region+day, pivoted region→columns, a LAG-based growth % derived, baseline rows unioned in, sorted newest first, then fanned out: Excel report on one branch, unpivoted back-to-fact MSSQL load on the other. | `pivot`, `unpivot`, `union`, `sort` |
| **16** — **Conditional Routing + Validation** | Signups CSV validated (regex email + required fields), then split into 4 region branches (EU adds GDPR flag → MSSQL, US/Asia → MSSQL, Other → quarantine CSV) plus a random 10% QA sample CSV. | `validate`, `conditional_split`, `sample` |
| **17** — **Event-Driven Alerts** | GitHub issues filtered to last 24h, classified by a Python `code_script` into critical/major/minor; `if_condition` routes critical rows to Slack; main flow aggregates by label and emails a digest; a parallel branch sets a counter variable and POSTs a JSON metric payload to a monitoring webhook. **Slack / SMTP / webhook URLs are PLACEHOLDERS** — see the pipeline's top-level description. | `code_script`, `if_condition`, `slack_notify`, `send_email`, `set_variable`, `http_request` |
| **18** — **XML Ingest + ForEach + Archive** | A `validation` step waits for `data/products.xml`, `xml_source` parses `<product>` rows, `execute_sql_task` truncates the staging table on the SQL Server side, typecast + data_quality clean + profile, the data lands in `dbo.products_staging`, a parallel branch reads it back, shapes per-partner rows, batches via `foreach_loop`, writes a partner-exports CSV, and finally `file_system` moves the consumed XML into `data/archive/`. | `validation`, `xml_source`, `execute_sql_task`, `foreach_loop`, `file_system` |

For #17, the placeholder URLs/hosts (`hooks.slack.com/services/PLACEHOLDER/REPLACE/ME`, SMTP `localhost:1025`, `webhook.site/PLACEHOLDER-…`) need to be replaced with real values in the Connections page before the alerts actually go anywhere. The nodes log the message locally otherwise — the pipeline run still succeeds.

## How to import

The importer reuses an **existing** SQL Server connection — it looks it up by name (or ID) via the F-Pulse API and rewrites each pipeline payload to point at the real connection ID before POSTing. You do **not** need to import `mssql.json` if you already have a working connection.

### Default flow (you already have a connection named `fpulse_test`)

```powershell
# From the repo root:
cd .\samples\free-api-pipelines
pwsh .\import.ps1
```

That's it. The script will:
1. `GET /api/connections` and find the row where name or id matches `fpulse_test`
2. Swap `conn_mssql_prod` → real connection ID inside each pipeline JSON in-memory
3. `POST /api/workflows` for all 18 pipelines

### Override flags

```powershell
# different connection name
pwsh .\import.ps1 -ConnectionName "my_warehouse"

# match by literal connection ID instead of name
pwsh .\import.ps1 -ConnectionId "a3f2c8d1b4e9"

# backend on a non-default port
pwsh .\import.ps1 -BaseUrl http://localhost:8001
```

### If the lookup fails

The script aborts and prints every `mssql`-type connection it found in your workspace, so you can re-run with the right `-ConnectionId` or `-ConnectionName`.

### What `connections/mssql.json` is for now

Only as a **template** if you want to create a fresh SQL Server connection from scratch (edit creds, then `Invoke-RestMethod -Method POST -Uri http://localhost:8001/api/connections -Body (Get-Content -Raw .\connections\mssql.json)`). It is **not** used by `import.ps1` anymore.

### 4. Open one in the canvas and Run

After import, every pipeline lands in the **default** workspace as a draft. Open `01 — JSONPlaceholder Posts → SQL Server` first — it has zero dependencies and finishes in under 2 seconds. Once that lands in `dbo.raw_posts`, work up to #10.

## OSS-only constraints (what's deliberately missing)

- **No streaming sources.** Wikipedia EventStreams (SSE) and Binance WebSocket would be the natural picks but OSS has no `sse_source` or `websocket_source` node type yet. These belong in a future PR.
- **XML source DOES exist** (`xml_source`) but reads from a local file path, not a URL — so a "World Bank XML" demo would need a shipped sample XML file. Skipped to keep the sample set portable. Easy to add via `code_script` (download → save → `xml_source`) if you want it.
- **No vault.** SQL Server credentials sit in plain JSON inside the SQLite connection store. Don't commit `mssql.json` after editing — it's added to `.gitignore`-worthy patterns.

## Rate-limit notes

| Source | Free-tier ceiling | Notes |
|---|---|---|
| JSONPlaceholder | unlimited | fake API |
| Open-Meteo | ~10k calls/day | no key |
| CoinGecko (public) | ~10–30/min | will 429 on rapid re-runs |
| GitHub (no auth) | 60/hr | add PAT in `headers.Authorization` for 5000/hr |
| REST Countries | unlimited | |
| Google Sheets (public CSV export) | ~5k/day per sheet | needs "Anyone with the link" share |
| Frankfurter | unlimited | ECB-backed |
| NASA APOD `DEMO_KEY` | 30/hr, 50/day | get a real key at api.nasa.gov |
| USGS | unlimited | |
| Local CSV (`data/orders.csv`, `customers.csv`, `daily_sales.csv`, `baseline_sales.csv`, `signups.csv`) | n/a | offline-capable, no network |
| Local XML (`data/products.xml`) | n/a | offline-capable, no network |

## Node-type coverage (v3 — 18 pipelines)

```
warehouse_sink     █████████████████████████  17 / 18
derived_column     █████████████████████      14 / 18
api_source         ███████████████████        13 / 18
transform (SQL)    █████████████████          11 / 18
typecast           █████████████               9 / 18
file_source        ██████                      4 / 18
csv_sink           ██████                      4 / 18
flatten_explode    ██████                      4 / 18
aggregate          ██████                      4 / 18
filter (expr)      ██████                      4 / 18
rename             █████                       3 / 18
filter (rules)     ████                        2 / 18
data_quality       ████                        2 / 18
sort               ████                        2 / 18
excel_sink         ████                        2 / 18
db_source          ████                        2 / 18
join               ██                          1 / 18
window             ██                          1 / 18
data_profile       ██                          1 / 18
union              ██                          1 / 18
upsert             ██                          1 / 18
materialize        ██                          1 / 18
gsheet_source      ██                          1 / 18
lookup             ██                          1 / 18
scd2               ██                          1 / 18
pivot              ██                          1 / 18
unpivot            ██                          1 / 18
validate           ██                          1 / 18
conditional_split  ██                          1 / 18
sample             ██                          1 / 18
code_script        ██                          1 / 18
if_condition       ██                          1 / 18
slack_notify       ██                          1 / 18
send_email         ██                          1 / 18
set_variable       ██                          1 / 18
http_request       ██                          1 / 18
validation         ██                          1 / 18
xml_source         ██                          1 / 18
execute_sql_task   ██                          1 / 18
foreach_loop       ██                          1 / 18
file_system        ██                          1 / 18
```

(`deduplicate` was only in the dropped #07 — gone from the coverage now, still exists as a node type. `webhook_sink` and `webhook_trigger` are deprecated from the palette — use `http_request` / `api_source` instead.)
