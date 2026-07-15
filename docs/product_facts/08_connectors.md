# F-Pulse connectors — what's available, where, and what's Plus

**Cross-reference:** `edition-matrix.md` line 27 (OSS connectors), line 44 (Plus enterprise connectors).

## 2026-05-23 — Connector family expansion (OSS)

Three families landed as first-class saved connections (testable + catalog + form + runtime):

* **Microsoft Graph** — generic Graph surface for SharePoint / OneDrive / Teams / Planner / Outlook / Users / Groups. Client-credentials OAuth against Azure App Registration; one connection covers all Graph endpoints.
* **Oracle Cloud family** — `oracle_fusion` (Fusion Cloud REST across FSCM / HCM / CRM) and `oracle_bip` (BI Publisher reports). Replaces the vague legacy `oracle_api` (still loads as a back-compat alias of oracle_fusion).
* **SAP product family** — `sap_s4hana` (S/4HANA OData v2 or v4 with sap-client routing) and `sap_successfactors` (HRIS via `<user>@<company_id>` login). Legacy `sap` type stays as an alias of sap_s4hana.

Five SaaS connectors promoted from manifest-only to first-class saved connections, each with its own tester + catalog provider: **GitHub, Shopify, Stripe, Notion, Asana**.

Three previously backend-only types now visible in the picker: **DB2, SAP HANA, Teradata** (databases) and **Athena, Presto** (warehouses).

## Quantitative summary

OSS Free ships **33 first-party connectors visible by default**, plus 10 Hidden manifests kept on disk for slug-reservation only:
- **4 database dialects** — PostgreSQL, MySQL, MS SQL Server, SQLite (used by Database Source/Sink).
- **2 bulk-load dialects** — Postgres (`COPY FROM STDIN`) + Snowflake (`PUT` + `COPY INTO`). Other warehouse dialects (BigQuery, Redshift, Databricks, MSSQL bulk, Oracle, MongoDB, ClickHouse) are designed and on the post-1.0 roadmap.
- **27 SaaS REST manifests visible by default** — 19 Beta tier + 8 Experimental tier. Read via the SaaS Connector node which picks any installed manifest at runtime. Tier per connector lives in `/api/connectors/cert-matrix`.
- **10 manifests Hidden by tier flag** — `airtable`, `facebook_ads`, `google_ads`, `google_analytics`, `linkedin_ads`, `mailchimp`, `monday`, `pipedrive`, `shopify`, `zoho_crm`. Consumer marketing / SMB CRM / ads — out of enterprise-data-engineering scope. Manifest files remain so slugs are reserved and direct links don't 404; reachable via `?include_hidden=true` on the cert-matrix endpoint.

## Certification breakdown (OSS) — current state

The cert matrix at `/api/connectors/cert-matrix` reports two parallel signals, since v1 and v2 manifests live on different certification paths:

**F0.1 cert path (v2 manifests only):** depth_score 0-5 based on schema + pagination + incremental + primary key + fixture coverage.
- **1 beta** — Salesforce v2 at depth-3 (REST + cursor pagination, partial Account schema, no Bulk API 2.0)

**v1 capability signal (v1 manifests):** v1_capability_score 0-3 based on auth + streams + pagination wiring at the manifest level. v1 manifests are functional at runtime — the score reflects "how much of the runtime contract is declared" not "how certified this is."
- **v1-functional** — auth + streams + pagination all wired (e.g. HubSpot, Stripe, Shopify, Slack). Works today; just hasn't migrated to F0.1 v2 with fixtures.
- **v1-basic** — partial wiring (auth declared but no pagination, or similar)
- **v1-stub** — placeholder with little or no runtime contract declared

**Validation status: `uncertified`** for v1 manifests — distinct from `fail`. The v1 manifests aren't broken; they just haven't been migrated to the F0.1 cert path yet. The migration is a non-breaking incremental project.

A v2 connector's depth score is the MAX over its streams — a connector with one production-grade stream and four stubs reports depth-5.

## OSS SaaS connectors (sample — full list at `/api/connectors/cert-matrix`)

CRM: HubSpot, Salesforce (starter version), Pipedrive, Zoho CRM
Finance: Stripe, QuickBooks
Support: Zendesk, Freshdesk, Intercom, PagerDuty
Project mgmt: Jira, Asana, Monday, ClickUp, Notion, Confluence
Marketing: Mailchimp, SendGrid, Google Analytics, Google Ads, Facebook Ads, LinkedIn Ads
Communication: Slack, Microsoft Teams, Twilio
Dev tools: GitHub, GitLab
Storage: Airtable, Shopify
Search/cache/queue: Elasticsearch, Redis, RabbitMQ
Observability: Datadog
Other: ServiceNow (starter), NetSuite (starter), Workday (starter), Dynamics 365 (starter), SAP OData (starter)

For "starter" connectors marked in OSS, the manifest exists but the **production-hardened version (full incremental, full pagination, full fixture set)** is shipped only in F-Pulse+.

## Plus-only connectors (per EDITION_MATRIX line 44)

These do not ship in the OSS repo at all:

- **SAP** (full SAP S/4HANA + R/3 connectors with BAPI/RFC support)
- **SAP HANA** (native HANA driver path)
- **NetSuite** (full SuiteQL + record types)
- **Workday** (REST + RaaS report ingestion)
- **Dynamics 365** (Dataverse, BC, F&O)
- **ServiceNow** (Table API + ImportSet API)
- **Salesforce** (production-grade; replaces the OSS starter)
- **Informix**, **Teradata**, **DB2** (legacy enterprise databases)

Plus also adds:
- **Vector DB sinks** — Pinecone, Weaviate, Qdrant
- **CDC sources** — Debezium-style change-data-capture from Postgres/MySQL
- **JDBC dialect registry** — register custom DB drivers at runtime; required for Snowflake/BigQuery/Redshift/Databricks SQL via JDBC nodes

## Connector authoring (both editions)

OSS users can write their own SaaS connectors using the **F0.1 manifest v2 format**. The validator at `python -m fpulse.connectors.certify <name>` reports the depth score; the goal is to hit 5 (full fixture coverage).

Custom connectors authored by OSS users stay OSS — Hybridyn's commercial Plus connectors are separately maintained and not affected by community contributions.

## How the agent picks a connector

When the user asks "ingest data from Salesforce" the agent:
1. Looks at the active edition (Layer 1 session-context block)
2. If OSS: picks the starter Salesforce manifest from `manifests/salesforce.json`
3. If Plus: picks the production-grade Salesforce connector instead

The agent will NEVER suggest a Plus-only connector to a Free user. If a Free user explicitly asks "can I use SAP?" the agent answers "SAP is Plus-only; the OSS path for SAP is REST/HTTP API + manual schema mapping, but it won't have BAPI/RFC support."
