# F-Pulse OSS — Deployment & Upgrade Runbook

**Audience:** the solo developer or small team running F-Pulse on their
own laptop, VPS, or single-node server.

This is the **canonical operator guide**. The README quickstart is
deliberately short; the scaling guide is about capacity. This document
is about **install, upgrade, backup, and recovery** — the four things
that decide whether your deployment survives Monday morning.

---

## 1. What you are actually running

A single F-Pulse install is **three independently-versioned components**:

| # | Component         | Versioned by             | Lives in                          |
|---|-------------------|--------------------------|-----------------------------------|
| 1 | F-Pulse itself    | `FPULSE_IMAGE_TAG`       | `hybridyn/fpulse:<tag>` image     |
| 2 | Ollama runtime    | `OLLAMA_IMAGE_TAG`       | `ollama/ollama:<tag>` image       |
| 3 | Ollama models     | pulled in-app on demand  | `ollama_data` volume              |

Treat them as independent. F-Pulse 1.0.0 will keep working when you bump
Ollama 0.5.7 → 0.6.x, and vice versa. The only constraint is the
**Tested with** matrix in `changelog.md` — that is the combination CI
green-lit. Anything else is best-effort.

For F-Pulse+ deployments add a fourth component: **PostgreSQL**, pinned
via `POSTGRES_IMAGE_TAG`. Same rules apply.

---

## 2. Install

### 2.1 Recommended: Docker Compose

```bash
git clone https://github.com/hybridyn/fpulse
cd fpulse
cp .env.example .env          # edit if you need overrides
docker compose up -d          # F-Pulse only
# or
docker compose --profile ai up -d   # F-Pulse + Ollama
```

Open <http://localhost:5174>.

`.env` is the **only place** you should change image tags. Editing
`docker-compose.yml` directly works but means your tag overrides walk
away on the next `git pull`.

### 2.2 From source (development)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
cd frontend && npm ci && npm run build && cd ..
python -m fpulse serve            # serves on :8001 (or: fpulse serve)
```

You are off the tested matrix the moment you run a Python or Node
version not in the CHANGELOG. Fine for hacking; not recommended for
production.

### 2.3 Run in the background (as a service)

`fpulse serve` / `fpulse open` run in the foreground — they stop when the
terminal closes. For an always-on install that survives terminal close,
logout, and reboot, register the OS-native supervisor (one command, no
admin/sudo):

```bash
fpulse install-service     # Windows Scheduled Task · macOS launchd · Linux user systemd
fpulse service-status      # is it running?
fpulse uninstall-service   # remove it (data is left intact)
```

- **Windows** — Scheduled Task at logon (`schtasks`); runs hidden, restarts on crash.
- **macOS** — launchd LaunchAgent; auto-starts at login, `KeepAlive` restart.
- **Linux** — user-mode `systemd` unit; add `loginctl enable-linger $USER` to keep it running after logout.

Under **Docker** you don't need this — `docker compose up -d` is already
detached and the compose file's restart policy brings the container back on
reboot/crash. Full walkthrough + per-OS table:
[README → "Run it in the background"](../README.md#run-it-in-the-background-as-a-service).

---

## 3. Upgrade — the three flows

### 3.1 Upgrading F-Pulse

```bash
git pull                                          # get the new compose + .env.example
# read changelog.md → Tested with → check Ollama tag still matches
docker compose pull fpulse
docker compose up -d fpulse
```

**Always read `changelog.md` before pulling.** Specifically the *Tested
with* and *Known limitations* sections. If F-Pulse 1.1.0 was tested
with Ollama 0.6.x and you are pinned to 0.5.7, bump Ollama in the same
maintenance window (section 3.2).

If your `.env` predates the new release, diff `.env.example` against it
and add any new variables before restarting.

### 3.2 Upgrading Ollama (the runtime)

Independent of F-Pulse. Edit `.env`:

```env
OLLAMA_IMAGE_TAG=0.6.0
```

Then:

```bash
docker compose pull ollama
docker compose up -d ollama
```

Models live in the `ollama_data` volume and survive container
replacement. No re-download.

### 3.3 Upgrading Ollama models (in-app)

Open **Insights → AI Provider → Ollama Models**. Each installed model
shows a pill; the picker pulls a new model on demand. The CLI fallback:

```bash
docker exec fpulse-ollama ollama pull qwen2.5:7b
```

Models are independent of both F-Pulse and Ollama versions. The
`qwen2.5:7b` default is the 2026-05-19 tool-use floor — ~6 GB RAM at
Q4_K_M, 30–60 s per agent turn on CPU. Sub-7B models advertise tool
schemas but can't reliably drive the agent loop; the in-app banner
catches that and offers a one-click upgrade. Switch to `llama3.1:8b`
or `phi-4` for equivalent floor behavior, or bump to `qwen2.5:14b` if
you have a 12 GB+ GPU.

### 3.4 Upgrade order, when you bump everything at once

1. Stop traffic (or expect read-only behavior during the window).
2. **Backup first** (section 4).
3. `docker compose pull` (all images).
4. `docker compose up -d`.
5. Smoke-test: open the app, run a Sample-mode pipeline, check
   `/api/health` returns 200 and `/api/health/memory` shows the
   feature flags you expect.

---

## 4. Backup

Two volumes hold all state worth backing up:

| Volume        | Contents                                            |
|---------------|-----------------------------------------------------|
| `fpulse_data` | SQLite database, uploaded files, encryption keys, run history |
| `ollama_data` | Pulled LLM models (regenerable; skip if disk-tight) |

**Daily snapshot (recommended cron):**

```bash
docker run --rm \
  -v fpulse_data:/data:ro \
  -v "$PWD/backups":/backups \
  alpine \
  tar czf /backups/fpulse-$(date +%F).tgz -C /data .
```

Rotate to 7 daily / 4 weekly / 12 monthly. `fpulse_data` is
typically <1 GB for solo-dev usage; the tarball compresses well.

The `fpulse_data` volume contains the encryption-key material that
unlocks stored connection credentials. **Treat backups like
credentials** — encrypt at rest, restrict access.

---

## 5. Disaster recovery

### 5.1 Container died, volumes intact

```bash
docker compose up -d
```

That is the entire procedure. Volumes are durable; container state is
not. If this does not bring the app back, jump to section 5.3.

### 5.2 Volume corrupted, backup intact

```bash
docker compose down
docker volume rm hybridyn-f-pulse-oss_fpulse_data
docker volume create hybridyn-f-pulse-oss_fpulse_data
docker run --rm \
  -v hybridyn-f-pulse-oss_fpulse_data:/data \
  -v "$PWD/backups":/backups:ro \
  alpine \
  tar xzf /backups/fpulse-2026-06-01.tgz -C /data
docker compose up -d
```

Verify by logging in, opening one pipeline, and running it in
Sample mode. If credentials decrypt correctly the keystream survived.

### 5.3 Whole host gone

You need three things from the old host: the `fpulse_data` tarball,
your `.env`, and the F-Pulse version tag from `changelog.md`. With
those, section 5.2 reconstructs the install on any host.

Without `.env`, you lose any environment-variable-only configuration
(DuckDB tuning, CORS allowlists, telemetry consent). The data
survives — F-Pulse rebuilds defaults — but operator config does not.
**Back up `.env` alongside the data tarball.**

---

## 6. Troubleshooting

| Symptom                                       | Likely cause                              | Fix                                              |
|-----------------------------------------------|-------------------------------------------|--------------------------------------------------|
| `docker compose pull` errors with `manifest unknown` | Tag in `.env` does not exist on Docker Hub | Check the tag in `changelog.md` Tested-with     |
| Agent panel shows "no provider"               | Ollama container not running or unreachable | `docker compose --profile ai up -d ollama`      |
| Agent times out at 30s on first message       | Cold model load on CPU                    | First request warms the model; retry, or switch to GPU model |
| Pipeline run hangs at "Acquiring pool slot"   | DuckDB pool exhausted                     | Check `FPULSE_MAX_CONCURRENT_RUNS` in `.env`    |
| `/api/health` 200 but UI 502                  | Frontend build stale                      | `docker compose pull fpulse && docker compose up -d` |

For everything else: `docker compose logs -f fpulse` and the
`/api/health/memory` flags surface.

---

## 6.5. Database schema versions

F-Pulse OSS uses a SQLite database under `${FPULSE_DATA_DIR}/fpulse.db`.
Schema is automatically migrated forward on every boot — there is no
manual migration step. Current head version: **31**. Each migration is
additive only and idempotent on re-run.

Highlights for operators upgrading from older installs:

| Version | Added | Why it matters |
|---|---|---|
| v27 | `settings.updated_at` column | fixes an `INSERT … ON CONFLICT` drift the auth router relied on |
| v28 | `schema_history` table | per-table column shape history for managed sink writes |
| v29 | `backfill_runs` table | parent + per-window status for chunked replays |
| v30 | `sink_idempotency` table | per-(pipeline, sink, key_hash) marker for external-sink dedup |
| v31 | `sync_state` table | per-(workflow, source-step) cursor watermark for incremental sources — db_source + api_source auto-load it at the top of each run when `sync_mode=incremental`, auto-save the new max at the end |

Migration code lives in `backend/fpulse/storage/database.py` (search for
`_migrate_vNN_*` methods). The migration runner records every applied
version in the `_meta` table so re-boots skip cleanly.

## 7. Backfill & schedule safety checks

`POST /api/backfills` and `POST /api/schedules/` perform preflight checks
on the pipeline being scheduled or replayed. When a check trips, the
endpoint returns **HTTP 400** with a machine-readable `code` plus the
exact `acknowledge_*` flag the caller must set to override:

| Code | Endpoint | Override flag |
|---|---|---|
| `unsafe_for_backfill` | `/api/backfills` | `acknowledge_side_effects=true` |
| `no_source_uses_cursor_param` | `/api/backfills` | `acknowledge_no_cursor_usage=true` |
| `unsafe_for_schedule` | `/api/schedules/` | `acknowledge_side_effects=true` |

`unsafe_for_*` fires when the pipeline contains external sinks (email,
webhook, Slack, etc.) that would replay side-effects. `no_source_uses_cursor_param`
fires when none of the pipeline's sources actually consume the backfill
window parameter — i.e. the replay would re-read the same data instead
of the historical slice. Set the acknowledgement flag explicitly to
confirm intent; otherwise leave it off so the preflight stays a
guardrail.

---

## 8. F-Pulse+ deltas

The Plus deployment adds PostgreSQL, splits API and worker, and adds a
worker-role guard (`FPULSE_WORKER_PLACEHOLDER_ACK`). The pinning
discipline, three-component-update model, and backup procedure are
identical — see the Plus deployment guide for the worker-specific bits.

---

## See also

- `changelog.md` — the *Tested with* matrix per release
- `docs/scaling.md` — capacity planning, not install
- `docs/security-deployment.md` — credential encryption and key rotation
- `docs/architecture.md` — what the binary actually contains
