# Derive a connector from Talend Open Studio (Apache 2.0)

A safe, repeatable workflow for porting connector logic from Talend Open Studio's Apache-2.0 source into F-Pulse. The goal: extract Talend's 15 years of production-tested vendor-specific knowledge (auth flows, pagination quirks, error handling, JDBC tuning constants) without the legal nervousness of "are we allowed to do this?"

> **Short answer: Yes, you're allowed.** Both projects are Apache License 2.0. The license is *designed* to enable exactly this kind of cross-pollination. Three obligations: keep a copy of Apache 2.0 (we do — `LICENSE`), add an attribution line to the `NOTICE` file (we have a section ready), state significant changes (a comment in the derived file is enough). That's the whole compliance story.

## When to use this path

| Use this when | Use the from-OpenAPI path instead when |
|---|---|
| The vendor system is legacy enterprise (SAP, Oracle EBS, JDE, Workday SOAP, Salesforce SOAP, etc.) | Vendor publishes a clean OpenAPI 3.x spec |
| The vendor's auth flow is complex (mutual TLS, HMAC signatures, custom token dances) | Auth is standard Bearer / Basic / OAuth2 |
| Vendor docs are vague or incomplete on real-world quirks | Vendor docs are complete + recent |
| You want a connector that already has years of production-bug-fix scar tissue | You're fine with a fresh implementation and don't need legacy edge cases |

The from-OpenAPI generator handles 80% of modern cloud APIs in 90 seconds. The derivation path is for the long tail of enterprise systems where Talend's accumulated knowledge is genuinely valuable.

## Step 0 — License verification (do this BEFORE reading code)

Apache 2.0 compliance hinges on the upstream component actually being Apache 2.0 **at the commit you derive from.** Verify per-file, not per-repo:

1. **Locate the source.** Talend's components live in repos under the [Talend GitHub org](https://github.com/Talend). The most useful are:
   - `Talend/tcommon-studio-se` — common component framework
   - `Talend/tcomp` — component definitions
   - `Talend/tdq-studio-se` — data-quality components
   - `Talend/components` — newer component model
   - Individual connector repos (`tdi-bigdata-components`, etc.)
2. **Pin to a specific commit hash.** Do NOT derive from `HEAD` or `main` — those may have changed after the 2023 Qlik acquisition. Pre-acquisition (≤ August 2023) is the safest baseline.
3. **Open the file and confirm the header.** Look for:
   ```
   // ============================================================================
   // Copyright (C) 2006-XXXX Talend Inc. - www.talend.com
   //
   // This source code is available under agreement available at
   // %InstallDIR%\AppData\Roaming\Talend\Studio\... (or similar)
   //
   // This source code has been contributed to the Apache 2.0 Talend
   // OSS project under the Apache License, Version 2.0
   // ============================================================================
   ```
   Or check the directory's `LICENSE` / `LICENSE.txt`.
4. **Watch for dual-licensed components.** A handful of components were dual-licensed (Apache + Talend's commercial license). For derivation we use the Apache 2.0 path; ignore the commercial terms. If a file only has Talend commercial terms (no Apache 2.0 grant), **stop** — pick a different starting point or write from vendor docs.
5. **Record the commit hash + verification date** in your scratch notes. You'll need both for the NOTICE entry.

If any of these checks fail, the answer isn't "be more flexible about the license" — it's "this isn't the right starting point, use the from-OpenAPI generator or write from vendor docs instead."

## Step 1 — Read, extract patterns, don't copy

What you're extracting is *logic*, not *code*. Talend's source is Java, F-Pulse is Python, and the manifest formats are different. Even if you could copy verbatim, it wouldn't compile in the target codebase. The value is in the patterns:

### Auth flow

Open the connector's connection-test class (usually `tXxxConnection.java` or `tXxxConnectionPojoComponent.java`). Note:

- **Endpoint** the auth ping hits (e.g. `/services/oauth2/token` for Salesforce)
- **HTTP method, headers, body**
- **Token-refresh logic** if applicable (when does it refresh, what triggers it)
- **Quirks**: do they send `Accept: */*` instead of `application/json`? Do they need `X-Requested-With: XMLHttpRequest`? This stuff matters and is rarely documented.

Port this into F-Pulse's `backend/fpulse/connections/tester.py` as a new `_test_<connector>` function, or into the manifest's `auth` section for the standard cases.

### Pagination

Look at the input/source component (e.g. `tSalesforceInput.java`). Find the loop that fetches pages. Extract:

- **Pagination type** (cursor / page-number / offset / link-header / vendor-custom)
- **Page-size limits** (Talend code often has constants like `MAX_BATCH_SIZE = 200` that encode hard-won knowledge about what the vendor actually accepts)
- **Termination condition** (empty response? null cursor? `has_more: false` flag?)

Port into your manifest's per-stream `pagination` block.

### Error response handling

Look for try/catch blocks around HTTP calls. Vendors that send `200 OK` with an error body in the JSON (instead of using the HTTP status code) are common — Talend's code usually catches these patterns.

Note which response codes are retryable (transient) vs terminal:

- 429 → retry with Retry-After
- 503 → retry with exponential backoff
- 401 → maybe try a token refresh once, then surface
- 403 → terminal, don't retry
- Custom vendor codes embedded in the body → look at how Talend maps them

Port into the connector's tester error-mapping logic.

### Field-type mappings

Look at the schema / metadata classes. Each vendor has weird mappings:

- Oracle `NUMBER(38)` → string (not int — it overflows int64)
- SAP date format `YYYYMMDD` (no dashes) → ISO date
- Salesforce `Reference` → string (the GUID), keep `Name` for join

Port into the manifest's stream schema definitions.

### Performance constants

Talend's code is full of constants like:

```java
private static final int FETCH_SIZE = 2000;
private static final int BATCH_SIZE = 1000;
private static final int CURSOR_BUFFER_SIZE = 10000;
```

These reflect what the vendor actually accepts in production. Adopt the same values in your connector's behaviour configuration. They're not arbitrary — they've been tuned against the real system.

## Step 2 — Build the F-Pulse-side artefacts

Open [build-a-connector.md](build-a-connector.md) and follow the "Full path: Hand-authored manifest" workflow, populating the manifest with the patterns you extracted in Step 1.

Two files end up on the F-Pulse side:

1. **Manifest** — `backend/fpulse/connectors/manifests/<name>.v2.json`
2. **Tester** (if needed) — code in `backend/fpulse/connections/tester.py` for non-standard auth flows that the manifest's declarative auth types can't express.

Add a top-of-file comment to each:

```json
// derived from Talend's tXxx component (Apache 2.0)
// See NOTICE for full attribution.
```

```python
def _test_xxx(connection: Connection) -> ConnectionTestResult:
    """
    Auth flow + endpoint + error map derived from Talend's
    tXxxConnection component (Apache License 2.0).
    See NOTICE for full attribution.
    """
```

## Step 3 — Fixtures

Talend ships test fixtures for many connectors. They're invaluable for F-Pulse's cert-matrix promotion (`v1 functional` → `v2 beta` → `production-certified`). Look in the upstream repo under `*/test/resources/` or `*/src/test/resources/`.

For each fixture:

- Check the license header (usually inherits the project's Apache 2.0)
- Translate format if needed (Talend might use XML; F-Pulse expects JSON for HTTP-style fixtures)
- Drop into `backend/fpulse/connectors/fixtures/<connector>/`

Required fixture set for `production-certified` status:

```
backend/fpulse/connectors/fixtures/<connector>/
├── auth_error.json       (401/403)
├── empty.json            (empty result)
├── happy_path.json       (representative success)
├── rate_limit.json       (429 + Retry-After)
└── schema_drift.json     (extra vendor field)
```

## Step 4 — Update the NOTICE

Open the repo-root `NOTICE` file. In the **Talend Open Studio** section, append:

```
* backend/fpulse/connectors/manifests/<your_connector>.v2.json
  Derived from: Talend/<repo>/<path-to-tXxx-component> @ <commit-hash>
  Ported: auth flow, pagination, error response handling, field-type mappings
  License verified at commit: Apache License 2.0
  Ported by: <your name or handle>  Date: YYYY-MM-DD
```

Be specific about what was ported. "Auth flow + pagination" is informative; "stuff" isn't.

## Step 5 — Tests

Run the manifest validator:

```bash
python -m fpulse.connectors.validate \
  backend/fpulse/connectors/manifests/<your_connector>.v2.json
```

Add an integration test in `backend/tests/test_<your_connector>.py` that uses your fixtures to exercise the connector end-to-end. Talend's test classes can be a useful reference for what edge cases to cover (rate-limit handling, schema drift, etc.) — same "read, don't copy" rule applies.

## Step 6 — Open a contribution PR

File via the [connector-contribution issue template](https://github.com/hybridyn/fpulse/issues/new/choose). The template now has a "derived from" field that captures the upstream attribution; fill it in even if your NOTICE entry already does — it makes reviewers' lives easier.

## What you CANNOT do, even under Apache 2.0

Three boundaries to respect:

1. **Trademarks.** "Talend" and "tMap" / "tFilter" / etc. are trademarks of Talend SA / Qlik. We can derive their behaviour; we can't use the names. Our nodes have F-Pulse-native names.
2. **Commercial-tier code.** Talend Data Fabric, Talend Cloud server, and the commercial enterprise components are **not** Apache 2.0. Don't derive from these even if they're accessible on a personal account.
3. **Bundled vendor SDKs.** TOS sometimes bundles vendor SDKs (Oracle JDBC, SAP JCo, etc.) that have their own licenses. We can call out to those SDKs from our connector code, but we can't redistribute them under F-Pulse's Apache 2.0 license — operators install the vendor SDK separately.

## Common pitfalls

- **Not pinning the commit.** A repo's license can change. Pin to a specific hash and put it in the NOTICE; the historic Apache 2.0 commits remain valid even if the repo's current state is different.
- **Copying file headers.** Talend's copyright headers refer to Talend SA. If you literally copy a file (even though you shouldn't because of the Java→Python translation), don't strip the original header — that's the *worst* outcome, both legally and ethically. Add your changes-attribution alongside.
- **Forgetting the NOTICE update.** This is the only Apache 2.0 obligation that's purely on us. Skipping it is the one mistake that turns a clean derivation into a license violation.
- **"It's open source so it's all the same."** Apache 2.0, GPL, MIT, BSD-3 are all different. We can derive from Apache 2.0 freely. We can't derive from GPL into our Apache 2.0 codebase without re-licensing. Always check.

## Top 10 port candidates

The connectors that close the largest gap vs Talend are listed (with TOS source paths and prioritisation rationale) in [talend-derivation-roadmap.md](talend-derivation-roadmap.md). Start there if you want to know what to port next.

## See also

- [build-a-connector.md](build-a-connector.md) — the from-scratch and from-OpenAPI paths
- [talend-derivation-roadmap.md](talend-derivation-roadmap.md) — prioritised list with TOS source paths
- [NOTICE](../../NOTICE) — root attribution file (you'll append an entry here for each port)
- [vs-talend.md](../vs-talend.md) — strategic context: why this matters for the connector-gap question
