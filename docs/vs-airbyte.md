# F-Pulse OSS vs Airbyte

A frank, side-by-side comparison for teams evaluating F-Pulse OSS against Airbyte. They look similar from a distance — both are open-source data-integration projects — but they're built for **different jobs**, and that's the most important thing to internalise before picking.

> **Scope of this comparison.** Unless explicitly noted, the "Airbyte" column refers to **Airbyte's open-source release** (the self-hosted, Elastic-licensed product) — *not* Airbyte Cloud. Airbyte Cloud is a paid SaaS that adds scheduling-via-Temporal, alerts, hardened observability, and managed connector updates on top of the OSS. Where a feature exists in Cloud but not in self-hosted OSS, we say so explicitly. Our goal is precision, not advocacy — if you spot a factual error, please open a PR.

## In one sentence

> **Most data tools observe execution. F-Pulse adds a workspace-level
> reliability + memory layer above it, with read-only boundaries and
> operator-approved learning.** That's the only sentence you need to
> walk away with. The rest of this page is a careful per-dimension
> comparison for evaluators who want to verify the claim.

## Summary

> Airbyte is a **warehouse-centric Extract-Load (EL) platform** designed to move data from many sources into a warehouse at scale, with transformations pushed to dbt and orchestration pushed to Airflow / Dagster / Prefect.
>
> F-Pulse OSS is a **single-tool end-to-end data orchestrator** designed to do extract + load + transform + schedule + alert in one binary, on a laptop or VM.
>
> If you're moving large volumes from many SaaS sources into a warehouse and have a team to run a stack, Airbyte is the better fit. If you want one tool that handles the whole pipeline on one machine without operating a four-component stack, F-Pulse is the better fit. They serve different buyers.

## Side-by-side

| Dimension | Airbyte | F-Pulse OSS |
|---|---|---|
| **License (core)** | Elastic License 2.0 (BSL-style, source-available, not OSI-approved) | Apache License 2.0 (OSI-approved) |
| **License (connectors)** | MIT for community connectors | Apache 2.0 |
| **Primary model** | Extract-Load (EL) — transform via dbt downstream | End-to-end ETL — E + L + T in one engine |
| **Transformation engine** | None native; relies on dbt-on-warehouse | DuckDB vectorised, in-process |
| **First-party connectors** | Many hundreds (Airbyte's docs are the canonical count) | 37 first-party + open framework to author more |
| **Connector framework** | Connector Development Kit (CDK), Python, community-driven | OpenAPI-to-manifest generator (no-compile), Python tester for custom auth, derivation path for upstream OSS |
| **Single-pipeline scope** | Move records from source → destination | Source → transform → destination, plus control flow (foreach / conditional / router) |
| **Deployment** | Docker Compose for dev; Kubernetes recommended for production | Single service — `pip install` or one Docker container |
| **Scheduler** | Per-connection cron schedule built in (basic); complex DAG-level orchestration typically delegated to Airflow / Dagster / Prefect | Built-in workspace-level cron + "Quick Schedule" UI |
| **Alerts** | Webhook + email alerts on sync failure in recent OSS releases; richer routing in Airbyte Cloud | Email / Slack / Teams / webhook destinations + alert-rule conditions in OSS |
| **Run history + lineage** | Sync history + status per connection in OSS; deeper observability + dashboards in Airbyte Cloud | Queryable execution DB, per-step row counts + duration, DAG lineage view, alert emails render the DAG |
| **Multi-user / RBAC** | OSS limited; full in Cloud / Enterprise | Workspaces + RBAC in OSS |
| **Cross-pipeline reliability layer** | Airbyte's observability is connection-scoped (sync status, history, error logs per connection). We're not aware of an equivalent workspace-level duplicate-detection / cross-pipeline learning surface in either Airbyte OSS or Cloud as of writing. | **F-Pulse Steward** ships in OSS: duplicate-source / duplicate-pipeline detection, time-clamped severity escalation, rebound detection, dismiss-with-reason + secret-sanitizer for tribal-knowledge capture, **F-Pulse Memory Layer** (durable approved lessons, propose → approve → revalidate lifecycle), notification-bell with strict de-dup, per-workspace isolation. See [steward/overview.md](steward/overview.md) |
| **Durable team-knowledge surface** | Operators typically maintain runbooks externally (Confluence, Notion, internal wikis) — no in-product approved-lesson store we're aware of. | **F-Pulse Memory Layer**: typed lessons (source quirks, failure-fix pairs, retry rules) with explicit propose → approve → revalidate workflow + lesson-search API (auto-invocation on failure lands in 1.2). YAML on disk, hand-editable. See [steward/memory-layer.md](steward/memory-layer.md) |
| **Managed cloud option** | Airbyte Cloud (paid) | None today; F-Pulse Plus on roadmap |
| **Audience** | Data-eng teams with warehouses + stack-operating capacity | Mid-market / small teams wanting one tool, no stack |
| **Production maturity** | Founded ~2020; thousands of installs, large user base | New (1.0-rc); small footprint to date |
| **Ecosystem** | Large — CDK contributors, integrations with dbt / Airflow / Snowflake / etc. | Building — extensibility framework first, ecosystem next |

## When F-Pulse OSS is the right call

If any of these are true for your situation, F-Pulse is likely the better fit:

- **You don't want to operate a stack.** "Airbyte + dbt + an orchestrator + a warehouse" is genuinely four things to install, monitor, upgrade, and on-call for. F-Pulse is one binary that handles all four roles.
- **Your data fits on one machine.** Anywhere from a few GB to a few hundred GB on a fat VM is F-Pulse's sweet spot. DuckDB's vectorised engine streams to disk past RAM.
- **You need transformations inside the pipeline.** Joins, aggregates, pivots, derived columns, filters, dedup — F-Pulse does these natively in the canvas. With Airbyte you'd land the raw data in a warehouse and run dbt on top.
- **Apache 2.0 is a hard requirement.** Procurement gates that require OSI-approved open-source licenses pass F-Pulse and reject Airbyte's core (Elastic License 2.0 is not OSI-approved).
- **You want first-pipeline-running in minutes.** `docker compose up`, click a sample template, run. Versus Airbyte's setup, which requires landing destinations + wiring connectors + scheduling externally.
- **You're a small team without dedicated data-engineering staff.** Built-in scheduler / alerts / lineage / RBAC means you don't need a separate orchestration person.
- **You're replacing Talend Open Studio or similar single-machine ETL tools.** Same workload shape, a vectorised analytic engine (DuckDB columnar), modern UI, built-in operations. See [vs-talend.md](vs-talend.md).
- **You want a watcher, not just a runner.** Airbyte's observability is sync-and-connection scoped (status, history, error logs per connection) — well-suited to its EL focus. We're not aware of an equivalent **workspace-level** cross-pipeline duplicate-detection / regression / dismiss-with-reason loop in either Airbyte OSS or Cloud. F-Pulse's **Steward** is that layer above execution: it watches the workspace and surfaces findings before they become bills or outages. Ships in OSS, not a paywall. See [steward/architecture.md](steward/architecture.md) for the design rationale. (If you can point to a closer Airbyte equivalent, open a PR — we'd rather update this page than overclaim.)

## When Airbyte is still the better choice

Honest gaps. These are not points to argue with — if they describe you, use Airbyte:

- **Connector breadth matters more than engine speed.** If your job is "ingest from 200 SaaS sources" and the actual transforms are trivial, Airbyte's larger catalog (and its community-built CDK ecosystem) wins. F-Pulse's open-framework + manifest-authoring path closes the gap one connector at a time, not by parity.
- **You're at warehouse scale.** Terabyte-per-day flows from many sources into Snowflake / BigQuery / Redshift with bulk-load optimisations. Airbyte is built for that; F-Pulse's single-machine engine is not.
- **You have a Kubernetes platform team and standard data-stack tooling.** If Airflow + dbt + Snowflake is already in production at your org, Airbyte slots in. F-Pulse asks you to consolidate, which is a bigger lift than addition.
- **You need an immediately-available managed cloud option.** Airbyte Cloud is shipping today; F-Pulse Plus is on the roadmap but not yet GA.
- **You're at "thousands of pipelines" scale.** Production maturity (4+ years, many installs) gives Airbyte a "every edge case has been hit" advantage that any new tool — F-Pulse included — can't claim on day one.

## They're aimed at different jobs

The clearest way to think about it: imagine two teams.

**Team A** runs a SaaS company. They have 200 SaaS sources (Salesforce / HubSpot / Stripe / Zendesk / etc.) feeding into Snowflake. They have a data-eng manager, an Airflow cluster, a dbt project with 400 models, and a Looker analyst team downstream. They want a tool whose only job is to move bytes from source to warehouse, reliably, with the most connectors possible. → **Airbyte.**

**Team B** runs a mid-market manufacturer. They have a few internal SQL databases, some CSV/XLSX files dropped by partners, a couple of REST APIs, and need daily reports loaded into a reporting DB plus Excel handoffs to operations. They have no data-eng team — one analyst owns the whole pipeline plus the rest of analytics. They need scheduling, alerts when things break, and someone-can-look-at-it lineage. → **F-Pulse.**

Both are valid jobs. Neither tool is wrong for its intended job. The mistake is assuming there's a single answer.

## Migration thoughts

If you're considering moving between the two, two patterns worth knowing:

- **From Airbyte → F-Pulse.** Usually motivated by operational simplification (drop the stack) or licence-driven (need OSI-approved OSS). One-pipeline-at-a-time migration is the safe path: pick a single Airbyte source-destination pair, rebuild it as an F-Pulse pipeline, run both in parallel for a week, then retire the Airbyte side. The connector-authoring path covers most modern SaaS sources via OpenAPI.
- **From F-Pulse → Airbyte.** Usually motivated by hitting scale beyond a single machine, or by needing a connector F-Pulse doesn't have. Same one-pipeline-at-a-time pattern in reverse.

There's nothing exclusive about either choice — pipelines in both products can coexist while you migrate.

## See also

- [vs-talend.md](vs-talend.md) — companion comparison for teams evaluating against Talend Open Studio
- [extend/build-a-connector.md](extend/build-a-connector.md) — when F-Pulse doesn't ship the connector you need
- [connectors.md](connectors.md) — current first-party catalog + cert matrix
