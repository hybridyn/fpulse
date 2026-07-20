# F-Pulse Steward

**Steward is a second pair of eyes on your whole workspace.** It spots
when you've accidentally built the same thing twice, catches a source
schema changing under you before it breaks downstream, watches your
connection health for sustained failures, remembers the fixes that
worked, and never touches your pipelines on its own.

That's the entire product in one sentence. Everything below explains
the mechanics — but if all you wanted was "what does it do for me?",
this is it.

## What changes for a real user

| Without Steward | With Steward (1.1 today) |
|---|---|
| Six months in, someone discovers Pipeline A and Pipeline B both read `orders.csv`. Nobody knew. You've been paying for duplicate data movement for half a year. | The first time both pipelines exist, the eye-icon flashes a finding: *"2 pipelines read the same source — Aggregation Report and Sales Pipeline."* You decide whether to consolidate or dismiss-as-intentional. Either way, you find out **immediately**, not in six months. |
| A column gets dropped from a source. Downstream transforms break at 2 AM. Operators spend 90 minutes tracing it. | The moment a schema-snapshot lands that differs from the previous one, a finding fires with the exact column + old/new type. P1 for drops + type changes (the kind that break casts), P3 for additions. |
| A connection has been failing for two days. Nobody noticed because the test page wasn't checked. | Sustained-failure streaks (≥2 consecutive failures over ≥5 minutes) emit a finding classified as auth / unreachable / rate-limit / timeout. Severity scales with streak length. |
| The fix from last month's incident lives in someone's Slack DM. The next operator finds nothing when they search. | Resolve a finding with `"what fixed this"` — it becomes a `PROPOSED` lesson the team can approve. The lesson search API returns it on the next similar failure. |
| You get 14 emails about the same issue because every scan retriggers the alert. | One notification per `(user, finding, severity, rebound)` tuple. Same issue, same operator, same severity → you get it **once**. |
| You dismissed an alert yesterday; it returns tomorrow as if you never triaged it. | Dismiss-with-reason resets the per-signature counter. The next legitimate recurrence starts fresh, not from where the previous escalation left off. |

## What Steward will **not** do — by architectural rule

- **Never edit a workflow.** Steward observes, recommends, escalates. The fix is always yours to apply.
- **Never run pipelines on its own.** Read-only is rule #1 of 7.
- **Never auto-promote operator notes into lessons.** Only explicit Resolve-with-fix-note flows hit the lesson store, and even then they stay `PROPOSED` until a human approves.
- **Never bury secrets in audit logs.** Dismiss reasons and fix notes pass through 5 regex sweeps (AWS keys, bearer tokens, password=, URI creds, private IPs) before persistence.
- **Never spam.** Time-clamped escalation, notification de-dup, dismiss-resets-counter, rebound detection — four independent fatigue guards.

## The technical contract underneath

The Steward emits typed `StewardFinding` records that flow through one
surface — eye-icon dropdown, notification bell, suppression store,
Memory Layer. Two **storage surfaces, deliberately distinct**: the
operational journal (`memory.jsonl`, append-only) and the Memory Layer
(`lessons/*.yaml + .json`, durable + human-gated).

A 7-level observability taxonomy is contract-shipped (enum, storage,
notification bridge, UI rendering all handle every level today). Active
detectors as of 1.1.x:

| Level | Active detectors |
|---|---|
| **architecture** | Archeologist (duplicate source + duplicate pipeline) |
| **connector** | Connector-health (auth-failure / unreachable / rate-limit / credential-near-expiry) |
| **data** | Schema-drift (added / dropped / type-changed) + quality engine (null_spike / duplicate_key_spike / volume_anomaly / freshness_miss / partition_missing / quality_check_failed) |
| **governance** | env_crossing + unapproved_destination + pii_leak (per-workspace policy) |
| **cost** | warehouse_waste (consecutive zero-output reads on a source) |
| **node** | empty_output (consecutive zero-output runs of a specific node within a workflow) |
| **+ user-defined** | YAML rules engine — admins emit findings at any of the 7 levels |
| pipeline | Contract-only; sla_breach / partial_output / retry_storm land with Sentinel in 1.2 |

**Advisor, not actor.** The Steward:
- recommends, annotates, and escalates
- **never** mutates pipeline state
- **never** runs fixes
- **never** changes execution policy on its own

This page explains what the Steward is, what it does in this release,
and the hard architectural rules it follows.

## The seven observability levels

Every finding declares its level so the UI groups by layer and you can
filter for "just architecture stuff" or "just data quality":

| Level            | What it watches                                                            | Example finding kinds (* = active detector today) |
|------------------|----------------------------------------------------------------------------|------------------------------------------------------|
| **Pipeline**     | End-to-end run health, SLA, partial output                                 | `sla_breach`, `partial_output`, `retry_storm`, `failure_rca` |
| **Node**         | Step-level transforms, join/filter/cast behaviour                          | `empty_output` *, `join_explosion`, `join_collapse`, `filter_dropped_all`, `cast_failure` |
| **Connector**    | Source/sink **transport**: auth, rate limit, reachability                  | `connector_auth_failure` *, `connector_rate_limit` *, `connector_unreachable` *, `credential_near_expiry` * |
| **Data**         | Schema, freshness, volume, quality                                         | `schema_drift` *, `null_spike` *, `duplicate_key_spike` *, `volume_anomaly` *, `freshness_miss` *, `partition_missing` * |
| **Architecture** | Structural / design-level — duplicate extraction, redundant transfer, lineage cascade | `duplicate_source` *, `duplicate_pipeline` *, `redundant_transfer`, `lineage_cascade` |
| **Governance**   | PII movement, credential sprawl, environment crossing                      | `pii_leak` *, `credential_sprawl`, `env_crossing` *, `unapproved_destination` * |
| **Cost**         | Cost drift, runaway compute, warehouse waste                               | `cost_drift`, `warehouse_waste` *, `cost_recommendation` |

**The contract for all finding kinds is shipped.** The starred kinds
above have an **active detector today** — spanning the architecture,
connector, data, node, cost, and governance levels (plus the
user-defined rules engine). The remaining kinds — pipeline-level
`sla_breach` / `partial_output` / `retry_storm`, structural
`join_explosion` / `join_collapse` / `cast_failure`,
`redundant_transfer`, `lineage_cascade`, `credential_sprawl`, and
`cost_drift` / `cost_recommendation` — are **contract-ready**: they
exist in the enum so the UI, notification bridge, memory layer, and
suppression rules don't need re-shaping when Sentinel (1.2),
Foreseer (1.3), Cost Steward (1.3), Architecture Steward (1.3), and
Governor (1.4) ship their detectors.

> For the in-depth design rationale (why the architecture is shaped
> the way it is, alternatives considered and rejected, extension model,
> performance characteristics), see [`architecture.md`](architecture.md).
>
> For the **F-Pulse Memory Layer** (the durable, human-approved lesson
> surface that turns one-off fixes into team knowledge), see
> [`memory-layer.md`](memory-layer.md).
>
> For **user-defined rules** (admins write additional detectors as
> YAML, no code, no plugins, no fork), see
> [`custom-rules.md`](custom-rules.md). Ships in OSS; in-app authoring
> UI ships in Plus.
>
> For **connector-health detection** (sustained-failure streaks
> activate `connector_auth_failure` / `connector_unreachable` /
> `connector_rate_limit` / `credential_near_expiry` — the first
> connector-level Steward signal), see
> [`connector-health.md`](connector-health.md). Ships in OSS.
>
> For **schema-drift detection** (event-driven; activates
> `schema_drift` at the data level the moment a new schema differs
> from the previous baseline for the same source), see
> [`schema-drift.md`](schema-drift.md). Ships in OSS.
>
> For **native data-quality checks** (event-driven recording surface
> for assertion results from F-Pulse executor / dbt / GX / Soda /
> custom probes; activates `null_spike`, `duplicate_key_spike`,
> `volume_anomaly`, `freshness_miss`, `partition_missing`,
> `quality_check_failed`), see [`quality-checks.md`](quality-checks.md).
> Ships in OSS.
>
> For **governance detectors** (per-workspace policy activates
> `env_crossing` when a workflow mixes dev/prod connections and
> `unapproved_destination` when a sink writes outside an allowlist),
> see [`governance.md`](governance.md). PII-leak and credential-
> sprawl detectors deferred. Ships in OSS.
>
> For **cost / movement tracking + EMPTY_OUTPUT** (event-driven
> recording of per-run rows/bytes/duration; activates `warehouse_waste`
> on consecutive zero-output reads AND `empty_output` on consecutive
> zero-output runs of a specific node), see
> [`cost-tracking.md`](cost-tracking.md). `cost_drift` and
> `cost_recommendation` deferred to 1.3 / 2.0. Ships in OSS.
>
> For **automatic volume-anomaly detection** (the `foreseer` sub-agent;
> activates `volume_anomaly` at the data level), see
> [`volume-anomaly.md`](volume-anomaly.md). This is distinct from the
> `row_count_min/max` *threshold* checks in `quality-checks.md`: foreseer
> needs **no configured numbers** — it learns each source's normal
> volume from history (robust median + MAD baseline) and flags drops /
> spikes against the source's own baseline (Hard Rule 6). Reuses the
> cost-event `rows_read` history; no extra setup. Ships in OSS.
>
> For the positioning / pitch summary suitable for sharing with
> non-engineering audiences, see [`positioning.md`](positioning.md).

---

## Why this is in F-Pulse OSS (not just Plus)

Most open-source orchestrators stop at execution: build a pipeline, run
it on a schedule, see the logs. F-Pulse adds a layer above that — one
that notices when two of your pipelines are reading the same source
into different warehouses, or when two engineers built effectively the
same flow without realising it, or (in future sub-agent releases) when
a pipeline that's failed three times in a row this week looks identical
to one that failed the same way last month.

We ship this in OSS because:

1. **It's what makes F-Pulse feel like more than another ETL tool.**
   The first time a user adds their fifth pipeline and sees "3 pipelines
   read the same source — consider a managed table" appear in the
   header, they understand the product is *thinking with them*, not
   just running their jobs.
2. **Single-user OSS still benefits.** Even one engineer on one laptop
   accidentally builds duplicates. The Steward catches them.
3. **Plus monetises team-scale, not capability-scale.** F-Pulse+ adds
   cross-workspace correlation, shared Steward memory across many
   teams, RBAC-aware approval chains on proposed actions — but the
   *detection capability itself* lives in OSS.

---

## What ships in 1.1 — Archeologist sub-agent

The Archeologist is the first concrete Steward capability. It runs on
demand (via the `Re-scan` button) and is fast enough (< 50 ms for
typical workspaces) to run on every `/api/steward/findings` request.

### What it detects

- **Duplicate source** — two or more pipelines read the same logical
  source (same connection_id + same object name) into different
  destinations. Often an opportunity to consolidate via a shared
  managed table that downstream pipelines read from.
- **Duplicate pipeline** — two or more pipelines have effectively
  identical shape (same source signature set + same sink signature
  set). Often an accident — two people built equivalent flows.

### What it deliberately does NOT flag

- Linear chains of `raw → staging → cleansed → modeled` reading from
  the same source. That's a single logical dataset traversing layers,
  not a duplicate.
- Fan-out — same source, different sinks IS flagged as
  `duplicate_source` (to surface the consolidation opportunity) but is
  NOT flagged as `duplicate_pipeline` (because the shapes differ).
- Workflows whose signatures the user has explicitly marked
  `Dismiss (intentional)`. The Steward remembers via a per-workspace
  suppression file and stops flagging them.

### Signature stability

A "source signature" is a SHA-256[:16] hash of the fields that
determine *what is being read*: `connection_id`, `connector_type`,
`table` / `file_path` / `query` / `url`. Pagination params, retry
config, sample size — all excluded, because they don't change what
dataset is being read. Field ordering inside the params dict is
normalised so two semantically-equal sources hash identically.

Finding IDs are deterministic — re-running the detector on the same
input produces the same IDs. The persistence layer uses this for
upsert semantics (a finding seen 17 times across scans is one row with
`occurrences = 17`, not 17 rows).

---

## How it learns from mistakes

The detector itself (Archeologist) is stateless — every scan re-derives
findings from the current workflow set. What makes the Steward
*learn* is a separate layer: a per-workspace append-only JSONL journal
at `<data_dir>/steward/<workspace>/memory.jsonl`.

Three concrete learning behaviours are wired in 1.1:

### Persistent occurrence counter

Every emit is recorded with the scan ID it came from. The
`StewardMemory.persistent_occurrences()` aggregate counts the
*distinct scans* a signature has appeared in — not the per-scan
workflow count. Re-running the same scan twice in 30 seconds doesn't
inflate the counter, because the two re-runs share scan boundaries
the user wouldn't perceive as separate events.

You can see this number in the **Memory tab** of the Steward dropdown.
Signatures that have crossed the escalation threshold (configurable;
default 5) show in red.

### Severity escalation

When persistent occurrences cross `escalate_after_n_occurrences`,
the next scan promotes the finding one severity step — P3 → P2, or
P2 → P1. A line is appended to the finding body explaining why
("_Severity escalated from P2 to P1 because this finding has been
surfaced in 7 separate scans without resolution._"). What you keep
ignoring gets louder, not quieter.

P1 findings (after escalation) light the header badge red instead of
violet, so they're visually distinct from routine review-worthy items.

### Rebound detection

If you mark a finding `Mark resolved` and the same signature later
re-emerges (someone re-built the duplicate, a teammate reverted your
consolidation), the next emit is annotated:

```
(rebounded) 2 pipelines have identical source → sink shape

…

_This finding had been **resolved previously** (last on
2026-06-05T12:17:08+00:00) and has re-appeared. Likely a regression —
review whether the original fix was reverted or a teammate
re-introduced the pattern._
```

This is the bit that turns a one-shot detector into something that
remembers your fixes.

### Dismiss-with-reason → future Curator input

When you click `Dismiss (intentional)`, the UI prompts for an optional
reason ("DR replication — intentional", "data-vault gold layer",
"legacy job kept for audit only", etc.). The reason is logged in the
memory journal. The 1.4 Curator sub-agent will mine these reasons to
distill patterns (e.g. "the user dismisses anything matching
`audit_log` — generalise the suppression rule") into the
`EPULSE_RUNBOOK.md` file.

### Where to find the proof

Three places, in increasing depth:

1. **Memory tab** in the Steward dropdown — live, in-app view of the
   persistent-occurrence counts + the recent event stream.
2. `<data_dir>/steward/<workspace>/memory.jsonl` — the raw on-disk
   journal. Grep-friendly, hand-editable for ops debugging.
3. `backend/scripts/validate_steward.py` — runs the full learning
   pipeline against a 9-workflow fixture and prints the proof. A
   captured run is preserved at `docs/steward/validation-output.txt`
   and a sample journal at `docs/steward/sample-memory.jsonl`.

---

## Finding lifecycle (the incident states)

Every finding moves through a small state machine. Knowing these
states helps you read the UI (and the notification metadata):

| State          | When                                                                                                                                                                          |
|----------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `open`         | The default — a new finding that hasn't been touched yet.                                                                                                                     |
| `acknowledged` | You saw it and accepted it as legitimate, but haven't acted yet (a "yes, I'll get to this" signal that suppresses bell re-pings without committing to dismiss or resolve).    |
| `dismissed`    | You marked it intentional (optional reason recorded in memory).                                                                                                               |
| `resolved`     | You took action that closes it (deleted a duplicate, consolidated).                                                                                                           |
| `rebounded`    | A previously-resolved finding has re-emerged in a later scan. Promoted (2026-06-05) from a body annotation to a first-class state — distinct from `open` because a regression is genuinely different information from a new issue. |
| `suppressed`   | Silenced for a fixed window (e.g. during a planned maintenance window). Auto-reverts to `open` when the window expires.                                                       |
| `expired`      | Auto-aged beyond `auto_stale_days` + a grace window with no signal. Hard-archived; out of the default view entirely.                                                          |
| `stale`        | Untouched for `auto_stale_days` (default 30); auto-aged. Hidden from default view but still inspectable.                                                                       |

The `rebounded` state has its own evidence field
(`previously_resolved_at`) so the UI can render a regression chip
showing the prior resolve timestamp, and the notification bridge
treats it as a NEW event (de-dup key changes) so the bell pings
again — but only once.

---

## Noise control (why you won't be spammed)

Three guards work together to prevent the alert fatigue that kills
most monitoring tools:

1. **De-dup across scans.** Re-emits of the same finding at the same
   severity produce ZERO new notifications. Only NEW or
   NEWLY-ESCALATED ones cross.
2. **Time-clamped escalation.** A finding only escalates to a higher
   severity after the threshold count AND a minimum age (default 24
   hours). A 60-second cron pipeline does not page out to P1 in 5
   minutes.
3. **Min-severity filter.** The notification bell has its own
   threshold separate from the dropdown — info-only P3 findings stay
   in the eye-icon badge without pinging the bell.

---

## Notification bell integration

The Steward eye-icon badge is the dedicated surface for browsing
findings. But that surface is only useful if the user looks at it.
For findings that are important enough to interrupt the user's
attention, the Steward also writes to the existing in-app
notification bell — the same bell that surfaces pipeline failures,
schedule misses, and approval requests.

### When the bell pings

Two notification types:

| Type                          | Triggered by                                                 | Icon              |
|-------------------------------|--------------------------------------------------------------|-------------------|
| `steward_finding`             | A new finding appears at or above `notify_min_severity`      | Violet eye        |
| `steward_finding_escalated`   | A finding's severity bumps to P1 via the learning layer      | Red warning triangle |

A finding being newly `(rebounded)` also counts as a new event and
pings the bell once — the rebound-state is part of the de-dup key.

### The de-dup invariant

This is the part most likely to bite, so it's the part with the
strongest test coverage. The rule:

> For each (user, finding_id) pair, at most ONE notification per
> (severity, rebound-state) tuple.

In practice: when the same P2 duplicate-source finding appears in
five consecutive 60-second scan polls, the bell receives **one**
notification, not five. When that finding then escalates to P1 via
the learning layer, the bell receives **one more** — because severity
changed, the de-dup key changed, and the escalation is genuinely new
information worth interrupting for.

### Click-through

Clicking a Steward row in the bell:

1. Navigates to `#dashboard`.
2. Dispatches `fpulse:steward-open` — the StewardBadge listens for
   this event and auto-opens its dropdown on the Findings tab.
3. The user lands in context with the relevant finding visible, not
   on a generic landing page.

### Acknowledgement clears the badge

When the user marks a finding `Mark resolved` or `Dismiss (intentional)`,
every related Steward notification in their bell is automatically
marked read. The unread-count badge stops nagging for issues that
have already been triaged.

### Channel fan-out

Steward notifications flow through the same notification pipeline as
pipeline run notifications — so if the user has email or Slack
channels configured, Steward findings reach those channels too. No
additional configuration needed.

### Opting out

Two settings in the Steward Settings tab control this:

- **Notify on new findings** (master toggle, default ON)
- **Minimum severity to notify** (default P2 — info-only P3 findings
  stay in the eye-icon badge without spamming the bell)

When the master toggle is off, the min-severity dropdown is disabled
so the UI never lies about what's happening.

---

## Settings

Per-workspace tunables, persisted to `<data_dir>/steward/<workspace>/settings.json`.
Accessible from the **Settings tab** of the Steward dropdown, or via
`PUT /api/steward/settings`. Hand-editable too — the file is plain JSON.

| Key                              | Default | Range  | Effect                                                                       |
|----------------------------------|---------|--------|------------------------------------------------------------------------------|
| `enabled`                        | `true`  | bool   | Master kill-switch. Hides the badge and short-circuits scans when false.     |
| `min_severity`                   | `"p3"`  | p1/p2/p3 | Hide findings below this severity. P3 shows everything.                    |
| `scan_on_save`                   | `true`  | bool   | Re-scan on every workflow save (dispatched via `fpulse:steward-refresh`).   |
| `auto_stale_days`                | `30`    | 1–365  | Untouched-finding age before it auto-transitions to `stale` status.         |
| `escalate_after_n_occurrences`   | `5`     | 2–50   | Persistent-scan count at which severity bumps one step.                     |
| `notify_on_finding`              | `true`  | bool   | Write Steward findings to the in-app notification bell (with de-dup).      |
| `notify_min_severity`            | `"p2"`  | p1/p2/p3 | Bell-only severity threshold, independent of the dropdown's `min_severity`. |

Defaults are chosen to be **useful but not noisy**. A solo user on a
laptop with 10 pipelines should never need to tune these. A larger
workspace evaluating the feature may want to dial `min_severity` up to
P2 while they triage the backlog.

---

## Hard architectural rules

These are invariants. They're pinned in
`backend/fpulse/steward/__init__.py` as the package docstring and
they do not relax.

### 1. Read-only

The Steward NEVER mutates workflows, connections, schedules,
credentials, or any other persisted state on its own. It produces
findings; the user (or another agent with explicit user approval)
applies them. The `Dismiss` and `Resolve` actions in the UI write to
the Steward's *own* suppression file, never to user-managed objects.

### 2. Out-of-band

The Steward must not block pipeline execution. It runs parallel to the
executor, consuming workflow store snapshots and the audit log. A
Steward outage (network failure, disk full, missing model) must not
take down the executor.

### 3. Deterministic core, LLM-narration shell

Each sub-agent's *detection logic* is plain code — graph traversal,
statistics, signature hashing, pattern matching. The LLM is only used
to **phrase the finding** in natural language. The Steward must remain
useful with the LLM disabled — you'd see slightly drier titles, but
every finding would still surface with the same evidence.

This is a deliberate trade vs an "AI-native" orchestrator where the LLM
gates correctness. We want correctness to be auditable; LLMs hallucinate
and we don't want a hallucinated duplicate-detection finding to confuse
a real ops investigation.

### 4. Explicit provenance

Every finding carries the IDs of the inputs it inspected — workflow
IDs, node IDs, source signatures — so a reviewer can trace it back to
source. No opaque "the AI thinks so." This is enforced by
`StewardFinding.evidence` being a required structured field on every
emit, not a free-text blob.

### 5. OSS-first

The Steward ships in F-Pulse OSS. F-Pulse+ Plus adds team-scale
features around it (shared memory across workspaces, RBAC-aware
approval chains, multi-tenant correlation) — but never gates the core
detection capability.

---

## Roadmap

Specialist modules land progressively. Each shipped module reuses
the same surface (eye-icon dropdown + bell + memory journal + lesson
store) — none of them invent parallel infrastructure.

| Release | Module                | What it does                                                                                |
|---------|-----------------------|---------------------------------------------------------------------------------------------|
| 1.1     | Archeologist          | Duplicate-source + duplicate-pipeline detection (shipped)                                   |
| 1.1     | Learning Layer        | Persistent occurrence counter + time-clamped severity escalation + rebound state (shipped)  |
| 1.1     | Memory Layer          | Durable lesson store with propose/approve/revalidate workflow + lesson search API (shipped) |
| 1.2     | Incident Analyst      | Auto-search the Memory Layer when a pipeline fails; surface matching approved fixes         |
| 1.2     | Sentinel              | Live pipeline-health monitoring + SLA pattern tracking                                      |
| 1.3     | Foreseer              | Volume + structural + schema-drift anomaly detection                                        |
| 1.3     | Cost Steward          | Cost-drift detection (today vs 30-day baseline) + warehouse-warm waste                      |
| 1.3     | Architecture Steward  | Cross-warehouse storage duplication ("same source extracted twice into separate lakehouses")|
| 1.4     | Knowledge Steward     | Learn SUCCESSFUL patterns and recommend them ("incremental watermark + partition pushdown") |
| 1.4     | Curator               | Distill `EPULSE_RUNBOOK.md` from approved lessons + recommendation patterns                 |
| 1.4     | Governor              | PII detection + credential sprawl + cross-project service-account use                       |
| 2.0     | Optimizer             | Cost + performance recommendations (consumes Cost + Knowledge memory)                       |
| 2.0     | Policy Adapter        | Workspace- or tier-specific severity / retention / suppression rules                        |
| 2.0     | Advisor               | Top-level UI presenter — synthesizes findings across all modules                            |

Every module obeys the same five rules in §"Hard architectural rules"
below. New capabilities add new `FindingKind` enum values rather than
re-shaping the existing contract.

> **Note on automation level today.** As of 1.1, the Memory Layer's
> lesson store + search API are shipped and reachable via
> `POST /api/steward/lessons/search`. The *automatic* failure-recovery
> flow ("pipeline fails → Steward searches lessons → surfaces approved
> fix in the editor") is the **Incident Analyst** module landing in
> 1.2. Today you can search the lesson store manually via the API;
> the editor-level auto-search is one release away.

---

## HTTP API

All endpoints are workspace-scoped (default workspace: `"default"`).
Auth is required (`require_auth` dependency).

### `GET /api/steward/findings?status=open`

Runs a fresh scan and returns findings matching `status` (default
`open`; pass `status=all` to see everything). Response:

```json
{
  "workspace_id": "default",
  "count": 2,
  "findings": [
    {
      "id": "dup-src-a1b2c3d4e5f6g7h8",
      "kind": "duplicate_source",
      "severity": "p2",
      "status": "open",
      "title": "3 pipelines read the same source",
      "body": "The same source object is read by **3 pipelines** ...",
      "evidence": {
        "source_signature": "a1b2c3d4e5f6g7h8",
        "source_node_type": "db_source",
        "workflows": [
          {"id": "wf-orders-analytics", "name": "Orders → Analytics"},
          {"id": "wf-orders-finance",   "name": "Orders → Finance"},
          {"id": "wf-orders-ops",       "name": "Orders → Ops"}
        ]
      },
      "proposed_actions": [
        {"label": "Consolidate via Managed Table",
         "action": "create_managed_table_from_source",
         "params": {"source_signature": "a1b2c3d4e5f6g7h8"}},
        {"label": "Dismiss (intentional duplicate)",
         "action": "suppress_finding",
         "params": {"finding_id": "dup-src-a1b2c3d4e5f6g7h8",
                    "scope": "signature"}}
      ],
      "first_seen": "2026-06-05T10:14:32.000Z",
      "last_seen":  "2026-06-05T10:14:32.000Z",
      "occurrences": 3
    }
  ]
}
```

### `POST /api/steward/scan`

Explicit re-scan. Equivalent to `GET /findings` but always re-runs and
returns just the count + workspace ID. Used by the UI's `Re-scan` button.

### `POST /api/steward/findings/{id}/dismiss`

Mark a finding as intentional. The finding's signature is added to the
per-workspace suppression file (`<data_dir>/steward/<workspace_id>/suppressions.json`)
so re-scans don't keep re-emitting the same finding.

### `POST /api/steward/findings/{id}/resolve`

Mark a finding as "I took action, close it." Does NOT add a permanent
suppression — if the underlying pattern recurs (someone re-creates the
duplicate), the Steward will flag it again. Logged for audit either way.

---

## Where the code lives

| Path                                                 | What it is                                |
|------------------------------------------------------|-------------------------------------------|
| `backend/fpulse/steward/__init__.py`                 | Package docstring + the five hard rules   |
| `backend/fpulse/steward/models.py`                   | `StewardFinding`, severity / kind enums   |
| `backend/fpulse/steward/archeologist.py`             | Duplicate detection (pure code)           |
| `backend/fpulse/api/steward.py`                      | FastAPI router + suppression store        |
| `backend/tests/test_steward_archeologist.py`         | Smoke + behavioural tests                 |
| `frontend/src/components/StewardBadge.tsx`           | Header badge + findings panel             |

Adding a new specialist module in a later release means: add a file
under `backend/fpulse/steward/<name>.py`, extend `FindingKind` in
`models.py`, extend `_run_scan()` in `api/steward.py` to call it, and
write tests. The UI's `StewardBadge` already renders any `FindingKind`
it sees, so no frontend change is required for new kinds beyond a
label + icon entry.

---

## One-liner

**F-Pulse Steward is a read-only operational intelligence layer that
observes pipelines, nodes, connectors, data quality, architecture,
governance, and cost signals; compares them against historical
baselines; remembers approved fixes; and alerts only when something
meaningfully deviates from normal — without ever changing anything
without approval.**
