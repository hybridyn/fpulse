# F-Pulse OSS 1.0.0 — release notes

Draft text for the GitHub release tag + announcement post. Keep this
short, sober, and verifiable. No marketing absolutes. Every claim
maps to shipped code.

---

## F-Pulse 1.0.0 — local-first data pipeline engine

**One command. Loopback by default. Your data stays on your host.**

F-Pulse OSS 1.0 is the first stable release of a single-binary,
local-first data pipeline engine. It's designed for the data
engineer who wants the productivity of a real pipeline orchestrator
without the operational cost of Docker, Kubernetes, or a managed
SaaS bill.

### What's in 1.0

- **`fpulse open`** — one-command launch. Backend boots on a free
  port (defaults to 8001, falls back if taken), browser opens
  automatically. Detects WSL / Docker / SSH / headless Linux and
  falls back to a printed URL.
- **Loopback by default.** Backend binds to `127.0.0.1`; LAN
  exposure requires explicit `FPULSE_BIND_HOST=0.0.0.0` or
  `FPULSE_ALLOW_LAN=1` opt-in. DNS-rebinding defense via Host
  header allowlist + Origin/Referer pinning. The hardening rationale
  is in [`docs/install/security-hardening.md`](../install/security-hardening.md).
- **DuckDB-powered execution.** Vectorised in-process transforms.
  Joins, group-bys, pivots, aggregates run column-at-a-time. Spills
  to disk for bigger-than-RAM datasets.
- **40 node types** across sources, transforms, sinks, control flow,
  AI assistance, and operational utilities.
- **33 first-party connectors visible by default** — 4 database
  dialects (PostgreSQL / MySQL / MS SQL Server / SQLite) + 2
  bulk-load dialects (Postgres `COPY FROM STDIN`, Snowflake
  `PUT` + `COPY INTO`) + 27 SaaS REST manifests. 10 additional
  consumer-marketing / SMB-CRM manifests ship Hidden by tier flag.
- **5-tier honest connector classification** —
  Production / Verified / Beta / Experimental / Hidden. Today's
  count: **0 Production, 0 Verified, 19 Beta, 8 Experimental**.
  The bar for Verified is a live-vendor smoke test in CI plus a
  stored fixture; no connector clears that bar yet. We chose to
  publish the actual numbers rather than soft-label everything
  "Certified."
- **Avro + ORC file readers** alongside the existing CSV / JSON /
  Parquet / Excel / XML support, in both the File source and all
  5 Cloud Files sources (S3 / Azure Blob / GCS / SharePoint / SFTP).
- **REST framework upgrade** — proper HTTP method + body support +
  pagination aliases + deep template substitution. The previous
  GET-only framework silently dropped POST bodies; this is now
  fixed with 18 contract tests pinning the behaviour.
- **32 optional dependency extras** in `pyproject.toml` for
  database drivers. `pip install fpulse[postgres]`,
  `pip install fpulse[oracle]`, `pip install fpulse[all]`, etc.
  Full per-database install + OS-driver instructions in
  [`docs/install/database-drivers.md`](../install/database-drivers.md).
- **Embedded AI assistance** — Copilot chat + inline helpers (SQL,
  transform, diagnose-error, post-run summary, anomaly detect).
  Local Ollama is the default; cloud LLM providers are opt-in
  bring-your-own-key, no proxying through us.
- **Plus license design specified** — named-user seat model with
  Ed25519-signed license file. Spec in
  [`docs/design/plus-license-model.md`](../design/plus-license-model.md).
  Implementation is a separate workstream, expected to land
  incrementally.

### How to install

```bash
pip install fpulse
fpulse open
```

For database connectors, add the matching extra:

```bash
pip install "fpulse[postgres]"
pip install "fpulse[oracle]"      # thin mode, no Instant Client
pip install "fpulse[snowflake]"
pip install "fpulse[all]"          # everything pip-only (no OS drivers)
```

MS SQL Server, Oracle thick mode, and IBM Db2 also need OS-level
drivers — see [`docs/install/database-drivers.md`](../install/database-drivers.md).

### What 1.0 deliberately does NOT promise

- **No "AI generates your pipeline end-to-end" claim.** Inline AI
  helpers exist; the headline "describe → working pipeline" flow
  is on the 1.1 roadmap, intentionally deferred until reliable.
- **No Verified-tier connectors yet.** The verification harness
  ships; the first wave of Verified rows lands as live-smoke CI
  + fixtures land per connector. Estimated first 10 within 4-6
  weeks post-launch.
- **No desktop binary.** `fpulse open` opens the system default
  browser. A native packaged desktop app is on the 1.1 roadmap.
  Two candidate paths under evaluation — see
  [`docs/roadmap/oss-1-1.md`](../roadmap/oss-1-1.md).
- **No Plus license enforcement.** The named-user seat model is
  specified; implementation comes when first customer signs.

### Tested with

| Component | Version |
|---|---|
| Python | 3.11.7, 3.12.1 |
| DuckDB | 1.1.3 |
| Docker Engine | 25.0+ |
| Ollama (optional, for local AI) | 0.5.7 |
| Node.js (build only) | 20.10 LTS |

### Acknowledgements

This release reflects feedback from multiple rounds of independent
review on architecture, security posture, connector certification,
and product positioning. Specific corrections from those reviews
are credited in the
[`changelog.md`](../../CHANGELOG.md) under the 2026-06-02 entry.

### License

Apache License 2.0. See [`LICENSE`](../../LICENSE) and
[`NOTICE`](../../NOTICE).

Built by [Hybridyn Data Labs](https://hybridyn.com).
