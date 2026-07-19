# F-Pulse OSS — Run as a Service (no terminal, auto-restart)

**Problem:** `start.bat` / `start.ps1` / `./start.sh` run F-Pulse
**inside the terminal window** you launched them from. Close the
terminal → F-Pulse stops. There's no boot survival, no auto-restart
on crash, no background behaviour.

**You have two choices.** Either install with a packaged installer
(`.exe` / `.pkg` / `.deb` / `.rpm` — see [`installer/readme.md`](../installer/readme.md))
which handles all of this automatically, or run the cross-platform
service CLI yourself (Option 1 below). Both end up at the same
place: F-Pulse registered as an OS-native service.

---

## Option 1 — `fpulse install-service` (recommended, all OSes)

The new built-in CLI registers F-Pulse as a proper OS service in
**one command on Windows, macOS, and Linux**. Same UX everywhere,
different machinery underneath.

**What you get:**

| OS | Mechanism | Boot survival | Logout survival |
|---|---|---|---|
| Windows | Scheduled Task (logon trigger, restart on failure) | yes | per-user |
| macOS | LaunchAgent in `~/Library/LaunchAgents/` (KeepAlive) | yes | per-user |
| Linux | systemd `--user` unit with `Restart=on-failure` | yes (with `loginctl enable-linger`) | yes |

**Install (any OS):**

```bash
# Prereq: pip install -e . (or you've installed a packaged build)
fpulse install-service
# Custom port + data dir:
fpulse install-service --port 8002 --data-dir /var/lib/fpulse
```

**Manage (any OS):**

```bash
fpulse service-status        # is it running?
fpulse uninstall-service     # stop + deregister
```

**OS-native fall-throughs** (use these for deep inspection):

```powershell
# Windows
Get-ScheduledTaskInfo -TaskName "FPulse"
schtasks /Query /TN FPulse /V /FO LIST
```

```bash
# macOS
launchctl list | grep com.hybridyn.fpulse
log show --predicate 'subsystem == "com.hybridyn.fpulse"' --last 1h

# Linux
systemctl --user status fpulse
journalctl --user -u fpulse -f
# Survive logout:
sudo loginctl enable-linger $USER
```

Open `http://localhost:8001/` after install.

---

## Option 2 — Packaged installer (`.exe` / `.pkg` / `.deb` / `.rpm`)

For non-technical users on machines without Python / Node installed.
The installer bundles a frozen Python runtime + the compiled frontend,
then calls `fpulse install-service` for you in its post-install step.

See [`installer/readme.md`](../installer/readme.md) for build recipes
per OS. End-user install is one double-click and a UAC / admin prompt.

---

## Option 3 — Windows Service via NSSM (when Option 1's Scheduled Task isn't enough)

`fpulse install-service` on Windows registers a **Scheduled Task**,
which is tied to your user account and stops if you log out. For
"always on / runs before any user logs in / proper Windows Service in
Services.msc," use NSSM instead.

**Prereq:** download NSSM (Non-Sucking Service Manager) from
<https://nssm.cc/download> — single ~300 KB exe. Put `nssm.exe` on
PATH.

**Install:**

```powershell
# Elevated PowerShell:
cd <repo-root>
.\scripts\install-windows-service.ps1
# If nssm.exe isn't on PATH:
.\scripts\install-windows-service.ps1 -NssmPath C:\path\to\nssm.exe
```

The script:
- Locates your project venv (`.venv\Scripts\python.exe`)
- Registers a service called `FPulse`
- Sets working dir, `PYTHONPATH`, `FPULSE_DATA_DIR`
- Redirects stdout/stderr to `logs/fpulse.out.log` / `logs/fpulse.err.log` (10 MB rotating)
- Configures restart-on-crash (5s delay, up to 99 retries)
- Sets startup type to Automatic
- Starts the service immediately

**Manage:**

```powershell
sc.exe query   FPulse
sc.exe start   FPulse
sc.exe stop    FPulse
Get-Content .\logs\fpulse.out.log -Wait -Tail 20
.\scripts\install-windows-service.ps1 -Uninstall
```

---

## Option 4 — Docker Compose with `restart: unless-stopped`

Cross-platform. Already in the repo. Survives reboot if you set
Docker Desktop to autostart.

```powershell
docker compose up -d
```

The compose file ships `restart: unless-stopped` — the container
auto-restarts on crash and re-starts when Docker comes up after a
reboot. Stop it explicitly with `docker compose down` to disable.

For the AI bundle, use `docker compose --profile ai up -d`.

---

## Option 5 — System-wide systemd unit (Linux VPS, root-managed)

`fpulse install-service` on Linux uses a **user-mode** systemd unit,
which is the right default for desktop installs but not for a VPS
that nobody is logged into.

For a VPS / root-managed deploy, write
`/etc/systemd/system/fpulse.service`:

```ini
[Unit]
Description=F-Pulse OSS — data pipeline orchestrator
After=network.target

[Service]
Type=simple
User=fpulse
Group=fpulse
WorkingDirectory=/opt/fpulse/backend
Environment=PYTHONPATH=/opt/fpulse/backend
Environment=FPULSE_DATA_DIR=/var/lib/fpulse
# 2026-06-02 hardening: default to loopback (127.0.0.1). If this service
# host is reachable from a trusted private network and you want LAN
# access, change to --host 0.0.0.0 OR set Environment=FPULSE_ALLOW_LAN=1.
# Background on the choice: docs/install/security-hardening.md
ExecStart=/opt/fpulse/.venv/bin/python -m uvicorn fpulse.main:app --host 127.0.0.1 --port 8001
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fpulse
sudo systemctl status fpulse
journalctl -u fpulse -f
```

---

## Decision tree

```
Need to install on someone else's machine (non-developer)?
└── YES → packaged installer (Option 2) — handles everything

Otherwise (developer / power user on own machine):
├── Want one cmd that works on any OS?       → Option 1 (fpulse install-service)
├── Windows + need "starts before login"?    → Option 3 (NSSM)
├── Linux VPS / headless / root-managed?     → Option 5 (system systemd unit)
└── Containerised deploy / shared infra?     → Option 4 (Docker)
```

---

## 4. Serving the frontend in service mode

`start.ps1` launches **two** processes: the uvicorn backend AND the
Vite dev server (for hot-reload during development). The service
installers above launch ONLY the backend, because:

- The Vite dev server is meant for development, not production
- The backend already serves the built `frontend/dist/` as static
  files at `/` (after all `/api/*` routes)

So before installing as a service, build the frontend once:

```powershell
cd frontend
npm install
npm run build
```

After that, the frontend lives at `frontend/dist/` and is served by
the backend at the same port (8001 by default). One URL, no Vite
dev server, no port 5174.

The packaged installer (Option 2) does this `npm run build` step for
you at build time, so end users never see it.

If you skip this step and start the service manually, you'll see the
API at `/api/*` work fine but the UI page will be empty / 404. The
fix is `npm run build` and restart the service.

---

## 5. Updates

| You installed via | You update by |
|---|---|
| `pip install fpulse` | `pip install -U fpulse && fpulse install-service` (re-registers against the new binary) |
| Packaged installer | Download new installer + run it. Stops service, swaps files, restarts. |
| Docker | `docker compose pull && docker compose up -d` |
| NSSM / system systemd | Update binary in place, then `sc.exe stop FPulse; sc.exe start FPulse` (Win) or `sudo systemctl restart fpulse` (Linux). |

The data dir is preserved across every update path above. Pipelines,
run history, credentials, and managed tables all survive.

---

## 6. Quick verification after install

After whichever option above:

```powershell
# Health check should return 200 OK with "status: ok"
Invoke-RestMethod http://localhost:8001/api/health
# →  { status = "ok", version = "1.0.0", product = "F-Pulse OSS", mode = "dev" }

# Then open the UI:
Start-Process http://localhost:8001       # service-mode (frontend served by backend)
# OR
Start-Process http://localhost:5174       # dev-mode (Vite running separately)
```

If `/api/health` returns 200 but the UI page is empty, see §4 —
you need to build the frontend.
