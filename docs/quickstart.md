# Quickstart

Run your first F-Pulse pipeline in 5 minutes.

## Install

### Option 1: Docker (recommended)

```bash
git clone https://github.com/hybridyn/fpulse.git
cd hybridyn-f-pulse-oss
docker compose up -d
```

Open [http://localhost:8001](http://localhost:8001).

### Option 2: From source

**Linux/macOS:**
```bash
git clone https://github.com/hybridyn/fpulse.git
cd hybridyn-f-pulse-oss
pip install -e .
fpulse open
```

`fpulse open` starts the backend on a free port (defaults to 8001, falls back if taken) and opens your default browser to the local URL. If you prefer manual launch: `fpulse serve` then open the printed URL yourself.

**Windows:**
```powershell
git clone https://github.com/hybridyn/fpulse.git
cd hybridyn-f-pulse-oss
.\start.ps1
```

Prerequisites: **Python 3.11+** and **Node.js 20+** installed. The first
`.\start.ps1` auto-installs backend + frontend dependencies, picks a free
port pair, and opens the UI in your browser; later runs start in seconds.

The UI opens at [http://localhost:5174](http://localhost:5174) (the
launcher prints the exact URL). Loopback-only by default — see
[`install/security-hardening.md`](install/security-hardening.md) if you
need LAN binding.

### One-click shortcut with the F-Pulse logo (optional)

Prefer a clickable icon over a terminal? Add one once, then launch from
your desktop / app menu. (For a source / dev checkout — the packaged
installers create this for you automatically.)

**Windows** — Desktop + Start Menu icon:

```powershell
.\Create-Shortcut.ps1            # remove with: .\Create-Shortcut.ps1 -Remove
```

**macOS** — double-click `Start-FPulse.command` in Finder (needs
`pip install -e .` once so the `fpulse` command exists).

**Linux** — add an application-menu entry:

```bash
./create-shortcut.sh             # remove with: ./create-shortcut.sh --remove
```

All three carry the F-Pulse logo and open the app in your browser.

### Ports already taken? F-Pulse picks a free pair automatically

If something else on your machine already owns `5174` or `8001` (Postman, another project's dev server, an earlier F-Pulse instance you forgot to stop), F-Pulse **just works** — the launcher scans for the next free pair, writes the chosen pair to `.fpulse/runtime/instance.json`, points Vite + the backend at those ports, and prints the single URL it ended up using:

```text
> .\start.bat
  [1/4] Resolving ports...
  Port 5174 in use by another app; using 5175 for frontend.
  Port 8001 free.
  ...
  F-Pulse is ready
   UI:   http://localhost:5175
   API:  http://localhost:8001/docs
```

No env vars to set, nothing to kill manually.

If you want F-Pulse to **prefer** a specific port (it'll still scan onward from there if busy), set the prefs once:

```powershell
$env:FPULSE_FRONTEND_PORT = 5180   # preferred start of scan range
$env:FPULSE_PORT          = 8010
.\start.bat
```

### Stopping F-Pulse safely

```powershell
.\stop.bat        # dev launcher
.\stop.ps1        # dev launcher (PowerShell native)
fpulse stop       # packaged install (any OS)
```

All three read **the same** `.fpulse/runtime/instance.json` and stop **only** the PIDs F-Pulse itself recorded — and only after three independent signals all agree (PID still alive, still listening on the recorded port, command line still matches the uvicorn / Vite signature). They will never kill a foreign process on those ports, even with `--force` (which only skips the "stop previous instance?" prompt, never the safety checks). The packaged CLI and the dev scripts share the same on-disk format, so you can `start.ps1` your dev backend then later `fpulse stop` it from any other terminal and the second tool finds the first tool's instance correctly.

> Two F-Pulse instances side-by-side? Just run `.\start.bat` a second time. The second instance sees the first one's runtime file, offers to either reuse those ports (after stopping the first) or auto-pick a new pair and run alongside it.

## First login

The first time F-Pulse starts it generates a bootstrap admin password into `INITIAL_ADMIN_PASSWORD.txt` next to the SQLite database. Use it to sign in, then immediately change the password and delete the file.

## Run the first-pipeline demo (60 seconds)

F-Pulse ships with a runnable demo so you can prove it works end-to-end before configuring anything.

1. After login, open the **Templates** page from the top nav.
2. Click the **First pipeline — CSV in, CSV out** card (the first one in the gallery, in the **Get Started** category).
3. Click **Use Template**. The pipeline lands on the canvas: `Read orders.csv → Active only → Add loaded_at → Write CSV`.
4. Click **Run** in the editor toolbar.

The execution log streams at the bottom; rows in / rows out and per-step timing render inline on each node. The output lands at `${FPULSE_DATA_DIR}/samples/output/active_orders.csv`:
- Docker: `/data/samples/output/active_orders.csv` inside the container (in the `fpulse_data` volume).
- Native: `./data/samples/output/active_orders.csv` under the directory you launched the backend from.

The source file `samples/orders.csv` is seeded on first startup from `backend/fpulse/seed_data/orders.csv`. You can edit it, replace it, or point the template at your own CSV.

## Build your own pipeline

1. Open the **Editor** from the top nav.
2. Drag a **CSV Source** node onto the canvas. Configure it to point at any local CSV (the seeded `samples/orders.csv` from the demo is a good starting point).
3. Drag a **Filter** transform. Connect the source to it. Add a condition like `quantity > 5`.
4. Drag an **Output** destination node. Connect the filter to it. Set the output path to `samples/output/filtered.csv` (paths are relative to `FPULSE_DATA_DIR`).
5. Click **Run** in the toolbar.

The execution log streams at the bottom; rows in/out and timing per node appear inline.

**Tip:** when you open an already-published pipeline, auto-save is intentionally off — click **Save** explicitly to overwrite as a new draft. The published version keeps running until you re-publish.

## Add a connector

1. Open **Connections** from the top nav, click **+ New Connection**.
2. Pick PostgreSQL (or any other connector).
3. Fill in host, port, database, username, password. Click **Test** to verify.
4. Save. The connection is available to any pipeline in the same project.

The connector catalog (37 built-in) is at [`connectors.md`](connectors.md). Status badges (Certified / Beta / F-Pulse+) tell you what to expect.

## Schedule it

1. Save the pipeline (top right).
2. Open **Scheduling** from the top nav.
3. Add a schedule — interval (e.g. every 60 minutes), daily at a specific time, or a custom expression for advanced patterns like "every Monday at 9 AM" (uses standard cron syntax).

## Use the AI agent

Open the **F-Pulse Copilot** from the bottom-right corner. The agent has 21 read tools (part of 25 total) to summarize your workspace, inspect connections, query metrics, and explain failures. To enable it:

- **Local AI (recommended):** install [Ollama](https://ollama.com/), pull `qwen2.5:7b` (4.7 GB, ~6 GB RAM at Q4_K_M, 30–60 s per turn on CPU — the reliable tool-use floor as of 2026-05-19). Smaller Qwen 2.5 models advertise tool support but can't drive the agent loop.
- **Cloud AI:** open **Insights → AI Provider** and configure Anthropic, OpenAI, OpenRouter, or another provider with your own API key.

See [AI guide](ai.md) for details.

## Meet the Steward

Once you have 3+ pipelines, look at the **violet eye icon** in the
header (immediately to the left of the DEV / PROD toggle). That's the
**F-Pulse Steward** — F-Pulse's read-only background reliability +
learning layer.

Click it to see findings:

- **Duplicate source** — two or more pipelines reading the same
  source object into different destinations (consolidation opportunity).
- **Duplicate pipeline** — two pipelines with the same source + sink
  shape (often "two engineers built the same flow" by accident).

Each finding lets you `Mark resolved` or `Dismiss (intentional)`
(with an optional reason — DR replication, data-vault layering, etc.).
The **Memory** tab in the dropdown shows the audit trail; the
**Settings** tab tunes the bell-integration and escalation thresholds.

The Steward never modifies your pipelines on its own. New + escalated
findings also ping the notification bell with strict de-duplication
so re-scans never spam you. Full design rationale in
[steward/architecture.md](steward/architecture.md).

## Next steps

- **Nodes** tab in this Help page — full reference for all 40 nodes
- **18 sample pipelines** — see `samples/free-api-pipelines/` for ready-to-import demos covering REST APIs, joins, SCD2, pivot/unpivot, conditional routing, XML ingest, and more. Run `pwsh ./import.ps1` (or `.\import.ps1` on Windows PowerShell 5) to land them all as drafts.
- [Connector catalog](connectors.md) — what works, what's beta
- [Pipelines guide](user-guides/pipelines.md) — covers scheduling, dependencies, and backfills
- [Steward overview](steward/overview.md) — read-only background reliability + learning layer (eye icon in header)
- [Vertical scaling](scaling.md) — tune for larger workloads
- [F-Pulse vs F-Pulse+](editions.md) — when to upgrade
