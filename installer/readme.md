# F-Pulse OSS — Distribution & Installer Recipes

This directory holds the per-OS recipes Hybridyn uses to ship
**packaged installers** of F-Pulse OSS to end users who don't have
Python or Node set up.

Three audiences, three install paths:

| Audience | Install path | Where it lives |
|---|---|---|
| Developer (any OS) | `pip install -e .` then `fpulse serve` | repo root + `pyproject.toml` |
| Power user (any OS) | `pip install fpulse` then `fpulse install-service` | `backend/fpulse/cli/install_service.py` |
| Non-technical end user | One-click installer — **`.exe` / `.pkg` / `.deb`** | **this directory** |

For ops folks: Docker Compose remains the supported deployment path
(`docker-compose.yml` at repo root).

---

## What the packaged installers do

Each installer bundles:

1. **A frozen Python runtime** — so the user doesn't need to install
   Python 3.11+. Built via PyInstaller (`--onedir` mode for fast
   startup + small per-update deltas).
2. **The compiled frontend** — `frontend/dist/` produced by
   `npm run build`. The backend serves it as static at `/`.
3. **A first-run wizard** — opens the browser to
   `http://localhost:8001/` after install completes.
4. **OS-native service registration** — internally calls
   `fpulse install-service` so the app auto-starts at logon /
   reboot and survives crashes.
5. **A clean uninstaller** — removes the binary, deregisters the
   service, OPTIONALLY removes the data dir (user is prompted).

---

## Build recipes per OS

### Windows — Inno Setup `.exe`

```
installer/windows/
├── fpulse.iss             # Inno Setup script — input
├── build.ps1              # one-command build wrapper
├── icons/                 # branding (the .ico file Inno embeds)
└── output/                # FPulse-Setup-1.0.0.exe lands here
```

Pipeline:

```powershell
# Prereqs (one-time): Inno Setup 6 from https://jrsoftware.org/isinfo.php
# Add Inno Setup's ISCC.exe to PATH.

cd installer\windows
.\build.ps1
# Produces: output\FPulse-Setup-1.0.0.exe (~50 MB signed if --sign passed)
```

What `build.ps1` does:

1. Cleans `dist/`
2. Runs `pyinstaller --onedir backend/fpulse/__main__.py`
   pinned to Python 3.12 from `..\..\.venv` so the bundled runtime
   matches CI.
3. Runs `npm run build` in `frontend/` so `frontend/dist/` is fresh.
4. Calls `ISCC.exe fpulse.iss` to compile the installer.
5. Optionally signs the result (`signtool.exe`) if `-CertThumbprint`
   is passed.

The installer ships a "Run F-Pulse at logon" checkbox in its Tasks
page. Checking it makes the post-install step run
`fpulse install-service` to register the Scheduled Task.

### macOS — `.pkg` via `pkgbuild` + `productbuild`

```
installer/macos/
├── build.sh               # one-command build (runs on macOS 12+)
├── scripts/
│   ├── preinstall         # POSIX shell — stop running service
│   └── postinstall        # POSIX shell — run fpulse install-service
├── component.plist
└── distribution.xml       # productbuild distribution
```

Pipeline:

```bash
cd installer/macos
./build.sh
# Produces: output/FPulse-1.0.0.pkg
```

What `build.sh` does:

1. Cleans `build/` + `output/`.
2. Builds the frontend (`cd ../../frontend && npm run build`).
3. Freezes the backend with PyInstaller into
   `build/payload/FPulse.app/Contents/Resources/`.
4. Runs `pkgbuild --root build/payload --identifier com.hybridyn.fpulse ...`
5. Wraps with `productbuild --distribution distribution.xml ...`.
6. Optionally signs (`--sign "Developer ID Installer: Hybridyn ..."`)
   and notarizes (`xcrun notarytool submit ...`).

The `postinstall` script calls `/Applications/FPulse.app/Contents/MacOS/fpulse install-service`,
which writes the LaunchAgent under `~/Library/LaunchAgents/`.

### Linux — `.deb` (Debian/Ubuntu) and `.rpm` (RHEL/Fedora/SUSE)

```
installer/linux/
├── build-deb.sh           # produces fpulse_1.0.0_amd64.deb
├── build-rpm.sh           # produces fpulse-1.0.0.x86_64.rpm
├── build-appimage.sh      # produces FPulse-1.0.0-x86_64.AppImage
└── debian/                # .deb metadata (control, postinst, prerm, ...)
```

Pipeline:

```bash
cd installer/linux
./build-deb.sh          # apt-installable on Ubuntu/Debian
./build-rpm.sh          # dnf-installable on Fedora/RHEL
./build-appimage.sh     # works on every glibc 2.27+ distro
```

The `postinst` (DEB) / `%post` (RPM) script calls
`fpulse install-service` as the invoking user.

---

## Why we DIDN'T pick a single cross-platform installer framework

| Framework | Why we passed |
|---|---|
| **Electron Builder** | We're a Python+SPA, not a JS-on-Desktop app. Adds Chromium to the bundle. |
| **PyInstaller alone** | Produces a binary, not an installer. Needs OS-specific wrapping. |
| **InstallForge / NSIS** | Windows-only. |
| **Snap** | Linux-only, requires snapd, doesn't fit on RHEL. |
| **Flatpak** | Linux-only, sandbox model conflicts with localhost-binding requirement. |

Per-OS native wrappers (Inno / pkgbuild / dpkg-deb) give the best
"feels like a normal app" experience on each platform, at the cost
of three build pipelines instead of one. We accept that cost.

---

## Update story

Two paths, depending on how the user installed:

| Install path | Update command |
|---|---|
| `pip install fpulse` | `pip install -U fpulse` |
| Packaged installer | Download new installer; run it. The installer detects the existing install, stops the service, swaps files, restarts. |
| Docker | `docker compose pull && docker compose up -d` |

For Plus tier (commercial), in-app auto-update is on the roadmap —
the OSS posture is "user controls when to update."

---

## Code signing & notarization checklist

We don't ship unsigned binaries to end users. Per-OS:

- **Windows**: EV code-signing cert. SmartScreen reputation builds
  up over the first ~1000 downloads — expect SmartScreen warnings
  on the first release until enough installs accumulate.
- **macOS**: Developer ID Application + Developer ID Installer
  certs. Notarize with `xcrun notarytool submit --wait`. Without
  this, Gatekeeper blocks first-launch with "unidentified developer."
- **Linux**: GPG-sign the `.deb` / `.rpm` so `apt-key` / `rpm
  --import` verify the signature.

The signing keys are NOT in this repo. Build scripts read them
from env vars (`FPULSE_WIN_CERT_THUMBPRINT`, `FPULSE_MACOS_SIGN_IDENTITY`,
`FPULSE_GPG_KEY_ID`).

---

## Where each path puts files (for reference)

| OS | Binary | Data dir | Service definition | Logs |
|---|---|---|---|---|
| **Windows** | `%PROGRAMFILES%\FPulse\` | `%LOCALAPPDATA%\FPulse\data` | Scheduled Task `FPulse` | `%LOCALAPPDATA%\FPulse\logs\` |
| **macOS** | `/Applications/FPulse.app/` | `~/Library/Application Support/FPulse` | `~/Library/LaunchAgents/com.hybridyn.fpulse.plist` | `~/Library/Logs/FPulse/` |
| **Linux** | `/usr/lib/fpulse/` + symlink in `/usr/bin/fpulse` | `~/.local/share/fpulse` | `~/.config/systemd/user/fpulse.service` | `journalctl --user -u fpulse` |

These paths match what `fpulse install-service` / `_default_data_dir()`
in `backend/fpulse/cli/install_service.py` use, so the CLI-installed
and packaged-installed deployments are interchangeable.
