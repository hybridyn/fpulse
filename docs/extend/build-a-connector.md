# Build your own connector in 30 minutes

F-Pulse ships ~37 first-party connector manifests, but the moment you have an internal API or a niche SaaS tool, you'll need one we don't ship. **This is fine.** F-Pulse OSS is designed around the assumption that you'll bring your own — the framework is the product, the catalog is just the starter pack.

This tutorial walks you end-to-end through building a working connector from scratch, three ways:

1. [**Fast path: From OpenAPI**](#fast-path-from-openapi) — 90 seconds, when the vendor publishes a spec
2. [**Medium path: From sample responses**](#medium-path-from-sample-responses) — ~10 minutes, when there's no spec
3. [**Full path: Hand-authored manifest**](#full-path-hand-authored-manifest) — ~30 minutes, for full control

Each produces a `v2.json` manifest that drops straight into the F-Pulse install. **No compile step, no IDE, no language tax.**

---

## Before you start

You need:

- A running F-Pulse install (any of [Docker / source / single-container](../../readme.md#quick-start))
- Admin or workspace-editor permission (Author Connector is a write surface)
- For paths 1 + 2: a browser pointed at `http://localhost:5174`
- For path 3: a text editor; the manifest goes into `backend/fpulse/connectors/manifests/`

You do **not** need:

- An LLM / AI provider — the generator is deterministic, no LLM call required
- Vendor-side setup — works against a public OpenAPI URL or pasted curl output
- Plus license — every authoring path is open in OSS

---

## Fast path: From OpenAPI

Best when the vendor publishes an OpenAPI 3.x spec. Works for most modern enterprise REST surfaces (Stripe, GitHub, Linear, Notion, ServiceNow, Workday REST, etc.).

### 1. Find the OpenAPI URL

Look in the vendor's developer docs for a link to `openapi.json` / `openapi.yaml` / `swagger.json`. Examples:

- Stripe: `https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json`
- GitHub: `https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json`
- Your internal API: whatever your team publishes (most teams have one)

### 2. Open Author Connector

In the F-Pulse sidebar: **Insights → Author Connector**.

Pick the **From OpenAPI** tab.

### 3. Paste the URL, click Generate

The generator reads the spec and produces:

| What it reads | What it produces |
|---|---|
| `servers[]` | Base URL configuration |
| `securitySchemes` | Auth section (basic / bearer / API key / OAuth2) |
| Each GET endpoint with array response | A **stream** (incremental + full-refresh aware) |
| Each POST/PUT/DELETE endpoint | An **action** (writable surface for sink nodes) |
| `responses[].content.schema` | Response shape + column types |
| Pagination patterns (page / cursor / link header) | Pagination config per stream |

You'll see a preview with every detected stream and action. Review it, click **Download manifest** → you get a `<connector>.v2.json` file.

### 4. Drop the manifest into the manifests directory

```bash
mv ~/Downloads/your-connector.v2.json backend/fpulse/connectors/manifests/
```

Restart F-Pulse (or hit `POST /api/connectors/reload-manifests` if you've enabled it in your install). The connector now shows up in the Connections page picker.

### 5. Create a connection + test

Connections page → **+ Create Connection** → pick the connector you just authored → fill credentials → **Test connection**. If the auth path validates, you're done.

You can now use this connector in pipelines (Source / Sink / Read / Write nodes), schedule pipelines that hit it, alert on failures, etc. — same as any first-party connector.

**Total time: ~90 seconds** from "I need this connector" to "running pipeline against it."

---

## Medium path: From sample responses

Best when the vendor doesn't publish an OpenAPI spec, or the spec is hand-wavy and you'd rather work from real responses.

### 1. Collect 1–5 sample responses

Use `curl` or Postman to hit the endpoints you care about, save the raw JSON. Two-to-three responses per endpoint is plenty — the generator uses them to infer the field types and the shape.

```bash
curl -H "Authorization: Bearer $TOKEN" https://api.example.com/v1/users > users.json
curl -H "Authorization: Bearer $TOKEN" https://api.example.com/v1/orders > orders.json
curl -H "Authorization: Bearer $TOKEN" https://api.example.com/v1/orders/123/items > order_items.json
```

### 2. Open Author Connector → From Samples

Same sidebar entry: **Insights → Author Connector**. Pick the **From Samples** tab.

### 3. Paste each response + tell us the URL pattern

For each sample you paste, fill in:

- The endpoint URL pattern (`/v1/users`, `/v1/orders`, etc.)
- The HTTP method (almost always GET for stream samples)
- The auth scheme (the generator infers from sample headers if you paste those too)
- Whether responses are paginated and how (page / cursor / link header)

### 4. Click Generate → review → download

Same workflow as the OpenAPI path from here. You get a `<connector>.v2.json` to drop in.

The "from samples" path produces a less-rich manifest than OpenAPI (no auto-detected sink actions, fewer streams) but covers the common "read endpoints from a SaaS" case in under 10 minutes.

---

## Full path: Hand-authored manifest

Use this when you want full control — custom auth (HMAC signatures, mutual TLS), unusual pagination, complex multi-stage handshakes, or stream-level fixtures.

### 1. Start from an existing manifest

Copy one that's close to what you need:

```bash
cp backend/fpulse/connectors/manifests/github.v2.json \
   backend/fpulse/connectors/manifests/my_connector.v2.json
```

Edit the copy in your editor.

### 2. The five required sections

A minimal v2 manifest has:

```json
{
  "id": "my_connector",
  "name": "My Connector",
  "version": "v2",
  "category": "saas",
  "auth": { "type": "bearer" },
  "base_url": "https://api.example.com/v1",
  "streams": [
    {
      "name": "items",
      "endpoint": "/items",
      "method": "GET",
      "pagination": { "type": "page", "page_param": "page", "size_param": "limit", "size": 100 },
      "primary_key": "id"
    }
  ],
  "actions": [],
  "fixtures": {}
}
```

### 3. Auth schemes supported

| `auth.type` | Inputs the connection UI will collect |
|---|---|
| `none` | (nothing) |
| `basic` | `username`, `password` |
| `bearer` | `token` |
| `api_key` | `api_key`, `header_name` (defaults to `X-API-Key`) |
| `oauth2_client_credentials` | `client_id`, `client_secret`, `token_url`, `scopes` |
| `oauth2_authorization_code` | Standard OAuth2 dance — see manifests/google_drive.v2.json |
| `aws_sigv4` | `access_key_id`, `secret_access_key`, `region`, `service` |
| `custom` | You'll wire a Python tester (see `backend/fpulse/connections/tester.py`) |

### 4. Pagination patterns supported

| `pagination.type` | When to use |
|---|---|
| `none` | Single-page responses |
| `page` | `?page=1&limit=100` style |
| `offset` | `?offset=0&limit=100` style |
| `cursor` | Response includes `next_cursor` / `next_page_token` |
| `link_header` | RFC 5988 `Link: <...>; rel="next"` (GitHub / Stripe style) |

### 5. Validate the manifest

```bash
python -m fpulse.connectors.validate backend/fpulse/connectors/manifests/my_connector.v2.json
```

The F0.1 validator checks every required field, type-compatibility, and runs each stream's fixtures (if present) through a dry-run.

### 6. Add fixtures for cert-matrix promotion

If you want your connector to graduate from "v1 functional" to **production-certified** in the cert matrix, ship the five required fixtures:

```
backend/fpulse/connectors/fixtures/my_connector/
├── auth_error.json       # 401 / 403 response — tester catches and surfaces it
├── empty.json            # empty array — stream handles cleanly, no crash
├── happy_path.json       # representative successful response
├── rate_limit.json       # 429 + Retry-After — backoff respected
└── schema_drift.json     # extra field added by vendor — manifest survives
```

The cert-matrix daemon picks these up automatically and bumps your connector's status.

---

## Share your connector

Built something useful? Two ways to share:

1. **Quick share** — drop your manifest into a Gist / your team's docs / internal wiki. Other F-Pulse installs can drop it into their `manifests/` directory the same way you did.
2. **Contribute back** — [open a connector-contribution PR](https://github.com/hybridyn/fpulse/issues/new/choose) → we review, run the fixtures, and ship it as a first-party manifest in the next release. Your name on the contributors list, every F-Pulse user gets the connector.

---

## See also

- [docs/connector-authoring.md](../connector-authoring.md) — full UI reference + all options for OpenAPI / sample modes
- [docs/connectors.md](../connectors.md) — current first-party catalog + cert-matrix status
- [docs/vs-talend.md](../vs-talend.md) — why this framework matters vs Talend's Eclipse + Java extension model
- [Request a connector or node](https://github.com/hybridyn/fpulse/issues/new/choose) — if your need is generic enough that we should ship it first-party
