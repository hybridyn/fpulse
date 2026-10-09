# Build your own connector

F-Pulse ships 37 v1 connector manifests, but the moment you have an internal API
or a niche SaaS tool you will need one we do not ship. **This is fine.** F-Pulse
OSS assumes you will bring your own — the framework is the product, the catalog
is a starter pack.

Three paths, in increasing order of effort and control:

1. [**From a specification**](#path-1-from-a-specification) — the API Explorer generates a runnable connector
2. [**From a tested response**](#path-2-from-a-tested-response) — when no spec exists
3. [**Hand-authored**](#path-3-hand-authored) — full control over auth, pagination and fixtures


---

## The one thing to understand first

There are two manifest formats and only one of them runs.

- **`<id>.json` (v1)** is the runtime manifest. The SaaS Connector node loads
  it. This is the file that makes a connector work.
- **`<id>.v2.json` (v2)** is the certification spec. `load_manifests()`
  deliberately skips it. Placed beside a v1 file it promotes that connector's
  tier to *certified*; on its own it does nothing at runtime.

Generate a v2 file, drop it in `manifests/`, restart, and you will find **no new
connector**. Paths 1 and 3 below produce v1.

---

## Before you start

You need:

- A running F-Pulse install ([Docker / PyPI / source](../../README.md#quick-start))
- **Developer** rank to send test requests; **admin** to save a connector
- For path 3: a text editor, and write access to
  `backend/fpulse/connectors/manifests/`

You do **not** need an LLM provider (the generator is deterministic), vendor-side
setup, or a Plus licence — every authoring path is open in OSS.

---

## Path 1: From a specification

Best when the vendor publishes OpenAPI 3.x.

### 1. Open Insights → API Explorer

Step one offers six known APIs with published specs, or paste/upload your own
(JSON or YAML, up to 2 MB). Your internal API probably publishes one too.

### 2. Pick an endpoint and test it

Select a discovered endpoint, choose authentication, and send one request.

Do not skip this. A spec's declared authentication is not evidence that your
credentials work, and the generated connector inherits whatever the spec
claims. Use the **Tests** tab to assert what you expect — a 200, a JSON path
that must exist, an acceptable response time.

### 3. Generate and review

Open the **Connector** tab, keep the **OpenAPI specification** source selected,
give it an id (lowercase, digits and underscores), and generate.

You get a v2 cert manifest **and** a v1 runtime manifest. Review the per-endpoint
authentication panel before going further. If it reports *no endpoint has a
supported authentication option*, the definition will save but will not execute.

### 4. Save

**Save as Beta connector** writes the v1 runtime manifest to the user manifest
store. It is usable immediately — no restart, no filesystem access. It appears
at the **Beta** tier, clearly distinct from first-party certified connectors.

Prefer a file on disk? **Download** gives you the JSON to place in
`backend/fpulse/connectors/manifests/` — save it as `<id>.json`, not
`<id>.v2.json`, and restart.

### 5. Create a connection

Connections → **+ Create Connection** → pick your connector → fill credentials →
**Test connection**. From there it behaves like any other connector in Source,
Sink, Read and Write nodes.

---

## Path 2: From a tested response

Best when there is no spec, or the spec is too hand-wavy to trust.

Test a real endpoint in the Explorer, then open the **Connector** tab and choose
**This response** as the source. The generator infers field types from actual
data — which is often more accurate than a stale spec.

**The limitation is important:** this path returns a v2 manifest only. There is
no runtime definition, so there is nothing to save as a connector. Use it to get
a reviewed schema draft, download it, and finish the runtime manifest by hand
(path 3).

---

## Path 3: Hand-authored

Use this for custom auth, unusual pagination, multi-stage handshakes, or when
you want fixtures.

### 1. Start from a manifest close to yours

```bash
cp backend/fpulse/connectors/manifests/github.json \
   backend/fpulse/connectors/manifests/my_connector.json
```

Copy a **v1** file — those are the ones that run.

### 2. Minimum shape

```json
{
  "id": "my_connector",
  "name": "My Connector",
  "category": "saas",
  "base_url": "https://api.example.com/v1",
  "auth": { "type": "bearer" },
  "streams": [
    {
      "name": "items",
      "path": "/items",
      "method": "GET",
      "pagination": { "type": "page", "page_param": "page", "size_param": "limit" },
      "primary_key": "id"
    }
  ]
}
```

### 3. Auth types in use across shipped manifests

| `auth.type` | Manifests using it |
|---|---|
| `bearer` | 15 |
| `basic` | 10 |
| `oauth2` | 8 |
| `api_key` | 4 |

### 4. Pagination types in use

| `pagination.type` | Streams using it |
|---|---|
| `cursor` | 37 |
| `offset` | 17 |
| `offset_limit` | 16 |
| `page` | 14 |
| `url` | 8 |
| `link_header` | 6 |
| `none` | 2 |

Both tables are counted from the shipped manifests rather than from a schema
document, so they reflect what the runtime demonstrably supports. Read a real
manifest for the exact field names each type expects.

### 5. Load it

Restart the backend. `load_manifests()` picks up `*.json` and skips `*.v2.json`.
There is no reload endpoint.

### 6. Fixtures, for a higher depth score

```
backend/fpulse/connectors/fixtures/my_connector/
├── auth_error.json       # 401 / 403 — the tester surfaces it
├── empty.json            # empty array — no crash
├── happy_path.json       # representative success
├── rate_limit.json       # 429 + Retry-After — backoff respected
└── schema_drift.json     # vendor adds a field — manifest survives
```

Fixtures feed the certification depth score reported by
`GET /api/connectors/cert-matrix`. They do not promote a connector on their own:
the bar for **Verified** is a live-vendor smoke test on every PR plus a stored
fixture, and **Production** adds a 30-day green streak and a named owner.

---

## Share your connector

Built something useful? Two ways to share:

1. **Quick share** — drop your manifest into a Gist / your team's docs / internal wiki. Other F-Pulse installs can drop it into their `manifests/` directory the same way you did.
2. **Contribute back** — [open a connector-contribution PR](https://github.com/hybridyn/fpulse/issues/new/choose) → we review, run the fixtures, and ship it as a first-party manifest in the next release. Your name on the contributors list, every F-Pulse user gets the connector.

---

## See also

- [docs/connector-authoring.md](../connector-authoring.md) — full UI reference + all options for OpenAPI / sample modes
- [../api-explorer.md](../api-explorer.md) — testing and assertions in full
- [docs/connectors.md](../connectors.md) — current first-party catalog + cert-matrix status
- [docs/vs-talend.md](../vs-talend.md) — why this framework matters vs Talend's Eclipse + Java extension model
- [Request a connector or node](https://github.com/hybridyn/fpulse/issues/new/choose) — if your need is generic enough that we should ship it first-party
