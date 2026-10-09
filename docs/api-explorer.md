# API Explorer

Insights > API Explorer (`#author`) is where you test a live API endpoint and, if
you want one, draft a connector from it. It replaced the separate "Author" and
"Gallery" tabs: six links to public specs are a starting point, not a
destination, and "Gallery" implied a catalogue of shipped connectors.

For the connectors F-Pulse already ships, see **Connections** (to use one) and
the **Trust** page (certification tiers per connector). The Explorer does not
list them.

## The three steps

The page states these on arrival, and the step rail navigates between them.

### 1. Choose an endpoint

Three ways in, all in the **Start here** card:

- **Start from a known API** — six curated references (Stripe, GitHub, Slack,
  Twilio, Plaid, DigitalOcean). *Load specification* downloads the public spec
  from a server-side allowlist and discovers searchable endpoints. It does not
  call the vendor's account APIs, generate a manifest or save a connection.
- **Paste or upload a specification** — OpenAPI JSON or YAML, parsed server-side.
- **Type any URL** — straight into the request bar; no specification needed.

Remote JSON imports allow up to 32 MB; YAML and pasted/uploaded specifications
retain the 2 MB parser limit. Downloads have a 30-second network deadline,
verified TLS, pinned validated DNS addresses, and no redirects or forwarded app
credentials. Discovery returns up to 5,000 operations and reports when the limit
is reached. A spec's declared authentication is not evidence of live access.

### 2. Test it

Fill path placeholders, authentication, headers, query parameters and an optional
body, then send **one** request. POST/PUT/PATCH/DELETE require confirmation.
There are no retries and no automatic pagination.

Inspect the result as collapsible JSON (with a key/value filter), a record table
with inferred column types, a structure listing, or response headers.

**Tests** asserts on the response: status code, response time, a header, a value
at a JSON path, or body text. Checks are declarative structs the engine
interprets — never evaluated code — and run on each send. An incomplete check is
skipped, not failed.

### 3. Generate a connector

Two possible inputs, and they are not equivalent:

| Source | Endpoint | Result |
|---|---|---|
| Imported OpenAPI spec | `/connectors/author/from-openapi` | v2 cert manifest **and** a v1 runtime manifest → savable as a Beta connector |
| The response you just tested | `/connectors/author/from-samples` | v2 manifest only → review/export, not savable |

Only the runtime manifest is loadable by the SaaS Connector node, so the
sample-derived path offers no Save button. Saving requires an admin account and
takes effect without a restart. Review the generated authentication per endpoint
before saving; a draft is a starting point, not a certified connector.

**Create connection** hands the endpoint to the REST connection form. The handoff
is one-shot and memory-only: it transfers the endpoint without its query, the
auth method and API key *name* — not passwords, tokens, custom headers, query
values or the body. Enter credentials in the connection setup.

## Boundaries

- Five authentication choices: none, Basic, Bearer, API key header, API key
  query. OAuth grant acquisition, cookie sessions and saved-credential selection
  are not implemented here.
- Network requests require developer rank. URLs are DNS-validated and connections
  pinned to a validated IP; TLS verifies the original hostname. Redirects are not
  followed. TLS verification cannot be disabled.
- Private targets are blocked unless the server sets
  `FPULSE_API_SOURCE_ALLOW_PRIVATE=1`. Link-local metadata, multicast, reserved
  and unspecified addresses remain blocked.
- Response bodies are bounded at 256 KB; JSON arrays are sampled to 100 entries.
  Sensitive JSON field names and supplied auth values are redacted. Responses may
  still contain business or personal data; nothing is persisted by the Explorer.
- Credentials and request history live in the page only, for the session. Nothing
  is written to disk.
- A successful response validates that one request — not pagination, other
  endpoints, API-wide permissions or an ETL workload. Failed HTTP responses
  remain inspectable.
- The backend must be running a build that registers
  `/api/connectors/author/explorer/{request,discover,reference}`.
