# Design: Connector Manifest Schema v2 (F0.1)

**Status:** Design — review pending
**Date:** 2026-05-03
**Owner:** F-Pulse core
**Replaces:** Manifest v1 (implicit, no formal schema)

---

## Problem

May 3 connector audit found four catastrophic absences in **every** current connector manifest:

1. **No `incremental_field`** → no watermark / CDC support → every sync is a full sync
2. **No `schema` block** → runtime schema discovery only → silent drift
3. **No `primary_key`** → no upsert / dedup → re-runs append duplicates
4. **No `fixtures`** → no offline tests → tests must hit live APIs

All 37 SaaS manifests + 8 JDBC dialects score depth ≤2 of 5 today. Sprint 1 bulk loaders cannot ship without v2 because they need PK + incremental_field declared at manifest level.

---

## Goals

1. Declarative connector contract per stream: PK, incremental field, schema, rate limits, dependencies
2. Offline testability: fixtures cover happy_path / empty / auth_error / rate_limit / schema_drift
3. Certification surface: `depth_score` (0-5) drives the customer-facing matrix
4. Backward compatible: v1 manifests valid; v2 is additive; v1 = `depth_score=0`

---

## Schema

```yaml
version: 2
connector:
  type: salesforce
  display_name: Salesforce
  category: SaaS
  oss: true                   # false = F-Pulse+ only

certification:
  depth_score: 5              # 0-5 (see rubric)
  status: certified
  last_validated: 2026-05-15
  owner: core

auth:
  schemes:
    - type: oauth2
      flow: authorization_code

rate_limit:
  default:
    requests_per_minute: 1000
  retry:
    max_attempts: 5
    backoff: exponential
    retry_on_status: [429, 500, 502, 503, 504]

streams:
  - name: account
    primary_key: [Id]
    incremental_field: SystemModstamp
    incremental_format: iso8601
    soft_delete_field: IsDeleted
    cursor_strategy: timestamp
    pagination:
      strategy: cursor
      page_size: 2000
    depends_on: []
    schema:
      $schema: http://json-schema.org/draft-07/schema#
      type: object
      required: [Id, Name, SystemModstamp]
      properties:
        Id: { type: string, maxLength: 18 }
        Name: { type: string, maxLength: 255 }
        SystemModstamp: { type: string, format: date-time }

fixtures:
  - { stream: account, name: happy_path, file: fixtures/account/happy_path.json }
  - { stream: account, name: empty, file: fixtures/account/empty.json }
  - { stream: account, name: auth_error, file: fixtures/account/auth_error.json }
  - { stream: account, name: rate_limit, file: fixtures/account/rate_limit.json }
  - { stream: account, name: schema_drift, file: fixtures/account/schema_drift.json }
```

---

## Depth score rubric (0–5)

| Score | Means |
|---|---|
| 0 | UI present, no backend |
| 1 | Basic API call works |
| 2 | Pagination handled |
| 3 | Incremental sync wired |
| 4 | Primary key + upsert path |
| 5 | Full v2 contract incl. bulk-load sink |

Today's max = 2. Goal: 18 production-grade connectors at score 5 within 3 weeks.

---

## Validation rules (CI-enforced)

- `version: 2`
- Every stream declares `primary_key` (or explicit `[]` for append-only)
- Every stream declares `incremental_field` OR `cursor_strategy: full_refresh`
- `schema` is valid JSON Schema draft-07
- Every stream has all 5 fixture types
- `depends_on` graph is acyclic

Failures → `depth_score=0` regardless of declared.

---

## File layout

```
connectors/<type>/
  manifest.yaml
  fixtures/<stream>/{happy_path,empty,auth_error,rate_limit,schema_drift}.json
  handlers/<stream>.py
  tests/test_<stream>.py
```

---

## Migration path from v1

1. `migrate_v1_to_v2.py` — generate v2 skeleton with `depth_score: 0` + TODO stubs
2. Per-connector uplift (humans): Tier-1 = Salesforce, Stripe, HubSpot, MS Teams (validates every major auth pattern)
3. CI gate becomes required after Tier-1 ships; legacy v1 gets 90-day grace

---

## Connector picker UX impact

Once v2 ships, the picker badges read `depth_score` from manifest (replacing the hardcoded `CONNECTOR_STATUS` map added on May 3). Tooltip shows depth_score + last_validated + known_issues. "Coming soon" badge replaces `roadmap`. F-Pulse+ chip for `oss: false`.

---

## Cost estimate

- Schema + validator + migration script: 3-4 days, one engineer
- Per-connector v2 uplift: 5-6 hours focused
- Tier-1 (4 connectors): ~24 hours, validates patterns
- 18 production-grade total: ~108 hours / 3 weeks focused

---

## What this unblocks

- Sprint 1 bulk-load nodes (need PK declaration)
- Sprint 1 incremental sync (need incremental_field)
- /trust connector certification matrix
- Customer answer to "does it support upsert?" (yes/no per manifest)
- Connector SDK for community
- Per-connector test isolation (fixtures, no live calls)

---

## Open questions

1. JSON or YAML for fixtures? **Proposal:** JSON for fixtures, YAML for manifests
2. Schema `$ref` reuse across streams? **Proposal:** yes, via `connectors/<type>/schemas/*.json`
3. Stream-level rate limit override? **Proposal:** allowed; falls through to connector-level
4. Sink-side schema (write semantics, batch size)? **Proposal:** optional `sink:` block per stream
5. Connector versioning (semver vs schema version)? **Proposal:** `connector.version: "1.2.3"` separate from manifest `version: 2`

---

## Status

Locked-in design pending one round of review. Implementation kickoff: post-OSS-launch.
