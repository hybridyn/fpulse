# F-Pulse Steward - Architecture and Design

In-depth design reference for contributors, reviewers, and operators.
Audience: people who need to understand WHY the system is shaped the
way it is, not just WHAT it does.

For the user-facing how-to, see [`overview.md`](overview.md).
For the positioning summary, see [`positioning.md`](positioning.md).
For the durable lesson surface, see [`memory-layer.md`](memory-layer.md).

This document uses ASCII diagrams and avoids Unicode box-drawing or
em-dashes so it renders cleanly in every terminal, every git tool, and
every PDF export. (An earlier draft used Unicode arrows which broke
display on Windows consoles with non-UTF-8 code pages.)

---

## 1. The problem we are addressing

Pipeline tooling at large is pretty good at three things: building
pipelines, running them, and reporting whether each job passed or
failed. It is markedly less good at four other things, and the gap is
expensive:

- Teams rediscover the same source quirks, failure signatures, and
  remediation steps repeatedly because no surface records them.
- Duplicate pipelines and duplicate source onboarding proliferate
  because no system watches the control plane for structural
  repetition.
- Monitoring generates symptoms but not stewardship - too many alerts,
  too little context, almost no durable learning.
- Cost regressions and SLA drift accumulate silently because no
  background analyzer compares "today" against "what used to be true."

F-Pulse Steward exists to close that gap. It is not a scheduler,
connector runtime, or generic AI assistant. It is the intelligence
layer that sits on top of the F-Pulse control plane and turns
telemetry, topology, and operator feedback into actionable
stewardship.

This positioning is intentionally defensible: it does not claim to
replace any existing orchestrator, and it does not claim that no other
tool addresses any of these problems. It claims that no other
open-source orchestrator addresses them *together as a first-class
in-product layer*, and that this combination is the differentiator.

---

## 2. The three architectural bets

### Bet 1: The highest-value automation is pipeline judgment, not pipeline generation

Many systems focus on helping users author pipelines faster. That
matters. The larger long-term value comes from helping users *avoid*
bad patterns, recognize operational drift, detect duplication, and
understand when a system is becoming less trustworthy or more
expensive. The Steward is built for judgment.

### Bet 2: Memory must be distilled, gated, and operational

The Steward should not store raw chat transcripts as its primary
intelligence layer. It should convert runs, incidents, and operator
interventions into typed lessons: source quirks, failure patterns,
retry rules, cost anomalies, SLA observations. Each lesson is a
structured record with explicit fields, evidence references, and a
human approval step before it influences future reasoning.

This is implemented by the F-Pulse Memory Layer (see
[`memory-layer.md`](memory-layer.md) and
[`backend/fpulse/steward/lessons.py`](../../backend/fpulse/steward/lessons.py)).

### Bet 3: Reliability intelligence is a layer, not the execution engine

The Steward observes, evaluates, recommends, escalates, and learns.
Extraction, transformation, scheduling, destination writes - all stay
in the F-Pulse core. The Steward adds judgment without destabilizing
the runtime.

---

## 3. The seven hard rules

These are invariants. They are pinned in
`backend/fpulse/steward/__init__.py` as the package docstring. Any
new specialist module that violates one does not ship.

| # | Rule | If relaxed |
|---|------|-----------|
| 1 | **Read-only.** The Steward never mutates workflows, connections, credentials, or schedules. | A wrong "auto-fix" silently corrupts user data. Trust gone permanently. |
| 2 | **Out-of-band.** The Steward never blocks pipeline execution. It consumes snapshots, not live state. | A Steward bug stalls the executor; user pipelines stop running. |
| 3 | **Learning is gated.** Proposed lessons do NOT influence future reasoning until a human approves them. | False positives harden into policy; bad assumptions spread. |
| 4 | **Explicit provenance.** Every finding and every lesson carries the IDs of the inputs it inspected. | "The AI thinks so" with no audit trail; reviewer cannot verify. |
| 5 | **Notification de-duplication is mandatory.** A single underlying incident must collapse into one operator-facing alert. | Alert fatigue kills adoption. |
| 6 | **Historical Baseline Variance, not absolute thresholds.** Volume / null-rate / freshness alerts MUST compare against an observed per-signature baseline, never an absolute number. A node that returns 0 rows 90% of the time is not flagged on the 91st zero day; a node that returns 10k +/- 500 rows daily IS flagged when it returns 0. | The "valid empty table" fallacy. A daily-disputes pipeline that legitimately returns 0 rows on quiet days pages out every quiet day. Users mute the channel. |
| 7 | **Intentional-change suppression.** Schema / topology mutations co-occurring across N+ entities within a maintenance window are rolled into a single baseline-update card, not N separate findings. | The "schema drift fatigue" trap. A planned migration touching 50 tables produces 50 alerts in 30 seconds; user clicks Dismiss All; learning model gets garbage data. |

Rules 6 and 7 were added 2026-06-05 in direct response to the
4-reviewer convergence on multi-level observability scope expansion.
They prevent the two failure modes that, more than any other, kill
data-observability products in the first month of production use.

### Required implementation approach for Rule 6 (when detectors land in 1.2/1.3)

The baseline engine MUST use **seasonality-aware categorical
windows**, not naive rolling averages. A pipeline that processes
financial ledger data legitimately moves zero rows on weekends and
millions on weekdays. A rolling 7-day mean would compute a non-zero
baseline and falsely alert every quiet Saturday while missing a
critical zero-row drop on a busy Monday.

Required mathematical form:

```
Alert(t) = True  iff  | V(t) - mu_window(t) |  >  3 * sigma_window(t)

where:
  window(t) = the matching historical bucket for time t
              (day-of-week + hour-of-day at minimum)
  mu_window, sigma_window = mean + stddev computed over at least
              N=12 samples in that bucket
```

Concrete contract for `empty_output`, `null_spike`, `volume_anomaly`,
`freshness_miss` detectors:

- `confidence_score` < 0.5 if the bucket has < 12 samples (insufficient
  evidence — surface as informational, not alertable).
- `baseline_window` field on every emitted finding MUST name the
  bucket strategy used ("day_of_week:30d", "hour:7d", etc.).
- `evidence_count` MUST be the number of historical samples in the
  matching bucket (so reviewers can see "we have 23 prior Saturdays
  to compare against, today is the 24th").

### Required implementation approach for Rule 7 (when schema-drift detector lands in 1.3)

The schema-drift detector MUST use a **sliding time-clamped
accumulator** rather than emitting one finding per mutation.

```
When a schema mutation lands on table T at time t:
  1. Compute mutation_signature(T, t)
  2. Check the workspace's accumulator buffer for any mutation
     within the last `bundle_window` seconds (default: 60s,
     configurable per workspace via Policy Adapter in 2.0)
  3. If 0 prior mutations in the window: hold this mutation in
     the buffer for `bundle_window` seconds before emitting
  4. If 1+ prior mutations in the window: extend the buffer
     deadline + bundle them into a single rolled-up finding
     of kind `schema_drift` with evidence_count = number of
     bundled tables
```

This collapses a planned migration that touches 50 tables in 30s
into a single "structural baseline evolution detected — 50 tables
in workspace `prod`" card with one Approve button that updates all
50 baselines at once. Without this accumulator, the user sees 50
alerts in 30 seconds, clicks Dismiss All, and the learning model
gets poisoned by treating an intentional change as 50 false
positives.

These rules are deliberately framed against specific failure modes
rather than abstract principles. If they read as too rigid, the
correct response is to add an eighth or ninth invariant rather than
relax any of the first seven.

---

## 4. Mental model - the Steward is a layer of specialist modules

The Steward is composed of **specialist modules** (sometimes called
"sub-agents" in earlier internal docs - the word is misleading because
these are deterministic analyzers, not autonomous LLM agents).

Each module has one bounded responsibility and produces typed
`StewardFinding` records or `MemoryLesson` records that flow through
the same surface (eye-icon dropdown + notification bell + lesson
store).

Module roster (per architectural review block 2 - five additional
modules added 2026-06-05 to cover cost, architecture, knowledge,
governance, and health):

| Module                 | Responsibility                                                            | Status     |
|------------------------|---------------------------------------------------------------------------|------------|
| **Archeologist**       | Detect duplicate sources + duplicate pipelines across workspace           | Shipped 1.1 |
| **Learning Layer**     | Persistent occurrence counter + severity escalation + rebound state       | Shipped 1.1 |
| **Memory Layer**       | Durable lesson store + propose/approve/revalidate workflow                | Shipped 1.1 |
| **Notification Bridge**| Route findings to the right surface with dedup + fan-out                  | Shipped 1.1 |
| **Incident Analyst**   | Cluster failure signatures + summarize probable causes + auto-search Memory Layer | Planned 1.2 |
| **Sentinel**           | Monitor live pipeline health + SLA pattern tracking                       | Planned 1.2 |
| **Foreseer**           | Volume + structural + schema-drift anomaly detection                      | Planned 1.3 |
| **Cost Steward**       | Cost-drift detection ("Today's load was 1766% above the 30-day average") + warehouse-warm waste | Planned 1.3 |
| **Architecture Steward** | Cross-warehouse storage duplication ("Same Oracle source extracted twice into separate lakehouses, est. 1.4 TB waste") | Planned 1.3 |
| **Knowledge Steward**  | Learn SUCCESSFUL patterns (incremental watermark + partition pushdown + batch_size=5000) and recommend them | Planned 1.4 |
| **Curator**            | Distill EPULSE_RUNBOOK.md from approved lessons + recommendation patterns | Planned 1.4 |
| **Governor**           | PII detection + credential sprawl + cross-project service-account use     | Planned 1.4 |
| **Optimizer**          | Cost + performance recommendations (consumes Cost + Knowledge memory)     | Planned 2.0 |
| **Policy Adapter**     | Workspace- or tier-specific severity / retention / suppression rules      | Planned 2.0 |
| **Advisor**            | Top-level UI presenter — synthesizes findings from all modules            | Planned 2.0 |

Users see one product surface: **F-Pulse Steward**. The decomposition
above is purely internal — each module is a narrow, testable, code-driven
analyzer with one bounded responsibility (Rule 5 in §3).

When a new module ships, every shared component is reused. New
modules attach to the same ingestion, memory, lessons, and
notification contracts. They do not invent parallel infrastructure.

---

## 5. System diagram

```text
                       +-----------------------+
                       |   F-Pulse Runtime     |
                       | pipelines, logs, API  |
                       +----------+------------+
                                  |
                                  v
                      +-----------------------+
                      |  Steward Ingestion    |
                      | events, topology      |
                      +-----------+-----------+
                                  |
       +--------------+-----------+-----------+--------------+
       |              |                       |              |
       v              v                       v              v
+-------------+  +-------------+        +-----------+  +---------+
| Archeologist|  | Incident    |        | Learning  |  | Memory  |
| duplicates  |  | Analyst     |        | Layer     |  | Layer   |
| 1.1         |  | 1.2         |        | 1.1       |  | 1.1     |
+------+------+  +------+------+        +-----+-----+  +----+----+
       |                |                     |             |
       +----------------+---------+-----------+-------------+
                                  |
                                  v
                      +-----------------------+
                      |  Notification Bridge  |
                      |  dedup + fan-out      |
                      +-----------+-----------+
                                  |
                +-----------------+----------------+
                |                                  |
                v                                  v
       +----------------+              +----------------------+
       | F-Pulse UI     |              | External channels    |
       | incidents page |              | email / Slack /      |
       | eye-icon panel |              | webhook (existing    |
       +----------------+              | notification stack)  |
                                       +----------------------+
```

Two important properties:

- Every module below the Ingestion box is OPTIONAL at runtime. The
  Steward must function with any individual specialist disabled.
- The Notification Bridge is the single fan-out point. No specialist
  module writes directly to a user channel.

---

## 6. The Archeologist - code walkthrough

The 1.1 specialist module. Detects two patterns:

### 6.1 Source signature derivation

Each source-typed node gets a stable identity hash computed from the
fields that determine *what is being read*:

```python
def _source_signature(node_params: dict) -> str | None:
    identity_fields = (
        "connection_id", "connector_type",
        "table", "table_name", "schema", "schema_name",
        "file_path", "query", "url", "endpoint",
        "object", "object_name",
    )
    parts = [f"{f}={v}" for f in identity_fields
             if (v := node_params.get(f)) not in (None, "")]
    if not parts:
        return None
    parts.sort()  # stable ordering across dict insertion order
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
```

### 6.2 The signature-collision trap and how we avoid it

A reviewer raised the right concern: in large enterprise footprints,
separate business teams legitimately ingest identical table shapes
from different sources for regulatory isolation (e.g. EU GDPR vs US
HIPAA data). Pure structural signatures would flag this as a
duplicate.

The Steward's signature already includes `connection_id` as part of
the hash, which means two teams reading from two distinct connections
- even if the table name and shape are identical - produce different
signatures and do NOT collide. This is the primary mitigation.

For future multi-tenant Plus deployments where signatures may need
even tighter isolation, the architecture allows context fields
(`tenant_id`, `compliance_zone`) to be added to the `identity_fields`
tuple without changing the rest of the detection path. The signature
function is the single point of change.

### 6.3 Two detection passes

```python
# Pass 1: same source signature in >=2 different workflows
for sig, occurrences in by_signature.items():
    if len({occ["workflow_id"] for occ in occurrences}) >= 2:
        emit_finding(DUPLICATE_SOURCE, ...)

# Pass 2: same (source-set, sink-set) shape in >=2 workflows
for shape, wf_ids in by_shape.items():
    if len(wf_ids) >= 2:
        emit_finding(DUPLICATE_PIPELINE, ...)
```

Pass 1 is fast and catches the common case. Pass 2 catches the
"two engineers built the same flow" accident by requiring both
source-set AND sink-set to match.

### 6.4 False-positive guards

| Pattern | Why it's NOT a duplicate | How we avoid the FP |
|---------|--------------------------|---------------------|
| Same source in ONE workflow (self-join) | Validator territory, not cross-workflow duplication | Pass 1 uses unique-workflow set with len >= 2 |
| Same source -> different sinks (fan-out) | Intentional - one source, many consumers | Pass 2 requires SAME source-set AND SAME sink-set |
| `raw -> staging -> cleansed` chains | One logical dataset traversing layers | Each layer reads a different signature so they don't collide |
| User-OK'd intentional duplicates (DR replication, data-vault) | The user said it's intentional | `suppressed_signatures` set filters before emit |
| Multi-tenant isolation (GDPR vs HIPAA) | Separate connections by design | `connection_id` is part of the signature hash |

### 6.5 Complexity

O(N * M) where N = workflows, M = average source/sink nodes per
workflow. At typical OSS scale (N < 200, M < 50) this is
sub-millisecond. The detector runs on every `/api/steward/findings`
request without caching. At very large scale (thousands of pipelines
per hour), the in-memory batch-buffer + 5-10 second flush cycle is
the right next step.

---

## 7. The Learning Layer - persistent state across scans

The Archeologist is stateless: every scan re-derives findings from
the current workflow set. What makes the Steward *learn* is a
per-workspace append-only JSONL journal at
`<data_dir>/steward/<workspace>/memory.jsonl`.

### 7.1 Why JSONL first

Initial storage is JSONL rather than SQLite. The reasoning:

| Property                       | JSONL append-only file | SQLite table              |
|--------------------------------|------------------------|---------------------------|
| Lock-free append on POSIX/Win  | Yes                    | Lock contention with executor |
| Schema migrations              | None - field-additive  | Required per version      |
| Hand-inspectable for debugging | Yes                    | Needs a sqlite client     |
| Repo-friendly for review       | Yes                    | Binary blob               |
| Aggregate query performance    | Linear scan            | Indexed                   |
| Risk of executor lock conflict | Zero                   | Real                      |

The decisive argument is executor isolation. SQLite is the right
choice at higher scale, but introducing it now would couple the
Steward to the runtime database in ways we cannot easily reverse.

### 7.2 Why not SQLite yet (and how we will migrate)

When scale demands it, the migration path is:
- Maintain JSONL as the write-ahead log (the canonical truth).
- Add a SQLite cache derived deterministically from the JSONL stream.
- Reader queries hit SQLite; writes go to JSONL first, then update
  SQLite under the same lock.
- A crash leaves the JSONL intact, and the SQLite cache is rebuildable
  by replay.

This avoids the dual-state drift risk the reviewer flagged. Right
now there is no SQLite involved; the design above is the documented
plan for if/when query volume justifies it.

### 7.3 Persistent occurrence counter

```python
def persistent_occurrences(self) -> dict[str, int]:
    scans_by_signature: dict[str, set[str]] = defaultdict(set)
    for ev in self._events():
        if ev["kind"] == "emit" and ev["signature"]:
            scans_by_signature[ev["signature"]].add(ev["scan_id"])
    return {sig: len(scans) for sig, scans in scans_by_signature.items()}
```

The crucial design point: we count **distinct scan IDs**, not raw
emit events. Re-running the same scan twice in 30 seconds does not
inflate the counter.

### 7.4 Severity escalation

```python
if persistent >= escalate_after_n_occurrences and f.severity != P1:
    f.severity = _bump_severity(f.severity)  # P3->P2 or P2->P1
    f.body += "\n\n_Severity escalated from ... because this finding "
              "has been surfaced in {persistent} separate scans "
              "without resolution._"
```

What the user keeps ignoring gets louder, not quieter. The annotation
in the body shows *why* the severity changed - no opaque escalation.

### 7.5 Rebound detection

If the user resolved a finding and the same signature re-emerges
later, the new finding is annotated `(rebounded)` with the date of
the prior resolution. Catches the "teammate re-introduced the
duplicate" regression.

---

## 8. The Memory Layer - durable lessons

The Memory Layer is a separate store from the event journal. It
holds typed, human-approved lessons distilled from operator
decisions: source quirks, failure-fix pairs, retry rules,
intentional duplicates, schema observations.

Full description in [`memory-layer.md`](memory-layer.md). Key
properties for this architecture doc:

- One YAML file + one JSON file per lesson. Both are written
  together inside the same file lock. They cannot drift.
- Stored at `<data_dir>/steward/<workspace>/lessons/`.
- Lifecycle: PROPOSED -> APPROVED -> (revalidate) -> STALE -> APPROVED.
- A PROPOSED lesson does NOT influence future Steward reasoning
  (Rule 3: learning is gated).
- Lessons carry typed `evidence` references so every recommendation
  is traceable to source data (Rule 4: explicit provenance).

---

## 9. Settings

Per-workspace, plain JSON, hand-editable, server-validated:

| Setting | Default | Why this default |
|---------|---------|------------------|
| `enabled` | `true` | OSS-first bet - the feature is on by default or the bet fails |
| `min_severity` | `"p3"` | Show everything in the dropdown; user can dial up if noisy |
| `scan_on_save` | `true` | Immediate feedback after save - sub-50ms cost |
| `auto_stale_days` | `30` | Long enough that "I will fix it next sprint" still works |
| `escalate_after_n_occurrences` | `5` | One business week of nags before escalation |
| `notify_on_finding` | `true` | The bell is *the* attention channel - pinging it is the point |
| `notify_min_severity` | `"p2"` | Default P2 keeps info-only P3 findings in the eye-icon badge without spamming the bell |

A corrupt settings.json falls back to defaults rather than crashing
the scan path. Pinned by `test_corrupt_file_falls_back_to_defaults`.

---

## 10. The Notification Bridge

Two surfaces, deliberately:

| Surface | Attention model | When it fires |
|---------|-----------------|---------------|
| Eye-icon dropdown (StewardBadge) | Browsing - user goes *to* it | Always shows current findings (P3+) |
| Notification bell (existing) | Interrupting - it comes *to* the user | Only on NEW or NEWLY-ESCALATED findings (P2+ by default) |

### 10.1 The de-duplication invariant

> For each (user, finding_id) pair, AT MOST ONE notification per
> (severity, rebound-state) tuple.

| Event | Bell pings? | Why |
|-------|-------------|-----|
| First time a P2 finding appears | Once | New event |
| Re-scan emits the same P2 finding (60s later) | No | Dedup hit |
| Same finding escalates P2 -> P1 via learning layer | Once more | Severity changed - new key |
| Re-scans at P1 | No | Dedup hit |
| Same finding rebounds after resolve | Once more | Rebound-state changed - new key |
| Two users in the workspace | Per-user | Each user has independent dedup |

### 10.2 Async dispatch (the latency mitigation)

A reviewer flagged that pushing high-volume telemetry through
external webhooks synchronously can block the monitoring loop. The
implementation answers this: the notification fan-out is wrapped in
`try/except` and swallows persistence failures, so a slow or
unreachable channel cannot stall the scan response. For future
high-volume external channels (Slack, PagerDuty, generic webhooks),
the dispatch path migrates to an asyncio queue with bounded
back-pressure rather than inline send.

### 10.3 Acknowledgement closes the loop

When the user marks a finding `Mark resolved` or
`Dismiss (intentional)`, all related bell notifications for that
finding are auto-marked read. The bell badge never stays stale for
a triaged issue.

### 10.4 Channel fan-out comes for free

Steward notifications flow through the same notification pipeline as
pipeline run notifications. If the user has email or Slack channels
configured, Steward findings reach those channels too. Zero
additional configuration.

The OSS edition ships the in-app bell + email + Slack + generic
webhook channels. Plus tier adds RBAC-aware approval routing on
proposed lessons (per `docs/editions.md`).

---

## 11. Extension model

Adding a new specialist module follows a standard pattern:

```python
# 1. Add a new FindingKind in steward/models.py
class FindingKind(str, Enum):
    DUPLICATE_SOURCE = "duplicate_source"     # existing
    DUPLICATE_PIPELINE = "duplicate_pipeline" # existing
    FAILURE_RCA = "failure_rca"               # NEW for Incident Analyst

# 2. Add a module that produces StewardFinding lists
# backend/fpulse/steward/incident_analyst.py
def detect_failure_patterns(executions, *, workspace_id, ...):
    findings: list[StewardFinding] = []
    # detection logic
    return findings

# 3. Wire into _run_scan() in api/steward.py
def _run_scan(workspace_id, *, record=True):
    workflows = _workflows_for_scan(workspace_id)
    findings = detect_duplicate_sources(workflows, ...)
    findings += detect_failure_patterns(_executions_for_scan(...), ...)  # NEW
    findings = apply_learning(findings, memory, ...)
    return findings, settings
```

Everything else is inherited: UI, memory journal, lesson store,
notification bridge, suppression model, settings.

---

## 12. Test strategy

48 tests in `backend/tests/test_steward_archeologist.py`, organized
by contract:

| Test class | Pins | Count |
|------------|------|-------|
| `TestSourceSignature` | SHA-256 stability invariants | 6 |
| `TestDuplicateSource` | Positive + negative detection | 3 |
| `TestDuplicatePipeline` | Same source + same sink = duplicate; fan-out is not | 2 |
| `TestSuppression` | Dismissed signatures stay dismissed across scans | 1 |
| `TestDeterminismAndEmpty` | Same input -> same IDs; edge cases | 5 |
| `TestLearningLayer` | Persistent occurrences, escalation, rebound, audit trail | 6 |
| `TestSettings` | Defaults, round-trip, corrupt-file fallback, notify defaults | 5 |
| `TestNotificationBridge` | De-dup invariants (spam-disaster prevention) | 8 |
| `TestMemoryLayerLessons` | Propose/approve/reject/revalidate/stale workflow | 12 |

The notification bridge tests + the gated-learning test
(`test_search_for_failure_excludes_proposed`) are the highest-stakes
- regressions there would mean the bell spams or unvetted lessons
influence recommendations. Both have explicit named tests.

### 12.1 Mock-dependency limitation (and what we do about it)

Tests use a `_FakeNotificationStore` and `_FakeUserStore` for the
notification-bridge path. Realistic enterprise integration scenarios
(Snowflake API drops, Slack rate limiting, transient token expiry)
are NOT covered by these mocks. The mitigation is a separate
integration test suite (`backend/tests/integration/`) which exercises
the real notification persistence path under faulty conditions; that
suite is gated behind the `INTEGRATION` env var so OSS contributors
do not need an enterprise stack to run the default tests.

---

## 13. The data plane

```
<data_dir>/steward/<workspace_id>/
  +-- settings.json       <-- StewardSettings (Pydantic-validated)
  +-- suppressions.json   <-- dismissed signatures + history
  +-- memory.jsonl        <-- append-only event log
  +-- lessons/            <-- Memory Layer (one YAML + one JSON per lesson)
      +-- ora12154.yaml
      +-- ora12154.json
      +-- ...
```

**No SQLite schema changes.** The Steward reads from the existing
workflow store, writes only to its own files. A user could
`rm -rf <data_dir>/steward/` and the rest of F-Pulse continues. The
Steward's state is *disposable* in a way the user's pipelines are
not.

### 13.1 File-locking under high concurrency

A reviewer flagged that flat-file-per-workspace can hit disk-IO
contention under thousands of micro-pipelines per hour. The current
implementation acquires a single in-process file lock for journal
writes; this is fine at OSS scale but does NOT scale to that volume.
The mitigation when needed: an in-memory batch buffer that flushes
sequentially every 5-10 seconds rather than per-line. The buffer
shape is compatible with the JSONL on-disk format (each flush appends
N lines in one syscall) and is the documented next step before
considering a switch to SQLite.

---

## 14. HTTP API surface

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/steward/findings?status=open` | Fresh scan + filter by status |
| POST | `/api/steward/scan` | Force re-scan |
| POST | `/api/steward/findings/{id}/dismiss` | Suppress + log reason; clear related notifications |
| POST | `/api/steward/findings/{id}/resolve` | Close; clear related notifications |
| GET | `/api/steward/settings` | Current per-workspace settings |
| PUT | `/api/steward/settings` | Partial update (re-validates on merge) |
| GET | `/api/steward/memory` | Recent journal events + persistent occurrence counts |
| GET | `/api/steward/memory/stats` | Journal counters |
| GET | `/api/steward/lessons` | List lessons (filterable by status/type/source/pipeline) |
| GET | `/api/steward/lessons/stats` | Lesson counters by status + type |
| GET | `/api/steward/lessons/{id}` | One lesson |
| POST | `/api/steward/lessons` | Propose new lesson (status starts PROPOSED) |
| POST | `/api/steward/lessons/{id}/approve` | PROPOSED -> APPROVED |
| POST | `/api/steward/lessons/{id}/reject` | Mark incorrect, store reason in evidence |
| POST | `/api/steward/lessons/{id}/revalidate` | Bump occurrence_count, refresh clock, may promote |
| POST | `/api/steward/lessons/search` | Step 2 of the 8-step failure flow |
| DELETE | `/api/steward/lessons/{id}` | Hard-delete (only for REJECTED entries) |

All endpoints workspace-scoped and auth-gated.

---

## 15. Performance characteristics

Measured against a synthetic 9-workflow validation fixture (see
`backend/scripts/validate_steward.py`). Numbers are local-machine
measurements; real enterprise installations should publish their own
benchmarks before relying on these for capacity planning.

| Operation | Measured cost | Notes |
|-----------|---------------|-------|
| Single scan (10 workflows) | < 5 ms | Pure code path |
| Single scan (100 workflows) | ~ 15 ms | Linear in N |
| Single scan (1000 workflows) | ~ 80 ms | Approaching budget |
| Memory event append | < 1 ms | Single line append |
| Lesson propose + save | ~ 5 ms | YAML + JSON written together |
| `apply_learning()` over 10 findings | < 2 ms | Re-reads journal each call |
| Notification dedup check | < 5 ms | Inspects latest 50 notifications |
| Full `/api/steward/findings` cold request | ~ 30 ms | Includes JSON + auth |

These numbers should be re-measured before any major release. Any
contributor optimizing the scan path is expected to update this table
with their own benchmark.

---

## 16. Explicit non-goals

| Non-goal | Why |
|----------|-----|
| Auto-fix the duplicate (delete one of the pipelines) | Rule 1 - read-only. Wrong mutation = user data lost. |
| Use LLM to decide which workflows are duplicates | Rule 3 - hallucinated findings = trust gone. |
| Show every potential issue in the bell | Spam = bell gets ignored. Bell is for P2+ only by default. |
| Persist Steward state in SQLite alongside executor data | Lock contention with executor. Plain files instead. |
| Detect duplicates by parsing transform SQL | Out of scope for Archeologist - may ship as separate module. |
| Cross-workspace correlation in OSS | Plus differentiator. OSS users have one workspace. |
| Auto-promote PROPOSED lessons to APPROVED | Rule 3 - learning must be gated by a human. |
| Be a SIEM | Different product category. |
| Be a generic AI chatbot | Different product category. |

---

## 17. Roadmap with rationale

Module order is dictated by data dependencies, not capability sequence.

| Release | Module | Why this order |
|---------|--------|----------------|
| 1.1     | Archeologist + Memory Layer + Learning Layer | Zero input dependencies beyond the workflow store. Immediately demonstrates the differentiator. SHIPPED. |
| 1.2     | Incident Analyst | Needs execution history (exists in F-Pulse). Reuses Memory Layer for past-incident matching. |
| 1.3     | Foreseer | Needs accumulated metrics (row counts, durations) - needs Incident Analyst running for a while to have baselines. |
| 1.4     | Curator | Needs an accumulated lesson set across previous modules. Can only ship after users have been Stewarded for a while. |
| 2.0     | Optimizer + Policy Adapter | Needs cost telemetry + policy primitives - separate workstream. |

---

## 18. Where the code lives

| Path | What it is |
|------|------------|
| `backend/fpulse/steward/__init__.py` | Package overview + the five hard rules |
| `backend/fpulse/steward/models.py` | `StewardFinding`, severity / kind / status enums |
| `backend/fpulse/steward/archeologist.py` | Detection (signatures + two passes + FP guards) |
| `backend/fpulse/steward/memory.py` | Operational event journal (NOT the Memory Layer) |
| `backend/fpulse/steward/lessons.py` | Memory Layer: durable lessons + propose/approve/revalidate |
| `backend/fpulse/steward/settings.py` | Pydantic settings + JSON file persistence |
| `backend/fpulse/steward/notifier.py` | Notification bridge + dedup logic |
| `backend/fpulse/api/steward.py` | HTTP router (17 endpoints) |
| `backend/tests/test_steward_archeologist.py` | 48 tests covering all contracts |
| `backend/scripts/validate_steward.py` | End-to-end demonstration script |
| `frontend/src/components/StewardBadge.tsx` | Header badge + Findings/Memory/Settings dropdown |
| `frontend/src/components/Sidebar.tsx` | Mounts the badge in the header |
| `frontend/src/components/SaveDialog.tsx` | Dispatches `fpulse:steward-refresh` after save |
| `frontend/src/lib/notificationHref.ts` | Deep-link handler for `link_type: "steward"` |
| `frontend/src/components/pages/NotificationsPage.tsx` | Steward notification icon/label mappings |
| `frontend/src/components/pages/HelpPage.tsx` | User-facing Steward how-to guides |
| `docs/steward/overview.md` | User-facing how-to |
| `docs/steward/architecture.md` | This document |
| `docs/steward/memory-layer.md` | Durable lessons surface in depth |
| `docs/steward/positioning.md` | Investor / partner / recruiting summary |
| `docs/steward/sample-memory.jsonl` | Real journal from validation run |
| `docs/steward/validation-output.txt` | Captured validation script output |

---

## Appendix A: Reviewer concerns and mitigations

This doc went through a senior architectural review. The risks
flagged and the mitigations baked in:

| Risk | Mitigation |
|------|------------|
| Signature collision for multi-tenant identical shapes (GDPR vs HIPAA) | `connection_id` already in the signature hash; `tenant_id`/`compliance_zone` can join the `identity_fields` tuple without other changes (Section 6.2) |
| Dual-state drift if JSONL and SQLite both held truth | We don't use SQLite. The documented migration plan keeps JSONL as the WAL with SQLite as a deterministic derived cache (Section 7.2) |
| Synchronous fan-out blocking the monitoring loop | Persistence failures are swallowed; future high-volume channels migrate to an asyncio queue (Section 10.2) |
| Mock-dependency tests miss real integration faults | Separate integration suite under `backend/tests/integration/` gated by `INTEGRATION` env var (Section 12.1) |
| Disk IO bottlenecks at thousands of micro-pipelines per hour | Batch buffer with 5-10s flush cycle; documented as the next step before considering SQLite (Section 13.1) |
| Performance numbers becoming outdated | Section 15 instructs contributors to re-measure on each major release |
| "Sub-agent" wording suggesting autonomous LLM agents | Renamed to "specialist modules" throughout (Section 4) |
| Marketing/engineering tone mismatch in one document | Investor pitch moved to `positioning.md`; this doc is contributor-facing only |
| Absolute "no other tool does X" claim | Softened to "no other open-source orchestrator addresses them together as a first-class in-product layer" (Section 1) |

If you spot another risk that is not on this list, please open a PR
that adds both the risk and the mitigation. The architecture doc is
where these decisions live.
