# Design: Tier-1 connector uplift to depth-score 5

**Status:** Design — implementation pending (blocked on F0.1 manifest v2)
**Owner:** F-Pulse core
**Estimated effort:** ~3 weeks (one engineer, focused) for all 18 production-grade SaaS connectors
**Tier-1 first wave:** Salesforce, Stripe, HubSpot, MS Teams (~1 week)

## Why these four first

The four Tier-1 connectors validate every major auth + pagination + rate-limit pattern we'll encounter in the remaining 14:

| Connector | Validates |
|---|---|
| **Salesforce** | OAuth2 + JWT bearer + nextRecordsUrl pagination + SystemModstamp incremental + Bulk API 2.0 path |
| **Stripe** | API-key auth + cursor (`starting_after`) pagination + idempotency keys on writes + webhook event source |
| **HubSpot** | OAuth2 + offset pagination + V3 API design + association batch reads |
| **MS Teams** | Microsoft Graph OAuth2 + `@odata.nextLink` pagination + delta queries + multi-tenant headers |

Once these four are at depth-5, the remaining 14 are pattern repetitions.

## Per-connector workflow

Each connector takes ~5-6 hours of focused work to move from depth-2 to depth-5:

1. **Manifest authoring** (~1 hr) — write `connectors/<type>/manifest.yaml` per F0.1 schema
2. **Stream handlers** (~2 hr) — one Python module per stream implementing read + write + incremental
3. **Fixtures** (~1 hr) — record 5 fixture types: happy_path, empty, auth_error, rate_limit, schema_drift
4. **Tests** (~1 hr) — pytest module that exercises all 5 fixtures offline
5. **Validation pass** (~30 min) — run F0.1 validator + measure depth_score (must be 5)
6. **Docs** (~30 min) — connector page on hybridyn.com/connectors/<type>

## Sequencing

Optimal order to minimize cross-connector blockers:

### Week 1 — Validate the patterns
- **Day 1-2: Salesforce** — most complex auth (OAuth2 + JWT + refresh rotation). If this works, every other OAuth2 connector follows.
- **Day 3: Stripe** — first API-key + cursor-pagination connector
- **Day 4-5: HubSpot** — OAuth2 again but offset-pagination; validates the offset path
- **Day 5: MS Teams** — Microsoft Graph (`@odata.nextLink`); validates Graph for downstream Outlook / OneDrive / SharePoint connectors

End-of-week-1 checkpoint: 4 connectors at depth-5, all 5 fixture types passing, F0.1 validator green, baseline perf measured.

### Week 2 — Pattern replication, finance + ops
6 connectors: PayPal, QuickBooks, Xero, Asana, Linear, PagerDuty

These reuse Stripe's API-key + cursor-pagination pattern.

### Week 3 — Comms + collab
8 connectors: Slack, GitHub, Notion, Mailchimp, Airtable, Zendesk, Intercom, Shopify

Mostly OAuth2 patterns already validated in week 1.

## Per-stream depth-5 contract (from F0.1)

Every stream must:

- ✅ Declare `primary_key` (or `[]` for append-only with reason)
- ✅ Declare `incremental_field` + `incremental_format` (or `cursor_strategy: full_refresh` with reason)
- ✅ Provide a JSON Schema (draft-07) describing every column with type + required-ness
- ✅ Handle pagination correctly across multiple pages (validated by happy_path fixture having ≥3 pages)
- ✅ Handle 401 → token refresh (validated by auth_error fixture)
- ✅ Handle 429 → exponential backoff with `Retry-After` (validated by rate_limit fixture)
- ✅ Log + continue on schema drift (validated by schema_drift fixture)
- ✅ Have a sink path with bulk-load (where applicable to dialect)

## Failure modes to test

The 5 fixture types map to 5 distinct failure modes:

1. **happy_path** — proves baseline works end-to-end across pagination
2. **empty** — connector handles 0-row response gracefully
3. **auth_error** — connector triggers token refresh + retries (or fails cleanly with actionable message)
4. **rate_limit** — connector backs off and retries (with bounded retries)
5. **schema_drift** — new fields in payload are logged + continue, removed required fields fail with actionable message

## Test infrastructure

```
backend/tests/connectors/
  test_salesforce_streams.py
  test_stripe_streams.py
  ...
  fixtures/                       (shared fixture loader)
    salesforce/account/
      happy_path.json
      empty.json
      auth_error.json
      rate_limit.json
      schema_drift.json
    ...
```

Each test follows this pattern:

```python
@pytest.mark.parametrize("fixture", ["happy_path", "empty", "auth_error", "rate_limit", "schema_drift"])
def test_salesforce_account_stream(fixture):
    handler = SalesforceAccountHandler(manifest=load_manifest("salesforce"))
    expected = load_fixture("salesforce", "account", fixture)
    rows = list(handler.read(client=MockClient(expected.responses)))
    assert handler.expected_outcome(fixture) == observed_outcome(rows, expected)
```

## Depth-score validation

CI runs `python -m fpulse.connector_certification --connector salesforce` after every connector PR:

```
$ python -m fpulse.connector_certification --connector salesforce
Connector: salesforce
Manifest version: 2 ✓
Streams: 7 (account, contact, opportunity, lead, case, user, task)
  account:
    primary_key: [Id] ✓
    incremental_field: SystemModstamp ✓
    schema: 38 fields, draft-07 valid ✓
    fixtures: happy_path, empty, auth_error, rate_limit, schema_drift ✓
    tests: 5/5 passing ✓
    depth_score: 5 ✓
  ...
Overall depth_score: 5 ✓
```

Failure to hit depth-5 blocks the merge.

## Customer-facing artifacts

Every Tier-1 connector ships with:

- A `/docs/connectors/<type>.md` page
- An entry in the `/trust` connector certification matrix with last-validated date
- A working example pipeline in `templates/<type>-to-warehouse.json`

## Effort breakdown

| Wave | Connectors | Days |
|---|---|---|
| Week 1 (pattern validation) | Salesforce, Stripe, HubSpot, MS Teams | 5 |
| Week 2 (finance + ops) | PayPal, QuickBooks, Xero, Asana, Linear, PagerDuty | 5 |
| Week 3 (comms + collab) | Slack, GitHub, Notion, Mailchimp, Airtable, Zendesk, Intercom, Shopify | 5 |
| **Total** | **18 connectors** | **15 days = 3 weeks focused** |

## Status

Design locked. Kickoff post-OSS-launch + post-F0.1. Tier-1 wave delivers the first marketing-credible "production-grade SaaS connectors" claim.
