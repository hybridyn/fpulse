# F-Pulse OSS — 10 Golden Demos

A curated index that maps the ten "golden" scenarios a reviewer expects from a
visual data-engineering tool to **assets that already ship in this repo**. Two
kinds of demo:

- **Pipeline demos** — importable workflow JSON under
  [`samples/free-api-pipelines/pipelines/`](free-api-pipelines/pipelines/).
  Import via the Editor's **Import pipeline** action (auto-provisions the
  source connection) or run [`free-api-pipelines/import.ps1`](free-api-pipelines/import.ps1)
  to load all 18 against your `fpulse_test` SQL Server connection. See the
  [free-api-pipelines readme](free-api-pipelines/readme.md) for the full matrix.
- **Steward demos** — these don't produce a table; they prove the read-only
  Steward *detects a risk*. Walk-throughs + live proof live under
  [`docs/steward/`](../docs/steward/).

`Run` column legend: **Offline** = no network, runs on a fresh install ·
**Public API** = free no-auth endpoint · **fpulse_test** = needs the sample SQL
Server connection · **Steward** = observation demo (no external data needed).

| # | Golden demo | F-Pulse asset | Run |
|---|---|---|---|
| 1 | **REST API → managed table** | [`01-jsonplaceholder-posts.json`](free-api-pipelines/pipelines/01-jsonplaceholder-posts.json) — baseline REST pull + rename → sink | Public API |
| 2 | **SQL Server → local table** | [`14-customer-360-enrichment.json`](free-api-pipelines/pipelines/14-customer-360-enrichment.json) and [`18-xml-ingest-foreach-archive.json`](free-api-pipelines/pipelines/18-xml-ingest-foreach-archive.json) read back from SQL Server via `db_source`; connection template [`connections/mssql.json`](free-api-pipelines/connections/mssql.json) | fpulse_test |
| 3 | **File data prep → managed table** | [`13-local-csv-materialize.json`](free-api-pipelines/pipelines/13-local-csv-materialize.json) — local CSV → typecast → `materialize` diamond DAG → two sinks | **Offline** |
| 4 | **API pagination → table** | [`05-github-issues.json`](free-api-pipelines/pipelines/05-github-issues.json) — GitHub offset pagination + visual-rules filter | Public API |
| 5 | **Schema-drift demo** | Steward `schema_drift` detector (data level). See [docs/steward/schema-drift.md](../docs/steward/schema-drift.md) | Steward |
| 6 | **Empty-output demo** | Steward node-level `empty_output` detector (consecutive zero-row runs). See [docs/steward/overview.md](../docs/steward/overview.md) | Steward |
| 7 | **Duplicate-source Steward demo** | Steward Archeologist (`duplicate_source` / `duplicate_pipeline`) with live screenshots. See [docs/steward/validation-scenarios.md](../docs/steward/validation-scenarios.md) | Steward |
| 8 | **Cost-warning demo** | Steward `warehouse_waste` (cost level) — repeated zero-output source reads. See [docs/steward/cost-tracking.md](../docs/steward/cost-tracking.md) | Steward |
| 9 | **Failed connector-health demo** | Steward `connector_health` (auth-failure / unreachable / rate-limit / near-expiry). See [docs/steward/connector-health.md](../docs/steward/connector-health.md) | Steward |
| 10 | **Backfill / idempotent-load demo** | [`03-open-meteo-hourly.json`](free-api-pipelines/pipelines/03-open-meteo-hourly.json) — `upsert` idempotent on `observed_at` (re-run = no dupes); plus the Backfill panel in the editor | Public API |

## How to run the pipeline demos (1–4, 10)

```powershell
# Import all 18 sample pipelines against your SQL Server connection named `fpulse_test`
cd .\samples\free-api-pipelines
pwsh .\import.ps1
# Then open the Editor, pick a pipeline, and hit Run. Start with #01 (zero deps).
```

Demo **3** is fully offline (local CSV) — the best first run on a machine with no
connections configured.

## How to see the Steward demos (5–9)

Steward scans the workspace on a timer (and on demand from the Steward panel).
The scenarios above are reproduced + screenshotted in
[docs/steward/validation-scenarios.md](../docs/steward/validation-scenarios.md).
Steward is **read-only** — these demos show detection + escalation +
dismiss/resolve, never a mutation of your pipelines.

> Note: items 5–9 are observation demos, not data pipelines — that's the point
> of Steward. The pipeline demos (1–4, 10) are the data-movement happy paths.
