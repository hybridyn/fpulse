<!--
  Use this template when adding or modifying a connector manifest.
  To select it manually, append `?template=connector.md` to your PR URL.
-->

# Connector

<!-- Name (e.g. "Salesforce", "Stripe") and one-line description. -->

# Auth method

<!-- One of: oauth2 / api_key / basic / bearer / none. -->

# Streams added or modified

<!--
  List each stream and its primary key + cursor (if incremental).
  Example:
  - `accounts` — pk: `id`, cursor: `updated_at`
  - `opportunities` — pk: `id`, cursor: `last_modified_date`
-->

# Rate limits documented?

- [ ] `rate_limit.requests_per_minute` set in the manifest
- [ ] Backoff strategy declared (`exponential`, `fixed`, `none`)

# Test-connection evidence

<!--
  How did you verify this manifest works against a real account?
  - Test account / sandbox URL (no credentials)
  - Output of `python -m fpulse.connectors.certify <id>` (paste the depth score line)
  - One stream you successfully read at least 1 page from
-->

# Maintenance owner

<!--
  GitHub handle of the person who will respond to issues for this
  connector. Connectors without a maintainer get archived after 90 days
  of unanswered breakage.
-->

Maintainer: @

# Checklist

- [ ] Manifest passes `python -m fpulse.connectors.certify <connector_id>` locally
- [ ] Auth method declared
- [ ] At least one stream with a primary key
- [ ] Rate limits documented
- [ ] Test-connection note included above
- [ ] Maintenance owner named
- [ ] No competitor product names in manifest text or PR description
- [ ] I understand external PRs are paused for v1.0.0 while the CLA clears legal review (see CONTRIBUTING.md) — this PR may sit until then
