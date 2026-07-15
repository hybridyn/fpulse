# F-Pulse Steward — Governance detectors

Activates **three** of the four contract-only governance
`FindingKind`s:

- **`env_crossing`** — a single workflow uses connections tagged with
  multiple environments (dev credential referenced inside a prod-
  destined pipeline, or vice-versa). P1.
- **`unapproved_destination`** — a workflow writes to a sink connection
  not on the workspace's approved-destinations allowlist (when one is
  configured). P2.
- **`pii_leak`** *(added 2026-06-08)* — schema-name-based heuristic.
  When a `POST /api/steward/schema-snapshot` lands with column names
  matching the curated PII catalog (`email`, `ssn`, `phone`, `address`,
  `dob`, `government_id`, `financial`, `health`, `credential_in_column`),
  a `PII_LEAK` finding is emitted. P1 for high-sensitivity classes
  (passwords, national IDs, financial, health); P2 for common PII
  (email/phone/address/dob/government_id). Rides the existing
  schema-snapshot path — no new instrumentation needed.

The fourth governance kind (`credential_sprawl`) still needs a real
credential catalog + scanning infrastructure — deferred.

## Configuration

Per-workspace policy at `<data_dir>/steward/<workspace>/governance.json`:

```json
{
  "env_tags": {
    "conn-prod-snowflake": "prod",
    "conn-dev-snowflake":  "dev",
    "conn-stg-postgres":   "staging"
  },
  "approved_destinations": [
    "conn-prod-snowflake",
    "conn-analytics-bigquery"
  ]
}
```

Both maps are **optional and independent**:

| State | Behaviour |
|---|---|
| `env_tags` empty | `env_crossing` detector disabled |
| `approved_destinations` empty | `unapproved_destination` detector disabled (treated as "no allowlist enforced", **not** "everything is unapproved") |

So a fresh install with no `governance.json` ships in a safely-off
state — admins opt in by populating one or both maps.

## API surface

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/steward/governance` | Read the workspace policy |
| `PUT` | `/api/steward/governance` | Replace the policy (Body: the JSON above) |

## How it works

State-derived, unlike the event-driven schema-drift / quality /
connector-health detectors. Runs at every `_run_scan` against the
same workflow snapshot Archeologist uses:

```
For each workflow:
  Collect all connection_ids referenced in any node's params
  (looks at connection_id / connection / credential_id, in that order)

  env_crossing:
    Map each ref to its env via policy.env_tags
    If the workflow touches ≥2 distinct envs → emit ENV_CROSSING P1

  unapproved_destination:
    For each sink-typed node (type ends in "_sink"):
      If its connection_id is NOT in policy.approved_destinations → emit P2
    Multiple unapproved sinks in ONE workflow → ONE finding listing them all
```

## Suppression

Per (workflow, kind) signature. Dismissing "this one workflow is
intentionally cross-env (e.g. one-time migration)" silences only that
combo — other env-crossing pipelines AND every unapproved-destination
finding on the same workflow keep firing.

## What does NOT happen

- **Steward doesn't tag connections itself.** Admins tag connections
  via the policy file (or via Plus's authoring UI when that ships).
  Auto-tagging based on naming heuristics ("contains 'prod'") was
  considered and rejected — too many false negatives on real-world
  naming.
- **Source nodes don't trigger unapproved_destination.** Reading from
  an unapproved source is a different concern (and arguably fine —
  the source already exists somewhere). The detector targets writes
  only.
- **Steward never auto-migrates a workflow to an approved sink.** The
  proposed action suggests adding the unapproved connection to the
  policy or changing the pipeline — the user makes the call.

## What Plus adds

OSS gets the full detector + policy file + GET/PUT endpoints. Plus
will add:

- A **governance authoring UI** for tagging connections + managing
  approved-destination allowlists without hand-editing JSON
- **Two-gate approval** before a governance policy change activates
- **Cross-workspace policy templates** so a centralised team can
  publish a baseline policy that subordinate workspaces inherit
- The **`pii_leak` + `credential_sprawl` detectors** with a
  professionally-curated regex catalog

Storage format stays identical between OSS and Plus.

## See also

- [`overview.md`](overview.md) — the 7-level Steward contract
- [`custom-rules.md`](custom-rules.md) — layer additional governance
  rules via YAML (e.g. "PII columns must not flow to S3")
- [`quality-checks.md`](quality-checks.md) — companion data-level detector
- [`pitches.md`](pitches.md) — audience-split product pitches
