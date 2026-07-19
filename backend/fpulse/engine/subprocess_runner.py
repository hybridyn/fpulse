"""
SubprocessRunner — spawn + cap + monitor a single child process.

Sprint 2 PR5 step 3 of the locked implementation order. One instance
per live subprocess. Owned by ExecutionManager; the manager registers
the runner in its task registry and removes it when on_exit fires.

Enforcement layers (in priority order):

  1. Linux — `resource.setrlimit(RLIMIT_AS, ...)` applied via
     `preexec_fn`. The child hits its own address-space ceiling and
     dies inside its own process; the parent never sees an OOM.

  2. Cross-platform — a daemon monitor thread polls
     `psutil.Process(pid).memory_info().rss` every `monitor_interval_s`
     seconds and terminates the tree on breach. This is the primary
     mechanism on Windows (no setrlimit) and a belt-and-suspenders on
     Linux (catches cases where setrlimit is bypassed or where the
     child spawns uncapped grandchildren).

  3. Runtime cap — the same monitor thread compares wall-clock elapsed
     against `budget.max_runtime_s` and terminates on expiry.

Memory peak is captured as the rolling max RSS observed by the monitor
thread. On normal exit it reflects the last poll before the process
ended; on budget_memory exit it reflects the breaching poll.

Docker `--memory` is a possible future mechanism once a containerised
deployment ships. This module leaves a hook for that path but does not implement it in
step 3 — OSS tier runs native, and the Plus Docker refactor is a
separate workstream.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger("fpulse.subprocess_runner")

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ── Platform capability flags (computed once at import) ────────────────

_IS_WINDOWS = sys.platform == "win32"

try:
    import resource as _resource  # Unix only
    _HAS_RESOURCE = True
except ImportError:
    _resource = None  # type: ignore[assignment]
    _HAS_RESOURCE = False

try:
    import psutil as _psutil
    _HAS_PSUTIL = True
except ImportError:
    _psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False


# ── Exit reasons (written to executions.exit_reason at step 7) ─────────

EXIT_OK = "ok"
EXIT_BUDGET_MEMORY = "budget_memory"
EXIT_BUDGET_RUNTIME = "budget_runtime"
EXIT_CANCELLED = "cancelled"
EXIT_KILLED_THROTTLE = "killed_throttle"   # reserved for step 4 governor
EXIT_ERROR = "error"


@dataclass
class SubprocessResult:
    """Emitted by the monitor thread once the child has exited or been
    terminated. Consumed by ExecutionManager's on_exit callback."""
    exit_code: int | None
    exit_reason: str
    memory_peak_mb: float | None
    runtime_ms: float
    started_at: datetime
    ended_at: datetime
    pid: int | None
    stdout_log_path: str | None = None
    stderr_log_path: str | None = None


class SubprocessRunner:
    """Spawns a subprocess, caps it, reaps it. Safe to use without
    psutil (in which case memory caps are best-effort via setrlimit
    only, and memory_peak_mb is None)."""

    def __init__(
        self,
        argv: list[str],
        *,
        max_memory_mb: int,
        max_runtime_s: int,
        env: dict[str, str] | None = None,
        on_exit: Callable[[SubprocessResult], None] | None = None,
        monitor_interval_s: float = 1.0,
        log_dir: str | None = None,
        log_name: str | None = None,
    ):
        self._argv = argv
        self._max_memory_mb = max_memory_mb
        self._max_runtime_s = max_runtime_s
        self._env_overlay = env or {}
        self._on_exit = on_exit
        self._monitor_interval_s = max(0.05, monitor_interval_s)
        self._log_dir = log_dir
        self._log_name = log_name or uuid.uuid4().hex[:12]

        self._proc: subprocess.Popen | None = None
        self._monitor_thread: threading.Thread | None = None
        self._started_at: datetime | None = None
        self._memory_peak_bytes: int = 0
        self._exit_reason: str = EXIT_OK
        self._lock = threading.Lock()
        self._finalized = threading.Event()

        # File handles held open for the duration of the subprocess;
        # closed in _finalize. Step 6: keeps child output off the
        # parent's heap by writing to disk directly via the OS.
        self._stdout_file = None
        self._stderr_file = None
        self._stdout_path: str | None = None
        self._stderr_path: str | None = None

    # ── Public API ────────────────────────────────────────────────

    def start(self, on_spawn: Callable[[int], None] | None = None) -> int:
        """Spawn the child and launch the monitor thread. Returns PID.

        The optional on_spawn callback fires between Popen success and
        the monitor thread start — it's the ExecutionManager's window
        to register _by_pid and _subprocess_runners before the monitor
        can fire _on_exit (relevant for very-fast-exit children)."""
        # Step 6: disk-backed stdout/stderr. Opens files before Popen
        # so the child never writes through subprocess.PIPE (which
        # would buffer in parent's memory). Falls back to DEVNULL when
        # no log_dir is configured — output is dropped, not retained.
        stdout_target: Any
        stderr_target: Any
        if self._log_dir is not None:
            os.makedirs(self._log_dir, exist_ok=True)
            safe_name = _SAFE_NAME_RE.sub("_", self._log_name)[:64] or "runner"
            self._stdout_path = os.path.join(self._log_dir, f"{safe_name}.stdout.log")
            self._stderr_path = os.path.join(self._log_dir, f"{safe_name}.stderr.log")
            # line-buffered append in binary mode — cheapest cross-platform
            # capture that survives multiple monitor polls.
            self._stdout_file = open(self._stdout_path, "ab", buffering=0)
            self._stderr_file = open(self._stderr_path, "ab", buffering=0)
            stdout_target = self._stdout_file
            stderr_target = self._stderr_file
        else:
            stdout_target = subprocess.DEVNULL
            stderr_target = subprocess.DEVNULL

        popen_kwargs: dict = {
            "args": self._argv,
            "stdout": stdout_target,
            "stderr": stderr_target,
            "env": {**os.environ, **self._env_overlay},
        }

        if _HAS_RESOURCE:
            limit_bytes = self._max_memory_mb * 1024 * 1024
            popen_kwargs["preexec_fn"] = _make_preexec(limit_bytes)

        self._proc = subprocess.Popen(**popen_kwargs)  # noqa: S603 — argv provided by trusted caller
        self._started_at = datetime.now(timezone.utc)

        if on_spawn is not None:
            try:
                on_spawn(self._proc.pid)
            except Exception:
                logger.exception("on_spawn callback raised — tearing down")
                try:
                    self._proc.kill()
                except Exception:
                    pass
                raise

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name=f"fpulse-sp-monitor-{self._proc.pid}",
            daemon=True,
        )
        self._monitor_thread.start()

        return self._proc.pid

    def cancel(self, *, timeout_s: float = 3.0) -> bool:
        """Request termination. Returns True if the process was alive
        at the moment of the call."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return False
            self._exit_reason = EXIT_CANCELLED
        self._terminate_tree(grace_s=timeout_s)
        return True

    def wait(self, timeout_s: float | None = None) -> int | None:
        """Block until the monitor thread finalizes. Returns the exit
        code if the process completed, else None."""
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=timeout_s)
        if self._proc is not None:
            return self._proc.returncode
        return None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def finalized(self) -> bool:
        return self._finalized.is_set()

    @property
    def stdout_log_path(self) -> str | None:
        return self._stdout_path

    @property
    def stderr_log_path(self) -> str | None:
        return self._stderr_path

    # ── Internals ─────────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        try:
            self._poll_until_exit()
        finally:
            self._finalize()

    def _poll_until_exit(self) -> None:
        if self._proc is None:
            return
        deadline = time.time() + self._max_runtime_s
        max_bytes = self._max_memory_mb * 1024 * 1024
        ps_proc = _psutil.Process(self._proc.pid) if _HAS_PSUTIL else None

        while True:
            if self._proc.poll() is not None:
                return

            if time.time() > deadline:
                with self._lock:
                    if self._exit_reason == EXIT_OK:
                        self._exit_reason = EXIT_BUDGET_RUNTIME
                self._terminate_tree(grace_s=2.0)
                return

            if ps_proc is not None:
                try:
                    rss = ps_proc.memory_info().rss
                    if rss > self._memory_peak_bytes:
                        self._memory_peak_bytes = rss
                    if rss > max_bytes:
                        with self._lock:
                            if self._exit_reason == EXIT_OK:
                                self._exit_reason = EXIT_BUDGET_MEMORY
                        self._terminate_tree(grace_s=2.0)
                        return
                except _psutil.NoSuchProcess:
                    return
                except _psutil.AccessDenied:
                    logger.debug("psutil AccessDenied for pid %s", self._proc.pid)

            time.sleep(self._monitor_interval_s)

    def _terminate_tree(self, *, grace_s: float = 3.0) -> None:
        """SIGTERM the whole tree, wait grace_s, SIGKILL anything alive.
        On Windows, Popen.terminate / psutil.terminate use TerminateProcess
        which is effectively SIGKILL — but the two-stage dance still
        serves the child-cleanup purpose."""
        if self._proc is None:
            return

        if not _HAS_PSUTIL:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=grace_s)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            except Exception as exc:
                logger.warning("fallback terminate failed: %s", exc)
            return

        try:
            parent = _psutil.Process(self._proc.pid)
        except _psutil.NoSuchProcess:
            return

        try:
            children = parent.children(recursive=True)
        except _psutil.NoSuchProcess:
            children = []

        for child in children:
            try:
                child.terminate()
            except _psutil.NoSuchProcess:
                pass
        try:
            parent.terminate()
        except _psutil.NoSuchProcess:
            pass

        gone, alive = _psutil.wait_procs([parent] + children, timeout=grace_s)
        for proc in alive:
            try:
                proc.kill()
            except _psutil.NoSuchProcess:
                pass

    def _finalize(self) -> None:
        if self._proc is None or self._started_at is None:
            self._finalized.set()
            return

        try:
            self._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2.0)
            except Exception:
                pass

        exit_code = self._proc.returncode
        ended_at = datetime.now(timezone.utc)
        runtime_ms = (ended_at - self._started_at).total_seconds() * 1000

        with self._lock:
            reason = self._exit_reason
        # Heuristic: if we never set a reason but exit code is non-zero
        # AND we saw memory approach the ceiling, attribute to memory.
        # This catches the Linux setrlimit path where the child dies on
        # its own before the monitor observes the breach.
        if reason == EXIT_OK and exit_code not in (0, None):
            max_bytes = self._max_memory_mb * 1024 * 1024
            if _HAS_PSUTIL and self._memory_peak_bytes > max_bytes * 0.9:
                reason = EXIT_BUDGET_MEMORY
            else:
                reason = EXIT_ERROR

        memory_peak_mb = None
        if self._memory_peak_bytes > 0:
            memory_peak_mb = round(self._memory_peak_bytes / (1024 * 1024), 2)

        # Close log files if we opened any — child is dead, so no
        # writer remains. Leave the files on disk; the caller owns
        # retention. Swallow errors: finalize must never raise.
        for fh in (self._stdout_file, self._stderr_file):
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
        self._stdout_file = None
        self._stderr_file = None

        result = SubprocessResult(
            exit_code=exit_code,
            exit_reason=reason,
            memory_peak_mb=memory_peak_mb,
            runtime_ms=round(runtime_ms, 2),
            started_at=self._started_at,
            ended_at=ended_at,
            pid=self._proc.pid,
            stdout_log_path=self._stdout_path,
            stderr_log_path=self._stderr_path,
        )

        self._finalized.set()

        if self._on_exit is not None:
            try:
                self._on_exit(result)
            except Exception:
                logger.exception("on_exit callback raised")


def _make_preexec(limit_bytes: int):
    """Build a preexec_fn closure that applies RLIMIT_AS in the child.
    Extracted so the closure can be passed to Popen cleanly on Unix."""
    def _apply():
        if _HAS_RESOURCE and _resource is not None:
            _resource.setrlimit(_resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    return _apply
