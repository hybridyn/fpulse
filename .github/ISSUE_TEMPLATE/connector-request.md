---
name: 🔌 Connector request
about: Request a new connector for a system F-Pulse doesn't ship yet
title: "[Connector Request] <system name>"
labels: ["connector-request", "needs-triage"]
assignees: []
---

> **Before you file:** could you build this yourself in ~90 seconds using **Insights → Author Connector → From OpenAPI**? If the vendor publishes an OpenAPI spec, the in-product generator usually beats waiting for us to ship it. See [docs/extend/build-a-connector.md](https://github.com/hybridyn/fpulse/blob/main/docs/extend/build-a-connector.md). If yes, please file a **connector-contribution** issue instead so we can ship it first-party.

## What system do you need a connector for?

<!-- e.g. "SAP IDocs over RFC", "Oracle E-Business Concurrent Programs", "InternalTool v3 REST API" -->

## What pipelines would you build with it?

<!-- A sentence or two. Helps us prioritise. -->

## API + auth details we need to act on this

- **Vendor docs URL:**
- **OpenAPI / Swagger URL (if any):**
- **Auth mechanism:** <!-- bearer / basic / OAuth2 / HMAC / mutual TLS / other -->
- **Auth docs URL:**
- **Sample successful response (paste a representative JSON):**

```json

```

- **Sample error response (paste a 401 / 4xx / 5xx if you have one):**

```json

```

- **Pagination style:** <!-- none / page / offset / cursor / link-header / other -->

## Streams / actions you need

<!-- Which endpoints matter for your pipelines? Just the URL paths is enough. -->

- [ ] `GET /...`
- [ ] `POST /...`
- [ ] ...

## Source vs sink

- [ ] Read-only source (we just need to ingest from this system)
- [ ] Write-only sink (we just need to push data into this system)
- [ ] Both

## Anything else

<!-- Quirks: rate limits, weird date formats, sandbox vs prod differences, etc. -->
