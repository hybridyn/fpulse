"""Local app launcher for F-Pulse OSS (2026-06-02).

Goal: ``fpulse open`` (or ``fpulse serve --open``) starts the backend,
finds a free port if the default is in use, and opens the default
browser to the local URL. Designed to address the 4 launcher-design
gotchas surfaced in the pre-launch review:

  1. **Port conflict** — if 8001 is taken (e.g. orphan backend from a
     previous run, another process), increment up to 10 times and try
     a fallback port instead of crashing with EADDRINUSE.

  2. **Headless / virtualised environments** — `webbrowser.open()` is
     notoriously flaky in WSL2, Docker containers, DevContainers, and
     remote SSH sessions. We detect those upfront, skip the auto-open,
     and print the URL prominently so the operator can paste it into
     a real browser on their host.

  3. **Token-in-URL leak** — by design we DO NOT pass a session token
     in the query string. The 2026-06-02 hardening (loopback bind +
     Host allowlist + Origin pinning) already provides the defense
     the token was meant to add. If a token is added later, it goes
     in a header, not the URL.

  4. **Graceful shutdown** — separate `local_hardening.shutdown` route
     handles the actual server termination when the frontend signals
     "user closed last tab." This module just orchestrates startup.

Frame this for users as a **local app launcher** — not a desktop app.
The OSS 1.0 promise is "one command, no manual URL typing"; full
native desktop is on the 1.1 roadmap (see docs/roadmap/oss-1-1.md).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from typing import Optional

# Number of consecutive ports to try if the starting one is in use.
# 10 is conservative — covers "I forgot to kill the previous instance"
# without silently grabbing a port a thousand miles from where the
# operator expected.
_PORT_FALLBACK_ATTEMPTS = 10


def find_free_port(start_port: int, host: str = "127.0.0.1",
                   max_attempts: int = _PORT_FALLBACK_ATTEMPTS) -> int:
    """Return the first available port >= start_port on the given host.

    Tries start_port, start_port+1, ... up to max_attempts. Raises
    RuntimeError with a clear actionable message if none are free.

    Why not just bind 0 and let the kernel pick? Because operators
    expect a stable port (links / bookmarks / docs reference 8001).
    The fallback only kicks in when the default is taken, and we
    print the chosen port loudly so the operator notices.
    """
    last_err: Optional[Exception] = None
    for offset in range(max_attempts):
        port = start_port + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                # SO_REUSEADDR not set on purpose — we want the bind
                # to FAIL if anything else holds the port (including
                # a TIME_WAIT remnant of a previous F-Pulse process),
                # so we don't accidentally clash a second later when
                # uvicorn actually opens the socket.
                sock.bind((host, port))
                return port
        except OSError as exc:
            last_err = exc
            continue
    # 2026-06-07 hardening - the previous message advised
    # `taskkill /F /IM python.exe` / `pkill -f uvicorn` which would
    # kill EVERY python / uvicorn process on the machine, not just the
    # F-Pulse orphan. That's exactly the failure mode the runtime
    # ownership-file work was built to prevent. New advice points at
    # `fpulse stop` (which applies the 3-signal ownership check) or
    # an explicit --port override.
    raise RuntimeError(
        f"No free port found in range {start_port}..{start_port + max_attempts - 1} "
        f"on {host}. Last error: {last_err}.\n"
        f"  - If a previous F-Pulse instance is still running, stop it cleanly:\n"
        f"      fpulse stop\n"
        f"  - Or relocate this run to a different port:\n"
        f"      fpulse open --port <N>\n"
        f"  - We DELIBERATELY do not suggest a blanket kill (e.g. `taskkill /F /IM python.exe`)\n"
        f"    because that would terminate every python process on this machine, not just F-Pulse."
    )


def is_headless() -> tuple[bool, str]:
    """Detect environments where webbrowser.open() will hang or fail.

    Returns (is_headless, reason). When True, the caller should skip
    auto-launch and just print the URL prominently.

    Detection rules (any one match → headless):
      - Linux without a DISPLAY / WAYLAND_DISPLAY env var
      - SSH session (SSH_CONNECTION env var set)
      - Running inside a Docker container (/.dockerenv file)
      - WSL (WSL_DISTRO_NAME env var set) — `webbrowser` on WSL2 can
        work if wslview is installed, but it's unreliable; safer to
        print the URL and let the operator paste it into the Windows
        host browser.
      - `--no-open` was passed (handled by the caller, not here)

    macOS and Windows desktop sessions are NOT headless by these rules.
    """
    if os.environ.get("SSH_CONNECTION"):
        return True, "SSH session (SSH_CONNECTION env var present)"
    if os.path.exists("/.dockerenv"):
        return True, "Docker container (/.dockerenv present)"
    if os.environ.get("WSL_DISTRO_NAME"):
        return True, "WSL session (WSL_DISTRO_NAME env var present)"
    # Linux GUI check — DISPLAY (X11) or WAYLAND_DISPLAY must be set
    if sys.platform.startswith("linux"):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return True, "Linux without DISPLAY/WAYLAND_DISPLAY"
    return False, ""


def _print_url_banner(url: str, was_opened: bool, headless_reason: str = "") -> None:
    """Print the launch URL with clear visual emphasis.

    Even when auto-open succeeded, we print the URL so the operator
    can copy it into a different browser, share it with a colleague
    they screen-share with, or bookmark it.
    """
    rule = "─" * 60
    print()
    print(rule)
    print("  F-Pulse OSS is running locally")
    print(rule)
    print(f"  URL:        {url}")
    if was_opened:
        print(f"  Status:     opened in your default browser")
    elif headless_reason:
        print(f"  Status:     auto-open skipped — {headless_reason}")
        print(f"              copy the URL above into a browser on your host machine")
    else:
        print(f"  Status:     auto-open disabled (--no-open)")
    print(f"  Stop:       press Ctrl+C")
    print(rule)
    print()


def launch_browser_if_possible(url: str, *, force_no_open: bool = False) -> None:
    """Open the user's default browser to the local URL, with safety net.

    Order of operations:
      1. If force_no_open=True (CLI --no-open), skip entirely.
      2. If headless environment detected, skip with a clear reason.
      3. Try webbrowser.open(url, new=2). If it returns False or
         raises, print a fallback message.

    The URL banner is always printed AFTER the open attempt so the
    operator sees the outcome plainly.
    """
    if force_no_open:
        _print_url_banner(url, was_opened=False)
        return

    headless, reason = is_headless()
    if headless:
        _print_url_banner(url, was_opened=False, headless_reason=reason)
        return

    opened = False
    try:
        # new=2 → open in a new tab if possible (better UX than
        # hijacking an existing tab). webbrowser.open returns False
        # if it tried but couldn't find a browser; some platforms
        # throw instead.
        opened = bool(webbrowser.open(url, new=2))
    except Exception as exc:
        # Don't crash the launcher just because the browser-open
        # path tripped. The URL is still printed below so the
        # operator can do it manually.
        print(f"  [note] auto-open failed ({type(exc).__name__}: {exc}); "
              f"copy the URL below manually.")

    _print_url_banner(url, was_opened=opened,
                      headless_reason="" if opened else
                      "webbrowser.open returned no handler")


# ──────────────────────────────────────────────────────────────────────
# App-mode window (2026-06-18) — make the installed product feel like an
# app, not a browser tab. A Chromium-family browser launched with
# `--app=<url>` opens a chromeless window (no tabs, no address bar) with
# its own taskbar entry. Edge ships on every Windows 11, so this almost
# always resolves. Falls back to a normal browser tab. This does NOT start
# a server — the background service owns that; we only open the window.
# (A true native shell — Tauri/pywebview — is the heavier 1.1 follow-up.)
# ──────────────────────────────────────────────────────────────────────


def _find_app_browser() -> Optional[str]:
    """Locate a Chromium-family browser that supports ``--app=``.

    Returns the executable path, or None if only non-Chromium browsers are
    available (in which case the caller falls back to a normal tab).
    """
    candidates: list[str] = []
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        lad = os.environ.get("LOCALAPPDATA", "")
        candidates += [
            os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(lad, "Google", "Chrome", "Application", "chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    # PATH lookups cover Linux + anything installed on PATH on Win/mac.
    for name in ("msedge", "microsoft-edge", "google-chrome", "chrome",
                 "chromium", "chromium-browser", "brave", "brave-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for exe in candidates:
        if exe and os.path.exists(exe):
            return exe
    return None


def _app_profile_dir() -> str:
    """Dedicated browser-profile dir for the F-Pulse app window.

    Using a separate ``--user-data-dir`` is what makes the window obey
    --start-maximized: a plain --app launch reuses an already-running
    browser process (which ignores geometry flags). It also isolates the
    app from the user's normal browsing — own session, no extensions.
    """
    base = os.environ.get("FPULSE_DATA_DIR")
    if not base:
        if sys.platform == "win32":
            base = os.path.join(os.environ.get("LOCALAPPDATA",
                                               os.path.expanduser("~")), "FPulse")
        elif sys.platform == "darwin":
            base = os.path.expanduser("~/Library/Application Support/FPulse")
        else:
            base = os.path.expanduser("~/.fpulse")
    return os.path.join(base, "app-browser")


def wait_for_server(url: str, timeout_s: float = 15.0) -> bool:
    """Poll ``url`` until it answers (any HTTP status) or timeout elapses.

    Used before opening an app window so we don't flash a connection-error
    page while the background service is still starting (e.g. at logon). A
    4xx/5xx still means "server is up", so those count as reachable.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1.5)
            return True
        except urllib.error.HTTPError:
            return True  # server answered (401/403/etc.) — it's up
        except Exception:
            time.sleep(0.4)
    return False


def open_app_window(url: str, *, force_no_open: bool = False) -> bool:
    """Open ``url`` in a chromeless app window; fall back to a browser tab.

    Returns True iff a Chromium ``--app`` window was launched. Never starts a
    server. Honors --no-open and headless detection like the tab launcher.
    """
    if force_no_open:
        _print_url_banner(url, was_opened=False)
        return False
    headless, reason = is_headless()
    if headless:
        _print_url_banner(url, was_opened=False, headless_reason=reason)
        return False
    exe = _find_app_browser()
    if exe:
        try:
            # A dedicated --user-data-dir is ESSENTIAL: if the browser is
            # already running (it usually is), a plain --app launch reuses
            # that process, which ignores geometry flags. A separate profile
            # forces a fresh process AND isolates the app (own session, no
            # extensions). --no-first-run / --no-default-browser-check
            # suppress the new-profile welcome prompts.
            #
            # We deliberately do NOT pass --start-maximized or a fixed
            # --window-size:
            #   * --start-maximized opens a maximized window, which Windows
            #     will NOT let you drag-resize (you'd have to "restore" first)
            #     — users read that as "I can't change the size".
            #   * a fixed --window-size in DIPs overflows small/HiDPI screens.
            # With neither, Chromium opens a NORMAL resizable window sized to
            # the work area, and the profile remembers whatever size the user
            # picks for next time.
            profile = _app_profile_dir()
            os.makedirs(profile, exist_ok=True)
            subprocess.Popen(
                [exe, f"--app={url}", f"--user-data-dir={profile}",
                 "--no-first-run", "--no-default-browser-check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print(f"  Opened F-Pulse in an app window ({os.path.basename(exe)}).")
            return True
        except Exception as exc:
            print(f"  [note] app-window launch failed ({type(exc).__name__}); "
                  f"falling back to a browser tab.")
    # Fallback: ordinary browser tab.
    opened = False
    try:
        opened = bool(webbrowser.open(url, new=2))
    except Exception:
        pass
    _print_url_banner(url, was_opened=opened,
                      headless_reason="" if opened else "no browser handler")
    return False


def _native_window_size() -> tuple[int, int]:
    """A comfortable WINDOWED size — ~66% wide x 70% tall of the screen, so it
    clearly floats as a window rather than filling the display, capped so it
    never gets oversized on big monitors or tiny on small ones. Logical
    pixels (queried before any DPI-awareness call), which is what pywebview
    expects. The window is resizable, so this is only the opening size."""
    try:
        import ctypes
        u = ctypes.windll.user32  # no SetProcessDPIAware → logical pixels
        sw, sh = int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
        if sw > 0 and sh > 0:
            w = max(1000, min(1200, int(sw * 0.72)))
            h = max(620, min(800, int(sh * 0.78)))
            return w, h
    except Exception:
        pass
    return 1100, 720


def open_native_window(url: str) -> bool:
    """Open ``url`` in a REAL native OS window via pywebview (WebView2 on
    Windows). A genuine, resizable, centered window sized to fit the screen —
    no browser chrome, no Chromium geometry quirks (the maximize-can't-resize
    and DPI-overflow problems that plague the --app approach). Blocks until
    the window is closed, which is correct for an app launcher.

    Returns True if the native window ran; False to fall back to a browser.
    """
    try:
        import webview
    except Exception:
        return False
    try:
        w, h = _native_window_size()
        win = webview.create_window(
            "F-Pulse OSS",
            url,
            width=w, height=h,     # centered by default; resizable below
            resizable=True,
            min_size=(1000, 560),
        )
        # Default to 80% page zoom (user preference). On a HiDPI/scaled
        # display this lands the app at a comfortable "normal" size with more
        # content visible. Applied ONCE on load — no dynamic resize-refit
        # (that earlier caused maximized clipping). The window stays resizable
        # and the user can Ctrl +/- from here.
        win.events.loaded += lambda: win.evaluate_js(
            "document.documentElement.style.zoom='0.8'"
        )

        # Persist the session (cookies/localStorage) so the user logs in ONCE,
        # not every launch. private_mode defaults to True (ephemeral); turn it
        # off and give it a stable storage dir next to the data dir.
        storage = os.path.join(os.path.dirname(_app_profile_dir()), "app-webview")
        try:
            os.makedirs(storage, exist_ok=True)
            webview.start(private_mode=False, storage_path=storage)
        except Exception:
            # Some pywebview/backends reject a custom storage_path; fall back
            # to defaults rather than failing to open at all.
            webview.start()
        return True
    except Exception as exc:
        print(f"  [note] native window unavailable ({type(exc).__name__}: {exc}); "
              f"opening a browser window instead.")
        return False
