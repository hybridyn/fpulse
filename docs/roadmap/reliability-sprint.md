# Reliability Sprint — post-1.0 (4-6 weeks)

**Status:** planned for immediately post-1.0 launch.
**Goal:** prove F-Pulse is **boring-reliable** before adding any AI-Native
marketing claim. Earn the right to v1.1 positioning by shipping
evidence, not features.

## Why this sprint, not the AI headline

External review consensus (4 independent reviewers, 2026-06-02) on
the post-1.0 plan:

> "Nobody buys ETL because it is cool. They buy ETL because it runs
> every day. If scheduling fails, incremental loads fail, retries
> fail, monitoring is weak — then AI + Local First doesn't matter.
> Reliability beats innovation in data engineering."

The AI-Native scaffolding experience is the right v1.1 wedge, but it
sits **above** the reliability floor. Shipping the AI demo before the
floor is proven means churning the first wave of users on bugs they
didn't expect from a "1.0" release.

This sprint addresses that floor. Once it lands, v1.1 work
(AI scaffolding, AI-generated tests, AI-generated docs) begins from
a credible base — not from a tool nobody trusts in production yet.

## The six work items

Ordered by impact × measurability. Each item has a clear acceptance
criterion that turns into a public artifact (CI run, soak-test
report, fixture file). No "we made it more reliable" hand-waving.

### 1. Live-smoke CI for the first 10 connectors → Verified tier

**Why:** the cert matrix today reports 0 Verified. The tier system
ships in 1.0 but the Verified row is empty. The fastest way to make
the "honest catalog" story credible is to fill it with real entries.

**Scope:** the 10 connectors from the post-launch roadmap (already
specced in `docs/roadmap/oss-1-1.md`):

| # | id | How we verify |
|---|---|---|
| 1 | `postgres` | `postgres:16` container in CI; seed schema; pagination + incremental cursor on timestamp |
| 2 | `mysql` | `mysql:8` container; same shape |
| 3 | `sqlite` | Fixture `.db` file checked into `backend/tests/fixtures/connectors/sqlite/` |
| 4 | `mongodb` | `mongo:7` container; find + change-stream incremental |
| 5 | `s3` / `minio` | `minio/minio` container; write + list + read; same SDK works against real S3 in nightly |
| 6 | `github` | Public unauth endpoints first, then PAT for rate-limit headroom |
| 7 | `stripe` | Test-mode keys (free); `customers` / `charges` / `invoices` |
| 8 | `weaviate` | Local container; schema + upsert + nearText |
| 9 | `qdrant` | Local container; collection + upsert + search |
| 10 | `clickhouse` | `clickhouse/clickhouse-server` container; pagination + incremental on `event_time` |

**Acceptance:** each connector has
- A fixture at `backend/tests/fixtures/connectors/<id>/smoke.json`
- An entry in `backend/fpulse/connectors/ci/live_smoke.yml`
- A green run in `.github/workflows/connector-smoke.yml` (already shipped — just needs entries)
- A cert-matrix output showing `tier: verified`

**Public artifact:** `GET /api/connectors/cert-matrix` shows
`"by_tier": {"verified": 10, "beta": 9, ...}` instead of `{"verified": 0, ...}`.

**Effort:** ~2 weeks (1 engineer). Most connectors are
infrastructure-only (containers) so no vendor account paperwork.

### 2. Scheduler reliability stress test

**Why:** the scheduler ticks every 30s and is supposed to fire pipelines
at their declared times. There's no current evidence that it survives
30 consecutive days without a silent missed run, scheduler-process
restart drift, DST transitions, or clock-skew on the host.

**Scope:** a CI-runnable stress test that:
- Boots 5 synthetic pipelines with varied schedules (every 5min, every
  hour at :17, daily at 02:30, weekly Mon 09:00, cron `*/15 9-17 * * 1-5`)
- Simulates 30 days of wall-clock time via the existing test-time
  abstraction
- Restarts the scheduler process at random points
- Asserts every expected fire happened within 90 seconds of its
  scheduled time
- Asserts no spurious extra fires
- Asserts the watchdog flags missed runs within the 5-minute grace
  window

**Acceptance:** `backend/tests/test_scheduler_30day_soak.py` runs
in CI, < 60s wall-clock, asserts zero missed/extra fires across the
simulated month.

**Public artifact:** dashboard sticker — "Scheduler tested under 30-day
simulated soak; 0 missed fires."

**Effort:** ~1 week. Test-time abstraction likely exists already; the
test file is the work.

### 3. Incremental cursor end-to-end correctness

**Why:** "Incremental sync" is the most-promised, least-tested
capability across the ETL category. F-Pulse has the schema (`sync_state`
table, `last_cursor` field) but the contract isn't proven against the
real failure modes operators hit.

**Scope:** end-to-end tests for `db_source` (Postgres + MySQL + SQLite)
+ at least 3 SaaS manifests (`github`, `stripe`, `mongodb`) covering:
- First run: cursor starts empty, processes everything, saves `MAX(cursor_column)`
- Second run: only reads rows where `cursor_column > last_cursor`
- Cursor survives backend restart between runs
- Cursor survives a deployed schema change that doesn't touch the cursor column
- Manual `watermark_value` override wins over auto-saved cursor (for backfill)
- Reset state (`DELETE /api/sync-state/{step_id}`) makes the next run treat as first run

**Acceptance:** `backend/tests/test_incremental_e2e.py` (one file
per source family) — all assertions green.

**Public artifact:** docs page `docs/user-guides/incremental-sync.md`
with worked examples + "what we tested" matrix.

**Effort:** ~1.5 weeks. The schema work is done; the test scaffolding
is new but mechanical.

### 4. Retry + backoff against the 8 common failure modes

**Why:** the Retry Handler node exists. The failure modes it claims
to handle haven't been explicitly tested. Real-world ETL fails for
predictable reasons; we should prove we handle each one.

**Scope:** simulated-failure tests for:
1. Auth token expired mid-run (refresh succeeds on retry)
2. Auth token expired + refresh also fails (gives up cleanly with
   actionable error, doesn't loop)
3. Vendor rate limit (429) — backs off + retries respecting
   `Retry-After` header when present, exponential when not
4. Network blip (connection reset / TLS handshake fail) — retries
5. Schema drift mid-run (new column appears) — flags warning,
   continues with declared columns
6. Schema drift breaking (declared column dropped) — fails clean,
   not silent data loss
7. Sink full / disk full — fails clean, doesn't corrupt partial state
8. Source returns empty / 404 — emits zero rows, doesn't crash

**Acceptance:** `backend/tests/test_retry_failure_modes.py` covering
all 8 modes against mocked HTTP fixtures.

**Public artifact:** decision table in
`docs/user-guides/failure-modes.md` — "this is what happens when X."

**Effort:** ~1 week.

### 5. Alerting plumbing end-to-end

**Why:** Email / Slack / Teams / webhook destinations exist in code.
The promise is "we'll tell you when something breaks." The "did the
alert actually arrive at the destination" loop isn't in CI today.

**Scope:** per-channel integration test that:
- Email → starts a local MailHog container, configures SMTP, asserts
  the message arrived with the right subject + body
- Slack webhook → mocks the Slack incoming-webhook URL, asserts the
  POST body matches the documented schema
- Teams webhook → same shape
- Generic webhook → starts a local HTTP recorder, asserts the
  payload matches the documented schema
- Browser desktop notification → JS-side unit test (can't end-to-end
  test in CI but can pin the dispatch logic)

**Acceptance:** `backend/tests/test_alerting_e2e.py` green across all
channels. MailHog container in `docker-compose.test.yml`.

**Public artifact:** sample-alerts gallery in
`docs/user-guides/alerts.md` showing what each channel renders.

**Effort:** ~3 days.

### 6. Seven-day soak test on a $5 VPS — published

**Why:** the loudest credibility multiplier we can ship. "F-Pulse ran
on a $5 DigitalOcean droplet for 7 days, here's the resource graph"
is the kind of evidence that beats any marketing claim.

**Scope:**
- Provision a $5/month DigitalOcean droplet (1 vCPU, 1 GB RAM, 25 GB SSD)
- Install F-Pulse via `pip install fpulse`
- Run a 3-pipeline production-shaped workload:
  - Pipeline A: hourly Postgres extract → DuckDB → daily aggregate
  - Pipeline B: 6-hourly REST source (GitHub events) → dedup → JSON sink
  - Pipeline C: daily S3 file ingest → schema-validate → managed table
- Capture: CPU %, RAM %, disk usage, scheduler hit rate, error count,
  per-pipeline duration, alert deliveries
- Run for 7 calendar days
- Publish the metrics + a blog post

**Acceptance:** the soak completes with:
- 100% scheduled-fire success
- 0 unrecovered errors
- Peak RAM < 800 MB
- Disk growth bounded (rollover working)
- Blog post published with raw Prometheus screenshots

**Public artifact:** `docs/proof/7-day-vps-soak.md` + the blog post
+ the raw metrics dump in a public gist.

**Effort:** ~1 week elapsed (~2 dev-days active work; the soak runs in
the background).

## Total timeline

| Week | Work |
|---|---|
| 1 | Items 1+2 in parallel (live-smoke fixtures for 5 of 10 + scheduler stress test) |
| 2 | Items 1 (other 5) + 3 (incremental cursor tests) |
| 3 | Items 4 (retry/backoff) + 5 (alerting plumbing) |
| 4 | Item 6 starts (provision VPS, baseline 24h) |
| 5 | Item 6 continues (soak running, capture metrics) |
| 6 | Item 6 wrap (publish soak results + blog post); start v1.1 AI work |

## What this sprint deliberately does NOT include

Per the post-1.0 plan, the AI-Native headline experience is the v1.1
work, NOT this sprint. To prevent scope creep:

- ❌ No new connectors (catalog stays at 33 visible)
- ❌ No new node types
- ❌ No AI features (no "describe → pipeline," no AI test gen, no doc gen)
- ❌ No Plus license implementation
- ❌ No desktop packaging work
- ❌ No new dependencies unless required for an item above
- ❌ No marketing language changes — stick with the 1.0 sober framing
  until the sprint's evidence justifies a tier-up

## Definition of "sprint done"

When you can credibly say all six of these:

1. "10 connectors in the cert matrix report `tier: verified` based on live CI"
2. "Scheduler is tested across 30 days of simulated time with 0 missed fires"
3. "Incremental cursor is end-to-end tested for 6 source families"
4. "Retry handler is proven against 8 named failure modes"
5. "Alerting works on 4 channels (email / Slack / Teams / webhook), each tested in CI"
6. "F-Pulse ran for 7 days on a $5 VPS with these public metrics: [link]"

Each of those is a sentence in the v1.0.1 announcement and a section
of an evidence page on the docs site. The v1.1 AI-Native pitch lands
on top of this, not in place of it.

## Risk / what could derail this

- **Connector fixtures take longer than 2 weeks.** Likely if vendor
  test accounts have unexpected approval flows. Mitigation: start
  with the 5 container-only ones (postgres, mysql, mongodb, minio,
  clickhouse) so the first half is unblocked.
- **Scheduler stress test reveals a real bug.** That's a win for the
  sprint goal but extends timeline by ~3 days while it gets fixed.
  Mitigation: budget the bug-fix time inside week 1.
- **Soak test reveals a memory leak or scheduler drift.** Same as
  above — this is what the sprint exists to surface. Fixing is part
  of the sprint, not a scope creep.

## After the sprint — earning v1.1 AI-Native

Once the floor is proven, the 6-week AI-Native sprint (per
`docs/roadmap/oss-1-1.md`) begins. The order is intentional: v1.0
ships sober, the reliability sprint earns operator trust, then
v1.1 makes the AI claim on top of a foundation that backs it up.

Skipping this sprint and jumping straight to AI-Native is the failure
mode reviewers explicitly warned about. Six weeks of unglamorous
reliability work is the unsexy moat.
