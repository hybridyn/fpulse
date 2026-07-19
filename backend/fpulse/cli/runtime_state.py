"""Runtime ownership state for the F-Pulse local launcher (2026-06-07).

Python sibling of ``launcher/launcher-utils.ps1`` — same JSON schema,
same three-signal ownership check, so the dev-mode PowerShell launcher
(``start.ps1`` / ``stop.ps1``) and the packaged ``fpulse open`` /
``fpulse stop`` CLIs can read each other's files when both ship in the
same checkout.

# Why this file exists

The reviewer audit (2026-06-07) flagged "deployment story still not
clean" with a specific concern: ``fpulse open`` could find a free port
and start a backend, but had no record of WHICH backend it started. The
default suggestion in ``find_free_port``'s error path was to run
``taskkill /F /IM python.exe`` — which would kill EVERY python process
on the machine, not just the F-Pulse orphan. That's the exact failure
mode the PowerShell launcher's ownership-file work was designed to
prevent.

# The three signals (mirrors stop.ps1)

A process is stopped only if ALL three hold:

  1. PID was recorded in ``.fpulse/runtime/instance.json``
  2. PID is currently listening on the port we recorded for it
  3. PID's command line still matches the uvicorn-fpulse signature

Single signal alone (PID match, port match, cmdline match) never
authorizes a kill. This survives PID recycling: if our recorded PID
died and the OS reused that number for an unrelated process, the
port-listening check rejects it.

# Storage location

``<cwd>/.fpulse/runtime/instance.json``

Cwd-relative on purpose:
  * In a dev checkout, this matches what ``start.ps1`` writes — the two
    launchers don't fight over different files.
  * In a packaged install, the operator chooses where to ``cd`` before
    running ``fpulse open``, so the runtime file lives next to their
    working dir (intuitive) rather than in some hidden user-data area.
  * ``.gitignore`` already excludes ``.fpulse/runtime/`` so the file
    never accidentally gets committed.
"""
from __future__ import annotations

import json
import os
import platform
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


_FILE_LOCK = threading.Lock()
_RUNTIME_DIR_NAME = ".fpulse/runtime"
_RUNTIME_FILE_NAME = "instance.json"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def runtime_dir(cwd: Path | None = None) -> Path:
    """Resolve the runtime directory (``<cwd>/.fpulse/runtime``).

    cwd defaults to ``Path.cwd()`` — pass a path explicitly only in
    tests to isolate per-test state."""
    base = Path(cwd) if cwd is not None else Path.cwd()
    return base / _RUNTIME_DIR_NAME


def runtime_file(cwd: Path | None = None) -> Path:
    return runtime_dir(cwd) / _RUNTIME_FILE_NAME


# ── Schema ───────────────────────────────────────────────────────────


class RuntimeInstance(BaseModel):
    """One running F-Pulse launch.

    Schema is intentionally compatible with what ``start.ps1`` writes
    so ``fpulse stop`` can clean up a PowerShell-launched instance and
    vice-versa. New fields can be added without breaking the older
    launcher (model_validate ignores unknown fields when populated via
    ConfigDict.extra='ignore', and Pydantic 2 defaults to that).

    For the packaged ``fpulse open`` path, frontend_* and backend_*
    refer to the SAME process / port — uvicorn serves both API and
    static frontend from one bind. In dev split-launcher mode
    (start.ps1), they're separate PIDs.
    """

    schema_version: int = 1
    instance_id: str
    frontend_port: int
    backend_port: int
    frontend_pid: int = 0
    backend_pid: int = 0
    cwd: str = ""
    started_at: str = Field(default_factory=_iso_now)
    pid_owner: int = 0  # the PID that owns this record (the launcher itself)
    # 2026-06-07: distinguishes the packaged single-process flow ("open")
    # from the dev split-process flow ("dev-script"). stop.ps1 / fpulse stop
    # both honor it but treat ports the same way.
    mode: str = "open"  # open | dev-script


# ── I/O ──────────────────────────────────────────────────────────────


def read_runtime(cwd: Path | None = None) -> RuntimeInstance | None:
    """Load the current runtime file. None if missing or corrupt."""
    path = runtime_file(cwd)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        return RuntimeInstance.model_validate_json(raw)
    except Exception:
        return None


def write_runtime(instance: RuntimeInstance, cwd: Path | None = None) -> Path:
    """Atomic-ish write — temp file then replace, matching the PS launcher."""
    path = runtime_file(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = instance.model_dump(mode="json")
    with _FILE_LOCK:
        with tmp.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    return path


def remove_runtime(cwd: Path | None = None) -> bool:
    """Delete the runtime file. Returns True if a file was removed."""
    path = runtime_file(cwd)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except Exception:
        return False


# ── Ownership check (the three signals) ─────────────────────────────


# Per-kind cmdline signatures - keep narrow.
#   backend  : require BOTH "uvicorn" AND "fpulse" in cmdline (specific
#              to us; "uvicorn" alone matches every other uvicorn user
#              on the machine).
#   frontend : "vite" / "npm" / "node" alone are too generic - we ALSO
#              require the recorded cwd path to appear somewhere in
#              the cmdline (catches the case where vite was launched
#              from inside this repo's frontend/ dir).
def _cmdline_matches(kind: str, cmdline: list[str] | None, cwd_marker: str) -> bool:
    if not cmdline:
        return False
    joined = " ".join(str(c) for c in cmdline).lower()
    if kind == "backend":
        return ("uvicorn" in joined) and ("fpulse" in joined)
    if kind == "frontend":
        if cwd_marker and cwd_marker.lower().replace("\\", "/") not in joined.replace("\\", "/"):
            return False
        return ("vite" in joined) or ("npm" in joined) or ("node" in joined)
    return False


def _port_holder_pid(port: int) -> int:
    """Return the PID listening on ``port`` on loopback, or 0 if none.

    Uses psutil so we get the same answer cross-platform (Windows lacks
    netstat parity with Linux netstat). psutil is already a project
    dependency — see backend/requirements.txt."""
    try:
        import psutil  # type: ignore
    except Exception:
        return 0
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status != psutil.CONN_LISTEN:
                continue
            laddr = getattr(conn, "laddr", None)
            if not laddr:
                continue
            # laddr is either (ip, port) namedtuple or addr object
            lport = getattr(laddr, "port", None) or (laddr[1] if isinstance(laddr, tuple) else None)
            if lport == port:
                return int(conn.pid or 0)
    except Exception:
        return 0
    return 0


def is_owned_fpulse(pid: int, expected_port: int, kind: str,
                     cwd_marker: str = "") -> bool:
    """The three-signal ownership check.

    Returns True ONLY if every signal agrees:
      (1) PID is alive
      (2) PID is currently listening on expected_port
      (3) PID's cmdline (or any ancestor within 5 hops) matches the
          kind+repo signature

    Walks the parent chain because the listener is often a leaf node
    of a launcher tree — for "open" mode the python.exe IS the leaf,
    for dev-script frontend a node.exe sits below a npm/cmd ancestor."""
    if pid <= 0:
        return False

    try:
        import psutil  # type: ignore
    except Exception:
        return False

    # Signal 1: PID alive?
    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            return False
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        return False

    # Signal 2: PID listening on expected_port?
    if _port_holder_pid(expected_port) != pid:
        return False

    # Signal 3: cmdline (or ancestor's cmdline within 5 hops) matches.
    current = proc
    for _ in range(5):
        try:
            cmdline = current.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            break
        except Exception:
            break
        if _cmdline_matches(kind, cmdline, cwd_marker):
            return True
        try:
            parent = current.parent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            parent = None
        except Exception:
            parent = None
        if parent is None:
            break
        current = parent
    return False


def stop_owned_process(pid: int, expected_port: int, kind: str,
                        cwd_marker: str = "", timeout: float = 5.0) -> bool:
    """Stop a process if and only if is_owned_fpulse confirms ownership.

    Tries a graceful terminate first, escalates to kill if the process
    doesn't exit within ``timeout``. Returns True on success, False if
    the ownership check rejected the kill.
    """
    if not is_owned_fpulse(pid, expected_port, kind, cwd_marker):
        return False
    try:
        import psutil  # type: ignore
        proc = psutil.Process(pid)
    except Exception:
        return False
    try:
        # Reap descendants first so they don't get orphaned to PID 1.
        for child in proc.children(recursive=True):
            try:
                child.terminate()
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except Exception:
            proc.kill()
        return True
    except Exception:
        return False


# ── Convenience builders ────────────────────────────────────────────


def make_open_instance(*, host: str, port: int, pid: int,
                        instance_id: str | None = None) -> RuntimeInstance:
    """Construct the RuntimeInstance record for a ``fpulse open`` launch.

    Single-process mode — frontend_* and backend_* are the same. Useful
    for tests and for the open command itself to keep the field-set in
    one place."""
    if instance_id is None:
        instance_id = "fpulse-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return RuntimeInstance(
        schema_version=1,
        instance_id=instance_id,
        frontend_port=port,
        backend_port=port,
        frontend_pid=pid,
        backend_pid=pid,
        cwd=str(Path.cwd()),
        started_at=_iso_now(),
        pid_owner=os.getpid(),
        mode="open",
    )
