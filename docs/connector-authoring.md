# Authoring a connector

F-Pulse ships 45 manifest files in `backend/fpulse/connectors/manifests/` —
37 v1 runtime manifests plus 8 v2 certification specs. The long tail of
internal APIs and niche SaaS tools means you will eventually need one we do not
ship. The **API Explorer** turns an OpenAPI spec, or a response you just tested,
into a connector definition.

The generator is deterministic. No LLM call is involved, and no AI provider
needs to be configured.

**Where:** Insights → API Explorer (`#author`).

---

## v1 runtime vs v2 certification — read this first

This distinction decides whether what you generate actually runs.

| | `<id>.json` (v1) | `<id>.v2.json` (v2) |
|---|---|---|
| Purpose | The **runtime** manifest | The **certification** spec (F0.1) |
| Loaded by the SaaS Connector node | **Yes** | **No** — `load_manifests()` explicitly skips `*.v2.json` |
| Produced by | `/from-openapi` (as `runtime_manifest`), `/from-openapi-runtime` | `/from-openapi`, `/from-samples` |
| Effect of shipping it | The connector works | Sitting beside a v1 file, it promotes that connector's tier to *certified* |

A v2 file on its own is **not runnable**. If you want a connector you can use,
you need the v1 runtime manifest — which is what the Explorer's **Save as Beta
connector** button persists.

---

## The three steps

### 1. Choose an endpoint

In the **Start here** card:

- **Start from a known API** — six references with publicly published specs
  (Stripe, GitHub, Slack, Twilio, Plaid, DigitalOcean). *Load specification*
  downloads the spec server-side from a fixed allowlist and lists its endpoints.
- **Paste or upload a specification** — OpenAPI JSON or YAML, parsed server-side
  so you need no YAML dependency in the browser.
- **Type any URL** — no specification required.

Only catalog IDs are accepted from the route (`#author?reference=stripe`).
Arbitrary spec URLs passed as route parameters are rejected by design.

### 2. Test it

Choose authentication, fill parameters, and send one request. Writes
(POST/PUT/PATCH/DELETE) require explicit confirmation. Inspect the response as
collapsible JSON, a typed record table, a structure listing, or headers — and
assert on it in the **Tests** tab (status, response time, a header, a JSON path,
or body text).

This step exists because a spec's *declared* authentication is not evidence that
your credentials work against the live API.

### 3. Generate a connector

Two inputs, which are **not** equivalent:

| Source | Endpoint | Returns | Savable |
|---|---|---|---|
| Imported OpenAPI spec | `/from-openapi` | v2 manifest **+ v1 runtime manifest** | Yes |
| The response you just tested | `/from-samples` | v2 manifest only | No |

Review the generated authentication per endpoint, then **Save as Beta
connector**. Saving requires an admin account, writes to the user manifest
store, and takes effect immediately — no restart and no filesystem access.
Saved connectors are listed by `GET /api/connectors/author/saved` and removable
by `DELETE /api/connectors/author/saved/{id}`.

---

### See also — `docs/extend/build-a-connector.md`

The API Explorer is one of four first-class paths covered by
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
| `servers[0].url` | Base URL |
| `components.securitySchemes` | `auth` block |
| `paths` — GET operations | One stream per resource |
| Response JSON Schema | Stream schema |
| `id` / `_id` / `*_id` | `primary_key` |
| `updated_at` / `created_at` / `created` | `incremental_field` |
| Query parameters | `pagination` config |

Pagination heuristic:

- `starting_after`, `cursor`, `after`, `next_token`, `page_token` → **cursor**
- `offset` or `skip` → **offset**
- `page` → page-number based
- none of the above → **none**, flagged for review

### From sample responses

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

Wrapped responses (`{ data: [...] }`, `{ results: [...] }`, `{ items: [...] }`,
`{ records: [...] }`) are unwrapped automatically; the schema is inferred from a
row, not the wrapper.

---

## What you still have to do

A generated manifest is a **starting point**, not a finished connector. It ships
at depth-score 1 with a `known_issues` list, marked beta. Before relying on it:

1. Confirm the inferred pagination field exists in the real response.
2. Verify the auth scheme matches what the API actually expects — some APIs
   declare `bearer` but want a custom header.
3. Exercise it against a real account.
4. Add fixtures if you want a higher depth score (see
   [extend/build-a-connector.md](extend/build-a-connector.md)).

If no endpoint has a supported authentication option, the Explorer says so —
the definition can still be saved, but execution will not work.

---

## HTTP API

The generators are reachable directly for scripting:

```bash
# v2 cert manifest + v1 runtime manifest
curl -X POST http://localhost:8001/api/connectors/author/from-openapi \
  -H "Content-Type: application/json" \
  -d '{"connector_id": "acme", "openapi_url": "https://api.acme.com/openapi.json"}'

# v2 only, inferred from real payloads
curl -X POST http://localhost:8001/api/connectors/author/from-samples \
  -H "Content-Type: application/json" \
  -d '{"connector_id": "acme", "samples": [{"id": "1", "created_at": "2026-05-06T00:00:00Z"}]}'

# v1 runtime manifest alone
curl -X POST http://localhost:8001/api/connectors/author/from-openapi-runtime \
  -H "Content-Type: application/json" \
  -d '{"connector_id": "acme", "openapi_url": "https://api.acme.com/openapi.json"}'
```

All three accept `openapi_spec` (a parsed dict) or `openapi_text` (raw JSON or
YAML) instead of `openapi_url`; precedence is spec > text > url. The URL fetch
is SSRF-guarded — private and internal addresses are rejected with `400`.

`from-openapi` and `from-samples` return `{ manifest, validation, mode }`, with
`runtime_manifest` added on the OpenAPI path.

Runtime-generated connectors appear at the **Generated** tier in the picker
until a curated `<id>.v2.json` cert manifest promotes them to **Certified**.

---

## Same thing from the Copilot

Ask the Copilot to build a connector and it uses the same engine behind a
human-approval gate:

- `draft_connector_from_openapi` — give it `openapi_text` (paste the spec the
  vendor gave you — the usual path for gated APIs like FactoHR), `openapi_spec`,
  or a public `openapi_url`. It creates an **inert PROPOSED draft** — nothing
  goes live and no credentials pass through the LLM (the manifest holds auth
  *templates* only).
- `draft_connector_from_samples` — when you only have example responses.
- An **admin approves** the draft (`POST /api/connectors/drafts/{id}/approve`),
  which activates it as a Beta connector. The API key is entered later, on the
  Connection.

### Optional: let the Copilot reach the web (default OFF)

F-Pulse is local-first, so the Copilot cannot browse by default. An admin turns
it on in **Settings → AI Provider → "Copilot web access"** — a live toggle, no
restart (or set `FPULSE_AI_WEB_ACCESS=1` for headless deploys). This registers
two READ-tier tools: `web_fetch` (SSRF-hardened, ≤1 MB — **needs no key**) and
`web_search`.

**`web_fetch` needs no provider** — point the Copilot at a URL you know (the
common case for building a connector from a vendor's spec). `web_search`
(discovery) needs a provider. Choose by deployment shape — enterprises should
**not** have their users sign up for third-party search:

| Provider | What it is | For |
|---|---|---|
| **searxng** | Your own [SearXNG](https://docs.searxng.org/) metasearch container — keyless, private, nothing leaves your network | ✅ enterprise / air-gap |
| **hybridyn** | Hybridyn-hosted search gateway — managed, no per-user signup (Plus/Enterprise) | managed / cloud |
| **brave** / **tavily** | A hosted search API you bring a personal key for (Tavily has a free, no-card tier; Brave now needs a card) | solo devs |

Configure the provider in the same Settings card. `searxng`/`hybridyn` take a
**URL** (no key); `brave`/`tavily` take an **API key**.

#### Self-host SearXNG (enterprise, keyless)

Run one container inside your network and point F-Pulse at it:

```yaml
# docker-compose.yml
services:
  searxng:
    image: searxng/searxng:latest
    ports: ["8080:8080"]
    environment:
      - SEARXNG_BASE_URL=http://localhost:8080/
    volumes:
      - ./searxng:/etc/searxng
```

In `./searxng/settings.yml`, enable the JSON API (F-Pulse calls
`/search?format=json`):

```yaml
search:
  formats: [html, json]
```

Then in the Settings card: provider **SearXNG**, URL `http://<host>:8080`. Done —
keyless web search that never leaves your perimeter.

Note: none of this helps for vendors like FactoHR that publish **no** spec
anywhere — there's nothing on the web to find; paste the spec instead.

---

## Limits and guards

- Pasted or uploaded specs: 2 MB. Remote JSON from the reference allowlist:
  32 MB. Discovery returns at most 5,000 operations and says when it truncates.
- Spec downloads: 30-second deadline, verified TLS, DNS-pinned addresses, no
  redirects, no forwarded app credentials.
- Test requests need developer rank; saving a connector needs admin.
- Response bodies are capped at 256 KB and arrays sampled to 100 entries;
  sensitive field names and supplied auth values are redacted.
- Nothing from the Explorer is persisted except a connector you explicitly save.

---

## See also

- [extend/build-a-connector.md](extend/build-a-connector.md) — end-to-end tutorial, including hand-authoring
- [api-explorer.md](api-explorer.md) — the Explorer surface in full
- [connectors.md](connectors.md) — the shipped catalog and cert-matrix status
