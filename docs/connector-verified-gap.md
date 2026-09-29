# The gap to the first Verified connector

Companion to [`connector-certification.md`](connector-certification.md). That doc
defines the tiers; this one records the **exact, code-measured reason** the
cert-matrix shows **0 Verified** today, and the honest recipe to change that.

## Finding (measured 2026-09-29)

The eight rich v2 manifests (`github.v2`, `hubspot.v2`, `jira.v2`, `notion.v2`,
`salesforce.v2`, `shopify.v2`, `slack.v2`, `stripe.v2`) **declare depth 3–4** —
i.e. the *capability* is there (streams, incremental, schema) — but every one
**fails validation** with the same error on every stream:

```
streams[N] fixtures: missing required fixture types:
  ['auth_error', 'empty', 'happy_path', 'rate_limit', 'schema_drift']
```

So **the blocker is fixtures, not capability.** The v2 schema refuses to call a
stream valid until it ships fixtures proving it handles the five canonical cases.
That is the "certify, not count" discipline enforced in code: you cannot be a
valid rich connector on paper alone.

Because `validation != "pass"`, `_compute_tier` (`api/cert_matrix.py`) can't reach
`beta` via the v2 path, let alone `verified` — which is why the honest count is
**0 Verified**, and why that number is trustworthy.

## The two fixture artifacts (don't conflate them)

| Artifact | Path | Gates |
|---|---|---|
| **Per-stream fixtures** | inside the v2 manifest, `streams[].fixtures` | manifest `validation == "pass"` (the 5 cases above) |
| **Smoke fixture** | `backend/tests/fixtures/connectors/<id>/smoke.json` | `_has_smoke_fixture` — required by the Verified tier |

Verified needs **both**: `depth_score ≥ 3 AND validation == "pass" AND issues == 0
AND smoke fixture present`. Production adds `depth_score ≥ 5 AND in
`live_smoke.yml``.

## Recipe to earn the first Verified connector (github is the cleanest)

1. **Record — do not hand-write — the five cases per stream.** Fixtures must be
   representative of real responses, or "Verified" is unearned (fabricated
   fixtures are the exact anti-pattern this tier exists to prevent). For a public
   API like GitHub, capture from a real call on a throwaway token:
   - `happy_path` — a real page of results for the stream's smallest call
   - `empty` — the zero-results response (shape, not error)
   - `auth_error` — the 401/403 body
   - `rate_limit` — the 429 body + headers
   - `schema_drift` — the happy_path with an added/renamed field
2. Populate `streams[].fixtures` in the v2 manifest and re-run
   `python -m fpulse.connectors.certify <id>` until `Manifest valid: ✓`,
   `errors: 0`, depth unchanged (≥ 3).
3. Add `backend/tests/fixtures/connectors/<id>/smoke.json` — the smallest live
   call that should succeed (per the `live_smoke.yml` header format).
4. The cert-matrix now computes **verified** for that connector. Add a test that
   asserts it, so the tier can't silently regress.
5. **Toward Production:** add the connector to `connectors/ci/live_smoke.yml` with
   its secret names; the existing `connector-smoke.yml` workflow then runs the
   smallest call against the real vendor on every PR (skipping cleanly when the
   secret is unset). Depth 5 + a green live streak promotes it to Production; a
   14-day red streak demotes it back.

## Why this isn't "just add the fixtures"

The fixtures are **real evidence**, so producing them needs a real (throwaway)
vendor call to record from — a data task, not a code edit. That is the point: the
Verified badge means "we have a recording of this connector actually working,
replayed on every PR," and it should cost exactly that much. This document is the
map; recording the first connector's fixtures is the next concrete step.
