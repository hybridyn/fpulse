# F-Pulse Steward — audience-split pitches

One file, four pitches. Each cut for a different audience and length.
**All four ship the same honest scope** — what's active in 1.1.x is
labelled active; what's contract-only is labelled contract-only. No
audience gets a more flattering version than another.

If you copy-paste from here into a deck / email / blog / Twitter
thread, leave the "Active in 1.1.x today" line intact. That's the
trust-marker.

---

## A. One-paragraph investor pitch (~80 words)

> F-Pulse OSS is a local-first visual data-pipeline workspace
> (single-binary, Apache 2.0). Its defining differentiator is
> **Steward** — a read-only reliability + memory layer that watches
> the workspace across pipelines, captures team-approved lessons, and
> never mutates a workflow. Active in 1.1.x today: cross-pipeline
> duplicate detection, schema-drift detection, connector-health
> monitoring, user-defined YAML rules, and a gated lesson store fed by
> resolve-with-fix-note. Five OSS-shipped detector surfaces, fully
> tested. The architectural bet: workspace observability above
> execution — a category no current open-source ETL tool occupies.

---

## B. README product pitch (~3 paragraphs, ~200 words)

> **F-Pulse is the visual data-pipeline workspace that watches itself.**
> Build pipelines on a drag-and-drop canvas, run them on a vectorised
> DuckDB engine, schedule them, see lineage, and stay on one machine
> (or one Docker container) for the whole lifecycle. Apache 2.0,
> single binary, no Kubernetes stack required.
>
> The thing that makes F-Pulse different from every other OSS ETL tool
> is the **Steward**: a read-only background observer that watches your
> entire workspace. It tells you when two pipelines accidentally read
> the same source, when a source schema changed under you, when a
> connection has been failing for two days, and when a previously-fixed
> finding has come back. Operators dismiss with a reason (sanitised for
> secrets) or resolve with a fix-note that becomes a `PROPOSED` lesson
> a team lead can approve. Approved lessons are searchable on the next
> similar failure — your tribal knowledge stops vanishing into Slack.
>
> Five active detectors in OSS 1.1.x — duplicate sources/pipelines
> (Archeologist), schema drift, connector health, user-defined YAML
> rules, resolve-to-lesson capture. 172 tests pinning the contracts.
> Read-only by architectural rule: Steward never edits a workflow.

---

## C. Technical architecture pitch (~1 page)

### What Steward is

A read-only observation layer above the F-Pulse execution engine. It
consumes snapshots of the workflow store + connection store + (event-
driven) external recording calls, and emits typed `StewardFinding`
records through one surface: eye-icon dropdown, notification bell,
suppression store, Memory Layer (gated lessons), per-finding 3-signal
ownership for safe stop-on-dismiss.

### The 7-level finding contract

| Level | Active detector |
|---|---|
| **architecture** | Archeologist (duplicate_source, duplicate_pipeline) |
| **connector** | Connector-health (auth_failure / unreachable / rate_limit / near_expiry) |
| **data** | Schema-drift; Foreseer volume_anomaly (baseline-variance) + threshold quality checks (null_rate / freshness / row_count / partition / quality_check) |
| **node** | Empty-output (consecutive zero-row node runs) |
| **governance** | env_crossing / unapproved_destination / pii_leak |
| **cost** | warehouse_waste (consecutive zero-output source reads) |
| pipeline | contract-only — sla_breach / partial_output / retry_storm: enum + storage + UI present, detector deferred |
| **+ user-defined** | YAML rules engine (admin emits at any of the 7 levels) |

### Seven hard architectural rules (pinned in `steward/__init__.py`)

1. **Read-only.** Steward never mutates a workflow.
2. **Out-of-band.** Steward never blocks execution.
3. **Deterministic core, LLM narration shell.** Detection is plain
   code; the LLM only phrases findings.
4. **Explicit provenance.** Every finding carries enough evidence to
   re-derive it from the audit log.
5. **OSS-first.** Detection ships in OSS; Plus adds team-scale.
6. **Historical baseline variance.** Quantitative alerts compare
   against observed baselines, never absolute thresholds.
7. **Intentional-change suppression.** Co-occurring schema/topology
   changes within a maintenance window roll into one finding, not N.

### Alert-fatigue model (three independent guards)

- **Time-clamped escalation.** Requires both N occurrences AND M hours
  since first sighting before escalating to P1.
- **Dismiss resets the per-signature counter.** A dismissed-then-
  recurring finding rebuilds escalation from zero.
- **Notification de-dup invariant.** At-most-one notification per
  `(user, finding, severity, rebound-state)`.

### Memory Layer (gated lesson store)

Distinct from the operational journal (`memory.jsonl`, append-only,
high-volume). Lessons live in `lessons/*.yaml + *.json`, are created
via `POST /lessons` or via Resolve-with-fix-note, start as `PROPOSED`,
and stay inert until a human `approve`s them. Search API today;
auto-invocation on failure lands with Incident Analyst in 1.2.

### Cross-launcher deployment story

`fpulse open` (packaged CLI) and `start.ps1` (dev launcher) share the
same `.fpulse/runtime/instance.json` ownership format. `fpulse stop`
applies a three-signal ownership check (PID alive + on recorded port +
cmdline matches signature) before stopping anything — refuses to
touch any process not recorded as ours.

### What ships next, in order

P4 native data-quality checks (event-driven, mirrors schema-drift) →
governance detectors (env_crossing, unapproved_destination) → P3
connector depth-pass (PostgreSQL first) → cost-tracking recording API
→ empty-output / volume-anomaly node-level detector.

---

## D. One-line versions for different audiences

| Audience | Line |
|---|---|
| **Engineer evaluator** | "Local-first visual ETL with a read-only Steward layer that catches duplicates, schema drift, and connector failures — and remembers what fixed them." |
| **Investor** | "Workspace observability above ETL execution — the first OSS-shipped reliability+memory layer; 5 active detectors, 172 tests, Apache 2.0." |
| **Operator (end user)** | "F-Pulse watches your whole workspace so you don't have to remember things, and captures fixes so the next operator doesn't have to rediscover them." |
| **Procurement** | "Apache 2.0, OSI-approved. Single binary, runs on a laptop or VM. Read-only Steward by architectural rule — never edits workflows." |
| **Twitter / social** | "F-Pulse OSS is the visual data-pipeline workspace that watches itself. Catches duplicates, schema drift, connector failures. Captures the fix when something breaks. Apache 2.0. No paywall on detection." |

---

## What none of these claim (the honesty list)

- **Not** broader connector breadth than Airbyte. (We don't ship
  hundreds.)
- **Not** a replacement for Datadog / Honeycomb. (Steward is a
  workspace-scoped observer, not a full APM.)
- **Not** auto-fix or auto-remediation. (Read-only Rule 1.)
- **Not** auto-invocation of Memory Layer on failure. (That's 1.2.)
- **Not** PII detection / cost intelligence / lineage cascade /
  failure RCA. (Contract-only — detectors land in later releases.)
- **Not** production-mature at scale of 4-year-old projects. (1.0-rc,
  small install base — bugs that take older projects months to surface
  haven't been hit yet.)

If you spot a claim in this file that exceeds what's shipped, that's
a bug — open an issue.
