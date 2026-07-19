# Author a connector with AI

> Add support for any external API in **under 90 seconds**, without
> hand-writing JSON.

F-Pulse ships with ~37 manifests in the catalog, but the long tail of
internal APIs and niche SaaS tools means you'll always need one we
don't ship. The **Author Connector** feature turns any OpenAPI spec
or sample response into a working v2 manifest skeleton, runs it
through the F0.1 validator, and hands you a downloadable
`<connector>.v2.json` file you can drop into the manifests directory.

The generator is deterministic — no LLM call is required. The
deterministic core is what makes the demo land in 90 seconds.

---

## Where to find it

**Insights → Author Connector** (sidebar nav). Pairs with **Insights → Gallery**, the browse-side of the same loop where you can pick from curated starting points or jump to the community board.

Two input modes:

| Mode | Use when | Input |
|---|---|---|
| **OpenAPI spec** *(recommended)* | The vendor publishes an OpenAPI 3.x spec | Public URL to a JSON or YAML spec |
| **Sample responses** | No public spec exists | Paste 1–5 raw JSON responses from `curl` |

The OpenAPI path produces dramatically better output because it gets
free signals: auth scheme, every paginated endpoint, response shapes.
Use it when you can.

### Common starting points (one-click pre-fill)

In OpenAPI mode the Basics step shows a small gallery of curated
starting points — six popular vendors with publicly published OpenAPI
specs (Stripe, GitHub, Slack, Twilio, DigitalOcean, Plaid). Click any
card → the connector ID, display name, and OpenAPI URL pre-fill
themselves. From there you go Continue → Generate and have a working
manifest in ~90 seconds without having to think up an example URL.

The same six cards appear (with credit lines for the source repos) on
the Insights → Gallery tab, where each one links back here pre-filled
via `prefill_id` + `prefill_url` URL parameters. Adding more starting
points is a one-line edit to `STARTING_POINTS` in
`frontend/src/components/pages/ConnectorAuthorPage.tsx`.

### See also — `docs/extend/build-a-connector.md`

The Author Connector UI is one of four first-class paths covered by
the end-to-end tutorial at [docs/extend/build-a-connector.md](extend/build-a-connector.md):

1. **Fast path — From OpenAPI** (this UI, OpenAPI mode) — ~90 seconds
2. **Medium path — From samples** (this UI, samples mode) — ~10 minutes
3. **Full path — hand-authored manifest** — ~30 minutes
4. **Derive from an existing Apache-2.0 OSS project** — ~1 day

The first two live in this page. Paths 3 and 4 walk through the manifest
format and the safe-derivation process. Pick whichever fits the vendor +
the time budget; the four paths produce the same artefact (a `v2.json`
manifest in your `backend/fpulse/connectors/manifests/` directory).

---

## What the generator infers

### From an OpenAPI spec

| What it reads | What it produces |
|---|---|
| `info.title` | `connector.display_name` |
| `servers[0].url` | `connector.homepage` |
| `components.securitySchemes` | `auth.schemes[*]` (jwt_bearer / api_key / basic / oauth2) |
| `paths` — every `GET` returning an array | One stream per resource |
| Stream's response JSON Schema | `streams[*].schema` |
| Stream's `id` / `_id` / `*_id` field | `streams[*].primary_key` |
| Stream's `updated_at` / `created_at` / `created` | `streams[*].incremental_field` |
| Query parameters (`starting_after`, `cursor`, `page`, `offset`, `limit`) | `streams[*].pagination.{strategy,*_param,page_size}` |
| Hard-coded sensible defaults | `rate_limit.{default,retry}` (60 rpm, exp backoff, retry on 429/5xx) |

Pagination heuristic:

- `starting_after`, `cursor`, `after`, `next_token`, `page_token` → **cursor** strategy
- `offset` or `skip` → **offset** strategy
- `page` → **page_token** strategy (page-number based)
- None of the above → **none** with a `_note` flagging it for review

### From sample responses

Schema is inferred per-field from the actual data:

| Sample value | Inferred type |
|---|---|
| `null` | `["string", "null"]` |
| `true` / `false` | `boolean` |
| `42` | `integer` |
| `3.14` | `number` |
| `"2026-05-06T12:00:00Z"` | `string`, `format: date-time` |
| `"foo@bar.com"` | `string`, `format: email` |
| `[...]` | `array` (item type from first element) |
| `{...}` | nested `object` |

Wrapped responses (`{ data: [...] }`, `{ results: [...] }`,
`{ items: [...] }`, `{ records: [...] }`) get unwrapped automatically;
schema is inferred from a single row, not the wrapper.

---

## Output

Every generated manifest:

- **Validates** through `manifest_v2.py`'s F0.1 validator
- **Ships at depth-score 1** by default (auto-generated → minimum depth)
- **Carries a `known_issues` list** with explicit TODOs for the pieces
  that need human review (cursor-field correctness, rate limits,
  fixture files for higher depth scores)
- **Marked `status: beta`, `owner: community`** so it's clearly distinct
  from F-Pulse-maintained certified connectors

The output is a **starter, not a finished product.** You still need to:

1. Confirm the inferred pagination cursor field actually exists in the
   API's response (the generator stubs it as `next_cursor`)
2. Verify the auth scheme matches the API's real expectation (some
   APIs declare `bearer` but want a custom header)
3. Add the 5 fixture files (`happy_path`, `empty`, `auth_error`,
   `rate_limit`, `schema_drift`) to reach depth ≥ 3
4. Test it against a real account before promoting beyond depth 1

---

## Example: Stripe in 60 seconds

1. Open **Insights → Author Connector**
2. Mode: **OpenAPI spec**
3. Connector ID: `stripe_demo`
4. OpenAPI URL: `https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json`
5. Click **Generate manifest**

Expected output:
- ~10 streams (`customers`, `charges`, `invoices`, `subscriptions`, …)
- Auth scheme: `basic` (Stripe uses HTTP Basic with API key as username)
- Pagination: `cursor` with `starting_after`
- Primary key: `["id"]` per stream
- Incremental field: `created` (unix_seconds format)
- Validation passes; depth score 1/5 with TODOs in `known_issues`

Click **Download .v2.json**, drop the file in
`backend/fpulse/connectors/manifests/`, restart the backend — your
new connector appears in the catalog.

---

## Example: from a single curl response

You don't have a spec? Paste one response:

```json
[
  {
    "id": "ord_123",
    "customer_id": "cus_456",
    "total_cents": 4999,
    "currency": "USD",
    "status": "paid",
    "created_at": "2026-05-06T10:00:00Z"
  }
]
```

Mode: **Sample responses**, paste the JSON, click Generate.

Output:
- One stream named `items` (rename via `stream_name` if you know better)
- Inferred schema: 6 properties, primary key `["id"]`,
  incremental field `created_at` (iso8601)
- Pagination: `none` with a `_note` reminding you to wire it up
- Auth: `custom` placeholder — TODO

---

## API endpoints

The same generators are reachable via HTTP for scripting:

```bash
# OpenAPI mode
curl -X POST http://localhost:8001/api/connectors/author/from-openapi \
  -H "Content-Type: application/json" \
  -d '{
    "connector_id": "stripe_demo",
    "openapi_url": "https://api.stripe.com/openapi.json"
  }'

# Samples mode
curl -X POST http://localhost:8001/api/connectors/author/from-samples \
  -H "Content-Type: application/json" \
  -d '{
    "connector_id": "internal_orders",
    "samples": [{"id": "1", "total": 49.99, "created_at": "2026-05-06T00:00:00Z"}]
  }'
```

`from-openapi` and `from-samples` both return:

```json
{
  "manifest": { ... },
  "validation": {
    "connector_id": "stripe_demo",
    "valid": true,
    "declared_depth_score": 1,
    "computed_depth_score": 1,
    "effective_depth_score": 1,
    "errors": [],
    "warnings": [...],
    "streams_evaluated": ["customers", "charges", ...]
  },
  "mode": "openapi"
}
```

### Runtime (v1) manifest — immediately usable

The two generators above produce a **v2 certification** manifest meant for
review and curation. If you just want a connector you can run *right now*,
call the runtime variant — it returns a **v1 runtime manifest** that the
SaaS Connector node loads directly (paths, methods, and pagination inferred
from the spec):

```bash
curl -X POST http://localhost:8001/api/connectors/author/from-openapi-runtime \
  -H "Content-Type: application/json" \
  -d '{
    "connector_id": "my_api",
    "openapi_url": "https://api.example.com/openapi.json"
  }'
```

Pass `openapi_spec` (an already-parsed dict) instead of `openapi_url` to skip
the fetch. The fetch is SSRF-guarded — internal / private-IP URLs are
rejected with `400`. Runtime-generated connectors appear at the **Generated**
tier in the picker until a curated `<id>.v2.json` cert manifest promotes them
to **Certified**.

---

## Roadmap

The deterministic generator is the foundation. Planned enhancements:

- **AI polish** — LLM rewrites pagination heuristics, suggests better
  primary keys, fills in stream-level `description` fields
- **Live test fetch** — preview a real `GET` against the inferred
  endpoint with the user's credentials before save
- **Save action** — drop the manifest into the local manifests
  directory in one click, with a "scan for new connectors" trigger
- **Diff against existing manifest** — re-generating with new sample
  data shows a structured diff so you don't lose hand-edits
