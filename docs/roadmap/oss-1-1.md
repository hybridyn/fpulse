# F-Pulse OSS 1.1 — roadmap notes

This document captures work intentionally **deferred from 1.0** to a
future 1.1 release. Items here are not commitments — they're the
prioritized post-launch backlog, ordered by user-visible impact.

A 1.1 item gets promoted to "doing now" when one of three things
happens: (a) operator pain-signal from real OSS users, (b) a
customer-blocking gap surfaces during a Plus sales conversation,
(c) the underlying tech reaches the maturity needed for a clean
implementation.

Updated: 2026-06-05

---

## 0. F-Pulse Steward — Archeologist sub-agent **[HEADLINE for 1.1]**

**Status:** Landed 2026-06-05 (this build).

**Why it's the headline:** Every OSS orchestrator on the market lets
you build pipelines. None of them watch your pipeline set for
duplication, drift, or accidental shape collisions. The Steward fills
that gap, and we put it in OSS — not behind a Plus paywall — because it
is what makes F-Pulse feel like more than "another ETL tool" the first
time a user opens the app.

**Shipped in 1.1:**

- Archeologist sub-agent — detects duplicate sources and duplicate
  pipelines (same source signature + same sink signature) across the
  workspace. Lineage-based detection, not naive name-matching, so
  layered `raw → staging → cleansed` chains aren't false-positive
  flagged.
- Read-only by design — the Steward never mutates workflows,
  connections, or credentials on its own.
- Workspace-scoped suppression — dismissed findings persist across
  re-scans so intentional duplicates (DR, data-vault) stop nagging.
- Header surface (`StewardBadge`) — count badge + dropdown panel with
  Dismiss / Resolve actions, paired with the notification bell.
- HTTP endpoints: `GET /api/steward/findings`, `POST /api/steward/scan`,
  `POST /api/steward/findings/{id}/dismiss`, `POST .../resolve`.
- Smoke tests pin: positive/negative detection, suppression honoured,
  IDs deterministic across runs.

**Deferred to 1.2+:**

- Autopsy sub-agent (failure RCA with memory of past incidents — 1.2)
- Foreseer sub-agent (volume + schema-drift anomaly detection — 1.3)
- Curator (`EPULSE_RUNBOOK.md` distillation of recurring guidance — 1.4)
- Optimizer (cost + performance recommendations — 2.0)

**Hard architectural rules** (pinned in
`backend/fpulse/steward/__init__.py` and `docs/steward/overview.md`):
read-only, out-of-band, deterministic core + LLM-narration shell,
explicit provenance, OSS-first.

---

## 1. OSS desktop application (Tauri or pip/Streamlit pattern)

**Status:** deferred from 1.0; target 1.1+

**Why not in 1.0:** 6–8 weeks of focused engineering for a packaging
shift would have pushed the launch back without solving any
correctness or trust problem. The 1.0 security concern (LAN exposure
via `0.0.0.0` default) was addressed in `backend/fpulse/api/local_hardening.py`
+ the loopback-by-default launchers — see
[docs/install/security-hardening.md](../install/security-hardening.md).

**What's still missing without desktop packaging:**

- "Type `http://localhost:8001` in a browser" is a developer reflex,
  not an enterprise-user expectation. People want double-click → app
  opens.
- Auto-update story (today users `git pull` or re-run installer).
- OS integration: system tray, native file dialogs, native
  notifications, dock/start-menu icon.

**Two candidate paths to evaluate before committing:**

### Path A — Tauri shell + Python sidecar

| Pros | Cons |
|---|---|
| True native binary (~10-20 MB shell + system WebView) | Python sidecar pushes the actual installer to 200 MB+ (pandas/duckdb/pyarrow). Reviewer 4 caught this. |
| Capability-based security model; React frontend talks to Python via IPC | Code-signing recurses into every bundled `.so` / `.dylib` on macOS — multi-week debugging tail for one bad C-extension deep in the dep tree. |
| Auto-update built in | Orphan-process risk on abrupt close (Alt+F4 mid-ingest → zombie Python). Need explicit heartbeat + graceful shutdown. |
| Industry-proven (1Password, Discord, others) | Adds Rust toolchain to the build matrix. |

### Path B — pip-install + auto-launched browser (Streamlit / Jupyter pattern)

| Pros | Cons |
|---|---|
| Zero new toolchain — keep the Python codebase as-is. | Still requires a Python install on the user's machine. |
| Distribution = `pip install fpulse` (already works). | Doesn't give the "double-click an app icon" feel. |
| No code-signing recursion — there's no app bundle. | Browser is still the front-end (UX criticism partially unaddressed). |
| Tools like Jupyter, Streamlit, Meltano use this and it works. | No system tray / native menu. |
| Auto-open browser to tokenised URL = same UX as Jupyter. | Updates via `pip install -U fpulse` — fine for devs, awkward for non-devs. |

### Recommendation for the 1.1 decision

Build a thin **Path B implementation first** (a `fpulse open` command
that picks an open port, starts the backend bound to loopback with
a per-launch token, opens the OS default browser to
`http://127.0.0.1:PORT/?token=...`). Ship that as 1.1 — it's a
~2-day delta from where we land 1.0.

Then evaluate whether the leap to Tauri (Path A) is justified by
actual user feedback. If non-technical operators say "I still want
an icon I can double-click," the Tauri spike happens for 1.2.

This staged approach matches what the 4 reviewers all converged on:
let evidence drive the desktop investment, not aesthetics.

### Implementation notes when desktop work starts (any path)

- Backend bind: ephemeral port chosen at launch, written to
  `~/.config/fpulse/runtime.json` mode 0600 alongside a one-time
  launch token. Frontend reads the file to discover both.
- Heartbeat: frontend pings backend every 5 s; backend self-shuts
  after 15 s of no heartbeat. Closes the zombie-process hazard.
- License layer: stays server-side; desktop OSS has no license
  check (it's open source).

---

## 2. Plus license enforcement implementation

**Status:** specced in [docs/design/plus-license-model.md](../design/plus-license-model.md);
not implemented in 1.0.

The 5-seat license model (1 Admin + 4 Developers, single Prod
environment) is fully specified — including the session identity
model that closes the share-one-username exploit Reviewer 4 caught.
Implementation is ~10 weeks for one focused engineer; ordered as
~5 sprint-sized PRs in the design doc.

Sales/legal sign-off on SKU + license wording + grace periods
required before implementation starts.

---

## 3. Verified-tier connector candidates

**Status:** tier system shipped in 1.0; no connector at Verified yet.

The cert matrix now carries a `tier` field per connector
(Production / Verified / Beta / Experimental / Hidden). All current
SaaS connectors are Beta or Experimental — none have the live-vendor
CI smoke runs required for Verified.

Target 1.1 set, ordered by verifiability:

| Tier-1 verify targets | How |
|---|---|
| postgres | `postgres:16` container in CI |
| mysql | `mysql:8` container |
| sqlite | fixture `.db` file |
| mongodb | `mongo:7` container |
| s3 / minio | `minio/minio` container |
| github | public unauth endpoints + PAT for rate-limit headroom |
| weaviate | local container |
| qdrant | local container |
| clickhouse | local container |

Each needs a fixture file at `backend/tests/fixtures/connectors/<id>/smoke.json`
and an entry in `backend/fpulse/connectors/ci/live_smoke.yml`. The CI
workflow (`.github/workflows/connector-smoke.yml`) already exists —
it's empty today because the allow-list is empty.

---

## 4. Connector framework — the SDK layer

**Status:** suggested by Reviewer 1; not yet broken down.

Today F-Pulse has a REST manifest framework (`rest_framework.py`) +
a database adapter layer (`db_source.py`). Reviewer 1's argument was
that **the differentiator isn't 100 connectors — it's a framework
where connector #50 takes hours, not weeks.**

Candidate SDK layers:

- **Auth SDK** — already partially exists in `rest_framework.py`
  (`bearer / api_key / basic / oauth2`); needs a clean
  cross-language extraction
- **REST SDK** — extract `rest_framework.py` into a more pluggable
  shape; let community contribute manifests against a stable spec
- **Database SDK** — formalize the dialect interface in `db_source.py`
- **File SDK** — file_node + cloud_files already form the seed
- **Streaming SDK** — currently missing; would enable proper Kafka /
  Event Hubs / Kinesis connectors at Beta+ tier

Pre-work needed before this becomes a coherent 1.1 item: pick
which SDK to extract first (probably REST since the surface is
largest).

---

## 5. AI-Native Data Engineering positioning

**Status:** Reviewer 1's strategic suggestion; positioning work, not code.

Reviewer 1 argued F-Pulse will not win on connector count (Airbyte
has 600+, n8n has 1000+). The win is in **AI-assisted pipeline
construction**: user says "bring Oracle HR data into Fabric," and the
platform discovers the Oracle schema, recommends tables, creates the
pipeline with watermark + incremental logic, suggests the warehouse
data model, and generates documentation.

F-Pulse already has the building blocks (`Embedder`, `LLM Guardrail`,
`Semantic Router`, the AI Authoring path for connectors). The 1.1
work is wiring them into a single "describe-your-pipeline-in-English"
flow.

Pre-work: write a separate positioning doc capturing the AI-Native
thesis + how it differs from "another ETL tool with N connectors."
Track at `docs/positioning/AI_NATIVE.md` when it lands.

---

## 6. Frontend "exposed on LAN" warning banner

**Status:** backend endpoint shipped in 1.0; banner UI deferred to 1.1.

`GET /api/health/bind-info` returns the current bind state. The UI
should render a sticky warning when `loopback_only: false`. The
backend hook is done; the React component is the one small piece
remaining.

Estimated effort: half a day. Could be done as a 1.0.1 patch if
operator feedback prioritizes it.

---

## 7. Streaming connectors (Kafka / Event Hub / Kinesis / Pulsar / MQTT)

**Status:** REST manifest framework is HTTP-shaped; streaming needs
a different adapter.

Today the `rest_framework` adapter pulls; streaming connectors push.
A new adapter shape is needed. Specifically:

- Long-running consumer process (not a single HTTP request)
- Offset / committed-position management
- Backpressure handling against downstream sinks
- At-least-once vs exactly-once delivery semantics

This is real engineering work, not just a manifest addition. Best
done after the Connector SDK layer (item 4) is settled so the
streaming adapter slots cleanly into the same framework.

---

## How to read this list

Items above are **deferred from 1.0**, not **promised for 1.1**. The
priority order between them isn't fixed — it will shift based on
which signals come back loudest from real OSS users + Plus sales
conversations after launch.

If you're a contributor and want to pick up any of these: open an
issue first to confirm scope before doing significant work. Several
items above are deliberately under-specified because the right shape
depends on user feedback that doesn't exist yet.
