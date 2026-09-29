# F-Pulse connector certification

F-Pulse's answer to the connector-count arms race is **certify, not count**: the
number that matters is not how many manifests exist, but how many pass a
repeatable test. This document defines the tiers, the evidence each requires, and
the **Tier-1** program that takes the first connectors to a certified state.

> **Where the truth lives:** the running app's `GET /api/connectors/cert-matrix`
> is authoritative for every connector's current tier and last-evidence date.
> This document defines the *bar*; the cert-matrix reports *who has cleared it*.
> As of 1.0.x that is **0 Production / 0 Verified** — we would rather show a
> true zero than soft-label everything "certified."

## Tiers

| Tier | What it means | Evidence required |
|---|---|---|
| **Experimental** | Declared; may work; no guarantees. | Manifest loads + schema validates. |
| **Beta** | Exercised by the author; shape is stable. | Above + a request-contract test against recorded responses. |
| **Verified** | Proven against the vendor, replayable on every PR. | Above + a **replay cassette** captured from a real vendor call, replayed in CI on every PR. |
| **Production** | Verified **and** watched over time, with an owner. | Above + a **30-day green streak** against a live vendor sandbox + a **named owner**. |
| **Hidden** | Out of enterprise-data-eng scope (consumer / SMB-CRM). | Not surfaced in the default picker. |

A connector's tier is derived from evidence, never asserted by hand.

## Two certification modes (why "Verified" splits)

You cannot run live CI against Snowflake, BigQuery, Oracle, SAP, or Salesforce
without paid standing tenants. So certification has two distinct modes, and the
cert-matrix labels which one produced a connector's badge:

- **Verified-Recorded** — a cassette (request/response fixture) is captured once
  from a real vendor call and **replayed on every PR**. No live credentials in
  CI. This is how connectors to credential-heavy enterprise systems reach
  Verified.
- **Production-Live** — a scheduled job (e.g. weekly) runs the connector against
  a **real vendor sandbox** using credentials held only in CI secrets, and the
  30-day green streak is measured from those runs.

**Hard rule: missing live credentials means "not live-tested," never "pass."**
A connector with no cassette and no live run stays Experimental/Beta — it is
never silently promoted.

## What every certified connector is tested for

Certification is not "it returned 200 once." Each connector must demonstrate:

- **Auth** — success, and a clean, typed failure on bad/expired credentials.
- **Pagination** — multiple pages assembled correctly.
- **Incremental / CDC** — cursor advances; a resumed run picks up where it left off.
- **Schema drift** — an added/removed field is handled, not fatal.
- **Rate limits & retries** — backoff on 429/5xx; no data loss on retry.
- **Empty results** — zero rows is a valid, non-error outcome.
- **Deletes & idempotent writes** — re-running a sink does not duplicate.
- **Cancellation** — a cancelled run stops cleanly.

Every certified connector also carries a **named owner** and a **last-evidence
date** in the cert-matrix; stale evidence ages a connector down.

## The Tier-1 program

Tier-1 is a **small** set of connectors — chosen by real OSS user demand, not
breadth — that we take to **Verified** first, as the reference implementation of
the bar above. Selection criteria:

1. Frequently requested by OSS users / evaluators.
2. Have a reachable sandbox or a stable public API (so a cassette can be captured).
3. Cover the common shapes (a REST SaaS source, a database dialect, a warehouse
   sink) so the program exercises the whole test matrix.

Tier-1 connectors ship their cassettes in-repo and are replayed on every PR; a
Tier-1 connector that goes red blocks the merge.

## How to certify a connector

```bash
python -m fpulse.connectors.certify <connector_id>
```

This runs the connector's validation + contract tests locally and reports the
tier it currently clears. For community connectors the same command **gates the
PR** — a manifest cannot merge until it passes at least the Beta bar (see
[`../CONTRIBUTING.md`](../CONTRIBUTING.md#contributing-a-connector)). The
live-smoke allowlist and cassettes live under
`backend/fpulse/connectors/ci/`; adding a connector to the allowlist is what
opts it into the Verified/Production live path.

## See also

- `GET /api/connectors/cert-matrix` — the authoritative live tier report
- [`connectors.md`](connectors.md) — the per-connector catalog
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — contributing a connector manifest
