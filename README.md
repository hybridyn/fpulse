<p align="center">
  <a href="https://hybridyn.com"><img src="docs/assets/hybridyn-logo.png" alt="Hybridyn Data Labs" width="88" height="88"></a>
</p>

<h1 align="center">F-Pulse</h1>

<p align="center">
  Built and maintained by <a href="https://hybridyn.com"><strong>Hybridyn Data Labs</strong></a>
</p>

<p align="center">
  <a href="https://github.com/hybridyn/fpulse/actions/workflows/ci.yml"><img src="https://github.com/hybridyn/fpulse/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/hybridyn/fpulse/actions/workflows/security-scan.yml"><img src="https://github.com/hybridyn/fpulse/actions/workflows/security-scan.yml/badge.svg?branch=main" alt="Security Scan"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="License: Apache 2.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.11+-blue" alt="Python 3.11+"></a>
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/status-1.0.0-blue" alt="Status: 1.0.0"></a>
</p>

Single-binary, local-first data pipeline engine. Clone it, `pip install -e .`, `fpulse open` — backend boots on loopback, browser opens, you're in. Vectorised DuckDB engine, built-in scheduler + alerts + run history, 40 node types, embedded AI assistance with a privacy-preserving local default, and an open connector framework you can extend in minutes. Apache 2.0 forever; predictable seat pricing for teams via F-Pulse+.

> **Status:** 1.0.0 stable. Tested with Python 3.11/3.12 · Docker 25+ · DuckDB 1.1.3 · Postgres 16. See [CHANGELOG.md](CHANGELOG.md) for the full tested-with matrix and known gaps.
>
> **Install today: from source or Docker** — both are one command and fully
> supported ([Quick start](#quick-start)). `pip install fpulse` and the
> prebuilt desktop installers are not published yet; the sections below say
> so where it matters rather than pointing you at a 404.

## Why F-Pulse

- **Fast engine.** DuckDB-powered, vectorised execution. Joins, group-bys, pivots and aggregates run column-at-a-time, not row-by-row. Streams to disk on bigger-than-RAM datasets so you don't blow a heap.
- **Operational layer built in.** Scheduler (runs pipelines automatically at fixed times), alerts (email / Slack / Teams / webhook), run history with per-step row counts + duration, lineage view, and version control with deploy / rollback. Ships in the box — nothing to bolt on. OSS runs as a solo / single-workspace install; multi-user team workspaces with per-workspace RBAC are an **F-Pulse+** upgrade.
- **Local-first.** Run it on your laptop or a VM — no cloud lock-in, telemetry off by default (opt-in only). Single binary, no IDE install, no runtime tuning required.
- **Visual + code.** Drag-and-drop canvas, a real expression engine (`$json`, `$now`, `$('Node').output`), and a DuckDB SQL transform when you need to escape the canvas.
- **Open connector framework — extend in minutes, not weeks.** 33 first-party connectors visible by default (4 database dialects + 2 bulk-load dialects + 27 SaaS REST manifests), tier-labeled honestly (currently 0 Production + 0 Verified + 19 Beta + 8 Experimental). 10 additional consumer-marketing / SMB-CRM manifests ship Hidden — out of enterprise-data-engineering scope. See [docs/connectors.md](docs/connectors.md) for the per-connector matrix and live `GET /api/connectors/cert-matrix`. When you need one we don't ship, you have **four** first-class paths — none of them require a vendor build cycle:
  1. **Paste an OpenAPI URL → get a working connector in 90 seconds.** `Insights → Author Connector → from OpenAPI`. See [docs/extend/build-a-connector.md](docs/extend/build-a-connector.md) for the 30-minute end-to-end tutorial.
  2. **Paste 1–5 sample API responses → get a draft connector.** Same UI, "from samples" mode — for vendors without a public OpenAPI spec.
  3. **Hand-author the manifest.** Full control when the vendor's API doesn't fit a generated shape — ~30 minutes. Same tutorial as above.
  4. **Suggest or contribute.** [Request a connector or node](https://github.com/hybridyn/fpulse/issues/new/choose), or open a PR. Templates pre-fill what we need to act on it.

  This is the OSS bet: we ship the framework, the community ships the long tail. **No connector is Plus-gated** — every manifest, every node, every extension path is open.
- **Honest status badges.** Every connector carries a user-facing tier — Production / Verified / Beta / Experimental / Hidden — derived from the cert matrix. Today: 0 Production, 0 Verified, 19 Beta, 8 Experimental visible (10 Hidden). The default picker shows Production + Verified + Beta; Experimental sits behind a toggle. The bar for Verified is a live-vendor smoke test on every PR plus a stored fixture; Production adds a 30-day green streak and a named owner. Treat the matrix output as ground truth — we'd rather show "0 Verified" than soft-label everything "Certified."
- **Embedded AI (optional).** Pluggable providers (Claude / OpenAI / Gemini / Ollama / OpenRouter) for ghost nodes, autoconfig, error diagnosis — works fully without an LLM via deterministic fallbacks. Local Ollama on `qwen2.5:7b` is the 2026-05-19 tool-use floor; see [docs/supported-models.md](docs/supported-models.md).
- **F-Pulse Steward — read-only workspace observer with a gated Memory Layer.** Most pipeline tools observe execution; Steward adds a workspace-level observation surface above it. **Actively detected today:** duplicate-source + duplicate-pipeline (Archeologist); connector health (auth-failure / unreachable / rate-limit / credential-near-expiry); schema drift; automatic volume anomaly (baseline-variance) plus threshold data-quality checks (null-rate / freshness / row-count / partition); node-level empty-output; warehouse-waste (cost); governance (env-crossing / unapproved-destination / PII-leak); and user-defined YAML rules. All carry persistent-occurrence counts, time-clamped severity escalation, rebound detection on previously-resolved findings, dismiss-with-reason (sanitized for AWS keys / bearer tokens / passwords / URI creds / private IPs before journal write), and notification de-dup at the (user, finding, severity, rebound-state) tuple. **Still contract-ready (enum + storage + UI present, detector deferred):** pipeline-level SLA-breach / partial-output / retry-storm, structural join-explosion / join-collapse, credential-sprawl, and cost-drift / cost-recommendation — future specialists plug in without contract changes. Detection is plain code — no LLM in the decision path, no hallucinated findings. Read-only by architectural rule: never mutates a workflow. **The F-Pulse Memory Layer** ([docs/steward/memory-layer.md](docs/steward/memory-layer.md)) is a separate, explicit lesson store — `POST /api/steward/lessons` creates a `PROPOSED` entry, which stays inert until a human `approve`s it. Dismiss and Resolve are separate flows (suppression / closure), neither auto-creates a lesson; that prevents the lesson store from being polluted with exception text. Auto-invocation of lesson search on failure ships with Incident Analyst in 1.2. **Ships in OSS, not paywalled.** See [docs/steward/overview.md](docs/steward/overview.md), [docs/steward/positioning.md](docs/steward/positioning.md), [docs/steward/architecture.md](docs/steward/architecture.md).

> **Evaluating against another orchestrator?** See [docs/vs-talend.md](docs/vs-talend.md) for the side-by-side comparison.

## Quick start

F-Pulse is a **self-hosted server + web app** (a backend that serves a browser
UI), not a desktop program. So the recommended path on **any OS** is Docker —
the *same one command* on Windows, macOS, and Linux. Pick what fits you:

| You are | Recommended path | Time |
|---|---|---|
| **On Windows / macOS / Linux — want it to just work** | **Docker Compose** below (needs Docker Desktop or Docker Engine) | 5 min |
| **On Linux, and you'd rather not run Docker** | **Linux package** below — `.deb` / `.rpm` / `.AppImage` (no Python, no Docker on the box) | 2 min |
| **A developer / contributor** | **From source** below (Python 3.11+, Node 20+) | 10 min |

**Windows / macOS — where's the native installer?** For a self-hosted server
app, the standard way to run it on Windows or macOS is **Docker Desktop** (the
Docker Compose path above) — that's how comparable tools (Grafana, Metabase,
Gitea, Nextcloud…) ship, and it works identically everywhere. A native `.exe` /
`.pkg` *builds and works*, but we don't publish it: unsigned, it trips Windows
SmartScreen / macOS Gatekeeper with a scary dialog, and we won't hand you that.
Want the native app anyway? Build it yourself in one command — see
[`installer/readme.md`](installer/readme.md) — and click through the warning.
Signed installers land once we have an EV certificate + Apple notarization.

**Not on the public registries yet** (we'd rather say so than send you to a 404):

| Not yet available | Use this instead |
|---|---|
| `pip install fpulse` | Builds and runs, just not on PyPI yet — use **From source** below (one extra command). |
| `docker pull hybridyn/fpulse` | Not on Docker Hub yet, but **you don't need it** — `docker compose up` builds it locally on first run (see below). |

> **Behind a corporate / office proxy?** If `pip install` — or the first-time
> `docker compose up` build, which runs `pip` *inside* the image — fails with
> `SSL: CERTIFICATE_VERIFY_FAILED … self-signed certificate in certificate chain`,
> that's your network's TLS-inspecting proxy, not F-Pulse. It breaks *any* Python
> install on that network. Fixes, best first:
> 1. **Trust your org's root CA (proper fix).** Get the cert from IT, then
>    `pip config set global.cert C:\path\to\corp-ca.pem` and point
>    `REQUESTS_CA_BUNDLE` at the same file. For the Docker build, bake the CA into
>    the image — or just pull a prebuilt one (next point).
> 2. **Pull a prebuilt image (cleanest, once published).** `docker pull` is a
>    single TLS handshake your Docker Desktop proxy settings already handle — no
>    in-network `pip` build at all.
> 3. **Quick unblock (less strict).**
>    `pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -e .`

### Linux package — `.deb` / `.rpm` / `.AppImage`

Bundles a frozen Python runtime and the compiled UI — no Python, no Node,
no Docker needed on the target machine. After install, F-Pulse registers
itself as a systemd **user** service that starts at login and survives
reboots.

Download from the [latest GitHub Release](https://github.com/hybridyn/fpulse/releases/latest):

| Distro | Package | Install |
|---|---|---|
| Debian / Ubuntu 22.04+ | `fpulse_1.0.0_amd64.deb` | `sudo apt install ./fpulse_1.0.0_amd64.deb` |
| Fedora / RHEL / Alma / Rocky 9+ | `fpulse-1.0.0-1.x86_64.rpm` | `sudo dnf install ./fpulse-1.0.0-1.x86_64.rpm` |
| Any Linux (glibc 2.27+) | `FPulse-1.0.0-x86_64.AppImage` | `chmod +x ./FPulse-*.AppImage && ./FPulse-*.AppImage` |

> **These packages are not GPG-signed**, so `apt` / `dnf` will note that the
> package is unauthenticated. Verify what you downloaded against
> `SHA256SUMS` on the release: `sha256sum -c SHA256SUMS`. That proves the
> file arrived intact, not who built it — signing is on the list.

After install, open <http://localhost:8001> — the service is already running.

**Manage the service** (any OS, same commands):

```bash
fpulse service-status        # is it running?
fpulse uninstall-service     # stop + deregister (does NOT delete data)
fpulse install-service       # re-register after an update
```

Building these installers yourself (CI / private builds): see
[`installer/readme.md`](installer/readme.md).

### Docker Compose

```bash
git clone https://github.com/hybridyn/fpulse.git
cd fpulse
cp .env.example .env            # optional — defaults work
docker compose up -d            # F-Pulse only
# or:  docker compose --profile ai up -d   # F-Pulse + local Ollama
```

Open <http://localhost:8001> — on first launch F-Pulse asks you to **create your admin account** (your email + a strong password; this first account owns the instance). Then open **Templates → First pipeline — CSV in, CSV out → Use Template → Run**. You'll see real output from a seeded sample dataset in under a minute — no Postgres, no API keys, no cloud account required.

> **Automating a headless deploy** that can't do the interactive first run? Set `FPULSE_BOOTSTRAP_ADMIN=1` and F-Pulse auto-creates `admin@fpulse.local` with a random password — read it with `docker compose exec fpulse cat /data/INITIAL_ADMIN_PASSWORD.txt`, sign in, and rotate it.

To upgrade later:

```bash
docker compose pull && docker compose up -d
```

> **Heads-up — `hybridyn/fpulse:1.0.0` is built locally, not pulled.** The
> image is not on Docker Hub yet. `docker-compose.yml` declares
> `build: context: .`, so `docker compose up` auto-builds it from the
> `Dockerfile` at repo root on first run — ~3-5 min, one-time, no
> `pull access denied` despite the `hybridyn/fpulse` name. `docker compose
> pull` will fail until the image is published; use `docker compose build`
> to pick up changes.

### Single-container alternative (`docker run`)

If you don't want compose, one container is enough — but **build the image
first**. Unlike compose, `docker run` has no build step and would try to
pull an image that isn't published yet:

```bash
docker build -t hybridyn/fpulse:1.0.0 .
docker run -d --name fpulse \
  -p 8001:8001 \
  -v fpulse_data:/data \
  -e FPULSE_DATA_DIR=/data \
  hybridyn/fpulse:1.0.0
```

Then open <http://localhost:8001>.

### From source (Python 3.11+ and Node 20+)

`pip install -e .` and `fpulse open` are **two separate commands** — install first,
then run. The `.` means "the project in the current folder", so you must be
**inside the cloned repo** when you run it. `fpulse` only exists *after* the
install succeeds (it's the command that install creates).

**macOS / Linux:**

```bash
git clone https://github.com/hybridyn/fpulse.git
cd fpulse

# 1. Build the UI (one-time). The built bundle isn't committed — skip this
#    and the API runs but / serves nothing.
cd frontend && npm ci && npm run build && cd ..

# 2. Install into a venv, then run.
python -m venv .venv && source .venv/bin/activate
pip install -e .
fpulse open
```

**Windows (PowerShell)** — run these **one line at a time**; `&&` chaining and
`source` are not valid in Windows PowerShell:

```powershell
git clone https://github.com/hybridyn/fpulse.git
cd fpulse

# 1. Build the UI (one-time).
cd frontend
npm ci
npm run build
cd ..

# 2. Install into a venv, then run.
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
fpulse open
```

> **Windows tip:** if `Activate.ps1` is blocked by execution policy, don't fight
> it — just call the venv's executables directly instead:
> `.\.venv\Scripts\pip.exe install -e .` then `.\.venv\Scripts\fpulse.exe open`.

> Skipping step 1 gets you a working API and a blank page. F-Pulse will say
> so loudly at startup (`no frontend build found …`) rather than leaving you
> to guess.

The `fpulse open` command starts the backend on a free port (defaults to 8001, falls back if in use) and opens your default browser to the local URL. No need to type or remember a URL.

If you prefer the manual flow:
```bash
fpulse serve        # starts on http://127.0.0.1:8001, you open the browser yourself
fpulse serve --open # same as `fpulse open`
fpulse serve --port 9000
```

Headless / WSL2 / Docker / DevContainer / remote-SSH users: `fpulse open` detects these automatically, skips the browser auto-launch, and prints the URL prominently so you can paste it into a browser on your host machine. You can also pass `--no-open` to force-skip the auto-launch while keeping the friendly port-fallback behaviour.

> **Bind defaults to loopback (127.0.0.1)** — invisible to your LAN, no port exposure to coworkers / hotel WiFi / conference networks. If you genuinely need LAN-visible binding for an on-prem multi-user install, set `FPULSE_ALLOW_LAN=1` or pass `--host 0.0.0.0` explicitly. Full rationale: [docs/install/security-hardening.md](docs/install/security-hardening.md).

For environment-variable reference see [`.env.example`](.env.example).
For day-2 ops (upgrades, backup, DR, troubleshooting) see the canonical
runbook: [docs/deployment.md](docs/deployment.md).

### Run it in the background (as a service)

`fpulse serve` / `fpulse open` run in the **foreground** — fine while you're
trying it out, but they stop the moment you close the terminal. For an
always-on install that survives terminal close, logout, and reboot, register
F-Pulse with your OS service manager. **One command, every platform, no
admin/sudo:**

```bash
fpulse install-service     # register + start it supervised in the background
fpulse service-status      # check whether it's running
fpulse uninstall-service   # remove it
```

| OS | What it registers | Notes |
|---|---|---|
| **Windows** | Scheduled Task at logon (`schtasks`) | runs hidden, restarts on crash — no NSSM / pywin32 |
| **macOS** | launchd LaunchAgent | auto-starts at login, `KeepAlive` restart |
| **Linux** | user-mode `systemd` unit | `systemctl --user enable --now`; add `loginctl enable-linger $USER` to keep it running after you log out |

**On Docker instead?** You don't need this — `docker compose up -d` is already
detached, and the compose file's restart policy brings the container back on
reboot/crash.

> This is the same OS-native supervisor the (not-yet-shipped) desktop
> installers wrap under the hood — so power users get background operation
> today without waiting for the `.msi` / `.pkg` / `.deb`.

### Deploy to cloud

F-Pulse runs on any platform that takes a Docker image. Config files
for the most common ones are in the repo root:

| Platform | Config | Quick start |
|---|---|---|
| Fly.io   | [`fly.toml`](fly.toml)         | `fly launch --copy-config` |
| Render   | [`render.yaml`](render.yaml)   | New → Blueprint → connect this repo |
| Railway  | [`railway.toml`](railway.toml) | New → Deploy from GitHub repo |

> **Persistence matters.** Cloud filesystems are ephemeral by default —
> every restart wipes the SQLite DB and all configuration. Each config
> above mounts `/data` to a managed volume so your work survives
> restarts. Free tiers have small disks; for production sizing see
> [docs/deployment.md](docs/deployment.md).

## What's in F-Pulse vs F-Pulse+

F-Pulse is the open-source core for individuals and small teams.
F-Pulse+ is a commercial layer adding team workspaces, RBAC, two-gate
approvals, audit logs, vault-backed credentials, sandbox, drift detection,
and SLA-backed support.

See [edition-matrix.md](edition-matrix.md) for the full per-feature breakdown.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

"F-Pulse" and "Hybridyn" are trademarks of Hybridyn Data Labs.
See [TRADEMARK.md](TRADEMARK.md) for usage policy.

## Contributing

**External pull requests are paused for v1.0.0** while our Contributor
License Agreement clears legal review — we won't ask you to sign a draft.
Bug reports, connector/node requests, questions and design feedback are all
open and wanted, and F-Pulse is Apache 2.0 so forking is always yours.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the details and for how to park a
fix you've already written.

[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) has the rules of the road.

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md). Please do not file
public issues for security reports.

## Links

- Documentation: in-repo at [docs/](docs/) (start with [docs/quickstart.md](docs/quickstart.md)). A hosted site at `docs.hybridyn.com/f-pulse` is on the launch checklist.
- F-Pulse+ commercial: `hybridyn.com/f-pulse` — email hello@hybridyn.com for details
- Issues: GitHub Issues on this repo
- Contact: hello@hybridyn.com
