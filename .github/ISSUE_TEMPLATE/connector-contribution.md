---
name: 🎁 Connector / node contribution
about: I've built a connector or node and want to contribute it back
title: "[Contribution] <connector or node name>"
labels: ["contribution", "needs-review"]
assignees: []
---

Thank you for contributing! This template captures what we need to review + ship your contribution as first-party in the next release.

## What did you build?

- [ ] Connector manifest (`*.v2.json`)
- [ ] Custom node (Python file under `backend/fpulse/nodes/`)
- [ ] Both

## Name + brief description

<!-- e.g. "InternalRiskAPI connector — read-only source for the org's risk-scoring REST API" -->

## Where to find your work

- [ ] Link to a Gist with the manifest / node code:
- [ ] Link to your fork / branch:
- [ ] PR opened (paste link):

## How it was authored

- [ ] `Insights → Author Connector → From OpenAPI`
- [ ] `Insights → Author Connector → From samples`
- [ ] Hand-authored manifest
- [ ] Custom first-class node type (a Python class subclassing `TransformNode` / `SourceNode` / ...)
- [ ] **Derived from another Apache-2.0 OSS project** (Airbyte CDK, Singer tap, another OSS connector library, etc.) — see below

### If derived from an Apache-2.0 project, fill this in

(Required for compliance — gets copied into the repo-root `NOTICE` file by the reviewer when the PR merges. Verify the upstream licence for the **specific component and commit** you read from, not the project as a whole: licence terms vary between a project's repositories and change over time.)

- **Upstream project + URL:** <!-- e.g. "<project name> — https://github.com/<org>/<repo>" -->
- **Upstream license (link to LICENSE file at the commit you derived from):**
- **Specific file/component you read:** <!-- e.g. "<class or module name> @ <commit-short-hash>" -->
- **Commit hash + date of that commit:** <!-- pin to a specific revision; pre-acquisition / pre-license-change commits are safest when in doubt -->
- **What you ported:** <!-- e.g. "OAuth2 token-refresh flow, page-cursor pagination, retry-after handling for 429s, field-type mappings for Oracle NUMBER(38)" -->
- **What you did NOT use:** <!-- e.g. "did not copy source verbatim; did not reuse upstream trademarks or component names" -->
- **License verified at commit:** <!-- "Confirmed Apache License 2.0 in LICENSE file at <commit>" -->
- **Credit line for the NOTICE entry:** <!-- your name / handle, or "anonymous" -->


## What's covered

- [ ] Auth path tested against the live system
- [ ] At least one stream / action returns real data end-to-end
- [ ] Pagination tested (if applicable)
- [ ] Error responses don't crash the pipeline (401 / 4xx / 5xx)

## Fixtures included (for connector contributions)

For cert-matrix promotion to **v1 functional** / **v2 beta** / **production-certified**, we need:

- [ ] `happy_path.json` — representative successful response
- [ ] `empty.json` — empty array / empty result
- [ ] `auth_error.json` — 401 / 403 response
- [ ] `rate_limit.json` — 429 with Retry-After
- [ ] `schema_drift.json` — extra field added by vendor, manifest survives

Don't worry if you can't ship all five — we can promote in stages. Just tell us what's there.

## Tests

- [ ] `pytest backend/tests/test_<your_thing>.py` passes locally
- [ ] Manifest validates: `python -m fpulse.connectors.validate path/to/manifest.v2.json`

## License + attribution

- [ ] I'm releasing this under the same license as F-Pulse OSS (Apache 2.0)
- **Credit line for the release notes:** <!-- your name / handle, or "anonymous" -->

## Anything else

<!-- Known limitations, things you'd like reviewer eyes on, etc. -->
