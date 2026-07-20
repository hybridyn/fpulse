# Community Connectors

This directory holds connector manifests contributed by the community. They
are reviewed and merged by maintainers, then ship to all F-Pulse users on
the next release.

> **Status:** Open for contributions starting with v1.0.0. Until the first
> external manifest lands, this directory is intentionally empty — the
> built-in connectors live in [`backend/fpulse/connectors/manifests/`](../backend/fpulse/connectors/manifests/).

## How it works

1. You author a connector manifest (a single JSON file) following the F0.1
   schema described in [`docs/connectors.md`](../docs/connectors.md).
2. You drop it in this directory and open a PR.
3. CI runs `python -m fpulse.connectors.certify` against your manifest. The
   PR cannot merge until certify passes.
4. A maintainer reviews for accuracy, completeness, and the contribution
   checklist below.
5. On merge, your manifest is promoted into the built-in catalog and ships
   in the next F-Pulse release.

## What ships, what doesn't

- **Manifests are data, not code.** This contribution path is for declarative
  JSON only. There is no Python/JS extension surface yet — that's deferred
  to a future plugin SDK.
- Auth, rate limits, pagination, schema, and incremental cursors are
  declared in the manifest. The built-in runtime executes them.
- If your connector needs custom transform logic that the runtime can't
  express declaratively, open a Discussion before opening a PR.

## Contribution checklist

Every PR adding or modifying a manifest must:

- [ ] Pass `python -m fpulse.connectors.certify <connector_id>` locally
- [ ] Declare auth method (`oauth2`, `api_key`, `basic`, `bearer`, `none`)
- [ ] Document rate limits in the manifest's `rate_limit` block
- [ ] Include at least one stream with a primary key
- [ ] Name a maintenance owner (GitHub handle in the PR description)
- [ ] Include a brief test-connection note in the PR (how you verified the
      manifest works against a real account)
- [ ] Use the `connector` PR template:
      `https://github.com/<org>/hybridyn-f-pulse-oss/compare/main...your-branch?template=connector.md`

## What we won't accept (yet)

- Custom Python execution code — manifest-only for now
- Connectors that require running long-lived background services
- Connectors that need credentials we can't safely store via the existing
  vault / env-var pattern
- Duplicates of an already-supported connector unless the existing one is
  marked deprecated

## Quality gate (depth score)

The `certify` CLI computes a depth score 0–5 based on declared capabilities
(streams, incremental, pagination, schema, error handling). Community
manifests start at the score they earn. Promotion to "production-grade" is
a maintainer call after real-world validation.

## Roadmap & demand signal

Want a connector that doesn't exist yet? Upvote or open a request:

- **Roadmap:** [GitHub Discussions → Connector Roadmap](https://github.com/discussions/categories/connector-roadmap)
- **Voting:** Use the 👍 reaction on a roadmap post — counts decide
  prioritization.

This is how we keep the contribution surface focused without inventing a
marketplace backend before we need one.
