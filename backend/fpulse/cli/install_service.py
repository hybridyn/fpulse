"""
Cross-platform service installer for F-Pulse.

Exposes ``fpulse install-service`` / ``fpulse uninstall-service`` /
``fpulse service-status``. Detects the host OS and does the right
thing on each:

  * **Windows** — registers a Scheduled Task at user logon that runs
    ``python -m uvicorn fpulse.main:app`` hidden, restarts on
    failure, survives terminal close.  Uses only built-in
    ``schtasks.exe`` — no NSSM or pywin32 dependency.  This is the
    same logic ``scripts/install-scheduled-task.ps1`` ships, but
    callable from ANY OS via the unified CLI.

  * **macOS** — writes ``~/Library/LaunchAgents/com.hybridyn.fpulse.plist``
    and ``launchctl load``s it.  Auto-starts at login, restarts on
    crash via ``KeepAlive``.  No sudo required for user-level agent.

  * **Linux** — writes ``~/.config/systemd/user/fpulse.service`` and
    ``systemctl --user enable --now fpulse``.  User-mode unit so no
    sudo required; pair with ``loginctl enable-linger <user>`` if
    you want it to keep running after logout.

The intent is: **one command, every OS, no manual editing of plist
or unit files**.  Power users on any platform can do:

    $ pip install fpulse
    $ fpulse install-service
    $ fpulse service-status

…and have F-Pulse running supervised in the background.

Why not a wrapped .msi / .pkg / .deb here?  Those bundles add their
own bytes-on-disk story (Python runtime, npm build, code signing).
This module is the runtime supervisor.  The packaged installers
in ``installer/{windows,macos,linux}/`` ultimately call into this
same module.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

SERVICE_NAME = "FPulse"
DISPLAY_NAME = "F-Pulse OSS"
DESCRIPTION = (
    "F-Pulse OSS by Hybridyn — local-first data pipeline orchestrator. "
    "Auto-starts in the background and restarts on crash."
)


# ──────────────────────────────────────────────────────────────────────
# Public API — called from fpulse/cli/__init__.py
# ──────────────────────────────────────────────────────────────────────


def cmd_install(args) -> int:
    """``fpulse install-service`` — register the OS-native supervisor."""
    os_name = platform.system()
    data_dir = args.data_dir or os.environ.get("FPULSE_DATA_DIR") or _default_data_dir()
    port = args.port or 8001
    at_boot = bool(getattr(args, "at_boot", False))
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    print()
    print(f"  Installing F-Pulse as a {os_name} service ...")
    print(f"  Python   : {sys.executable}")
    print(f"  Data dir : {data_dir}")
    print(f"  Port     : {port}")
    print(f"  Trigger  : {'system boot (runs as SYSTEM, no login)' if at_boot else 'user logon'}")
    print()
    if os_name == "Windows":
        return _install_windows(data_dir=data_dir, port=port, at_boot=at_boot)
    if at_boot:
        # ONSTART-style "before login" needs a ROOT daemon on macOS/Linux
        # (LaunchDaemon / system systemd unit), which requires sudo and a
        # service account. Out of scope for the user-level installer here —
        # be honest and fall back to the login-scoped agent rather than
        # silently pretend it's boot-time.
        print("  [NOTE] --at-boot is Windows-only for now. On this OS the "
              "service installs at user login; for true boot-time, run the "
              "server under a root systemd/launchd unit (see docs/deployment.md).")
    if os_name == "Darwin":
        return _install_macos(data_dir=data_dir, port=port)
    if os_name == "Linux":
        return _install_linux(data_dir=data_dir, port=port)
    print(f"  ERROR: unsupported OS {os_name!r}.")
    return 2


def cmd_uninstall(args) -> int:
    """``fpulse uninstall-service`` — remove the supervisor.

    Does NOT touch data dir, venv, or project files.
    """
    os_name = platform.system()
    print()
    print(f"  Uninstalling F-Pulse {os_name} service ...")
    if os_name == "Windows":
        return _uninstall_windows()
    if os_name == "Darwin":
        return _uninstall_macos()
    if os_name == "Linux":
        return _uninstall_linux()
    print(f"  ERROR: unsupported OS {os_name!r}.")
    return 2


def cmd_status(args) -> int:
    """``fpulse service-status`` — show whether the supervisor is running."""
    os_name = platform.system()
    if os_name == "Windows":
        return _status_windows()
    if os_name == "Darwin":
        return _status_macos()
    if os_name == "Linux":
        return _status_linux()
    print(f"  Unsupported OS {os_name!r}.")
    return 2


# ──────────────────────────────────────────────────────────────────────
# Helpers — shared
# ──────────────────────────────────────────────────────────────────────


def _default_data_dir() -> str:
    """Per-OS reasonable default for ``FPULSE_DATA_DIR``."""
    if platform.system() == "Windows":
        return str(Path.home() / "AppData" / "Local" / "FPulse" / "data")
    if platform.system() == "Darwin":
        return str(Path.home() / "Library" / "Application Support" / "FPulse")
    # Linux + everything else: XDG-friendly
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return str(base / "fpulse")


def _python_exe() -> str:
    """The python interpreter to bake into the service. Always uses
    the one this CLI is currently running under, so the service runs
    against the same venv the user installed `fpulse` into."""
    return sys.executable


def _service_python(backend_dir: Path) -> str:
    """Interpreter to bake into the service. Prefer a project venv (which has
    F-Pulse's deps installed) over whatever interpreter ran the CLI — users
    often invoke `python -m fpulse` from a GLOBAL Python that doesn't have
    fastapi/duckdb/etc., which would make the service start and immediately
    crash on import."""
    win = platform.system() == "Windows"
    rel = ("Scripts", "python.exe") if win else ("bin", "python")
    for base in (backend_dir / ".venv", backend_dir.parent / ".venv"):
        cand = base.joinpath(*rel)
        if cand.exists():
            return str(cand)
    return _python_exe()


# ──────────────────────────────────────────────────────────────────────
# Windows — Scheduled Task at logon (no NSSM, no pywin32)
# ──────────────────────────────────────────────────────────────────────


def _install_windows(*, data_dir: str, port: int, at_boot: bool = False) -> int:
    # Use schtasks /Create — works on every supported Windows since 7.
    # The `fpulse` package lives in the backend/ source dir; a Scheduled
    # Task runs with cwd=System32, so we bake that dir onto PYTHONPATH below.
    backend_dir = Path(__file__).resolve().parents[2]
    python = _service_python(backend_dir)
    # 2026-06-02: respect the same loopback-default policy as the OSS
    # local launcher. Service installs go to long-running deployments,
    # so the operator may genuinely want LAN binding — but they have to
    # opt in EXPLICITLY rather than getting it as a silent default.
    # See docs/install/security-hardening.md for the rationale.
    bind_host = (
        os.environ.get("FPULSE_BIND_HOST", "").strip()
        or ("0.0.0.0" if os.environ.get("FPULSE_ALLOW_LAN", "").strip() in
            {"1", "true", "yes", "on"} else "127.0.0.1")
    )
    # Safe-by-default: a LAN-visible service runs with SECURITY_MODE=server
    # (auth required, no anonymous workspace fallback) unless the operator
    # explicitly opts back to 'local'. Loopback installs stay 'local'.
    security_mode = os.environ.get("FPULSE_SECURITY_MODE", "").strip().lower()
    if not security_mode:
        security_mode = "local" if bind_host == "127.0.0.1" else "server"
    if bind_host != "127.0.0.1":
        print(
            f"  [INFO] Service will bind to {bind_host} (LAN-visible), so it "
            f"runs with FPULSE_SECURITY_MODE={security_mode} "
            "(authentication required, no anonymous access). Set "
            "FPULSE_SECURITY_MODE=local to override, or FPULSE_BIND_HOST="
            "127.0.0.1 to restrict to loopback."
        )
    args = [
        "-m", "uvicorn", "fpulse.main:app",
        "--host", bind_host, "--port", str(port),
    ]
    # schtasks caps /TR at 261 characters, so we do NOT inline the full
    # command (env exports + venv python path + uvicorn args overflow it).
    # Instead write a tiny launcher script to the data dir and point the
    # task at its short path. The launcher:
    #   * sets PYTHONPATH (backend src) so `uvicorn fpulse.main:app` imports
    #     regardless of the task's cwd (System32 for a SYSTEM task);
    #   * sets FPULSE_DATA_DIR so the server uses the configured data dir;
    #   * redirects all output to a log file so a boot failure is debuggable.
    os.makedirs(data_dir, exist_ok=True)
    log_dir = os.path.join(data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "service.log")
    launcher = os.path.join(data_dir, "fpulse-service.ps1")
    launcher_body = (
        "# Auto-generated by `fpulse install-service`. Do not edit by hand.\n"
        f"$env:FPULSE_DATA_DIR = '{data_dir}'\n"
        f"$env:PYTHONPATH = '{backend_dir}'\n"
        f"$env:FPULSE_SECURITY_MODE = '{security_mode}'\n"
        f"& '{python}' {' '.join(args)} *> '{log_path}'\n"
    )
    Path(launcher).write_text(launcher_body, encoding="utf-8")
    print(f"  Service runs : {python}")
    print(f"  Launcher     : {launcher}")
    print(f"  Log          : {log_path}")

    # Register via PowerShell's ScheduledTasks module rather than raw
    # `schtasks /Create`. The schtasks defaults are WRONG for an always-on
    # service:
    #   * DisallowStartIfOnBatteries=true → on a laptop the task is parked in
    #     "Queued" and NEVER runs until AC power. We disable that.
    #   * ExecutionTimeLimit=72h → it would kill the server after 3 days.
    #     We set it to unlimited (PT0S).
    #   * /Change flags can't express real restart-on-crash. We set a proper
    #     RestartInterval/RestartCount, plus IgnoreNew so repeated /Run calls
    #     don't pile up in the queue.
    launcher_arg = (
        f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{launcher}"'
    )
    if at_boot:
        # SYSTEM account + boot trigger → up before/without any login.
        trigger_line = "$trigger = New-ScheduledTaskTrigger -AtStartup"
        principal_line = (
            "$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest"
        )
    else:
        # Current user + logon trigger → starts in the user's session.
        trigger_line = "$trigger = New-ScheduledTaskTrigger -AtLogOn"
        principal_line = (
            '$principal = New-ScheduledTaskPrincipal '
            '-UserId "$env:USERDOMAIN\\$env:USERNAME" -LogonType Interactive -RunLevel Highest'
        )
    register_body = (
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        f"  $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '{launcher_arg}'\n"
        f"  {trigger_line}\n"
        f"  {principal_line}\n"
        "  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries"
        " -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew"
        " -ExecutionTimeLimit ([TimeSpan]::Zero)"
        " -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 9999\n"
        f"  Register-ScheduledTask -TaskName '{SERVICE_NAME}' -Action $action"
        " -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null\n"
        "  Write-Output 'REGISTER_OK'\n"
        "} catch {\n"
        "  Write-Output ('REGISTER_FAIL: ' + $_.Exception.Message)\n"
        "  exit 1\n"
        "}\n"
    )
    register_ps = os.path.join(data_dir, "fpulse-register.ps1")
    Path(register_ps).write_text(register_body, encoding="utf-8")

    create = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", register_ps],
        capture_output=True, text=True, check=False,
    )
    out = ((create.stdout or "") + (create.stderr or "")).strip()
    if create.returncode != 0 or "REGISTER_OK" not in out:
        print("  ERROR: registering the scheduled task failed:")
        print("  " + (out or "(no output)"))
        print()
        low = out.lower()
        if any(s in low for s in ("access", "denied", "administrator", "privilege")):
            print("  This means the terminal isn't elevated.")
            if at_boot:
                print("  --at-boot (SYSTEM) ALWAYS needs Administrator.")
            print("  Right-click your terminal -> Run as Administrator, then retry.")
        else:
            print("  (Not an elevation issue — see the message above.)")
        return create.returncode or 1

    print(f"  Registered Scheduled Task '{SERVICE_NAME}'.")
    if at_boot:
        print(f"  Auto-starts at system boot as SYSTEM (runs without anyone logged in).")
    else:
        print(f"  Auto-starts at every Windows logon (your user session).")
    print(f"  Runs on battery too; no time limit; restarts ~1 min after a crash.")
    print()
    print(f"  Start now:")
    print(f"    schtasks /Run /TN {SERVICE_NAME}")
    print(f"  Stop:")
    print(f"    schtasks /End /TN {SERVICE_NAME}")
    print(f"  Uninstall:")
    print(f"    fpulse uninstall-service")
    print()
    print(f"  App URL after start: http://localhost:{port}")
    print(f"  If it doesn't come up, check the log: {log_path}")
    print()
    return 0


def _uninstall_windows() -> int:
    res = subprocess.run(
        ["schtasks", "/Delete", "/TN", SERVICE_NAME, "/F"],
        capture_output=True, text=True, check=False,
    )
    if res.returncode == 0:
        print(f"  Removed Scheduled Task '{SERVICE_NAME}'.")
        return 0
    out = (res.stderr or res.stdout).strip().lower()
    if "cannot find" in out or "does not exist" in out:
        print(f"  Task '{SERVICE_NAME}' is not registered. Nothing to do.")
        return 0
    print(f"  ERROR: schtasks /Delete failed: {res.stderr.strip()}")
    return res.returncode


def _status_windows() -> int:
    res = subprocess.run(
        ["schtasks", "/Query", "/TN", SERVICE_NAME, "/FO", "LIST", "/V"],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        print(f"  Task '{SERVICE_NAME}' is not registered.")
        return 1
    print(res.stdout)
    return 0


# ──────────────────────────────────────────────────────────────────────
# macOS — LaunchAgent under ~/Library/LaunchAgents
# ──────────────────────────────────────────────────────────────────────


_MACOS_PLIST_LABEL = "com.hybridyn.fpulse"


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_MACOS_PLIST_LABEL}.plist"


def _install_macos(*, data_dir: str, port: int) -> int:
    python = _python_exe()
    plist = _macos_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    log_dir = Path.home() / "Library" / "Logs" / "FPulse"
    log_dir.mkdir(parents=True, exist_ok=True)

    # 2026-06-02 hardening: respect FPULSE_BIND_HOST / FPULSE_ALLOW_LAN
    # at install time. The plist is written once; if the operator wants
    # to flip LAN binding later they re-run `fpulse service install`.
    bind_host = (
        os.environ.get("FPULSE_BIND_HOST", "").strip()
        or ("0.0.0.0" if os.environ.get("FPULSE_ALLOW_LAN", "").strip() in
            {"1", "true", "yes", "on"} else "127.0.0.1")
    )
    if bind_host != "127.0.0.1":
        print(
            f"  [INFO] launchd service will bind to {bind_host} (LAN-visible)."
        )

    # Indentation matters in plist XML — keep it tight.
    plist_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{_MACOS_PLIST_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>fpulse.main:app</string>
    <string>--host</string>
    <string>{bind_host}</string>
    <string>--port</string>
    <string>{port}</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>FPULSE_DATA_DIR</key>
    <string>{data_dir}</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>ThrottleInterval</key>
  <integer>5</integer>

  <key>StandardOutPath</key>
  <string>{log_dir}/fpulse.out.log</string>

  <key>StandardErrorPath</key>
  <string>{log_dir}/fpulse.err.log</string>
</dict>
</plist>
"""
    plist.write_text(plist_xml, encoding="utf-8")

    # Reload — unload first in case an old version is there.
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, check=False)
    res = subprocess.run(["launchctl", "load", str(plist)],
                         capture_output=True, text=True, check=False)
    if res.returncode != 0:
        print(f"  ERROR: launchctl load failed: {res.stderr.strip()}")
        return res.returncode

    print(f"  Installed LaunchAgent at {plist}")
    print(f"  Auto-starts at every login (KeepAlive=true keeps it up).")
    print(f"  Logs: {log_dir}/fpulse.{{out,err}}.log")
    print()
    print(f"  Stop:        launchctl stop  {_MACOS_PLIST_LABEL}")
    print(f"  Start:       launchctl start {_MACOS_PLIST_LABEL}")
    print(f"  Uninstall:   fpulse uninstall-service")
    print()
    print(f"  App URL: http://localhost:{port}")
    print()
    return 0


def _uninstall_macos() -> int:
    plist = _macos_plist_path()
    if not plist.exists():
        print(f"  LaunchAgent '{_MACOS_PLIST_LABEL}' not present. Nothing to do.")
        return 0
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, check=False)
    plist.unlink(missing_ok=True)
    print(f"  Removed LaunchAgent and unloaded.")
    return 0


def _status_macos() -> int:
    res = subprocess.run(["launchctl", "list", _MACOS_PLIST_LABEL],
                         capture_output=True, text=True, check=False)
    if res.returncode != 0:
        print(f"  LaunchAgent '{_MACOS_PLIST_LABEL}' is not loaded.")
        return 1
    print(res.stdout)
    return 0


# ──────────────────────────────────────────────────────────────────────
# Linux — user-mode systemd unit
# ──────────────────────────────────────────────────────────────────────


_LINUX_UNIT_NAME = "fpulse.service"


def _linux_unit_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "systemd" / "user" / _LINUX_UNIT_NAME


def _install_linux(*, data_dir: str, port: int) -> int:
    if not shutil.which("systemctl"):
        print("  ERROR: systemctl not found. This OS doesn't appear to use systemd.")
        print("  For non-systemd Linux, write your own init script invoking:")
        print(f"    {_python_exe()} -m uvicorn fpulse.main:app --host 127.0.0.1 --port {port}")
        print("  (Use --host 0.0.0.0 only if LAN exposure is intentional.)")
        return 2

    python = _python_exe()
    unit_path = _linux_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)

    # 2026-06-02 hardening: respect FPULSE_BIND_HOST / FPULSE_ALLOW_LAN
    # at install time. Operator can edit the unit later if they need to
    # switch — re-running `fpulse install-service` regenerates it.
    bind_host = (
        os.environ.get("FPULSE_BIND_HOST", "").strip()
        or ("0.0.0.0" if os.environ.get("FPULSE_ALLOW_LAN", "").strip() in
            {"1", "true", "yes", "on"} else "127.0.0.1")
    )
    if bind_host != "127.0.0.1":
        print(f"  [INFO] systemd unit will bind to {bind_host} (LAN-visible).")

    unit = f"""# F-Pulse OSS — user-mode systemd unit
# Generated by `fpulse install-service`. Safe to edit, but re-running
# the CLI will overwrite this file.

[Unit]
Description={DISPLAY_NAME}
After=network.target

[Service]
Type=simple
Environment=FPULSE_DATA_DIR={data_dir}
ExecStart={python} -m uvicorn fpulse.main:app --host {bind_host} --port {port}
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
"""
    unit_path.write_text(unit, encoding="utf-8")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    res = subprocess.run(
        ["systemctl", "--user", "enable", "--now", _LINUX_UNIT_NAME],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        print(f"  ERROR: systemctl enable --now failed:")
        print("  " + (res.stderr or res.stdout).strip())
        return res.returncode

    print(f"  Installed user-mode unit at {unit_path}")
    print(f"  Enabled + started.")
    print()
    print(f"  Stop:        systemctl --user stop   {_LINUX_UNIT_NAME}")
    print(f"  Start:       systemctl --user start  {_LINUX_UNIT_NAME}")
    print(f"  Logs:        journalctl --user -u {_LINUX_UNIT_NAME} -f")
    print(f"  Uninstall:   fpulse uninstall-service")
    print()
    print(f"  By default a user-mode unit stops when you log out.")
    print(f"  To keep it running after logout:")
    print(f"    sudo loginctl enable-linger $USER")
    print()
    print(f"  App URL: http://localhost:{port}")
    print()
    return 0


def _uninstall_linux() -> int:
    unit_path = _linux_unit_path()
    if not unit_path.exists():
        print(f"  Unit '{_LINUX_UNIT_NAME}' not present. Nothing to do.")
        return 0
    subprocess.run(["systemctl", "--user", "disable", "--now", _LINUX_UNIT_NAME],
                   capture_output=True, check=False)
    unit_path.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"  Removed user-mode unit and disabled.")
    return 0


def _status_linux() -> int:
    res = subprocess.run(
        ["systemctl", "--user", "status", _LINUX_UNIT_NAME],
        capture_output=True, text=True, check=False,
    )
    print(res.stdout or res.stderr)
    return 0 if res.returncode == 0 else res.returncode
