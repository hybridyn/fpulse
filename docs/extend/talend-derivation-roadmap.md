# Talend → F-Pulse derivation roadmap

A prioritised list of connectors worth deriving from Talend Open Studio's Apache-2.0 source. Each entry includes the upstream component path, the rough effort estimate, and the strategic rationale. **This is a living document** — entries get checked off as ports land, and the roadmap re-prioritises based on what shows up in `connector-request` GitHub issues.

> Before deriving any of these, read [derive-from-talend.md](derive-from-talend.md) for the safe-derivation process. Pin to a pre-acquisition commit (≤ August 2023) for legal safety.

## Prioritisation framework

Two axes:

- **Strategic value** — how many enterprise teams currently can't move to F-Pulse because we don't have this connector
- **Port effort** — how much Talend source we'd realistically extract before we hit something F-Pulse already does better

| Tier | Strategic value | Effort | When to take |
|---|---|---|---|
| **🔥 Top 5** | Blocks the biggest migration cohorts | 3–7 days each | First quarter post-launch |
| **⭐ Next 10** | Unblocks meaningful enterprise pockets | 2–5 days each | Continuous as bandwidth allows |
| **💡 Opportunistic** | Niche but high-impact when someone needs them | 1–3 days each | Community-contribution driven |

## 🔥 Top 5 — first quarter

These five close the biggest "we can't move to F-Pulse because connector X" objections from TOS migration prospects.

### 1. SAP S/4HANA (OData v2 + v4) — depth pass

**Status:** F-Pulse ships `sap_s4hana` as a first-party manifest. The Talend source has additional production-tested logic worth porting in: stream-paging quirks, large-payload handling, SAP-client routing edge cases, retry-on-cluster-failover behaviour.

- **Upstream:** Talend's S/4HANA REST components in `Talend/tcomp` (search for `tSAP` / `tHana` in the SAP-OData category)
- **Estimated effort:** 3 days for the depth pass
- **Why this first:** SAP shops are the single largest enterprise cohort, and our current `sap_s4hana` is a v1 manifest — porting Talend's auth + paging edge cases bumps it toward `production-certified`

### 2. Oracle E-Business Suite (REST + Concurrent Programs)

**Status:** Not in F-Pulse catalog. Talend has `tOracleEBSConnection` and related components.

- **Upstream:** Talend's Oracle EBS components (search Talend GitHub for `tOracleEBS`)
- **Estimated effort:** 5–7 days (Concurrent Programs API is byzantine — Talend's code captures real handshake quirks)
- **Why this matters:** Every mid-market manufacturer / distributor running Oracle EBS is currently blocked. This single connector unlocks a major enterprise segment.

### 3. Salesforce — Bulk API v2 + SOAP Partner

**Status:** F-Pulse has manifest-level Salesforce REST. Talend's source has the Bulk API v2 implementation (chunked upserts, query streaming) and the legacy SOAP Partner API (still required for some metadata operations).

- **Upstream:** `Talend/tcomp/salesforce` or similar — multiple `tSalesforce*` components
- **Estimated effort:** 4 days (Bulk API alone is the high-value cut)
- **Why this matters:** Most TOS-using teams who do Salesforce work use the Bulk API for performance. Our REST-only Salesforce loses to TOS on perf.

### 4. Workday REST + RaaS Reports

**Status:** Not in F-Pulse catalog. Talend has `tWorkdayInput` for both the REST API and the Reports-as-a-Service custom-report runner.

- **Upstream:** `Talend/tcomp` Workday components
- **Estimated effort:** 4 days
- **Why this matters:** Workday for HR / Finance is in virtually every Fortune 1000. The RaaS path is genuinely useful and undocumented anywhere else.

### 5. JD Edwards EnterpriseOne (BSSV / AIS)

**Status:** Not in F-Pulse catalog. Talend has `tJDE*` components covering both Business Services (BSSV) SOAP and the newer REST AIS surface.

- **Upstream:** Talend's JDE component family
- **Estimated effort:** 5 days (the BSSV WSDL handling is painful — Talend's code is genuinely valuable here)
- **Why this matters:** Mid-market manufacturing + agriculture verticals. Niche but the orgs that need it really need it.

## ⭐ Next 10 — continuous

Lower-leverage individually but together cover the long-tail enterprise stack.

| # | Connector | Talend component family | Effort | Notes |
|---|---|---|---|---|
| 6 | NetSuite SuiteTalk REST | `tNetSuiteInput` / `tNetSuiteOutput` | 4d | NetSuite token-based auth + SuiteQL — Talend has the OAuth dance baked in |
| 7 | MSSQL bulk load | `tMSSqlBulkExec` | 3d | We have a generic MSSQL connector; this adds BCP / native bulk-copy depth |
| 8 | Snowflake bulk depth | `tSnowflakeBulkExec` | 2d | We have v1 Snowflake; Talend's PUT + COPY INTO has years of tuning for stage paths + file-format options |
| 9 | Teradata bulk + FastLoad | `tTeradataFastLoad` | 4d | FastLoad protocol is undocumented outside of Talend / Informatica source |
| 10 | IBM DB2 z/OS | `tDB2Input` mainframe variant | 4d | DRDA protocol quirks — Talend's connection params are gold |
| 11 | Microsoft Dynamics 365 (Dataverse) | `tMicrosoftDynamicsCrm*` | 4d | OAuth + Dataverse Web API — Talend has the entity-paging edge cases |
| 12 | Hubspot CRM | `tHubspotInput` | 2d | Mostly standard; Talend's rate-limit handling is worth porting |
| 13 | Marketo | `tMarketoInput` | 3d | OAuth + bulk-export polling — Talend's polling loop avoids common pitfalls |
| 14 | Google Analytics 4 Data API | (newer Talend Cloud component) | 3d | GA4's odd dimension/metric pivot is tricky — check Talend's handling |
| 15 | ServiceNow Table API | `tServiceNow*` | 3d | Pagination + change-tracking timestamps — Talend covers the gotchas |

## 💡 Opportunistic — community-driven

Worth porting when someone in the community asks for them via a `connector-request` issue, but not worth scheduled effort.

- SAP Concur (T&E)
- SuccessFactors HRIS (already manifest-only in F-Pulse — depth pass available from Talend source)
- Coupa
- Ariba
- Epicor Kinetic
- Microsoft Power BI Dataflows
- Tableau Server REST
- Splunk HEC depth
- Various mainframe formats (COBOL copybook readers, etc.)

## Estimating a quarter's progress

Realistic single-developer pace, assuming a 5-day work-week and other duties:

- Top-5 ports: 1 per 1–2 weeks = **3 ports per quarter** at sustainable pace
- Next-10 ports: 1 per week if no top-5 work = **8 ports per quarter** if focused

A reasonable launch-quarter target: **top 5 ports done + 2–3 from the next 10**. That's 7–8 new high-value connectors in 3 months — comparable to a Talend connector-team's pace, with the bonus that we're starting from their production-tested code rather than vendor docs.

## Tracking ports

Each completed port gets:

1. **NOTICE entry** appended to the repo-root `NOTICE` file
2. **PR linked to this doc** — line through the entry below + add a "✅ shipped in v1.X.Y" suffix
3. **Cert-matrix update** — the new connector starts at `v1 functional`; subsequent fixture work promotes it to `v2 beta` and eventually `production-certified`

When something ships, edit this file to reflect status. The roadmap is the artefact that turns "we'd like to derive from Talend" into a concrete, trackable program.

## See also

- [derive-from-talend.md](derive-from-talend.md) — the safe-derivation process
- [build-a-connector.md](build-a-connector.md) — general connector-authoring paths
- [../connectors.md](../connectors.md) — current first-party catalog status
- [NOTICE](../../NOTICE) — where attribution lives
